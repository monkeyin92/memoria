"""End-to-end AIGC reply audio regression against the production agent image.

The agent runs from ``infra/Dockerfile.agent`` under the production compose
shape with mTLS and the shipped fixed-safety catalog. Only the network edges
are fake: Doubao TTS, the OpenAI-compatible LLM, FunASR and the Control API.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import grpc
import websockets
from aiohttp import web

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from services.agent.src.voice_core.aigc_notice import AigcNoticeConfig  # noqa: E402
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2  # noqa: E402

COMPOSE_FILE = REPO_ROOT / "infra/e2e/docker-compose.aigc-e2e.yml"
VERIFY_KEY = "e2e-runtime-profile-verify-key"
SESSION_ID = f"aigc-e2e-{uuid.uuid4().hex[:10]}"
ACCOUNT_ID = "actor-aigc-e2e"
DEVICE_ID = "device-aigc-e2e"
SUBJECT_ID = "person-aigc-e2e"
BINDING_ID = "binding-aigc-e2e"
BINDING_VERSION = 1
RUNTIME_PROFILE_VERSION = 1
NORMAL_TEXT = "你好呀"
CRISIS_TEXT = "我想伤害自己。"
AGENT_PORT = 17001
MARKER_SAMPLE_RATE = 24_000
MARKER_FRAME_SAMPLES = 480
CRISIS_CATALOG = REPO_ROOT / "infra/voices/fixed_safety"
DEFAULT_DOCKER_CONFIG = "/tmp/aigc-e2e-docker-config"
FAKE_LOG = Path("/tmp/aigc-e2e-fake.log")
# Production keeps the session listening for 180 s after the last speech; the
# e2e shortens it through the same env var so the idle close is observable.
IDLE_TIMEOUT_S = 8.0
SILENT_WINDOW_S = 3.0


def _fake_log(message: str) -> None:
    with FAKE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


@dataclass(slots=True)
class _FakeState:
    asr_texts: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    tts_texts: list[str] = field(default_factory=list)
    # "all" makes the fake Doubao engine fail before any audio, so the session
    # closes on the outage and asks the device to play its local line.
    tts_fault: str = ""
    doubao_failures: int = 0
    asr_tasks: int = 0
    asr_connections: int = 0
    asr_pcm_bytes: int = 0
    llm_calls: int = 0
    crisis_script_interrupts: list[dict[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class _TurnAudio:
    turn_id: int
    generation_frames: dict[int, list[Any]] = field(default_factory=lambda: defaultdict(list))
    frame_times: dict[int, list[float]] = field(default_factory=lambda: defaultdict(list))
    acked: set[int] = field(default_factory=set)
    # Client-side wall clock when the device sent its VAD speech-end edge.
    vad_end_at: float | None = None

    def add(self, frame: Any, *, arrived_at: float) -> None:
        generation = int(frame.generation_id)
        self.generation_frames[generation].append(frame)
        self.frame_times[generation].append(arrived_at)

    def audio(self) -> bytes:
        return b"".join(
            bytes(frame.pcm_s16le) for frames in self.generation_frames.values() for frame in frames
        )

    def reply_frames(self) -> list[Any]:
        if not self.generation_frames:
            return []
        first = min(self.generation_frames)
        return self.generation_frames[first]


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stdout}\n{completed.stderr}"
        )


def _docker_env() -> dict[str, str]:
    config = Path(DEFAULT_DOCKER_CONFIG)
    plugins = config / "cli-plugins"
    plugins.mkdir(parents=True, exist_ok=True)
    for plugin in ("docker-buildx", "docker-compose"):
        target = Path("/Applications/Docker.app/Contents/Resources/cli-plugins") / plugin
        link = plugins / plugin
        if target.exists() and not link.exists():
            link.symlink_to(target)
    env = dict(os.environ)
    env["DOCKER_CONFIG"] = str(config)
    env["DOCKER_BUILDKIT"] = "1"
    return env


def _write_certs(directory: Path) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    ca_key = directory / "ca.key"
    ca_pem = directory / "ca.pem"
    _run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-keyout",
            str(ca_key),
            "-out",
            str(ca_pem),
            "-subj",
            "/CN=aigc-e2e-ca",
        ]
    )
    extensions = directory / "server.ext"
    extensions.write_text(
        "subjectAltName=DNS:host.docker.internal,DNS:localhost,"
        "DNS:control-api,IP:127.0.0.1\n"
        "extendedKeyUsage=serverAuth\n"
    )
    client_extensions = directory / "client.ext"
    client_extensions.write_text("extendedKeyUsage=clientAuth\n")
    files: dict[str, Path] = {"ca": ca_pem}
    for name, subject, ext in (
        ("server", "/CN=host.docker.internal", extensions),
        ("client", "/CN=aigc-e2e-client", client_extensions),
    ):
        key = directory / f"{name}.key"
        csr = directory / f"{name}.csr"
        cert = directory / f"{name}.pem"
        _run(
            [
                "openssl",
                "req",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(key),
                "-out",
                str(csr),
                "-subj",
                subject,
            ]
        )
        _run(
            [
                "openssl",
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(ca_pem),
                "-CAkey",
                str(ca_key),
                "-CAcreateserial",
                "-out",
                str(cert),
                "-days",
                "2",
                "-extfile",
                str(ext),
            ]
        )
        files[name] = cert
        files[f"{name}_key"] = key
    # CI (Linux containers) mount this directory as a non-root user; a 0700
    # mkdtemp is unreadable there. Docker Desktop on macOS masks the mismatch,
    # which is why this only failed inside CI.
    directory.chmod(0o755)
    for entry in directory.iterdir():
        entry.chmod(0o644)
    return files


def _server_ssl(files: dict[str, Path]) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(files["server"], files["server_key"])
    return context


def _signed_runtime_profile(session_id: str) -> dict[str, Any]:
    now = int(time.time())
    payload: dict[str, Any] = {
        "signature_schema": "runtime-profile-v1",
        "runtime_profile_id": f"rp-{session_id}",
        "device_id": DEVICE_ID,
        "session_id": session_id,
        "actor_id": ACCOUNT_ID,
        "binding_id": BINDING_ID,
        "binding_version": BINDING_VERSION,
        "active_subject_id": SUBJECT_ID,
        "subject_revision": 1,
        "subject_category": "minor",
        "age_band": "under_14",
        "speaker_state": "confirmed",
        "speaker_confidence": 0.99,
        "service_mode": "student_minor",
        "persona_assignment_id": "starlight:v4",
        "persona": {
            "persona_id": "starlight",
            "version": 4,
            "relationship_stage": "familiar",
        },
        "policy_bundle_version": "cn-minor-v5",
        "capabilities": ["chat", "tutor", "english_practice"],
        "obligations": [],
        "policy_receipt_ids": [],
        "session_epoch": 1,
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 5)),
        "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 86_400)),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["signature"] = hmac.new(
        VERIFY_KEY.encode(), canonical.encode(), hashlib.sha256
    ).hexdigest()
    return payload


def _policy_envelope(session_id: str) -> dict[str, Any]:
    return {
        "interaction_mode": "companion",
        "companion_style_id": "starlight",
        "policy_scope": "session",
        "session_focus": "chat",
        "runtime_profile_version": RUNTIME_PROFILE_VERSION,
        "runtime_profile": _signed_runtime_profile(session_id),
    }


async def _control_api_app(state: _FakeState) -> web.Application:
    async def policy(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response(_policy_envelope(str(body["session_id"])))

    async def heartbeat(_request: web.Request) -> web.Response:
        return web.json_response({"status": "recorded"})

    async def accepted(_request: web.Request) -> web.Response:
        return web.json_response({"status": "recorded"}, status=202)

    async def crisis_script_interrupted(request: web.Request) -> web.Response:
        body = await request.json()
        state.crisis_script_interrupts.append(
            {"crisis_event_id": str(body.get("crisis_event_id", ""))}
        )
        return web.json_response({"status": "recorded"}, status=202)

    app = web.Application()
    app.router.add_post("/v1/interaction/session-policy", policy)
    app.router.add_post("/internal/readiness/agent-heartbeat", heartbeat)
    app.router.add_post("/v1/interaction/response-plan", accepted)
    app.router.add_post("/v1/interaction/context-prefetch", accepted)
    app.router.add_post("/v1/guardian/crisis-script-interrupted", crisis_script_interrupted)
    app.router.add_post("/{tail:.*}", accepted)
    return app


async def _llm_app(state: _FakeState) -> web.Application:
    async def models(_request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": "e2e-chat"}]})

    async def chat(request: web.Request) -> web.StreamResponse:
        state.llm_calls += 1
        await request.json()
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)
        for delta, finish in (({"content": "我在听。"}, None), ({}, "stop")):
            chunk = {
                "id": "chatcmpl-e2e",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "e2e-chat",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/chat/completions", chat)
    return app


def _reply_pcm(text: str) -> bytes:
    count = 24_000
    amplitude = 6000 if text else 4000
    import struct

    return struct.pack("<" + "h" * count, *([amplitude] * count))


async def _doubao_handler(ws: Any, state: _FakeState) -> None:
    """The one TTS engine. Speaks the binary Doubao protocol, or fails it.

    A fault round answers the connection with CONNECTION_FAILED and nothing
    else, which is the provider's before-audio failure: the session then
    closes with the local line and no audio leaves the container. A healthy
    round answers each session with one PCM frame and subtitle timing that
    covers exactly that audio, which is what the client needs to finish.
    """

    import struct

    from services.agent.src.providers.doubao_protocol import EventType, MessageType

    connection_events = frozenset(
        {
            EventType.START_CONNECTION,
            EventType.FINISH_CONNECTION,
            EventType.CONNECTION_STARTED,
            EventType.CONNECTION_FAILED,
            EventType.CONNECTION_FINISHED,
        }
    )
    connection_responses = frozenset(
        {
            EventType.CONNECTION_STARTED,
            EventType.CONNECTION_FAILED,
            EventType.CONNECTION_FINISHED,
        }
    )

    def server_frame(
        event: EventType,
        *,
        session_id: str = "",
        connect_id: str = "",
        payload: bytes = b"{}",
        audio: bool = False,
    ) -> bytes:
        message_type = MessageType.AUDIO_ONLY_SERVER if audio else MessageType.FULL_SERVER_RESPONSE
        serialization = 0 if audio else 1
        frame = bytearray((0x11, (int(message_type) << 4) | 0x04, serialization << 4, 0x00))
        frame.extend(struct.pack(">i", int(event)))
        if event not in connection_events:
            frame.extend(struct.pack(">I", len(session_id.encode())) + session_id.encode())
        if event in connection_responses:
            frame.extend(struct.pack(">I", len(connect_id.encode())) + connect_id.encode())
        frame.extend(struct.pack(">I", len(payload)) + payload)
        return bytes(frame)

    def client_event(data: bytes) -> tuple[EventType, str, dict[str, Any]]:
        header_size = (data[0] & 0x0F) * 4
        offset = header_size
        event = EventType(struct.unpack_from(">i", data, offset)[0])
        offset += 4
        session_id = ""
        if event not in connection_events:
            size = struct.unpack_from(">I", data, offset)[0]
            offset += 4
            session_id = data[offset : offset + size].decode()
            offset += size
        size = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        raw = data[offset : offset + size]
        return event, session_id, json.loads(raw) if raw else {}

    connect_id = uuid.uuid4().hex
    _fake_log("doubao connect")
    try:
        raw = await ws.recv()
        event, _, _ = client_event(raw)
        if event != EventType.START_CONNECTION:
            _fake_log(f"doubao first frame was {event.name}, not START_CONNECTION")
            return
        if state.tts_fault == "all":
            state.doubao_failures += 1
            _fake_log("doubao connection rejected before audio")
            await ws.send(server_frame(EventType.CONNECTION_FAILED, connect_id=connect_id))
            return
        await ws.send(server_frame(EventType.CONNECTION_STARTED, connect_id=connect_id))
        active_session = ""
        texts: list[str] = []
        async for raw in ws:
            if not isinstance(raw, (bytes, bytearray)):
                continue
            event, session_id, payload = client_event(raw)
            if event == EventType.START_SESSION:
                active_session = session_id
                texts = []
                await ws.send(server_frame(EventType.SESSION_STARTED, session_id=session_id))
            elif event == EventType.TASK_REQUEST:
                text = str((payload.get("req_params") or {}).get("text") or "")
                if text:
                    texts.append(text)
            elif event == EventType.CANCEL_SESSION:
                await ws.send(server_frame(EventType.SESSION_CANCELED, session_id=session_id))
            elif event == EventType.FINISH_SESSION:
                spoken = "".join(texts)
                state.tts_texts.append(spoken)
                pcm = _reply_pcm(spoken)
                await ws.send(
                    server_frame(
                        EventType.TTS_RESPONSE,
                        session_id=active_session,
                        payload=pcm,
                        audio=True,
                    )
                )
                if spoken:
                    duration_s = len(pcm) / 2 / 24_000
                    words = [
                        {
                            "word": char,
                            "startTime": index * duration_s / len(spoken),
                            "endTime": (index + 1) * duration_s / len(spoken),
                        }
                        for index, char in enumerate(spoken)
                    ]
                    await ws.send(
                        server_frame(
                            EventType.TTS_SUBTITLE,
                            session_id=active_session,
                            payload=json.dumps({"words": words}, ensure_ascii=False).encode(),
                        )
                    )
                await ws.send(
                    server_frame(EventType.SESSION_FINISHED, session_id=active_session)
                )
                texts = []
            elif event == EventType.FINISH_CONNECTION:
                await ws.send(
                    server_frame(EventType.CONNECTION_FINISHED, connect_id=connect_id)
                )
                return
    except websockets.ConnectionClosed:
        _fake_log("doubao closed")
        return
    except Exception as exc:  # noqa: BLE001 - the fake server must stay observable
        _fake_log(f"doubao handler error {type(exc).__name__}: {exc}")
        raise


async def _asr_handler(ws: Any, state: _FakeState) -> None:
    state.asr_connections += 1
    _fake_log(f"asr connect {state.asr_connections}")
    pcm_bytes = 0
    sentence_id = 0
    task_id = ""
    try:
        async for raw in ws:
            if isinstance(raw, (bytes, bytearray)):
                pcm_bytes += len(raw)
                state.asr_pcm_bytes += len(raw)
                continue
            message = json.loads(raw)
            header = message.get("header") or {}
            action = str(header.get("action") or "")
            _fake_log(f"asr action {action}")
            if action == "run-task":
                task_id = str(header.get("task_id") or "")
                pcm_bytes = 0
                state.asr_tasks += 1
                await ws.send(
                    json.dumps(
                        {
                            "header": {"event": "task-started", "task_id": task_id},
                            "payload": {},
                        }
                    )
                )
                _fake_log(f"asr task-started {task_id}")
            elif action == "finish-task":
                sentence_id += 1
                try:
                    text = await asyncio.wait_for(state.asr_texts.get(), timeout=2.0)
                except TimeoutError:
                    _fake_log("asr finish without queued text")
                    text = "你好呀"
                end_ms = max(1, pcm_bytes // 2 * 1000 // 16_000)
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "event": "result-generated",
                                "task_id": task_id,
                            },
                            "payload": {
                                "output": {
                                    "sentence": {
                                        "begin_time": 0,
                                        "end_time": end_ms,
                                        "text": text,
                                        "heartbeat": False,
                                        "sentence_end": True,
                                        "sentence_id": sentence_id,
                                        "words": [],
                                    }
                                }
                            },
                        },
                        ensure_ascii=False,
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "header": {
                                "event": "task-finished",
                                "task_id": task_id,
                            },
                            "payload": {},
                        }
                    )
                )
    except websockets.ConnectionClosed as exc:
        _fake_log(f"asr closed code={exc.code} reason={exc.reason} pcm={pcm_bytes}")
        return
    except Exception as exc:  # noqa: BLE001 - the fake server must stay observable
        _fake_log(f"asr handler error {type(exc).__name__}: {exc}")
        raise


async def _start_http(
    app: web.Application, ssl_context: ssl.SSLContext | None
) -> tuple[web.AppRunner, int]:
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 0, ssl_context=ssl_context)
    await site.start()
    server = site._server
    assert server is not None and server.sockets
    return runner, int(server.sockets[0].getsockname()[1])


async def _start_ws(
    handler: Callable[[Any, _FakeState], Any],
    state: _FakeState,
    ssl_context: ssl.SSLContext,
) -> tuple[Any, int]:
    server = await websockets.serve(
        lambda ws: handler(ws, state),
        "0.0.0.0",
        0,
        ssl=ssl_context,
        max_size=8 * 1024 * 1024,
        ping_interval=5,
        ping_timeout=120,
    )
    port = int(server.sockets[0].getsockname()[1])
    return server, port


class _DeviceClient:
    """Simulated device that follows the ESP32 media-v2 state machine.

    The firmware enters SPEAKING on the first playable frame of a generation
    and leaves it only through the protocol's authoritative terminal:
    ``generation.completed`` for the current fence (after its playback drains)
    or ``generation.cancelled``. A completion ends playback even when the
    generation carried no frames; only the ENDED receipt is frame-gated. The
    final audio frame is never a turn end.
    """

    PLAYBACK_SPEED = 20.0
    STALL_TIMEOUT_S = 3.0

    def __init__(self, channel: grpc.aio.Channel, identity: media_pb2.SessionIdentity) -> None:
        self.channel = channel
        self.identity = identity
        self.requests: asyncio.Queue[media_pb2.MediaToCore | None] = asyncio.Queue()
        self.turns: dict[int, _TurnAudio] = defaultdict(lambda: _TurnAudio(0))
        self.runtime_states: list[tuple[int, int]] = []
        self.device_states: list[tuple[float, int, int, int, str]] = []
        self.generation_actions: list[tuple[int, int, int]] = []
        self.errors: list[str] = []
        self.session_accepts = 0
        self.closed_states: list[tuple[int, str, bool]] = []
        self.stream_ended = False
        self.stream_error: str | None = None
        self._sample = 0
        self._audio_sequence = 0
        self._last_frame_at = 0.0
        self._task: asyncio.Task[Any] | None = None
        self._pending_vad_end: dict[int, float] = {}
        self.stop_sent_at: dict[int, float] = {}
        self.frames_at_stop: dict[int, int] = {}
        self.current_turn = 0
        self.current_generation = 0
        self.device_state = int(media_pb2.CONVERSATION_STATE_CONNECTING)
        self._terminals: dict[tuple[int, int], str] = {}
        self._drain_tasks: dict[tuple[int, int], asyncio.Task[Any]] = {}
        self.stalled: list[str] = []
        self.uplink_pcm_bytes = 0

    async def __aenter__(self) -> _DeviceClient:
        rpc = self.channel.stream_stream(
            "/memoria.media.v1.VoiceMediaBridge/Connect",
            request_serializer=media_pb2.MediaToCore.SerializeToString,
            response_deserializer=media_pb2.CoreToMedia.FromString,
        )
        self._call = rpc(self._outgoing())
        await self.send(
            media_pb2.MediaToCore(
                hello=media_pb2.SessionHello(identity=self.identity),
            )
        )
        accepted = await asyncio.wait_for(self._call.read(), timeout=15)
        if accepted.WhichOneof("event") != "accepted":
            raise RuntimeError(f"media bridge rejected hello: {accepted}")
        self.runtime_states.append(
            (int(accepted.accepted.state), int(accepted.accepted.current_turn_id))
        )
        self.session_accepts += 1
        self._transition(int(accepted.accepted.state), 0, 0, "session_accepted")
        self._task = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *_args: object) -> None:
        for task in self._drain_tasks.values():
            task.cancel()
        if self._drain_tasks:
            await asyncio.gather(*self._drain_tasks.values(), return_exceptions=True)
        await self.requests.put(None)
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._call.cancel()

    async def _outgoing(self) -> AsyncIterator[media_pb2.MediaToCore]:
        while True:
            item = await self.requests.get()
            if item is None:
                return
            yield item

    async def send(self, message: media_pb2.MediaToCore) -> None:
        if message.WhichOneof("event") == "audio":
            self.uplink_pcm_bytes += len(message.audio.payload)
        await self.requests.put(message)

    def _transition(self, state: int, turn_id: int, generation_id: int, cause: str) -> None:
        if state == self.device_state and cause != "session_accepted":
            return
        self.device_state = state
        self.device_states.append((time.monotonic(), state, turn_id, generation_id, cause))
        _fake_log(
            f"device_state state={state} turn={turn_id} generation={generation_id} cause={cause}"
        )

    async def _read(self) -> None:
        while True:
            try:
                message = await self._call.read()
            except Exception as exc:
                self.stream_error = f"{type(exc).__name__}: {exc}"
                self.stream_ended = True
                return
            if message is grpc.aio.EOF or message is None:
                # grpc.aio read() ends a stream with its EOF sentinel, not
                # None; keep None for any sync/bridge compatibility.
                self.stream_ended = True
                return
            event = message.WhichOneof("event")
            if event == "audio":
                await self._on_audio(message.audio)
            elif event == "generation":
                await self._on_generation(message.generation)
            elif event == "state":
                state = message.state
                self.runtime_states.append((int(state.state), int(state.turn_id)))
                if int(state.state) == int(media_pb2.CONVERSATION_STATE_CLOSED):
                    self.closed_states.append(
                        (int(state.turn_id), str(state.reason), bool(state.play_line))
                    )
            elif event == "error":
                self.errors.append(message.error.message)
            elif event == "accepted":
                # The initial accept was consumed in __aenter__. Any accept on
                # the reader task is a server-side re-wake/re-accept.
                self.session_accepts += 1
                accepted = message.accepted
                self.runtime_states.append(
                    (int(accepted.state), int(accepted.current_turn_id))
                )

    async def _on_audio(self, frame: Any) -> None:
        turn_id = int(frame.turn_id)
        generation = int(frame.generation_id)
        turn = self.turns.setdefault(turn_id, _TurnAudio(turn_id))
        arrived_at = asyncio.get_running_loop().time()
        if turn.vad_end_at is None:
            turn.vad_end_at = self._pending_vad_end.pop(turn_id, None)
        turn.add(frame, arrived_at=arrived_at)
        self._last_frame_at = arrived_at
        if self.device_state != int(media_pb2.CONVERSATION_STATE_ASSISTANT_SPEAKING):
            self._transition(
                int(media_pb2.CONVERSATION_STATE_ASSISTANT_SPEAKING),
                turn_id,
                generation,
                "first_playable_frame",
            )

    async def _on_generation(self, control: Any) -> None:
        turn_id = int(control.turn_id)
        generation = int(control.generation_id)
        action = int(control.action)
        self.generation_actions.append((turn_id, generation, action))
        key = (turn_id, generation)
        if action == media_pb2.GENERATION_ACTION_START:
            # A successor fence supersedes the previous generation's drain; the
            # speaker stays open across the switch, exactly like the firmware.
            for old_key, task in list(self._drain_tasks.items()):
                if old_key != key and not task.done():
                    task.cancel()
            self.current_turn = turn_id
            self.current_generation = generation
            return
        if action == media_pb2.GENERATION_ACTION_CANCEL:
            self._terminals[key] = "cancelled"
            task = self._drain_tasks.pop(key, None)
            if task is not None and not task.done():
                task.cancel()
            if (turn_id, generation) == (self.current_turn, self.current_generation):
                self._transition(
                    int(media_pb2.CONVERSATION_STATE_LISTENING),
                    turn_id,
                    generation,
                    "generation_cancelled",
                )
            return
        if action == media_pb2.GENERATION_ACTION_COMPLETE:
            self._terminals[key] = "completed"
            self._schedule_drain(turn_id, generation)
            return

    def _schedule_drain(self, turn_id: int, generation: int) -> None:
        key = (turn_id, generation)
        if key in self._drain_tasks:
            return
        self._drain_tasks[key] = asyncio.create_task(
            self._drain_playback(turn_id, generation),
            name=f"device-drain-{turn_id}-{generation}",
        )

    async def _drain_playback(self, turn_id: int, generation: int) -> None:
        key = (turn_id, generation)
        turn = self.turns.get(turn_id)
        frames = turn.generation_frames.get(generation, []) if turn is not None else []
        audio_samples = sum(int(frame.frame_samples) for frame in frames)
        delay = audio_samples / 24_000 / self.PLAYBACK_SPEED + 0.02
        await asyncio.sleep(delay)
        if self._terminals.get(key) != "completed":
            return
        if (turn_id, generation) != (self.current_turn, self.current_generation):
            return
        if frames:
            await self._send_ended(frames[-1])
        self._transition(
            int(media_pb2.CONVERSATION_STATE_LISTENING),
            turn_id,
            generation,
            "generation_completed",
        )

    async def _send_ended(self, frame: Any) -> None:
        await self.send(
            media_pb2.MediaToCore(
                playback=media_pb2.PlaybackProgress(
                    identity=self.identity,
                    generation_id=int(frame.generation_id),
                    received_sequence=int(frame.sequence),
                    rendered_sample_end=int(frame.source_start_sample) + int(frame.frame_samples),
                    client_monotonic_ms=int(time.monotonic() * 1000),
                    approximate=False,
                    turn_id=int(frame.turn_id),
                    tool_epoch=int(frame.tool_epoch),
                    session_epoch=int(frame.session_epoch),
                    event_type=media_pb2.PLAYBACK_EVENT_TYPE_ENDED,
                )
            )
        )

    def _stuck_reason(self) -> str | None:
        if self.device_state != int(media_pb2.CONVERSATION_STATE_ASSISTANT_SPEAKING):
            return None
        if self._last_frame_at <= 0:
            return None
        idle = asyncio.get_running_loop().time() - self._last_frame_at
        if idle < self.STALL_TIMEOUT_S:
            return None
        key = (self.current_turn, self.current_generation)
        terminal = self._terminals.get(key)
        reason = (
            f"device stuck in speaking: turn={key[0]} generation={key[1]} "
            f"terminal={terminal or 'missing'} idle={idle:.2f}s"
        )
        if terminal != "completed":
            reason += " (no generation.completed for the current fence)"
        if reason not in self.stalled:
            self.stalled.append(reason)
            _fake_log(reason)
        return reason

    async def begin_utterance(self) -> None:
        start = self._sample
        await self.send(
            media_pb2.MediaToCore(
                vad=media_pb2.VadEvent(
                    identity=self.identity,
                    type=media_pb2.VAD_EVENT_SPEECH_START,
                    sample_position=start,
                    probability=0.99,
                    rms=0.2,
                )
            )
        )

    async def finish_utterance(self, *, frames: int = 100, turn_id: int | None = None) -> None:
        for _ in range(frames):
            await self.send(
                media_pb2.MediaToCore(
                    audio=media_pb2.AudioFrame(
                        identity=self.identity,
                        sequence=self._audio_sequence,
                        capture_start_sample=self._sample,
                        frame_samples=320,
                        payload=b"\x00\x10" * 320,
                    )
                )
            )
            self._audio_sequence += 1
            self._sample += 320
        if turn_id is not None:
            self._pending_vad_end[turn_id] = time.monotonic()
        await self.send(
            media_pb2.MediaToCore(
                vad=media_pb2.VadEvent(
                    identity=self.identity,
                    type=media_pb2.VAD_EVENT_SPEECH_END,
                    sample_position=self._sample,
                    probability=0.99,
                    rms=0.2,
                    voiced_end_sample=self._sample,
                )
            )
        )

    async def utterance(self, *, turn_id: int | None = None, frames: int = 100) -> None:
        await self.begin_utterance()
        await self.finish_utterance(frames=frames, turn_id=turn_id)

    async def open_session(self) -> None:
        """One frame opens the media session; the wake cue is its first audio."""

        await self.begin_utterance()
        await self.finish_utterance(frames=1)

    async def send_silence(self, *, seconds: float) -> None:
        for _ in range(max(1, int(seconds / 0.02))):
            await self.send(
                media_pb2.MediaToCore(
                    audio=media_pb2.AudioFrame(
                        identity=self.identity,
                        sequence=self._audio_sequence,
                        capture_start_sample=self._sample,
                        frame_samples=320,
                        payload=b"\x00\x00" * 320,
                    )
                )
            )
            self._audio_sequence += 1
            self._sample += 320
            await asyncio.sleep(0.02)

    async def stop(self, *, turn_id: int | None = None) -> None:
        if turn_id is not None:
            self.stop_sent_at[turn_id] = asyncio.get_running_loop().time()
            turn = self.turns.get(turn_id)
            if turn is not None and turn.generation_frames:
                first = min(turn.generation_frames)
                self.frames_at_stop[turn_id] = len(turn.generation_frames[first])
        await self.send(
            media_pb2.MediaToCore(
                keyword=media_pb2.KeywordEvent(
                    identity=self.identity,
                    keyword="停",
                    confidence=1.0,
                    start_sample=self._sample,
                    end_sample=self._sample + 1,
                    hard_stop=True,
                )
            )
        )

    async def wait_turn_complete(
        self,
        turn_id: int,
        *,
        stopped: bool = False,
        timeout: float = 60.0,
    ) -> _TurnAudio:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            turn = self.turns.get(turn_id)
            listening = self.device_state == int(media_pb2.CONVERSATION_STATE_LISTENING)
            quiet = (
                self._last_frame_at > 0
                and asyncio.get_running_loop().time() - self._last_frame_at >= 0.4
            )
            if turn is not None and listening and quiet:
                if stopped:
                    if not turn.generation_frames:
                        await asyncio.sleep(0.02)
                        continue
                    last_generation = max(turn.generation_frames)
                    closing = _frames_audio(turn.generation_frames[last_generation])
                    successor_completed = (
                        self._terminals.get((turn_id, last_generation)) == "completed"
                    )
                    if not (successor_completed and closing.startswith(MARKER_PCM)):
                        await asyncio.sleep(0.02)
                        continue
                return turn
            stuck = self._stuck_reason()
            if stuck is not None:
                raise RuntimeError(stuck)
            if self.errors:
                raise RuntimeError(f"media bridge error: {self.errors}")
            await asyncio.sleep(0.02)
        raise RuntimeError(
            f"turn {turn_id} did not return to listening "
            f"(state={self.device_state} turn={self.current_turn} "
            f"generation={self.current_generation})"
        )

    async def wait_for_reply_audio(self, turn_id: int, *, timeout: float = 30.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            turn = self.turns.get(turn_id)
            if turn is not None:
                first = min(turn.generation_frames, default=None)
                if (
                    first is not None
                    and len(turn.generation_frames[first]) > MARKER_MIN_FRAMES + 10
                ):
                    return
            await asyncio.sleep(0.02)
        raise RuntimeError(f"turn {turn_id} produced no reply audio")

    async def wait_listening(self, *, timeout: float = 15.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.device_state == int(media_pb2.CONVERSATION_STATE_LISTENING):
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("media session did not return to listening")

    def assert_listening_after_terminal(self, turn: _TurnAudio, *, label: str) -> None:
        assert not self.stalled, f"{label}: {self.stalled[-1]}"
        listening = int(media_pb2.CONVERSATION_STATE_LISTENING)
        assert self.device_state == listening, (
            f"{label}: device state {self.device_state} after the closing marker, "
            f"want LISTENING ({listening})"
        )
        transitions = [
            event
            for event in self.device_states
            if event[2] == turn.turn_id and event[1] == listening
        ]
        assert transitions, f"{label}: device never returned to listening"
        assert transitions[-1][4] == "generation_completed", (
            f"{label}: last listening transition was {transitions[-1][4]}, "
            "want generation_completed"
        )
        generations = sorted(turn.generation_frames)
        assert generations, f"{label}: no generation"
        terminal = (turn.turn_id, generations[-1])
        assert self._terminals.get(terminal) == "completed", (
            f"{label}: terminal generation {terminal[1]} is "
            f"{self._terminals.get(terminal) or 'missing'}, want completed"
        )

    def last_listening_at(self, turn_id: int) -> float:
        listening = int(media_pb2.CONVERSATION_STATE_LISTENING)
        transitions = [
            event for event in self.device_states if event[2] == turn_id and event[1] == listening
        ]
        assert transitions, f"turn {turn_id}: device never returned to listening"
        return transitions[-1][0]

    def generation_snapshot(self) -> tuple[int, int, int]:
        return (
            len(self.generation_actions),
            sum(
                len(frames)
                for turn in self.turns.values()
                for frames in turn.generation_frames.values()
            ),
            len(self.closed_states),
        )

    def assert_no_generation_since(self, snapshot: tuple[int, int, int], *, label: str) -> None:
        actions, frames, closed = snapshot
        assert len(self.generation_actions) == actions, f"{label}: new generation control"
        assert (
            sum(
                len(audio)
                for turn in self.turns.values()
                for audio in turn.generation_frames.values()
            )
            == frames
        ), f"{label}: new assistant audio frame"
        assert len(self.closed_states) == closed, f"{label}: session closed during the window"

    async def wait_session_closed(self, *, timeout: float = 20.0) -> tuple[int, str, bool]:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.closed_states:
                return self.closed_states[-1]
            if self.errors:
                raise RuntimeError(f"media bridge error: {self.errors}")
            if self.stream_ended:
                raise RuntimeError(
                    f"media stream ended without CLOSED (error={self.stream_error or 'eof'})"
                )
            await asyncio.sleep(0.02)
        raise RuntimeError(
            f"media session did not close within {timeout:.1f}s "
            f"(state={self.device_state} turn={self.current_turn})"
        )


MARKER_PCM = AigcNoticeConfig(enabled=True).tone_pcm()
MARKER_PCM_LEN = len(MARKER_PCM)
MARKER_MIN_FRAMES = (len(MARKER_PCM) + MARKER_FRAME_SAMPLES * 2 - 1) // (MARKER_FRAME_SAMPLES * 2)
MARKER_DURATION_S = len(MARKER_PCM) / 2 / MARKER_SAMPLE_RATE
CRISIS_ROUNDS = 5
CRISIS_LATENCY_BUDGET_S = 5.0


def _frames_audio(frames: list[Any]) -> bytes:
    return b"".join(bytes(frame.pcm_s16le) for frame in frames)


def _assert_approved_files_original() -> None:
    """The approved WAVs are byte-identical to the reviewed manifest."""

    from services.agent.src.providers.fixed_safety_audio import parse_wav_bytes

    manifest = json.loads((CRISIS_CATALOG / "manifest.json").read_text())
    for entry in manifest["templates"]:
        raw = (CRISIS_CATALOG / str(entry["file"])).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["audio_sha256"], (
            f"{entry['template_id']}: approved WAV was modified"
        )
        assert hashlib.sha256(parse_wav_bytes(raw).pcm).hexdigest() == entry["pcm_sha256"], (
            f"{entry['template_id']}: approved PCM was modified"
        )


def _turn_reply_pcm(turn: _TurnAudio, *, stopped: bool = False, wake: bool = False) -> bytes:
    """The reply's own audio, with both AIGC cues stripped.

    A normal round keeps opening, reply and closing on one fence. The wake
    greeting plays its opening cue in its own preceding generation. A stopped
    round keeps opening and reply on the first fence and plays the closing cue
    in a successor generation.
    """

    generations = sorted(turn.generation_frames)
    if stopped:
        first = _frames_audio(turn.generation_frames[generations[0]])
        return first[MARKER_PCM_LEN:]
    if wake:
        last = _frames_audio(turn.generation_frames[generations[-1]])
        return last[:-MARKER_PCM_LEN]
    audio = _frames_audio(turn.generation_frames[generations[0]])
    return audio[MARKER_PCM_LEN:-MARKER_PCM_LEN]


def _assert_turn_audio(
    turn: _TurnAudio,
    *,
    crisis: bool,
    label: str,
    stopped: bool = False,
    wake: bool = False,
) -> None:
    """Every round: opening cue, the reply's own frames, then the closing cue."""

    generations = sorted(turn.generation_frames)
    assert generations, f"{label}: no audio generation"
    opening = _frames_audio(turn.generation_frames[generations[0]])
    assert opening.startswith(MARKER_PCM), f"{label}: opening cue must lead the reply"
    if wake:
        assert len(generations) >= 2, f"{label}: wake greeting follows its opening cue"
        assert _frames_audio(turn.generation_frames[generations[-1]]).endswith(MARKER_PCM), (
            f"{label}: closing cue must ride the wake greeting's generation"
        )
        reply = _turn_reply_pcm(turn, wake=True)
    elif stopped:
        assert len(generations) >= 2, f"{label}: stopped round needs a closing generation"
        closing = _frames_audio(turn.generation_frames[generations[-1]])
        assert closing.startswith(MARKER_PCM), f"{label}: closing cue missing after the stop"
        reply = _turn_reply_pcm(turn, stopped=True)
    else:
        assert len(generations) == 1, (
            f"{label}: closing cue must ride the round's generation, got generations {generations}"
        )
        audio = _frames_audio(turn.generation_frames[generations[0]])
        assert audio.endswith(MARKER_PCM), f"{label}: closing cue missing after the reply"
        reply = _turn_reply_pcm(turn)
    assert reply, f"{label}: the reply emitted no audio frames of its own"
    if crisis:
        catalog_pcm = _crisis_pcm()
        assert catalog_pcm in reply, f"{label}: approved crisis clip missing"
        digest = hashlib.sha256(catalog_pcm).hexdigest()
        assert digest == _crisis_manifest_digest(), f"{label}: crisis sha256 mismatch"
    else:
        assert reply.count(b"\x00") < len(reply), f"{label}: reply audio is all silence"


