"""End-to-end AIGC reply audio regression against the production agent image.

The agent runs from ``infra/Dockerfile.agent`` under the production compose
shape with mTLS and the shipped fixed-safety catalog. Only the network edges
are fake: StepFun TTS, the OpenAI-compatible LLM, FunASR and the Control API.

The simulated device drives the public media-v1 gRPC stream and asserts, per
turn, that the opening cue leads reply audio, the reply itself emitted frames,
the closing cue followed, and the crisis turn played the approved catalog. The
last round sends 停 mid-crisis-script and requires the full approved clip.
"""

from __future__ import annotations

import asyncio
import base64
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


def _fake_log(message: str) -> None:
    with FAKE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


@dataclass(slots=True)
class _FakeState:
    asr_texts: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    stepfun_texts: list[str] = field(default_factory=list)
    asr_tasks: int = 0
    asr_connections: int = 0
    llm_calls: int = 0


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


async def _control_api_app() -> web.Application:
    async def policy(request: web.Request) -> web.Response:
        body = await request.json()
        return web.json_response(_policy_envelope(str(body["session_id"])))

    async def heartbeat(_request: web.Request) -> web.Response:
        return web.json_response({"status": "recorded"})

    async def accepted(_request: web.Request) -> web.Response:
        return web.json_response({"status": "recorded"}, status=202)

    app = web.Application()
    app.router.add_post("/v1/interaction/session-policy", policy)
    app.router.add_post("/internal/readiness/agent-heartbeat", heartbeat)
    app.router.add_post("/v1/interaction/response-plan", accepted)
    app.router.add_post("/v1/interaction/context-prefetch", accepted)
    app.router.add_post("/v1/guardian/crisis-script-interrupted", accepted)
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


