"""StepFun primary/backup behavior against a local mock WebSocket server."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
import websockets
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.providers import tts_factory
from services.agent.src.providers.provider_errors import APIConnectOptions
from services.agent.src.providers.stepfun_tts import (
    STEPFUN_INSTRUCTION_GUARD,
    STEPFUN_TTS_MODEL,
    STEPFUN_TTS_VOICE,
    StepFunTTS,
    StepFunTTSConfig,
    strip_bracket_instructions,
)
from services.agent.src.providers.tts_failover import (
    StepFunFailoverConfig,
    StepFunPrimaryTTS,
    TTSFailoverBreaker,
)
from services.agent.src.providers.tts_stream import AudioFrame, SynthesizedAudio
from services.agent.src.runtime_provenance import (
    DuplexRuntimeProvenanceMixin,
    GenerationVoiceSnapshot,
)
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close

RATE = 24000
PCM = b"\x01\x00" * 2400  # 100 ms of 16-bit mono


def _event(event_type: str, data: dict[str, Any]) -> str:
    return json.dumps({"type": event_type, "data": data}, ensure_ascii=False)


def _audio_event(pcm: bytes) -> str:
    return _event(
        "tts.response.audio.delta",
        {
            "session_id": "mock-session",
            "status": "unfinished",
            "audio": base64.b64encode(pcm).decode(),
        },
    )


def _done_event(pcm: bytes = b"") -> str:
    return _event(
        "tts.response.audio.done",
        {"session_id": "mock-session", "audio": base64.b64encode(pcm).decode()},
    )


class MockStepFunServer:
    """One mock server; handlers receive the connection and the server."""

    def __init__(self, handler: Callable[[MockStepFunServer, Any], Any]) -> None:
        self._handler = handler
        self._server: Any = None
        self.port = 0
        self.connections = 0
        self.paths: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.client_events: list[dict[str, Any]] = []
        self.create_count = 0
        self.texts: list[str] = []
        self.closed = False

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    async def __aenter__(self) -> MockStepFunServer:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.closed = True
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, ws: Any) -> None:
        self.connections += 1
        self.paths.append(ws.request.path)
        self.headers.append(dict(ws.request.headers))
        await self._handler(self, ws)

    async def next_event(self, ws: Any) -> dict[str, Any]:
        raw = await ws.recv()
        event = json.loads(raw)
        self.client_events.append(event)
        if event.get("type") == "tts.create":
            self.create_count += 1
        if event.get("type") == "tts.text.delta":
            self.texts.append(str(event["data"].get("text", "")))
        return event


async def _connection(server: MockStepFunServer, ws: Any, session_id: str = "mock-session") -> None:
    await ws.send(_event("tts.connection.done", {"session_id": session_id}))


def _config(server: MockStepFunServer, **overrides: Any) -> StepFunTTSConfig:
    values: dict[str, Any] = {
        "ws_url": f"{server.url}/v1/realtime/audio",
        "api_key": "test-stepfun-secret",
        "pool_size": 1,
        "connect_timeout_s": 1.0,
        "first_audio_timeout_s": 1.0,
        "total_timeout_s": 2.0,
    }
    values.update(overrides)
    return StepFunTTSConfig(**values)


def _frame(pcm: bytes = b"\x02\x00" * 240) -> SynthesizedAudio:
    return SynthesizedAudio(
        frame=AudioFrame(
            data=pcm,
            sample_rate=RATE,
            num_channels=1,
            samples_per_channel=len(pcm) // 2,
        ),
        request_id="backup",
        segment_id="backup",
        is_final=True,
    )


class FakeBackupStream:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.ended = asyncio.Event()
        self.produced = False
        self.closed = False

    def push_text(self, text: str) -> None:
        self.texts.append(text)

    def end_input(self) -> None:
        self.ended.set()

    async def __anext__(self) -> SynthesizedAudio:
        if self.produced:
            raise StopAsyncIteration
        await self.ended.wait()
        self.produced = True
        return _frame()

    def __aiter__(self) -> FakeBackupStream:
        return self

    async def aclose(self) -> None:
        self.closed = True
        self.ended.set()


class FakeBackupTTS:
    provider = "volcengine_doubao"
    current_model = "seed-tts-2.0"
    current_voice = "backup-voice"
    current_voice_kind = "designed"
    current_voice_profile_id = "backup-profile"
    model = "seed-tts-2.0"

    def __init__(self) -> None:
        self.streams: list[FakeBackupStream] = []
        self.fence: GenerationFence | None = None

    def bind_fence(self, fence: GenerationFence) -> None:
        self.fence = fence

    def apply_speech_plan(self, **_kwargs: Any) -> None:
        return None

    def apply_voice_profile(self, **_kwargs: Any) -> None:
        return None

    def use_baseline_voice(self) -> None:
        return None

    def stream(self, *, conn_options: APIConnectOptions | None = None) -> FakeBackupStream:
        del conn_options
        stream = FakeBackupStream()
        self.streams.append(stream)
        return stream

    async def aclose(self) -> None:
        return None

    @property
    def received(self) -> list[str]:
        return [text for stream in self.streams for text in stream.texts]


class FakePrimary:
    provider = "stepfun"
    model = STEPFUN_TTS_MODEL
    sample_rate = RATE
    current_model = STEPFUN_TTS_MODEL
    current_voice = STEPFUN_TTS_VOICE
    current_voice_kind = "designed"
    current_voice_profile_id = None
    current_instruction = STEPFUN_INSTRUCTION_GUARD

    def __init__(self, stream_factory: Callable[[], Any]) -> None:
        self._stream_factory = stream_factory
        self.fence: GenerationFence | None = None
        self.traces: list[tuple[str, str, dict[str, Any] | None]] = []

    def bind_fence(self, fence: GenerationFence) -> None:
        self.fence = fence

    def set_trace_callback(
        self, callback: Callable[[str, str, dict[str, Any] | None], None]
    ) -> None:
        self._trace_callback = callback

    def trace(self, name: str, status: str = "ok", detail: dict[str, Any] | None = None) -> None:
        self.traces.append((name, status, detail))

    def apply_speech_plan(self, **_kwargs: Any) -> None:
        return None

    def use_baseline_voice(self) -> None:
        return None

    def stream(self, *, conn_options: APIConnectOptions | None = None) -> Any:
        del conn_options
        return self._stream_factory()

    async def aclose(self) -> None:
        return None

    @property
    def pool(self) -> Any:
        return _FakePool()


class _FakePool:
    async def warm(self, size: int | None = None) -> None:
        del size

    async def discard_active_connection(self, fence: GenerationFence) -> None:
        del fence

    @property
    def available_approx(self) -> int:
        return 0


def test_bracket_cleaning_removes_full_and_half_width_instructions() -> None:
    assert strip_bracket_instructions("（笑）你好（小声）呀") == "你好呀"
    assert strip_bracket_instructions("(laugh)你好") == "你好"
    assert strip_bracket_instructions("未闭合（笑") == "未闭合"
    assert strip_bracket_instructions("没有括号") == "没有括号"


@pytest.mark.asyncio
async def test_normal_synthesis_uses_documented_events_and_24k_pcm() -> None:
    async def handler(server: MockStepFunServer, ws: Any) -> None:
        await _connection(server, ws)
        while True:
            event = await server.next_event(ws)
            if event["type"] == "tts.create":
                await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
            elif event["type"] == "tts.text.delta":
                await ws.send(_audio_event(PCM))
            elif event["type"] == "tts.text.done":
                await ws.send(_done_event(PCM))
                return

    async with MockStepFunServer(handler) as server:
        tts = StepFunTTS(_config(server))
        stream = tts.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("（笑）你好，今天天气很好。")
        stream.end_input()
        events = [event async for event in stream]
        await tts.aclose()

    pcm = b"".join(event.frame.data for event in events)
    assert pcm == PCM
    assert all(
        event.frame.sample_rate == 24000 and event.frame.num_channels == 1 for event in events
    )
    assert server.paths[0] == f"/v1/realtime/audio?model={STEPFUN_TTS_MODEL}"
    assert (
        server.headers[0].get("Authorization") == "Bearer test-stepfun-secret"
        or server.headers[0].get("authorization") == "Bearer test-stepfun-secret"
    )
    create = next(event["data"] for event in server.client_events if event["type"] == "tts.create")
    assert create["voice_id"] == STEPFUN_TTS_VOICE == "yuanqishaonv"
    assert create["response_format"] == "pcm"
    assert create["sample_rate"] == 24000
    # A deployment with no STEPFUN_TTS_INSTRUCTION sends no guard wording.
    assert "instruction" not in create
    assert server.texts == ["你好，今天天气很好。"]
    assert any(event["type"] == "tts.text.done" for event in server.client_events)


@pytest.mark.asyncio
async def test_first_packet_failure_replays_the_whole_sentence_on_doubao() -> None:
    async def handler(server: MockStepFunServer, ws: Any) -> None:
        await _connection(server, ws)
        while True:
            event = await server.next_event(ws)
            if event["type"] == "tts.create":
                await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
            elif event["type"] == "tts.text.delta":
                await ws.send(
                    _event(
                        "tts.response.error",
                        {"session_id": "mock-session", "code": "503", "message": "overloaded"},
                    )
                )
                return

    backup = FakeBackupTTS()
    fragments: list[tuple[int, str, str, str, bool]] = []
    async with MockStepFunServer(handler) as server:
        wrapper = StepFunPrimaryTTS(
            primary=StepFunTTS(_config(server)),
            backup=backup,
            config=StepFunFailoverConfig(failures=3, open_s=30),
            breaker=TTSFailoverBreaker(failures=3, open_s=30),
        )
        fence = GenerationFence("s", 1, 1, 0, 0)
        wrapper.bind_fence(fence)
        wrapper.set_tts_sentence_callback(
            lambda fence, index, engine, model, voice, switched: fragments.append(
                (index, engine, model, voice, switched)
            )
        )
        stream = wrapper.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("（笑）第一句。")
        stream.push_text("第二句。")
        stream.end_input()
        events = [event async for event in stream]
        await wrapper.aclose()

    assert [event.frame.data for event in events] == [b"\x02\x00" * 240, b"\x02\x00" * 240]
    assert backup.received == ["第一句。", "第二句。"]
    # A failed unit uses the backup, but the next unit retries StepFun.
    assert wrapper.degraded is False
    assert wrapper.breaker.consecutive_failures == 2
    assert fragments == [
        (0, "volcengine_doubao", "seed-tts-2.0", "backup-voice", True),
        (1, "volcengine_doubao", "seed-tts-2.0", "backup-voice", True),
    ]


@pytest.mark.asyncio
async def test_failure_after_a_sentence_audio_repeats_the_unit_on_doubao() -> None:
    async def handler(server: MockStepFunServer, ws: Any) -> None:
        await _connection(server, ws)
        while True:
            event = await server.next_event(ws)
            if event["type"] == "tts.create":
                await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
            elif event["type"] == "tts.text.delta":
                text = str(event["data"].get("text", ""))
                if text == "第一句。":
                    await ws.send(_audio_event(PCM))
                    await ws.send(_done_event())
                else:
                    # The second sentence starts playing, then the stream dies.
                    await ws.send(_audio_event(PCM))
                    await ws.send(
                        _event(
                            "tts.response.error",
                            {"session_id": "mock-session", "code": "503", "message": "overloaded"},
                        )
                    )
                return

    backup = FakeBackupTTS()
    fragments: list[tuple[int, str, bool]] = []
    async with MockStepFunServer(handler) as server:
        wrapper = StepFunPrimaryTTS(
            primary=StepFunTTS(_config(server)),
            backup=backup,
            breaker=TTSFailoverBreaker(failures=3, open_s=30),
        )
        fence = GenerationFence("s", 1, 1, 0, 0)
        wrapper.bind_fence(fence)
        wrapper.set_tts_sentence_callback(
            lambda _fence, index, engine, _model, _voice, switched: fragments.append(
                (index, engine, switched)
            )
        )
        stream = wrapper.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("第一句。")
        stream.push_text("第二句。")
        stream.push_text("第三句。")
        stream.end_input()
        events = [event async for event in stream]

        second = wrapper.stream(conn_options=APIConnectOptions(max_retry=0))
        second.push_text("第四句。")
        second.end_input()
        [event async for event in second]
        await wrapper.aclose()

    joined = b"".join(event.frame.data for event in events)
    assert joined.startswith(PCM)
    assert joined.endswith(b"\x02\x00" * 240)
    assert len(joined) > len(PCM) + 240 * 2
    assert server.texts == ["第一句。", "第二句。", "第三句。", "第四句。"]
    # A unit that dies after its first packet is re-spoken whole on Doubao: a
    # truncated sentence is worse than a repeated one, especially for the approved
    # fixed safety script, which the media adapter now pushes as a single unit.
    assert backup.received == ["第二句。", "第三句。", "第四句。"]
    assert fragments == [
        (0, "stepfun", False),
        (1, "stepfun", False),
        (2, "stepfun", False),
        (0, "stepfun", False),
    ]
    assert wrapper.degraded is True


@pytest.mark.asyncio
async def test_failure_before_audio_retries_that_sentence_on_doubao() -> None:
    async def handler(server: MockStepFunServer, ws: Any) -> None:
        await _connection(server, ws)
        while True:
            event = await server.next_event(ws)
            if event["type"] == "tts.create":
                await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
            elif event["type"] == "tts.text.delta":
                text = str(event["data"].get("text", ""))
                if text == "第一句。":
                    await ws.send(_audio_event(PCM))
                    await ws.send(_done_event())
                else:
                    await ws.send(
                        _event(
                            "tts.response.error",
                            {"session_id": "mock-session", "code": "503", "message": "overloaded"},
                        )
                    )
                return

    backup = FakeBackupTTS()
    async with MockStepFunServer(handler) as server:
        wrapper = StepFunPrimaryTTS(
            primary=StepFunTTS(_config(server)),
            backup=backup,
            breaker=TTSFailoverBreaker(failures=3, open_s=30),
        )
        wrapper.bind_fence(GenerationFence("s", 1, 1, 0, 0))
        stream = wrapper.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("第一句。")
        stream.push_text("第二句。")
        stream.push_text("第三句。")
        stream.end_input()
        [event async for event in stream]
        await wrapper.aclose()

    assert backup.received == ["第二句。", "第三句。"]
    # The third sentence retries StepFun before it too falls to the backup.
    assert server.texts == ["第一句。", "第二句。", "第三句。"]
    assert wrapper.degraded is False


@pytest.mark.asyncio
async def test_odd_pcm_length_falls_back() -> None:
    async def handler(server: MockStepFunServer, ws: Any) -> None:
        await _connection(server, ws)
        while True:
            event = await server.next_event(ws)
            if event["type"] == "tts.create":
                await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
            elif event["type"] == "tts.text.delta":
                await ws.send(_audio_event(b"\x01"))
                return

    backup = FakeBackupTTS()
    async with MockStepFunServer(handler) as server:
        wrapper = StepFunPrimaryTTS(
            primary=StepFunTTS(_config(server)),
            backup=backup,
            breaker=TTSFailoverBreaker(failures=3, open_s=30),
        )
        wrapper.bind_fence(GenerationFence("s", 1, 1, 0, 0))
        stream = wrapper.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("第一句。")
        stream.end_input()
        [event async for event in stream]
        await wrapper.aclose()

    assert backup.received == ["第一句。"]
    assert wrapper.degraded is False


@pytest.mark.asyncio
async def test_handshake_failure_falls_back_without_leaking_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def handler(_server: MockStepFunServer, ws: Any) -> None:
        await ws.close()

    backup = FakeBackupTTS()
    async with MockStepFunServer(handler) as server:
        primary = StepFunTTS(_config(server))
        wrapper = StepFunPrimaryTTS(
            primary=primary,
            backup=backup,
            breaker=TTSFailoverBreaker(failures=3, open_s=30),
        )
        wrapper.bind_fence(GenerationFence("s", 1, 1, 0, 0))
        stream = wrapper.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("第一句。")
        stream.end_input()
        events = [event async for event in stream]
        await wrapper.aclose()

    assert len(events) == 1
    assert backup.received == ["第一句。"]
    assert "test-stepfun-secret" not in repr(primary)
    assert "test-stepfun-secret" not in repr(wrapper)
    assert "test-stepfun-secret" not in "\n".join(record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_auth_error_opens_a_long_breaker_and_subsequent_sessions_use_doubao() -> None:
    async def handler(server: MockStepFunServer, ws: Any) -> None:
        await _connection(server, ws)
        while True:
            event = await server.next_event(ws)
            if event["type"] == "tts.create":
                await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
            elif event["type"] == "tts.text.delta":
                await ws.send(
                    _event(
                        "tts.response.error",
                        {
                            "session_id": "mock-session",
                            "code": "402",
                            "message": "insufficient balance",
                        },
                    )
                )
                return

    now = [1000.0]
    backup = FakeBackupTTS()
    breaker = TTSFailoverBreaker(
        failures=3,
        open_s=30,
        auth_open_s=3600,
        clock=lambda: now[0],
    )
    async with MockStepFunServer(handler) as server:
        wrapper = StepFunPrimaryTTS(
            primary=StepFunTTS(_config(server)),
            backup=backup,
            breaker=breaker,
        )
        wrapper.bind_fence(GenerationFence("s", 1, 1, 0, 0))
        first = wrapper.stream(conn_options=APIConnectOptions(max_retry=0))
        first.push_text("第一句。")
        first.end_input()
        [event async for event in first]

        assert breaker.state == "open"
        now[0] += 30
        assert breaker.allow() is False
        now[0] += 3600
        assert breaker.allow() is True
        breaker.record_failure(auth=True)
        now[0] += 31
        assert breaker.allow() is False

        # A fresh session-level wrapper with the same process breaker goes
        # straight to Doubao while the circuit is open.
        second = StepFunPrimaryTTS(
            primary=StepFunTTS(_config(server)),
            backup=backup,
            breaker=breaker,
        )
        second.bind_fence(GenerationFence("s2", 1, 1, 0, 0))
        stream = second.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("第二句。")
        stream.end_input()
        [event async for event in stream]
        await wrapper.aclose()
        await second.aclose()

    assert backup.received == ["第一句。", "第二句。"]
    assert server.create_count == 1
    assert second.degraded is True


@pytest.mark.asyncio
async def test_text_deltas_are_split_at_the_documented_1000_character_limit() -> None:
    async def handler(server: MockStepFunServer, ws: Any) -> None:
        await _connection(server, ws)
        while True:
            event = await server.next_event(ws)
            if event["type"] == "tts.create":
                await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
            elif event["type"] == "tts.text.delta":
                await ws.send(_audio_event(PCM))
            elif event["type"] == "tts.text.done":
                await ws.send(_done_event())
                return

    async with MockStepFunServer(handler) as server:
        tts = StepFunTTS(_config(server))
        stream = tts.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("字" * 2500)
        stream.end_input()
        [event async for event in stream]
        await tts.aclose()

    assert len(server.texts) == 3
    assert [len(text) for text in server.texts] == [1000, 1000, 500]
    assert "".join(server.texts) == "字" * 2500


@pytest.mark.asyncio
async def test_first_audio_timeout_falls_back_to_doubao() -> None:
    async def handler(server: MockStepFunServer, ws: Any) -> None:
        await _connection(server, ws)
        while True:
            event = await server.next_event(ws)
            if event["type"] == "tts.create":
                await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
            elif event["type"] == "tts.text.delta":
                await asyncio.sleep(0.2)
                return
            elif event["type"] == "tts.text.done":
                return

    backup = FakeBackupTTS()
    async with MockStepFunServer(handler) as server:
        wrapper = StepFunPrimaryTTS(
            primary=StepFunTTS(_config(server, first_audio_timeout_s=0.05)),
            backup=backup,
            breaker=TTSFailoverBreaker(failures=3, open_s=30),
        )
        wrapper.bind_fence(GenerationFence("s", 1, 1, 0, 0))
        stream = wrapper.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("第一句。")
        stream.end_input()
        [event async for event in stream]
        await wrapper.aclose()

    assert backup.received == ["第一句。"]


@pytest.mark.asyncio
async def test_moderation_rejection_switches_that_sentence_to_doubao() -> None:
    async def handler(server: MockStepFunServer, ws: Any) -> None:
        await _connection(server, ws)
        while True:
            event = await server.next_event(ws)
            if event["type"] == "tts.create":
                await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
            elif event["type"] == "tts.text.delta":
                await ws.send(
                    _event(
                        "tts.response.error",
                        {
                            "session_id": "mock-session",
                            "code": "451",
                            "message": "content moderation rejected",
                        },
                    )
                )
                return

    backup = FakeBackupTTS()
    async with MockStepFunServer(handler) as server:
        wrapper = StepFunPrimaryTTS(
            primary=StepFunTTS(_config(server)),
            backup=backup,
            breaker=TTSFailoverBreaker(failures=3, open_s=30),
        )
        wrapper.bind_fence(GenerationFence("s", 1, 1, 0, 0))
        stream = wrapper.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("危机脚本。")
        stream.end_input()
        events = [event async for event in stream]
        await wrapper.aclose()

    assert backup.received == ["危机脚本。"]
    assert [event.frame.data for event in events] == [b"\x02\x00" * 240]
    assert wrapper.degraded is False


@pytest.mark.asyncio
async def test_connection_close_before_audio_switches_to_doubao() -> None:
    async def handler(server: MockStepFunServer, ws: Any) -> None:
        await _connection(server, ws)
        while True:
            event = await server.next_event(ws)
            if event["type"] == "tts.create":
                await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
            elif event["type"] == "tts.text.delta":
                await ws.close(code=1011, reason="upstream reset")
                return

    backup = FakeBackupTTS()
    async with MockStepFunServer(handler) as server:
        wrapper = StepFunPrimaryTTS(
            primary=StepFunTTS(_config(server)),
            backup=backup,
            breaker=TTSFailoverBreaker(failures=3, open_s=30),
        )
        wrapper.bind_fence(GenerationFence("s", 1, 1, 0, 0))
        stream = wrapper.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("危机脚本。")
        stream.end_input()
        events = [event async for event in stream]
        await wrapper.aclose()

    assert backup.received == ["危机脚本。"]
    assert [event.frame.data for event in events] == [b"\x02\x00" * 240]


@pytest.mark.asyncio
async def test_first_audio_timeout_switches_within_the_five_second_budget() -> None:
    async def handler(server: MockStepFunServer, ws: Any) -> None:
        await _connection(server, ws)
        while True:
            event = await server.next_event(ws)
            if event["type"] == "tts.create":
                await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
            elif event["type"] == "tts.text.delta":
                # Never send audio: the first-packet budget must fail over.
                await asyncio.sleep(0.2)
                return
            elif event["type"] == "tts.text.done":
                return

    backup = FakeBackupTTS()
    async with MockStepFunServer(handler) as server:
        wrapper = StepFunPrimaryTTS(
            primary=StepFunTTS(_config(server, first_audio_timeout_s=0.05)),
            backup=backup,
            breaker=TTSFailoverBreaker(failures=3, open_s=30),
        )
        wrapper.bind_fence(GenerationFence("s", 1, 1, 0, 0))
        stream = wrapper.stream(conn_options=APIConnectOptions(max_retry=0))
        started = time.monotonic()
        stream.push_text("危机脚本。")
        stream.end_input()
        events = [event async for event in stream]
        elapsed = time.monotonic() - started
        await wrapper.aclose()

    assert events
    assert [event.frame.data for event in events] == [b"\x02\x00" * 240]
    assert backup.received == ["危机脚本。"]
    assert elapsed < 5.0


@pytest.mark.asyncio
async def test_aclose_closes_the_websocket_for_a_barge_in() -> None:
    created = asyncio.Event()
    disconnected = asyncio.Event()

    async def handler(server: MockStepFunServer, ws: Any) -> None:
        try:
            await _connection(server, ws)
            while True:
                event = await server.next_event(ws)
                if event["type"] == "tts.create":
                    await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
                    created.set()
        finally:
            disconnected.set()

    async with MockStepFunServer(handler) as server:
        tts = StepFunTTS(_config(server))
        stream = tts.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("第一句。")
        await asyncio.wait_for(created.wait(), timeout=2)
        await stream.aclose()
        await asyncio.wait_for(disconnected.wait(), timeout=2)
        await tts.aclose()


@pytest.mark.parametrize(
    ("sender_error", "expected_error"),
    [
        (ValueError("sender bug"), ValueError),
        (ConnectionClosedOK(Close(1000, "stop"), None), None),
    ],
)
@pytest.mark.asyncio
async def test_sender_cleanup_only_hides_a_clean_socket_close(
    sender_error: Exception,
    expected_error: type[Exception] | None,
) -> None:
    sender_failed = asyncio.Event()

    class _Socket:
        def __init__(self) -> None:
            self._created = False

        async def send(self, raw: str) -> None:
            event = json.loads(raw)
            if event["type"] == "tts.text.delta":
                sender_failed.set()
                raise sender_error

        async def recv(self) -> str:
            if not self._created:
                self._created = True
                return _event("tts.response.created", {"session_id": "mock-session"})
            await asyncio.wait_for(sender_failed.wait(), timeout=1)
            return _done_event(PCM)

    class _Pool:
        def __init__(self, socket: _Socket) -> None:
            self._socket = socket
            self.discarded: list[str] = []

        async def acquire(self) -> SimpleNamespace:
            return SimpleNamespace(session_id="mock-session", ws=self._socket)

        async def discard(self, connection: SimpleNamespace, *, reason: str) -> None:
            del connection
            self.discarded.append(reason)

        async def aclose(self) -> None:
            return None

    tts = StepFunTTS(
        StepFunTTSConfig(pool_size=1),
        pool=cast(Any, _Pool(_Socket())),
    )
    stream = tts.stream(conn_options=APIConnectOptions(max_retry=0))
    stream.push_text("你好。")
    stream.end_input()
    if expected_error is None:
        events = [event async for event in stream]
        assert events
    else:
        with pytest.raises(expected_error, match="sender bug"):
            _ = [event async for event in stream]
    await tts.aclose()


def test_breaker_opens_half_opens_and_keeps_auth_failures_long() -> None:
    now = [100.0]
    breaker = TTSFailoverBreaker(
        failures=2,
        open_s=10,
        auth_open_s=1000,
        clock=lambda: now[0],
    )
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state == "closed"
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state == "open"
    assert breaker.allow() is False
    now[0] += 10
    assert breaker.allow() is True
    assert breaker.state == "half_open"
    assert breaker.allow() is False
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow() is True

    breaker.record_failure(auth=True)
    assert breaker.state == "open"
    now[0] += 11
    assert breaker.allow() is False
    now[0] += 1000
    assert breaker.allow() is True


def test_bound_engine_follows_a_shared_open_breaker_without_consuming_a_probe() -> None:
    now = [100.0]
    breaker = TTSFailoverBreaker(failures=1, open_s=10, clock=lambda: now[0])
    wrapper = StepFunPrimaryTTS(
        primary=FakePrimary(lambda: None),
        backup=FakeBackupTTS(),
        breaker=breaker,
    )

    assert wrapper.bound_engine() is wrapper.primary
    breaker.record_failure()
    assert breaker.state == "open"
    assert wrapper.degraded is False
    assert wrapper.bound_engine() is wrapper.backup

    now[0] += 10
    assert wrapper.bound_engine() is wrapper.primary
    # The provenance query must not claim the half-open probe; selection still may.
    assert breaker.allow() is True


def test_factory_is_doubao_by_default_and_requires_both_switches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from services.agent.src.providers import doubao_tts, stepfun_tts, tts_factory

    sentinel = object()
    monkeypatch.setattr(doubao_tts.DoubaoTTS, "from_env", classmethod(lambda cls: sentinel))

    settings = SimpleNamespace(tts_provider="stepfun_primary", stepfun_tts_enabled=False)
    assert tts_factory.build_tts(settings) is sentinel
    assert tts_factory.tts_provider_label(settings) == "volcengine_doubao"

    # The switch is off by default even when the provider name is new.
    monkeypatch.delenv("STEPFUN_TTS_ENABLED", raising=False)
    default_off = SimpleNamespace(tts_provider="stepfun_primary")
    assert tts_factory.build_tts(default_off) is sentinel

    monkeypatch.setattr(
        tts_factory,
        "PROCESS_TTS_BREAKER",
        TTSFailoverBreaker(),
        raising=False,
    )
    monkeypatch.setenv("STEPFUN_TTS_MOCK_WS_URL", "ws://127.0.0.1:1")
    monkeypatch.setenv("DOUBAO_TTS_MOCK_WS_URL", "ws://127.0.0.1:1")
    enabled = SimpleNamespace(
        tts_provider="stepfun_primary",
        stepfun_tts_enabled=True,
        stepfun_tts_model=STEPFUN_TTS_MODEL,
        stepfun_tts_voice=STEPFUN_TTS_VOICE,
        doubao_tts_fallback_speaker="zh_female_fallback_test",
    )
    built = tts_factory.build_tts(enabled)
    assert isinstance(built, StepFunPrimaryTTS)
    assert isinstance(built.primary, stepfun_tts.StepFunTTS)
    assert isinstance(built.backup, doubao_tts.DoubaoTTS)
    assert tts_factory.tts_provider_label(enabled) == "stepfun"

    assert (
        tts_factory.build_tts(SimpleNamespace(tts_provider="doubao", stepfun_tts_enabled=True))
        is sentinel
    )


def test_env_defaults_use_stepaudio_3_and_yuanqishaonv_and_hide_the_key() -> None:
    config = StepFunTTSConfig.from_env(
        {
            "STEPFUN_API_KEY": "hidden-secret",
            "STEPFUN_TTS_MOCK_WS_URL": "ws://127.0.0.1:1",
        }
    )
    assert config.model == STEPFUN_TTS_MODEL == "stepaudio-3-tts"
    assert config.voice == STEPFUN_TTS_VOICE == "yuanqishaonv"
    # The delivery guard is deployment wording, so code carries no default text.
    assert config.instruction == ""
    assert STEPFUN_INSTRUCTION_GUARD == ""
    assert "hidden-secret" not in repr(config)


def test_env_instruction_guard_is_appended_after_a_plan_instruction() -> None:
    guard = "用平和的语气朗读。"
    config = StepFunTTSConfig.from_env(
        {
            "STEPFUN_API_KEY": "key",
            "STEPFUN_TTS_MOCK_WS_URL": "ws://127.0.0.1:1",
            "STEPFUN_TTS_INSTRUCTION": guard,
        }
    )
    assert config.instruction == guard
    assert len(config.instruction) <= 500
    from services.agent.src.providers import stepfun_tts

    original = stepfun_tts.STEPFUN_INSTRUCTION_GUARD
    stepfun_tts.STEPFUN_INSTRUCTION_GUARD = guard
    try:
        assert stepfun_tts._effective_instruction("") == guard
        assert stepfun_tts._effective_instruction("高兴一点") == f"高兴一点。{guard}"
    finally:
        stepfun_tts.STEPFUN_INSTRUCTION_GUARD = original


def test_personal_voice_forces_the_doubao_backup() -> None:
    backup = FakeBackupTTS()
    wrapper = StepFunPrimaryTTS(
        primary=FakePrimary(lambda: None),
        backup=backup,
        breaker=TTSFailoverBreaker(failures=3, open_s=30),
    )
    wrapper.apply_voice_profile(
        model="seed-icl-2.0",
        voice="personal-speaker",
        profile_id="personal-profile",
        provider="volcengine_doubao",
        voice_kind="personal",
        resource_id="seed-icl-2.0",
    )
    assert wrapper.degraded is True
    assert wrapper.degrade_reason == "personal_voice"


class _ProvenanceRuntime(DuplexRuntimeProvenanceMixin):
    def __init__(self, fence: GenerationFence) -> None:
        from services.agent.src.generation_records import GenerationRecords

        self._fence = fence
        self.session_id = fence.session_id
        self._generation_records = GenerationRecords()

    @property
    def fence(self) -> GenerationFence:
        return self._fence


def test_sentence_provenance_is_written_into_the_generation_record() -> None:
    fence = GenerationFence("s", 1, 1, 0, 0)
    runtime = _ProvenanceRuntime(fence)
    assert runtime.bind_response_provenance(fence, {"fence": {"session_id": "s"}}) is True
    assert runtime.record_tts_sentence_provenance(
        fence,
        index=0,
        engine="stepfun",
        model=STEPFUN_TTS_MODEL,
        voice=STEPFUN_TTS_VOICE,
        switched=False,
    )
    assert runtime.record_tts_sentence_provenance(
        fence,
        index=1,
        engine="volcengine_doubao",
        model="seed-tts-2.0",
        voice="backup-voice",
        switched=True,
    )
    provenance = runtime.response_provenance_for(fence)
    assert provenance is not None
    assert "tts_sentences" not in provenance
    assert provenance["tts_provider"] == "volcengine_doubao"
    assert provenance["tts_model"] == "seed-tts-2.0"
    stored = runtime._generation_records.response_provenance_for(fence)
    assert stored is not None
    assert [item["engine"] for item in stored["tts_sentences"]] == [
        "stepfun",
        "volcengine_doubao",
    ]


def test_a_stepfun_male_sentence_keeps_its_model_after_the_voice_snapshot() -> None:
    fence = GenerationFence("s", 3, 1, 0, 0)
    runtime = _ProvenanceRuntime(fence)
    assert runtime.bind_response_provenance(
        fence,
        {
            "fence": {"session_id": "s"},
            "tts_provider": "stepfun",
            "tts_model": "stepaudio-2.5-tts",
        },
    )
    runtime._generation_records.bind_voice_snapshot(
        fence,
        GenerationVoiceSnapshot(
            profile_id="warm_companion",
            resource_id="seed-tts-2.0",
            speaker_sha256="a" * 64,
            voice_kind="designed",
        ),
    )
    assert runtime.record_tts_sentence_provenance(
        fence,
        index=0,
        engine="stepfun",
        model="stepaudio-2.5-tts",
        voice="wenrounansheng",
        switched=False,
    )

    provenance = runtime.response_provenance_for(fence)

    assert provenance is not None
    assert provenance["tts_provider"] == "stepfun"
    assert provenance["tts_model"] == "stepaudio-2.5-tts"
    assert "tts_sentences" not in provenance
    stored = runtime._generation_records.response_provenance_for(fence)
    assert stored is not None
    assert stored["tts_sentences"][0]["voice"] == "wenrounansheng"
    assert stored["tts_sentences"][0]["model"] == "stepaudio-2.5-tts"


def test_a_stepfun_male_plan_without_sentences_keeps_stepaudio_2_5() -> None:
    fence = GenerationFence("s", 4, 1, 0, 0)
    runtime = _ProvenanceRuntime(fence)
    assert runtime.bind_response_provenance(
        fence,
        {
            "fence": {"session_id": "s"},
            "tts_provider": "stepfun",
            "tts_model": "stepaudio-2.5-tts",
        },
    )
    runtime._generation_records.bind_voice_snapshot(
        fence,
        GenerationVoiceSnapshot(
            profile_id="warm_companion",
            resource_id="seed-tts-2.0",
            speaker_sha256="a" * 64,
            voice_kind="designed",
        ),
    )

    provenance = runtime.response_provenance_for(fence)

    assert provenance is not None
    assert provenance["tts_provider"] == "stepfun"
    assert provenance["tts_model"] == "stepaudio-2.5-tts"
    assert provenance["actual_voice_resource_id"] == "seed-tts-2.0"


def test_a_stepfun_turn_reports_stepfun_without_the_sentence_list() -> None:
    fence = GenerationFence("s", 2, 1, 0, 0)
    runtime = _ProvenanceRuntime(fence)
    assert runtime.bind_response_provenance(
        fence,
        {
            "fence": {"session_id": "s"},
            "tts_provider": "volcengine_doubao",
            "tts_model": "seed-tts-2.0",
        },
    )
    assert runtime.record_tts_sentence_provenance(
        fence,
        index=0,
        engine="stepfun",
        model=STEPFUN_TTS_MODEL,
        voice=STEPFUN_TTS_VOICE,
        switched=False,
    )

    provenance = runtime.response_provenance_for(fence)

    assert provenance is not None
    assert provenance["tts_provider"] == "stepfun"
    assert provenance["tts_model"] == STEPFUN_TTS_MODEL
    assert "tts_sentences" not in provenance


def test_doubao_backup_defaults_to_the_product_voice_without_style_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOUBAO_TTS_MOCK_WS_URL", "ws://127.0.0.1:1")
    monkeypatch.setenv("DOUBAO_TTS_API_KEY", "test")
    monkeypatch.setenv("DOUBAO_TTS_STYLE_CONTROL_ENABLED", "true")
    settings = SimpleNamespace(
        tts_provider="stepfun_primary",
        stepfun_tts_enabled=True,
        doubao_tts_fallback_speaker="",
        doubao_tts_resource_id="seed-tts-2.0",
    )

    backup = tts_factory.build_doubao_backup(settings)

    assert backup.current_voice == "zh_female_kailangjiejie_uranus_bigtts"
    assert backup.current_instruction is None
    assert backup.current_pitch == 0
    assert backup._config.style_control_enabled is False


def test_main_backup_rejects_a_clone_voice_before_construction() -> None:
    settings = SimpleNamespace(
        tts_provider="stepfun_primary",
        stepfun_tts_enabled=True,
        doubao_tts_fallback_speaker="S_personal_synth_ready",
        doubao_tts_resource_id="seed-tts-2.0",
    )

    with pytest.raises(ValueError, match="clone/ICL"):
        tts_factory.build_doubao_backup(settings)


def test_stepfun_voice_profile_keeps_the_configured_backup_for_a_clone() -> None:
    class RecordingBackup(FakeBackupTTS):
        def __init__(self) -> None:
            super().__init__()
            self.profiles: list[dict[str, Any]] = []

        def apply_voice_profile(self, **kwargs: Any) -> None:
            self.profiles.append(kwargs)

    backup = RecordingBackup()
    wrapper = StepFunPrimaryTTS(
        primary=FakePrimary(lambda: None),
        backup=backup,
        breaker=TTSFailoverBreaker(failures=3, open_s=30),
    )

    wrapper.apply_voice_profile(
        model="seed-icl-2.0",
        voice="S_personal_synth_ready",
        profile_id="personal-profile",
        provider="volcengine_doubao",
        voice_kind="personal",
        resource_id="seed-icl-2.0",
    )

    assert wrapper.degraded is True
    assert backup.profiles == []


@pytest.mark.asyncio
async def test_stepfun_speech_plan_sends_no_emotion_or_voice_label() -> None:
    async def handler(server: MockStepFunServer, ws: Any) -> None:
        await _connection(server, ws)
        while True:
            event = await server.next_event(ws)
            if event["type"] == "tts.create":
                await ws.send(_event("tts.response.created", {"session_id": "mock-session"}))
            elif event["type"] == "tts.text.delta":
                await ws.send(_audio_event(PCM))
            elif event["type"] == "tts.text.done":
                await ws.send(_done_event(PCM))
                return

    async with MockStepFunServer(handler) as server:
        tts = StepFunTTS(_config(server))
        tts.apply_speech_plan(emotion="happy", rate=1.0, instruction="")
        stream = tts.stream(conn_options=APIConnectOptions(max_retry=0))
        stream.push_text("我在呢。")
        stream.end_input()
        _ = [event async for event in stream]
        await tts.aclose()

    create = next(event["data"] for event in server.client_events if event["type"] == "tts.create")
    assert "emotion" not in create
    assert "voice_label" not in create
    assert "style_control" not in create
    assert create["voice_id"] == STEPFUN_TTS_VOICE


def test_env_example_matches_the_runtime_tts_defaults() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    example = (root / ".env.example").read_text(encoding="utf-8")
    production = (root / "infra" / "memoria.env.production.example").read_text(encoding="utf-8")

    for text in (example, production):
        assert "STEPFUN_TTS_MODEL=stepaudio-3-tts" in text
        assert "STEPFUN_TTS_VOICE=yuanqishaonv" in text
        assert "DOUBAO_TTS_FALLBACK_SPEAKER=zh_female_kailangjiejie_uranus_bigtts" in text


def test_main_backup_rejects_a_male_preset_before_construction() -> None:
    settings = SimpleNamespace(
        tts_provider="stepfun_primary",
        stepfun_tts_enabled=True,
        doubao_tts_fallback_speaker="zh_male_test_voice_uranus_bigtts",
        doubao_tts_resource_id="seed-tts-2.0",
    )

    with pytest.raises(ValueError, match="male preset"):
        tts_factory.build_doubao_backup(settings)