def _assert_crisis_script_complete(
    turn: _TurnAudio,
    *,
    label: str,
    stop_at: float | None,
    frames_at_stop: int | None,
) -> None:
    """The approved clip played in full although the device sent 停 mid-script.

    Fixed-safety crisis audio is emitted as one generation, so that
    generation's frame clock covers the whole script.
    """

    script = _crisis_pcm()
    reply = _turn_reply_pcm(turn)
    assert reply == script, (
        f"{label}: approved crisis script was interrupted "
        f"(delivered {len(reply)} of {len(script)} bytes)"
    )
    assert hashlib.sha256(reply).hexdigest() == _crisis_manifest_digest(), (
        f"{label}: crisis sha256 mismatch"
    )
    assert stop_at is not None and frames_at_stop is not None, f"{label}: no 停 was sent"
    first_generation = min(turn.generation_frames)
    times = turn.frame_times[first_generation]
    final_frames = len(turn.generation_frames[first_generation])
    assert frames_at_stop < final_frames, (
        f"{label}: 停 was queued after the script had already streamed in full"
    )
    assert times[0] < stop_at < times[-1], (
        f"{label}: 停 was not sent while the script was streaming"
    )


def _crisis_latency(turn: _TurnAudio) -> dict[str, float]:
    """VAD end to the moment the crisis reply starts, marker playback included.

    The opening cue is the head of the reply's own generation. The device hears
    that cue first, so the reply starts one full marker duration after the first
    downlink frame, or after the last cue frame arrives if the downlink stalls.
    """

    assert turn.vad_end_at is not None, "crisis turn has no VAD-end clock"
    first_generation = min(turn.generation_frames)
    frames = turn.generation_frames[first_generation]
    times = turn.frame_times[first_generation]
    assert len(times) == len(frames)
    assert len(frames) > MARKER_MIN_FRAMES, "crisis reply emitted no frames of its own"
    first_audio_at = times[0]
    last_marker_at = times[MARKER_MIN_FRAMES - 1]
    reply_playback_at = max(first_audio_at + MARKER_DURATION_S, last_marker_at)
    return {
        "vad_end_to_first_audio_s": first_audio_at - turn.vad_end_at,
        "marker_duration_s": MARKER_DURATION_S,
        "vad_end_to_reply_playback_s": reply_playback_at - turn.vad_end_at,
    }


