"""Local protocol mock WebSocket/HTTP servers for speech and LLM providers."""

from __future__ import annotations

import asyncio
import json
import struct
import threading
from dataclasses import dataclass, field
from typing import Any

import websockets
from aiohttp import web
from services.agent.src.providers.doubao_protocol import EventType, MessageType
from websockets.asyncio.server import ServerConnection

_LOOPBACK_CLIENT_HOST = "localhost"  # Keep mock traffic out of system proxies.


@dataclass
class MockFunASRServer:
    host: str = "127.0.0.1"
    port: int = 0
    scenario: str = "happy"  # happy|task_reuse|task_reuse_late_event|heartbeat_stall|context_leak|interim_rewrite|duplicate_final|heartbeat|missing_ts|fail|disconnect_once
    connections_closed: int = 0
    connections_started: int = 0
    tasks_started: list[str] = field(default_factory=list)
    pcm_by_connection: list[int] = field(default_factory=list)
    connection_closed: threading.Event = field(default_factory=threading.Event)
    _server: Any = None
    _thread: threading.Thread | None = None
    _loop: asyncio.AbstractEventLoop | None = None

    @property
    def ws_url(self) -> str:
        return f"ws://{_LOOPBACK_CLIENT_HOST}:{self.port}"

    def start(self) -> None:
        ready = threading.Event()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def _start() -> Any:
                return await websockets.serve(self._handler, self.host, self.port)

            self._server = self._loop.run_until_complete(_start())
            sock = self._server.sockets[0]
            self.port = int(sock.getsockname()[1])
            ready.set()
            self._loop.run_forever()
            self._loop.run_until_complete(self._server.wait_closed())
            self._loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        ready.wait(timeout=5)

    def stop(self) -> None:
        if self._loop is None or self._server is None:
            return

        def _close() -> None:
            self._server.close()

            async def _wait() -> None:
                await self._server.wait_closed()
                assert self._loop is not None
                self._loop.stop()

            self._loop.create_task(_wait())

        self._loop.call_soon_threadsafe(_close)
        if self._thread is not None:
            self._thread.join(timeout=5)

    async def _handler(self, ws: ServerConnection) -> None:
        task_id = ""
        self.connections_started += 1
        try:
            raw = await ws.recv()
            msg = json.loads(raw if isinstance(raw, str) else raw.decode())
            if self.scenario in {
                "task_reuse",
                "task_reuse_late_event",
                "heartbeat_stall",
            }:
                await self._handle_reused_tasks(ws, msg)
                return
            task_id = str(msg.get("header", {}).get("task_id") or "")
            self.tasks_started.append(task_id)
            connection_index = len(self.tasks_started) - 1
            self.pcm_by_connection.append(0)
            if self.scenario == "handshake_timeout":
                await ws.wait_closed()
                return
            await ws.send(
                json.dumps({"header": {"event": "task-started", "task_id": task_id}, "payload": {}})
            )
            if self.scenario == "fail":
                # Still emit task-started first so clients complete handshake,
                # then fail the task (connection must not be reused).
                await ws.send(
                    json.dumps(
                        {"header": {"event": "task-started", "task_id": task_id}, "payload": {}}
                    )
                )
                await ws.send(
                    json.dumps(
                        {
                            "header": {"event": "task-failed", "task_id": task_id},
                            "payload": {"message": "simulated failure"},
                        }
                    )
                )
                await ws.close()
                self.connections_closed += 1
                return

            # Consume some PCM then emit results
            pcm_got = 0
            while pcm_got < 3200:
                try:
                    frame = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except TimeoutError:
                    break
                if isinstance(frame, bytes):
                    pcm_got += len(frame)
                    self.pcm_by_connection[connection_index] += len(frame)
                elif isinstance(frame, str):
                    obj = json.loads(frame)
                    if obj.get("header", {}).get("action") == "finish-task":
                        break

            if self.scenario == "disconnect" or (
                self.scenario == "disconnect_once" and connection_index == 0
            ):
                await ws.close()
                self.connections_closed += 1
                return

            if self.scenario == "heartbeat":
                await ws.send(_result(task_id, "", sentence_end=False, heartbeat=True, words=[]))

            if self.scenario == "context_leak":
                context = msg.get("payload", {}).get("input", {}).get("context", [])
                previous = " ".join(
                    str(content.get("text") or "")
                    for item in context
                    for content in item.get("content", [])
                    if isinstance(content, dict)
                ).strip()
                text = f"{previous} 介绍一下南京。".strip()
                await ws.send(_result(task_id, text, sentence_end=True, words=_chars(text)))
            elif self.scenario == "interim_rewrite":
                await ws.send(
                    _result(task_id, "我想定", sentence_end=False, words=_chars("我想定"))
                )
                await ws.send(
                    _result(task_id, "我想订下", sentence_end=False, words=_chars("我想订下"))
                )
                await ws.send(
                    _result(
                        task_id,
                        "我想订下周",
                        sentence_end=False,
                        words=_chars("我想订下周"),
                    )
                )
                await ws.send(
                    _result(
                        task_id,
                        "我想订下周三的票",
                        sentence_end=True,
                        words=_chars("我想订下周三的票"),
                    )
                )
            elif self.scenario == "duplicate_final":
                text = "重复结果只提交一次。"
                await ws.send(_result(task_id, text, sentence_end=True, words=_chars(text)))
                await ws.send(_result(task_id, text, sentence_end=True, words=_chars(text)))
            elif self.scenario == "missing_ts":
                await ws.send(
                    json.dumps(
                        {
                            "header": {"event": "result-generated", "task_id": task_id},
                            "payload": {
                                "output": {
                                    "sentence": {
                                        "begin_time": 0,
                                        "end_time": 500,
                                        "text": "你好",
                                        "heartbeat": False,
                                        "sentence_end": True,
                                        "sentence_id": 1,
                                        "words": [],
                                    }
                                }
                            },
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                text = "你好，这是实时语音测试。"
                await ws.send(
                    _result(task_id, text[:4], sentence_end=False, words=_chars(text[:4]))
                )
                await ws.send(_result(task_id, text, sentence_end=True, words=_chars(text)))

            # wait finish-task if not already
            try:
                while True:
                    frame = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    if isinstance(frame, str) and "finish-task" in frame:
                        break
            except TimeoutError:
                pass

            await ws.send(
                json.dumps(
                    {"header": {"event": "task-finished", "task_id": task_id}, "payload": {}}
                )
            )
        finally:
            self.connections_closed += 1
            self.connection_closed.set()

    async def _handle_reused_tasks(
        self,
        ws: ServerConnection,
        run_message: dict[str, Any],
    ) -> None:
        """Exercise the documented finish-task -> run-task reuse sequence."""

        current = run_message
        task_number = 0
        while True:
            header = current.get("header") or {}
            if header.get("action") != "run-task":
                raise RuntimeError("task reuse mock expected run-task")
            task_id = str(header.get("task_id") or "")
            task_number += 1
            self.tasks_started.append(task_id)
            self.pcm_by_connection.append(0)
            pcm_index = len(self.pcm_by_connection) - 1
            await ws.send(
                json.dumps(
                    {"header": {"event": "task-started", "task_id": task_id}, "payload": {}}
                )
            )

            while True:
                frame = await ws.recv()
                if isinstance(frame, bytes):
                    self.pcm_by_connection[pcm_index] += len(frame)
                    continue
                message = json.loads(frame)
                action = (message.get("header") or {}).get("action")
                if action == "continue-task":
                    continue
                if action == "finish-task":
                    break
                raise RuntimeError(f"unexpected task action: {action}")

            if self.scenario == "heartbeat_stall":
                while True:
                    await ws.send(
                        _result(
                            task_id,
                            "",
                            sentence_end=False,
                            heartbeat=True,
                            words=[],
                            sentence_id=0,
                        )
                    )
                    await asyncio.sleep(0.03)

            text = f"第{task_number}段识别。"
            await ws.send(_result(task_id, text[:-1], sentence_end=False, words=_chars(text[:-1])))
            await ws.send(_result(task_id, text, sentence_end=True, words=_chars(text)))
            await ws.send(
                json.dumps(
                    {"header": {"event": "task-finished", "task_id": task_id}, "payload": {}}
                )
            )
            if self.scenario == "task_reuse_late_event" and task_number == 1:
                late_text = "这个旧任务结果必须丢弃。"
                await ws.send(
                    _result(task_id, late_text, sentence_end=True, words=_chars(late_text))
                )

            raw = await ws.recv()
            current = json.loads(raw if isinstance(raw, str) else raw.decode())


def _chars(text: str) -> list[dict[str, Any]]:
    words = []
    t = 0
    for ch in text:
        words.append({"begin_time": t, "end_time": t + 80, "text": ch, "punctuation": ""})
        t += 80
    return words


def _result(
    task_id: str,
    text: str,
    *,
    sentence_end: bool,
    words: list[dict[str, Any]],
    heartbeat: bool = False,
    sentence_id: int = 1,
) -> str:
    return json.dumps(
        {
            "header": {"event": "result-generated", "task_id": task_id},
            "payload": {
                "output": {
                    "sentence": {
                        "begin_time": 0,
                        "end_time": max((w["end_time"] for w in words), default=0),
                        "text": text,
                        "heartbeat": heartbeat,
                        "sentence_end": sentence_end,
                        "sentence_id": sentence_id,
                        "words": words,
                    }
                }
            },
        },
        ensure_ascii=False,
    )


@dataclass
class MockCosyVoiceServer:
    host: str = "127.0.0.1"
    port: int = 0
    scenario: str = "happy"  # happy|late_ts|fail|slow|split_pcm
    closed_without_reuse: int = 0
    active: int = 0
    connections: int = 0
    run_requests: list[dict[str, Any]] = field(default_factory=list)
    _server: Any = None
    _thread: threading.Thread | None = None
    _loop: asyncio.AbstractEventLoop | None = None

    @property
    def ws_url(self) -> str:
        return f"ws://{_LOOPBACK_CLIENT_HOST}:{self.port}"

    def start(self) -> None:
        ready = threading.Event()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def _start() -> Any:
                return await websockets.serve(self._handler, self.host, self.port)

            self._server = self._loop.run_until_complete(_start())
            self.port = int(self._server.sockets[0].getsockname()[1])
            ready.set()
            self._loop.run_forever()
            self._loop.run_until_complete(self._server.wait_closed())
            self._loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        ready.wait(timeout=5)

    def stop(self) -> None:
        if self._loop is None or self._server is None:
            return

        def _close() -> None:
            self._server.close()

            async def _wait() -> None:
                await self._server.wait_closed()
                assert self._loop is not None
                self._loop.stop()

            self._loop.create_task(_wait())

        self._loop.call_soon_threadsafe(_close)
        if self._thread is not None:
            self._thread.join(timeout=5)

    async def _handler(self, ws: ServerConnection) -> None:
        self.active += 1
        self.connections += 1
        connection_index = self.connections - 1
        task_id = ""
        texts: list[str] = []
        try:
            raw = await ws.recv()
            msg = json.loads(raw if isinstance(raw, str) else raw.decode())
            self.run_requests.append(msg)
            task_id = str(msg["header"]["task_id"])
            if self.scenario == "fail" or (self.scenario == "fail_once" and connection_index == 0):
                await ws.send(
                    json.dumps(
                        {
                            "header": {"event": "task-failed", "task_id": task_id},
                            "payload": {"message": "fail"},
                        }
                    )
                )
                return

            await ws.send(
                json.dumps({"header": {"event": "task-started", "task_id": task_id}, "payload": {}})
            )

            while True:
                frame = await ws.recv()
                if isinstance(frame, bytes):
                    continue
                obj = json.loads(frame)
                action = obj.get("header", {}).get("action")
                if action == "continue-task":
                    texts.append(str(obj.get("payload", {}).get("input", {}).get("text") or ""))
                elif action == "finish-task":
                    break

            full = "".join(texts) or "你好，这是语音合成测试。"
            if self.scenario == "slow" or (self.scenario == "slow_once" and connection_index == 0):
                await asyncio.sleep(0.3)
            # sentence-begin
            await ws.send(
                json.dumps(
                    {
                        "header": {"event": "result-generated", "task_id": task_id},
                        "payload": {
                            "output": {
                                "type": "sentence-begin",
                                "sentence": {"index": 0, "words": []},
                                "original_text": full,
                            }
                        },
                    },
                    ensure_ascii=False,
                )
            )
            await ws.send(
                json.dumps(
                    {
                        "header": {"event": "result-generated", "task_id": task_id},
                        "payload": {
                            "output": {
                                "type": "sentence-synthesis",
                                "sentence": {"index": 0, "words": []},
                            }
                        },
                    }
                )
            )
            if self.scenario == "split_pcm":
                # Two transport chunks from one continuous stream. Their different
                # amplitudes make any per-chunk gain adjustment observable.
                for level in (1200, 16000):
                    await ws.send(struct.pack("<480h", *([level] * 480)))
            else:
                # 20ms of 24kHz mono 16-bit silence * N
                samples = max(480, len(full) * 240)  # rough
                await ws.send(b"\x00\x00" * samples)

            words = []
            t = 0
            for ch in full:
                words.append({"begin_time": t, "end_time": t + 80, "text": ch, "punctuation": ""})
                t += 80

            if self.scenario == "empty_ts" or (
                self.scenario == "empty_ts_once" and connection_index == 0
            ):
                words = []

            if self.scenario == "late_ts":
                # timestamps intentionally short vs PCM
                for w in words:
                    w["end_time"] = int(int(w["end_time"]) * 0.5)
                    w["begin_time"] = int(int(w["begin_time"]) * 0.5)

            await ws.send(
                json.dumps(
                    {
                        "header": {"event": "result-generated", "task_id": task_id},
                        "payload": {
                            "output": {
                                "type": "sentence-end",
                                "sentence": {"index": 0, "words": words},
                                "original_text": full,
                            }
                        },
                    },
                    ensure_ascii=False,
                )
            )
            await ws.send(
                json.dumps(
                    {"header": {"event": "task-finished", "task_id": task_id}, "payload": {}}
                )
            )
        except Exception:
            self.closed_without_reuse += 1
            raise
        finally:
            self.active -= 1
            try:
                await ws.close()
            except Exception:
                pass
            self.closed_without_reuse += 1


_DOUBAO_CONNECTION_EVENTS = frozenset(
    {
        EventType.START_CONNECTION,
        EventType.FINISH_CONNECTION,
        EventType.CONNECTION_STARTED,
        EventType.CONNECTION_FAILED,
        EventType.CONNECTION_FINISHED,
    }
)
_DOUBAO_CONNECTION_RESPONSES = frozenset(
    {
        EventType.CONNECTION_STARTED,
        EventType.CONNECTION_FAILED,
        EventType.CONNECTION_FINISHED,
    }
)


def _doubao_sized(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def _doubao_server_frame(
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
    if event not in _DOUBAO_CONNECTION_EVENTS:
        frame.extend(_doubao_sized(session_id.encode()))
    if event in _DOUBAO_CONNECTION_RESPONSES:
        frame.extend(_doubao_sized(connect_id.encode()))
    frame.extend(_doubao_sized(payload))
    return bytes(frame)


def _parse_doubao_client_frame(data: bytes) -> tuple[EventType, str, dict[str, Any]]:
    header_size = (data[0] & 0x0F) * 4
    offset = header_size
    event = EventType(struct.unpack_from(">i", data, offset)[0])
    offset += 4

    def take_sized() -> bytes:
        nonlocal offset
        size = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        value = data[offset : offset + size]
        offset += size
        return value

    session_id = ""
    if event not in _DOUBAO_CONNECTION_EVENTS:
        session_id = take_sized().decode()
    raw_payload = take_sized()
    payload = json.loads(raw_payload) if raw_payload else {}
    return event, session_id, payload


@dataclass
class MockDoubaoServer:
    host: str = "127.0.0.1"
    port: int = 0
    scenario: str = "happy"  # happy|split_pcm|split_pcm_odd|odd_pcm|slow|slow_once|slow_after_first|slow_after_second|slow_after_sixth|empty_ts|scaled_ts|degraded_ts
    connections: int = 0
    sessions: int = 0
    task_requests: list[list[str]] = field(default_factory=list)
    start_session_params: list[dict[str, Any]] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)
    canceled_sessions: list[str] = field(default_factory=list)
    request_headers: list[dict[str, str]] = field(default_factory=list)
    pcm: bytes = b""
    _server: Any = None
    _thread: threading.Thread | None = None
    _loop: asyncio.AbstractEventLoop | None = None

    @property
    def ws_url(self) -> str:
        return f"ws://{_LOOPBACK_CLIENT_HOST}:{self.port}"

    def start(self) -> None:
        ready = threading.Event()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def _start() -> Any:
                return await websockets.serve(self._handler, self.host, self.port)

            self._server = self._loop.run_until_complete(_start())
            self.port = int(self._server.sockets[0].getsockname()[1])
            ready.set()
            self._loop.run_forever()
            self._loop.run_until_complete(self._server.wait_closed())
            self._loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        ready.wait(timeout=5)

    def stop(self) -> None:
        if self._loop is None or self._server is None:
            return

        def _close() -> None:
            self._server.close()

            async def _wait() -> None:
                await self._server.wait_closed()
                assert self._loop is not None
                self._loop.stop()

            self._loop.create_task(_wait())

        self._loop.call_soon_threadsafe(_close)
        if self._thread is not None:
            self._thread.join(timeout=5)

    async def _handler(self, ws: ServerConnection) -> None:
        self.connections += 1
        connection_index = self.connections - 1
        headers = {name.lower(): value for name, value in ws.request.headers.raw_items()}
        self.request_headers.append(headers)
        connect_id = f"mock-doubao-{self.connections}"
        active_session = ""
        active_texts: list[str] | None = None
        try:
            raw = await ws.recv()
            assert isinstance(raw, bytes)
            event, _, _ = _parse_doubao_client_frame(raw)
            assert event == EventType.START_CONNECTION
            await ws.send(
                _doubao_server_frame(
                    EventType.CONNECTION_STARTED,
                    connect_id=connect_id,
                )
            )
            async for raw in ws:
                if not isinstance(raw, bytes):
                    continue
                event, session_id, payload = _parse_doubao_client_frame(raw)
                if event == EventType.START_SESSION:
                    active_session = session_id
                    active_texts = []
                    self.sessions += 1
                    self.task_requests.append(active_texts)
                    req_params = payload.get("req_params", {})
                    self.start_session_params.append(dict(req_params))
                    self.speakers.append(str(req_params.get("speaker") or ""))
                    await ws.send(
                        _doubao_server_frame(
                            EventType.SESSION_STARTED,
                            session_id=session_id,
                        )
                    )
                elif event == EventType.TASK_REQUEST and active_texts is not None:
                    req_params = payload.get("req_params", {})
                    active_texts.append(str(req_params.get("text") or ""))
                elif event == EventType.CANCEL_SESSION:
                    self.canceled_sessions.append(session_id)
                    await ws.send(
                        _doubao_server_frame(
                            EventType.SESSION_CANCELED,
                            session_id=session_id,
                        )
                    )
                elif event == EventType.FINISH_SESSION and active_texts is not None:
                    if (
                        self.scenario == "slow"
                        or (self.scenario == "slow_after_first" and self.sessions > 1)
                        or (self.scenario == "slow_after_second" and self.sessions > 2)
                        or (self.scenario == "slow_after_sixth" and self.sessions > 6)
                    ):
                        continue
                    if self.scenario == "slow_once" and connection_index == 0:
                        await asyncio.sleep(0.3)
                    await self._send_session_result(ws, active_session, active_texts)
                    active_session = ""
                    active_texts = None
                elif event == EventType.FINISH_CONNECTION:
                    await ws.send(
                        _doubao_server_frame(
                            EventType.CONNECTION_FINISHED,
                            connect_id=connect_id,
                        )
                    )
                    return
        except websockets.exceptions.ConnectionClosed:
            return

    async def _send_session_result(
        self,
        ws: ServerConnection,
        session_id: str,
        texts: list[str],
    ) -> None:
        full_text = "".join(texts) or "你好"
        samples = max(480, len(full_text) * 240)
        if self.scenario in {"scaled_ts", "degraded_ts"}:
            samples = max(samples, 24_000)
        pcm = struct.pack(f"<{samples}h", *([1200] * samples))
        self.pcm = pcm
        if self.scenario == "split_pcm":
            midpoint = len(pcm) // 2
            chunks = (pcm[:midpoint], pcm[midpoint:])
        elif self.scenario == "split_pcm_odd":
            chunks = (pcm[:1], pcm[1:3], pcm[3:257], pcm[257:])
        elif self.scenario == "odd_pcm":
            chunks = (pcm + b"\x00",)
        else:
            chunks = (pcm,)
        for chunk in chunks:
            await ws.send(
                _doubao_server_frame(
                    EventType.TTS_RESPONSE,
                    session_id=session_id,
                    payload=chunk,
                    audio=True,
                )
            )
        if self.scenario != "empty_ts":
            duration_s = samples / 24000
            # A realistic long utterance can be linearly normalized when its
            # 200ms drift remains below the relative-error gate.  A 500ms
            # drift at this duration must remain degraded.
            if self.scenario == "scaled_ts":
                subtitle_duration_s = duration_s + 0.2
            elif self.scenario == "degraded_ts":
                subtitle_duration_s = duration_s + 0.5
            else:
                subtitle_duration_s = duration_s
            step = subtitle_duration_s / len(full_text)
            words = [
                {"word": char, "startTime": index * step, "endTime": (index + 1) * step}
                for index, char in enumerate(full_text)
            ]
            await ws.send(
                _doubao_server_frame(
                    EventType.TTS_SUBTITLE,
                    session_id=session_id,
                    payload=json.dumps({"words": words}, ensure_ascii=False).encode(),
                )
            )
        await ws.send(
            _doubao_server_frame(
                EventType.SESSION_FINISHED,
                session_id=session_id,
            )
        )


@dataclass
class MockDeepSeekServer:
    host: str = "127.0.0.1"
    port: int = 0
    scenario: str = "happy"  # happy|thinking|tools|bad_json|timeout|429|500|cancel
    cancelled: bool = False
    last_body: dict[str, Any] | None = None
    requests: int = 0
    _runner: web.AppRunner | None = None
    _site: web.TCPSite | None = None
    _thread: threading.Thread | None = None
    _loop: asyncio.AbstractEventLoop | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self) -> None:
        ready = threading.Event()

        def _run() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def _main() -> None:
                app = web.Application()
                app.router.add_post("/chat/completions", self._chat)
                self._runner = web.AppRunner(app)
                await self._runner.setup()
                self._site = web.TCPSite(self._runner, self.host, self.port)
                await self._site.start()
                # aiohttp TCPSite keeps the bound server on a private attr.
                server = getattr(self._site, "_server", None)
                assert server is not None
                sockets = server.sockets
                self.port = int(sockets[0].getsockname()[1])
                ready.set()

            self._loop.run_until_complete(_main())
            self._loop.run_forever()
            if self._runner is not None:
                self._loop.run_until_complete(self._runner.cleanup())
            self._loop.close()

        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        ready.wait(timeout=5)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        self.requests += 1
        request_index = self.requests - 1
        body = await request.json()
        self.last_body = body
        if "parallel_tool_calls" in body or "max_completion_tokens" in body:
            return web.json_response({"error": "forbidden fields"}, status=400)

        if self.scenario == "429":
            return web.Response(status=429, headers={"Retry-After": "1"})
        if self.scenario == "500" or (self.scenario == "500_once" and request_index == 0):
            return web.Response(status=503, text="unavailable")

        if self.scenario == "timeout" or (self.scenario == "timeout_once" and request_index == 0):
            await asyncio.sleep(5)

        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)

        async def send_data(obj: dict[str, Any]) -> None:
            await resp.write(f"data: {json.dumps(obj, ensure_ascii=False)}\n\n".encode())

        try:
            if self.scenario == "bad_json":
                await resp.write(b"data: {not-json\n\n")
                await resp.write(b"data: [DONE]\n\n")
                return resp

            if self.scenario == "thinking":
                await send_data(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "reasoning_content": "私有推理不应出现在TTS",
                                    "content": "",
                                }
                            }
                        ]
                    }
                )
                await send_data({"choices": [{"delta": {"content": "连接正常"}}]})
            elif self.scenario == "tools":
                await send_data(
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call_1",
                                            "function": {
                                                "name": "search",
                                                "arguments": '{"q":"x"}',
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                )
            else:
                for ch in "连接正常":
                    await send_data({"choices": [{"delta": {"content": ch}}]})
                    if self.scenario == "slow_after_content":
                        await asyncio.sleep(5)
                    await asyncio.sleep(0.01)

            await send_data({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            await resp.write(b"data: [DONE]\n\n")
        except (asyncio.CancelledError, ConnectionResetError):
            self.cancelled = True
            raise
        return resp


def free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