async def _stepfun_handler(ws: Any, state: _FakeState) -> None:
    session_id = uuid.uuid4().hex
    texts: list[str] = []
    _fake_log("stepfun connect")
    await ws.send(
        json.dumps(
            {
                "type": "tts.connection.done",
                "data": {"session_id": session_id},
            }
        )
    )
    try:
        async for raw in ws:
            if not isinstance(raw, str):
                # The Doubao backup shares this endpoint; its protocol is
                # binary and only opens when StepFun fails.
                _fake_log("stepfun binary frame ignored")
                continue
            event = json.loads(raw)
            event_type = str(event.get("type") or "")
            if event_type == "tts.create":
                await ws.send(
                    json.dumps(
                        {
                            "type": "tts.response.created",
                            "data": {"session_id": session_id},
                        }
                    )
                )
            elif event_type == "tts.text.delta":
                text = str((event.get("data") or {}).get("text") or "")
                if text:
                    texts.append(text)
            elif event_type == "tts.text.done":
                spoken = "".join(texts)
                state.stepfun_texts.append(spoken)
                pcm = _reply_pcm(spoken)
                encoded = base64.b64encode(pcm).decode("ascii")
                await ws.send(
                    json.dumps(
                        {
                            "type": "tts.response.audio.delta",
                            "data": {"session_id": session_id, "audio": encoded},
                        }
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "type": "tts.response.audio.done",
                            "data": {"session_id": session_id},
                        }
                    )
                )
    except websockets.ConnectionClosed:
        _fake_log("stepfun closed")
        return
    except Exception as exc:  # noqa: BLE001 - the fake server must stay observable
        _fake_log(f"stepfun handler error {type(exc).__name__}: {exc}")
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
    def __init__(self, channel: grpc.aio.Channel, identity: media_pb2.SessionIdentity) -> None:
        self.channel = channel
        self.identity = identity
        self.requests: asyncio.Queue[media_pb2.MediaToCore | None] = asyncio.Queue()
        self.turns: dict[int, _TurnAudio] = defaultdict(lambda: _TurnAudio(0))
        self.runtime_states: list[tuple[int, int]] = []
        self.errors: list[str] = []
        self._sample = 0
        self._audio_sequence = 0
        self._last_frame_at = 0.0
        self._task: asyncio.Task[Any] | None = None
        # Turn ids are deterministic from the scenario; remembering the VAD end
        # per turn keeps the latency clock on the device's speech edge.
        self._pending_vad_end: dict[int, float] = {}
        # Loop-clock moment each 停 was queued, and the number of the turn's
        # first-generation frames received by then. The final count is read
        # later, so a larger total proves the downlink kept streaming after the
        # stop.
        self.stop_sent_at: dict[int, float] = {}
        self.frames_at_stop: dict[int, int] = {}

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
        self._task = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *_args: object) -> None:
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
        await self.requests.put(message)

    async def _read(self) -> None:
        while True:
            try:
                message = await self._call.read()
            except Exception:
                return
            event = message.WhichOneof("event")
            if event == "audio":
                frame = message.audio
                turn_id = int(frame.turn_id)
                turn = self.turns.setdefault(turn_id, _TurnAudio(turn_id))
                arrived_at = asyncio.get_running_loop().time()
                if turn.vad_end_at is None:
                    turn.vad_end_at = self._pending_vad_end.pop(turn_id, None)
                turn.add(frame, arrived_at=arrived_at)
                self._last_frame_at = arrived_at
                if frame.final_frame:
                    await self._ack(frame)
            elif event == "state":
                self.runtime_states.append((int(message.state.state), int(message.state.turn_id)))
            elif event == "error":
                self.errors.append(message.error.message)
            elif event == "accepted":
                continue

    async def _ack(self, frame: Any) -> None:
        turn = self.turns[int(frame.turn_id)]
        generation = int(frame.generation_id)
        if generation in turn.acked:
            return
        turn.acked.add(generation)
        await self.send(
            media_pb2.MediaToCore(
                playback=media_pb2.PlaybackProgress(
                    identity=self.identity,
                    generation_id=generation,
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

    async def stop(self, *, turn_id: int | None = None) -> None:
        if turn_id is not None:
            self.stop_sent_at[turn_id] = asyncio.get_running_loop().time()
            turn = self.turns.get(turn_id)
            if turn is not None and turn.generation_frames:
                first = min(turn.generation_frames)
                # A snapshot of what the device has received. Frames arriving
                # after this line still append to the same generation.
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

    async def wait_turn_complete(self, turn_id: int, *, timeout: float = 40.0) -> _TurnAudio:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            turn = self.turns.get(turn_id)
            if turn is not None and len(turn.generation_frames) >= 2:
                closing = max(turn.generation_frames)
                if any(int(frame.final_frame) for frame in turn.generation_frames[closing]):
                    await self.wait_quiet(quiet_s=0.5, timeout=10)
                    return turn
            if self.errors:
                raise RuntimeError(f"media bridge error: {self.errors}")
            await asyncio.sleep(0.05)
        raise RuntimeError(f"turn {turn_id} did not complete AIGC reply audio")

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

    async def wait_quiet(self, *, quiet_s: float = 1.0, timeout: float = 40.0) -> None:
        """Wait until the downlink stopped carrying assistant audio."""

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            idle_for = asyncio.get_running_loop().time() - self._last_frame_at
            listening = (
                bool(self.runtime_states)
                and self.runtime_states[-1][0] == media_pb2.CONVERSATION_STATE_LISTENING
            )
            if listening and idle_for >= quiet_s:
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("assistant audio did not settle")

    async def wait_listening(self, *, timeout: float = 15.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if (
                self.runtime_states
                and self.runtime_states[-1][0] == media_pb2.CONVERSATION_STATE_LISTENING
            ):
                return
            await asyncio.sleep(0.02)
        raise RuntimeError("media session did not return to listening")


MARKER_PCM = AigcNoticeConfig().tone_pcm()
MARKER_MIN_FRAMES = (len(MARKER_PCM) + MARKER_FRAME_SAMPLES * 2 - 1) // (MARKER_FRAME_SAMPLES * 2)
MARKER_DURATION_S = len(MARKER_PCM) / 2 / MARKER_SAMPLE_RATE
CRISIS_ROUNDS = 5
CRISIS_LATENCY_BUDGET_S = 5.0


def _frames_audio(frames: list[Any]) -> bytes:
    return b"".join(bytes(frame.pcm_s16le) for frame in frames)


def _assert_approved_files_original() -> None:
    """The four approved WAVs are byte-identical to the reviewed manifest."""

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


def _turn_reply_pcm(turn: _TurnAudio) -> bytes:
    """The reply's own audio: after the opening marker, before the closing one."""

    generations = sorted(turn.generation_frames)
    reply = _frames_audio(turn.generation_frames[generations[0]][MARKER_MIN_FRAMES:])
    for later in generations[1:-1]:
        reply += _frames_audio(turn.generation_frames[later])
    return reply


def _assert_turn_audio(turn: _TurnAudio, *, crisis: bool, label: str) -> None:
    """Every round: opening cue, the reply's own frames, then the closing cue."""

    generations = sorted(turn.generation_frames)
    assert len(generations) >= 2, f"{label}: reply and closing cue are separate generations"
    opening_frames = turn.generation_frames[generations[0]]
    opening = _frames_audio(opening_frames)
    assert opening.startswith(MARKER_PCM), f"{label}: opening cue must lead the reply"
    assert len(opening_frames) > MARKER_MIN_FRAMES, (
        f"{label}: the reply emitted no audio frames of its own"
    )
    reply = _turn_reply_pcm(turn)
    assert reply, f"{label}: the reply emitted no audio frames of its own"
    closing_frames = turn.generation_frames[generations[-1]]
    closing = _frames_audio(closing_frames)
    assert closing.startswith(MARKER_PCM), f"{label}: closing cue missing after the reply"
    assert closing[: len(MARKER_PCM)] == MARKER_PCM
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


async def _run_scenario(*, cert_dir: Path) -> dict[str, Any]:
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
        turn = await client.wait_turn_complete(turn_id, timeout=timeout)
        _assert_turn_audio(turn, crisis=crisis, label=label)
        results[label] = {
            "turn_id": turn_id,
            "generations": sorted(turn.generation_frames),
            "audio_bytes": len(turn.audio()),
            "reply_bytes": len(turn.audio()) - len(MARKER_PCM) * 2,
        }
        return turn

    try:
        async with _DeviceClient(channel, identity) as client:
            # The first frames open the session and play the wake
            # acknowledgement as turn 1. Its audio must finish before the first
            # real utterance or the VAD edge is dropped as echo.
            await client.open_session()
            await client.wait_turn_complete(1, timeout=40)
            await client.wait_quiet(quiet_s=1.2, timeout=40)

            # Round 1: an ordinary reply still carries the opening cue as the
            # head of its own audio, then the closing cue on a new generation.
            await run_round(client, turn_id=2, label="normal", stop=False, crisis=False, timeout=45)
            await client.wait_quiet(quiet_s=0.8, timeout=30)

            # Round 2: the child says 停 mid-reply. The interrupted turn must
            # still show opening, its own reply frames, and the closing cue.
            await run_round(client, turn_id=3, label="stop", stop=True, crisis=False, timeout=45)
            await client.wait_quiet(quiet_s=0.8, timeout=30)

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
                await client.wait_quiet(quiet_s=0.8, timeout=30)
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
    finally:
        await channel.close()
    return results


async def main() -> int:
    cert_dir = Path(tempfile.mkdtemp(prefix=".aigc-e2e-certs-", dir=REPO_ROOT))
    files = _write_certs(cert_dir)
    ssl_context = _server_ssl(files)
    state = _FakeState()
    FAKE_LOG.write_text("", encoding="utf-8")
    control_runner, control_port = await _start_http(await _control_api_app(), None)
    llm_runner, llm_port = await _start_http(await _llm_app(state), None)
    asr_server, asr_port = await _start_ws(_asr_handler, state, ssl_context)
    stepfun_server, stepfun_port = await _start_ws(_stepfun_handler, state, ssl_context)
    docker_env = _docker_env()
    compose_env = _compose_env(
        cert_dir=cert_dir,
        control_port=control_port,
        asr_port=asr_port,
        stepfun_port=stepfun_port,
        llm_port=llm_port,
    )
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
        results = await _run_scenario(cert_dir=cert_dir)
        logs = _compose_logs(docker_env)
        for line in logs.splitlines():
            if "ERROR" in line or "output_intent_not_selected" in line:
                raise RuntimeError(f"E2E agent emitted an error line: {line}")
        if any(CRISIS_TEXT in spoken for spoken in state.stepfun_texts):
            raise RuntimeError("crisis text reached the live StepFun engine")
        results["llm_calls"] = state.llm_calls
        results["stepfun_units"] = len(state.stepfun_texts)
        results["asr_connections"] = state.asr_connections
        results["asr_tasks"] = state.asr_tasks
        results["crisis_sha256"] = hashlib.sha256(_crisis_pcm()).hexdigest()
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