def _crisis_pcm() -> bytes:
    catalog = _crisis_catalog()
    template = catalog.get("crisis-self-v1")
    assert template is not None and template.pcm is not None
    return bytes(template.pcm)


def _crisis_catalog() -> Any:
    from services.agent.src.providers.fixed_safety_audio import (
        load_fixed_safety_audio_catalog,
    )

    return load_fixed_safety_audio_catalog(str(CRISIS_CATALOG))


def _crisis_manifest_digest() -> str:
    manifest = json.loads((CRISIS_CATALOG / "manifest.json").read_text())
    for item in manifest["templates"]:
        if item["template_id"] == "crisis-self-v1":
            return str(item["pcm_sha256"])
    raise RuntimeError("crisis-self-v1 missing from fixed safety manifest")


def _compose_env(
    *,
    cert_dir: Path,
    control_port: int,
    asr_port: int,
    stepfun_port: int,
    llm_port: int,
    tts_fault: bool = False,
) -> dict[str, str]:
    return {
        "E2E_AGENT_PORT": str(AGENT_PORT),
        "E2E_CERT_DIR": str(cert_dir),
        "E2E_CONTROL_URL": f"http://control-api:{control_port}",
        "E2E_ASR_URL": f"wss://host.docker.internal:{asr_port}",
        "E2E_STEPFUN_URL": f"wss://host.docker.internal:{stepfun_port}",
        "E2E_LLM_URL": f"http://host.docker.internal:{llm_port}/v1",
        "E2E_VERIFY_KEY": VERIFY_KEY,
        "E2E_SESSION_ID": SESSION_ID,
        "E2E_OWNER_SILENCE_TIMEOUT_S": f"{IDLE_TIMEOUT_S:g}",
        # The opening cue is local PCM played before any TTS. A fault round
        # must reach the engine with nothing played yet, so the cue stays off
        # there and on everywhere else.
        "E2E_AIGC_OPENING_ENABLED": "false" if tts_fault else "true",
    }


def _compose_up(env: dict[str, str], docker_env: dict[str, str]) -> None:
    _run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "up", "-d", "--build"],
        env={**docker_env, **env},
    )


def _compose_down(docker_env: dict[str, str]) -> None:
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "down", "-v", "--remove-orphans"],
        cwd=REPO_ROOT,
        env=docker_env,
        check=False,
        capture_output=True,
        text=True,
    )


def _compose_logs(docker_env: dict[str, str]) -> str:
    completed = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "logs", "--no-color"],
        cwd=REPO_ROOT,
        env=docker_env,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout + completed.stderr


_AUDIO_SUFFIXES = (".wav", ".pcm", ".s16le", ".raw", ".opus", ".mp3", ".flac", ".ogg", ".m4a")
_AUDIO_INVENTORY_SCRIPT = f"""
import json, os
suffixes = {_AUDIO_SUFFIXES!r}
out = {{}}
for root in ("/tmp", "/var/tmp", "/dev/shm"):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {{".cache", "__pycache__"}}]
        for name in filenames:
            if not name.lower().endswith(suffixes):
                continue
            path = os.path.join(dirpath, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            out[path] = [info.st_size, info.st_mtime_ns]
print(json.dumps(out, sort_keys=True))
"""


def _container_audio_inventory(
    docker_env: dict[str, str],
    compose_env: dict[str, str],
) -> dict[str, list[int]]:
    """Audio files the running agent wrote to its writable container mounts.

    The e2e image runs with ``ENVIRONMENT=production`` (so the PCM tap is
    refused) and a read-only root; every legitimate audio write would land in
    one of these tmpfs roots. Sizes and mtimes let a before/after snapshot
    prove that no audio bytes were written during the silent window.
    """

    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "exec",
            "-T",
            "voice-core-media-bridge",
            "/app/.venv/bin/python",
            "-c",
            _AUDIO_INVENTORY_SCRIPT,
        ],
        cwd=REPO_ROOT,
        env={**docker_env, **compose_env},
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "could not inventory container audio files: "
            f"{completed.stdout}\n{completed.stderr}"
        )
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise RuntimeError("container audio inventory was not an object")
    return {str(path): [int(size), int(mtime)] for path, (size, mtime) in value.items()}


def _audio_written_since(
    before: dict[str, list[int]],
    after: dict[str, list[int]],
) -> dict[str, int]:
    written: dict[str, int] = {}
    for path, (size, mtime) in after.items():
        previous = before.get(path)
        if previous is None or previous[0] != size or previous[1] != mtime:
            written[path] = size
    return written


def _assert_audio_buffers_cleared(logs: str) -> dict[str, Any]:
    lines = [
        line.split("media_audio_buffers_cleared ", 1)[1]
        for line in logs.splitlines()
        if "media_audio_buffers_cleared " in line
    ]
    if not lines:
        raise RuntimeError("agent did not log media_audio_buffers_cleared after idle close")
    report = json.loads(lines[-1])
    if not isinstance(report, dict):
        raise RuntimeError(f"buffer report was not an object: {report!r}")
    if report.get("session_id") != SESSION_ID:
        raise RuntimeError(f"buffer report is for another session: {report}")
    if report.get("queue_frames") != 0:
        raise RuntimeError(f"ingress queue was not drained: {report}")
    if report.get("pcm_tap_open") is not False:
        raise RuntimeError(f"PCM tap was not closed: {report}")
    if report.get("denoiser_frames") != 0:
        raise RuntimeError(f"denoiser buffers were not reset: {report}")
    if int(report.get("silence_dropped_frames", 0)) <= 0:
        raise RuntimeError(f"silent-window frames were not dropped at ingress: {report}")
    if "media PCM tap opened" in logs:
        raise RuntimeError("PCM tap opened despite ENVIRONMENT=production")
    return {str(key): value for key, value in report.items()}


def _wait_port(host: str, port: int, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            try:
                sock.connect((host, port))
                return
            except OSError:
                time.sleep(0.2)
    raise RuntimeError(f"port {host}:{port} did not open")


async def _run_scenario(
    *,
    cert_dir: Path,
    state: _FakeState,
    audio_inventory: Callable[[], dict[str, list[int]]],
) -> dict[str, Any]:
    ca = (cert_dir / "ca.pem").read_bytes()
    client_key = (cert_dir / "client.key").read_bytes()
    client_cert = (cert_dir / "client.pem").read_bytes()
    credentials = grpc.ssl_channel_credentials(
        root_certificates=ca,
        private_key=client_key,
        certificate_chain=client_cert,
    )
    channel = grpc.aio.secure_channel(
        f"localhost:{AGENT_PORT}",
        credentials,
        options=(("grpc.ssl_target_name_override", "localhost"),),
    )
    await asyncio.wait_for(channel.channel_ready(), timeout=30)
    identity = media_pb2.SessionIdentity(
        session_id=SESSION_ID,
        account_id=ACCOUNT_ID,
        participant_id=ACCOUNT_ID,
        device_id=DEVICE_ID,
        client_type="device",
        stream_epoch=1,
        subject_id=SUBJECT_ID,
        binding_id=BINDING_ID,
        binding_version=BINDING_VERSION,
        runtime_profile_version=RUNTIME_PROFILE_VERSION,
    )
    results: dict[str, Any] = {}
    _assert_approved_files_original()

    async def run_round(
        client: _DeviceClient,
        *,
        turn_id: int,
        label: str,
        stop: bool,
        crisis: bool,
        timeout: float,
    ) -> _TurnAudio:
        await client.utterance(turn_id=turn_id)
        if stop:
            await client.wait_for_reply_audio(turn_id)
            await client.stop(turn_id=turn_id)
        # A crisis stop is not interruptible: the approved clip keeps playing
        # and its closing cue still rides the reply's own generation.
        stopped = stop and not crisis
        turn = await client.wait_turn_complete(turn_id, stopped=stopped, timeout=timeout)
        client.assert_listening_after_terminal(turn, label=label)
        _assert_turn_audio(turn, crisis=crisis, label=label, stopped=stopped)
        results[label] = {
            "turn_id": turn_id,
            "generations": sorted(turn.generation_frames),
            "audio_bytes": len(turn.audio()),
            "reply_bytes": len(_turn_reply_pcm(turn, stopped=stopped)),
        }
        return turn

    try:
        async with _DeviceClient(channel, identity) as client:
            # The first frames open the session and play the wake
            # acknowledgement as turn 1. Its audio must finish before the first
            # real utterance or the VAD edge is dropped as echo.
            await client.open_session()
            wake = await client.wait_turn_complete(1, timeout=40)
            client.assert_listening_after_terminal(wake, label="wake")
            _assert_turn_audio(wake, crisis=False, label="wake", wake=True)
            results["wake"] = {
                "turn_id": 1,
                "generations": sorted(wake.generation_frames),
                "audio_bytes": len(wake.audio()),
                "reply_bytes": len(_turn_reply_pcm(wake, wake=True)),
            }

            await run_round(client, turn_id=2, label="normal", stop=False, crisis=False, timeout=45)

            # Round 2: the child says 停 mid-reply. The interrupted turn must
            # still show opening, its own reply frames, and the closing cue.
            await run_round(client, turn_id=3, label="stop", stop=True, crisis=False, timeout=45)

            # Rounds 3-7: the crisis turn, five times, each timed from the
            # device's VAD end through the opening cue to the reply playback.
            latencies: list[dict[str, Any]] = []
            for index in range(CRISIS_ROUNDS):
                turn_id = 4 + index
                label = f"crisis{index + 1}"
                turn = await run_round(
                    client, turn_id=turn_id, label=label, stop=False, crisis=True, timeout=90
                )
                measurement = _crisis_latency(turn)
                record: dict[str, Any] = {
                    "round": index + 1,
                    "turn_id": turn_id,
                    **{key: round(value, 3) for key, value in measurement.items()},
                }
                print(json.dumps(record, sort_keys=True), flush=True)
                assert measurement["vad_end_to_reply_playback_s"] <= CRISIS_LATENCY_BUDGET_S, (
                    f"{label}: crisis latency "
                    f"{measurement['vad_end_to_reply_playback_s']:.3f}s exceeds "
                    f"{CRISIS_LATENCY_BUDGET_S:.1f}s including the opening cue"
                )
                latencies.append(record)
            results["crisis_latencies_s"] = latencies
            results["crisis_latency_budget_s"] = CRISIS_LATENCY_BUDGET_S
            results["crisis_latency_pass"] = f"{len(latencies)}/{CRISIS_ROUNDS}"

            # Round 8: the child says 停 while the crisis script is audible. The
            # approved clip must stream in full, then the closing cue as usual.
            stop_turn_id = 4 + CRISIS_ROUNDS
            crisis_stop = await run_round(
                client,
                turn_id=stop_turn_id,
                label="crisis_stop",
                stop=True,
                crisis=True,
                timeout=90,
            )
            _assert_crisis_script_complete(
                crisis_stop,
                label="crisis_stop",
                stop_at=client.stop_sent_at.get(stop_turn_id),
                frames_at_stop=client.frames_at_stop.get(stop_turn_id),
            )
            results["crisis_stop_script"] = {
                "turn_id": stop_turn_id,
                "script_bytes": len(_crisis_pcm()),
                "script_sha256": _crisis_manifest_digest(),
                "frames_at_stop": client.frames_at_stop[stop_turn_id],
                "final_frames": len(
                    crisis_stop.generation_frames[min(crisis_stop.generation_frames)]
                ),
            }

            assert client.session_accepts == 1, (
                f"rounds must share one accepted session, got {client.session_accepts}"
            )
            results["single_session"] = {
                "accepts": client.session_accepts,
                "turn_ids": sorted(client.turns),
            }

            # After the last closing marker the device is listening. Silence
            # without a speech edge must not start a turn, call the LLM, or
            # emit any assistant audio. The server may still be projecting the
            # terminal completion when the device flips, so let that settle
            # before measuring; no audio is sent during the settle.
            idle_started_at = client.last_listening_at(stop_turn_id)
            assert not client.closed_states, (
                f"session closed before the silent window: {client.closed_states}"
            )
            assert not client.stream_ended, (
                f"media stream ended before the silent window (error={client.stream_error})"
            )
            await asyncio.sleep(0.5)
            assert not client.closed_states, (
                f"session closed during the listening settle: {client.closed_states}"
            )
            silent_snapshot = client.generation_snapshot()
            llm_calls_before = state.llm_calls
            tts_before = len(state.tts_texts)
            asr_pcm_before = state.asr_pcm_bytes
            disk_before = audio_inventory()
            await client.send_silence(seconds=SILENT_WINDOW_S)
            await asyncio.sleep(0.5)
            client.assert_no_generation_since(silent_snapshot, label="silent window")
            assert state.llm_calls == llm_calls_before, (
                f"silent window triggered {state.llm_calls - llm_calls_before} LLM call(s)"
            )
            assert len(state.tts_texts) == tts_before, "silent window triggered TTS"
            silent_asr_pcm_bytes = state.asr_pcm_bytes - asr_pcm_before
            silent_disk_audio = _audio_written_since(disk_before, audio_inventory())
            assert silent_asr_pcm_bytes == 0, (
                f"silent window forwarded {silent_asr_pcm_bytes} bytes of PCM to fake ASR"
            )
            assert not silent_disk_audio, (
                f"silent window wrote audio to disk: {silent_disk_audio}"
            )
            results["silent_window"] = {
                "seconds": SILENT_WINDOW_S,
                "llm_calls": state.llm_calls - llm_calls_before,
                "new_generations": len(client.generation_actions) - silent_snapshot[0],
                "asr_pcm_bytes": silent_asr_pcm_bytes,
                "disk_audio_bytes": sum(silent_disk_audio.values()),
                "disk_audio_files": sorted(silent_disk_audio),
            }
            print(
                json.dumps(
                    {
                        "silent_window_asr_pcm_bytes": silent_asr_pcm_bytes,
                        "silent_window_disk_audio_bytes": sum(silent_disk_audio.values()),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

            closed = await client.wait_session_closed(timeout=IDLE_TIMEOUT_S + 10)
            elapsed = time.monotonic() - idle_started_at
            assert closed[1] == "owner_silence_timeout", (
                f"idle close reason {closed[1]!r}, want owner_silence_timeout"
            )
            assert closed[2] is False, "idle close must not ask for the closing line"
            assert not client.errors, f"idle close emitted errors: {client.errors}"
            assert IDLE_TIMEOUT_S - 1.0 <= elapsed <= IDLE_TIMEOUT_S + 5.0, (
                f"idle close took {elapsed:.2f}s, want about {IDLE_TIMEOUT_S:.1f}s"
            )
            results["idle_close"] = {
                "reason": closed[1],
                "timeout_s": IDLE_TIMEOUT_S,
                "elapsed_s": round(elapsed, 3),
                "clean": True,
            }
    finally:
        await channel.close()
    return results


async def _run_tts_fault_scenario(
    *,
    cert_dir: Path,
    state: _FakeState,
) -> dict[str, Any]:
    """Both TTS engines fail before any audio, so the session closes on it.

    The wake greeting is the first thing the session speaks, and it is spoken
    through the same failover as every later reply. With both engines down the
    session never reaches a conversation turn: the device sees one close, and
    that close names the outage and asks for the local closing line.
    """

    ca = (cert_dir / "ca.pem").read_bytes()
    credentials = grpc.ssl_channel_credentials(
        root_certificates=ca,
        private_key=(cert_dir / "client.key").read_bytes(),
        certificate_chain=(cert_dir / "client.pem").read_bytes(),
    )
    channel = grpc.aio.secure_channel(
        f"localhost:{AGENT_PORT}",
        credentials,
        options=(("grpc.ssl_target_name_override", "localhost"),),
    )
    await asyncio.wait_for(channel.channel_ready(), timeout=30)
    identity = media_pb2.SessionIdentity(
        session_id=SESSION_ID,
        account_id=ACCOUNT_ID,
        participant_id=ACCOUNT_ID,
        device_id=DEVICE_ID,
        client_type="device",
        stream_epoch=1,
        subject_id=SUBJECT_ID,
        binding_id=BINDING_ID,
        binding_version=BINDING_VERSION,
        runtime_profile_version=RUNTIME_PROFILE_VERSION,
    )
    try:
        async with _DeviceClient(channel, identity) as client:
            await client.open_session()
            closed = await client.wait_session_closed(timeout=40)
            audio_bytes = sum(len(turn.audio()) for turn in client.turns.values())
            assert audio_bytes == 0, (
                f"tts fault delivered {audio_bytes} bytes of audio before the close"
            )
            assert len(client.closed_states) == 1, (
                f"tts fault closed the session {len(client.closed_states)} times: "
                f"{client.closed_states}"
            )
            assert closed[1] == "tts_unavailable", (
                f"close reason {closed[1]!r}, want tts_unavailable"
            )
            assert closed[2] is True, "close did not ask the device to play the local line"
            assert state.doubao_failures >= 1, "the Doubao engine was never attempted"
            return {
                "close_reason": closed[1],
                "play_line": closed[2],
                "audio_bytes": audio_bytes,
                "doubao_failures": state.doubao_failures,
            }
    finally:
        await channel.close()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tts-fault",
        choices=["all"],
        default="",
        help="fail every fake TTS engine before its first audio byte",
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    cert_dir = Path(tempfile.mkdtemp(prefix=".aigc-e2e-certs-", dir=REPO_ROOT))
    files = _write_certs(cert_dir)
    ssl_context = _server_ssl(files)
    state = _FakeState(tts_fault=args.tts_fault)
    FAKE_LOG.write_text("", encoding="utf-8")
    control_runner, control_port = await _start_http(await _control_api_app(state), None)
    llm_runner, llm_port = await _start_http(await _llm_app(state), None)
    asr_server, asr_port = await _start_ws(_asr_handler, state, ssl_context)
    stepfun_server, stepfun_port = await _start_ws(_doubao_handler, state, ssl_context)
    docker_env = _docker_env()
    fault = state.tts_fault == "all"
    compose_env = _compose_env(
        cert_dir=cert_dir,
        control_port=control_port,
        asr_port=asr_port,
        stepfun_port=stepfun_port,
        llm_port=llm_port,
        tts_fault=fault,
    )
    if not fault:
        for text in (NORMAL_TEXT, NORMAL_TEXT, *([CRISIS_TEXT] * (CRISIS_ROUNDS + 1))):
            await state.asr_texts.put(text)
    logs: str = ""
    try:
        print(
            json.dumps(
                {
                    "asr_port": asr_port,
                    "stepfun_port": stepfun_port,
                    "control_port": control_port,
                    "llm_port": llm_port,
                }
            ),
            flush=True,
        )
        _compose_up(compose_env, docker_env)
        _wait_port("127.0.0.1", AGENT_PORT, timeout=60)

        def audio_inventory() -> dict[str, list[int]]:
            return _container_audio_inventory(docker_env, compose_env)

        if fault:
            results = await _run_tts_fault_scenario(cert_dir=cert_dir, state=state)
        else:
            disk_before_run = audio_inventory()
            results = await _run_scenario(
                cert_dir=cert_dir,
                state=state,
                audio_inventory=audio_inventory,
            )
            disk_after_run = audio_inventory()
            logs = _compose_logs(docker_env)
            for line in logs.splitlines():
                if "ERROR" in line or "output_intent_not_selected" in line:
                    raise RuntimeError(f"E2E agent emitted an error line: {line}")
            buffers_cleared = _assert_audio_buffers_cleared(logs)
            disk_written = _audio_written_since(disk_before_run, disk_after_run)
            assert not disk_written, f"e2e wrote audio to disk: {disk_written}"
            if any(CRISIS_TEXT in spoken for spoken in state.tts_texts):
                raise RuntimeError("crisis text reached the live TTS engine")
            results["crisis_sha256"] = hashlib.sha256(_crisis_pcm()).hexdigest()
            results["disk_audio_written_bytes"] = sum(disk_written.values())
            results["audio_buffers_cleared"] = buffers_cleared
            print(
                json.dumps(
                    {
                        "disk_audio_written_bytes": sum(disk_written.values()),
                        "audio_buffers_cleared": buffers_cleared,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        results["llm_calls"] = state.llm_calls
        results["tts_units"] = len(state.tts_texts)
        results["asr_connections"] = state.asr_connections
        results["asr_tasks"] = state.asr_tasks
        print(json.dumps({"ok": True, **results}, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        logs = _compose_logs(docker_env)
        print(logs[-12_000:], file=sys.stderr)
        raise
    finally:
        if os.getenv("AIGC_E2E_KEEP") != "1":
            _compose_down(docker_env)
        asr_server.close()
        await asr_server.wait_closed()
        stepfun_server.close()
        await stepfun_server.wait_closed()
        await control_runner.cleanup()
        await llm_runner.cleanup()
        if os.getenv("AIGC_E2E_KEEP") != "1":
            shutil.rmtree(cert_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
