"""SenseVoice offline rescue integration against local mock servers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any, cast

import pytest
from livekit import rtc
from livekit.agents import stt
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.providers.funasr_stt import (
    FunASRConfig,
    FunASRSession,
    FunASRSTT,
)
from services.agent.src.providers.sensevoice import SenseVoiceRescueConfig
from services.agent.src.voice_core.media_protocol import AudioFrame, SessionIdentity
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.agent.tests.integration.mock_servers import (
    MockFunASRServer,
    MockSenseVoiceServer,
)


def _speech_pcm(samples: int = 4000, amplitude: int = 200) -> bytes:
    return bytes((amplitude & 0xFF, (amplitude >> 8) & 0xFF)) * samples


@pytest.fixture
def silent_funasr() -> Iterator[MockFunASRServer]:
    srv = MockFunASRServer(scenario="silent")
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def rescue_server() -> Iterator[MockSenseVoiceServer]:
    srv = MockSenseVoiceServer(scenario="happy")
    srv.start()
    yield srv
    srv.stop()


@pytest.mark.asyncio
async def test_rescue_emits_synthetic_final_for_silent_task(
    silent_funasr: MockFunASRServer,
    rescue_server: MockSenseVoiceServer,
) -> None:
    metrics = MetricsRegistry()
    session = FunASRSession(
        FunASRConfig(
            api_key="test",
            ws_url=silent_funasr.ws_url,
            rescue_config=SenseVoiceRescueConfig(endpoint=rescue_server.url),
        ),
        metrics=metrics,
    )
    await session.connect()
    task_id = session.task_id
    await session.send_pcm(_speech_pcm())
    await session.rotate_task(require_consumed=False)

    events = []
    while not session.events.empty():
        events.append(session.events.get_nowait())
    await session.aclose()

    kinds = [event.event for event in events]
    assert kinds == ["task-started", "task-finished", "result-generated"]
    final = events[-1].sentence
    assert final is not None
    assert final.sentence_end is True
    assert final.text == "兜底识别成功。"
    assert events[-1].task_id == task_id
    assert session.task_id == task_id

    assert rescue_server.requests == 1
    assert rescue_server.received_bytes == 8000
    assert rescue_server.sample_rates == ["16000"]
    assert rescue_server.formats == ["pcm"]

    assert metrics.get("funasr_rescue_total", {"outcome": "rescued"}) == 1.0
    # Segment evidence resets at the boundary: the next empty turn can be
    # rescued again instead of being suppressed by stale state.
    assert session._segment_pcm_samples == 0
    assert session._segment_nonempty_final_seen is False


@pytest.mark.asyncio
async def test_rescue_skipped_when_provider_final_seen(
    rescue_server: MockSenseVoiceServer,
) -> None:
    srv = MockFunASRServer(scenario="happy")
    srv.start()
    try:
        session = FunASRSession(
            FunASRConfig(
                api_key="test",
                ws_url=srv.ws_url,
                rescue_config=SenseVoiceRescueConfig(endpoint=rescue_server.url),
            )
        )
        await session.connect()
        await session.send_pcm(_speech_pcm())
        await session.rotate_task(require_consumed=False)
        await session.aclose()
        assert rescue_server.requests == 0
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_rescue_runs_when_provider_final_does_not_cover_segment(
    silent_funasr: MockFunASRServer,
    rescue_server: MockSenseVoiceServer,
) -> None:
    """An early nonempty FunASR final must not suppress SenseVoice for the rest of the VAD."""

    metrics = MetricsRegistry()
    session = FunASRSession(
        FunASRConfig(
            api_key="test",
            ws_url=silent_funasr.ws_url,
            rescue_config=SenseVoiceRescueConfig(endpoint=rescue_server.url),
        ),
        metrics=metrics,
    )
    await session.connect()
    await session.send_pcm(_speech_pcm())
    # Reproduce the 11:33 weather miss: FunASR closed the sentence after
    # ~80 ms of text while the VAD segment still had ~250 ms of speech.
    session._segment_nonempty_final_seen = True
    session._last_emitted_final_sample = 1_280
    await session.rotate_task(require_consumed=False)

    events = []
    while not session.events.empty():
        events.append(session.events.get_nowait())
    await session.aclose()

    finals = [
        event.sentence.text
        for event in events
        if event.event == "result-generated" and event.sentence is not None
    ]
    assert finals == ["兜底识别成功。"]
    assert rescue_server.requests == 1
    assert metrics.get("funasr_rescue_total", {"outcome": "rescued"}) == 1.0


@pytest.mark.asyncio
async def test_rescue_skipped_without_speech_energy(
    silent_funasr: MockFunASRServer,
    rescue_server: MockSenseVoiceServer,
) -> None:
    session = FunASRSession(
        FunASRConfig(
            api_key="test",
            ws_url=silent_funasr.ws_url,
            rescue_config=SenseVoiceRescueConfig(endpoint=rescue_server.url),
        )
    )
    await session.connect()
    await session.send_pcm(b"\x00\x00" * 2000)
    await session.rotate_task(require_consumed=False)

    events = []
    while not session.events.empty():
        events.append(session.events.get_nowait())
    await session.aclose()

    assert [event.event for event in events] == ["task-started", "task-finished"]
    assert rescue_server.requests == 0


@pytest.mark.asyncio
async def test_rescue_runs_when_peak_abs_is_high_but_rms_is_low(
    silent_funasr: MockFunASRServer,
    rescue_server: MockSenseVoiceServer,
) -> None:
    """Post-DTLN speech can have low RMS while still carrying usable peaks."""

    metrics = MetricsRegistry()
    session = FunASRSession(
        FunASRConfig(
            api_key="test",
            ws_url=silent_funasr.ws_url,
            rescue_config=SenseVoiceRescueConfig(
                endpoint=rescue_server.url,
                min_rms=100,
                min_peak_abs=350,
            ),
        ),
        metrics=metrics,
    )
    await session.connect()
    # Low RMS envelope with a few strong peaks, similar to post-denoise uplink.
    pcm = b"".join(
        b"\x00\x00" * 199 + b"\x90\x01"
        for _ in range(200)
    )
    await session.send_pcm(pcm)
    await session.rotate_task(require_consumed=False)

    events = []
    while not session.events.empty():
        events.append(session.events.get_nowait())
    await session.aclose()

    assert rescue_server.requests == 1
    assert metrics.get("funasr_rescue_total", {"outcome": "rescued"}) == 1.0


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["error", "empty"])
async def test_rescue_failure_or_empty_text_never_raises(
    silent_funasr: MockFunASRServer,
    scenario: str,
) -> None:
    srv = MockSenseVoiceServer(scenario=scenario)
    srv.start()
    try:
        session = FunASRSession(
            FunASRConfig(
                api_key="test",
                ws_url=silent_funasr.ws_url,
                rescue_config=SenseVoiceRescueConfig(endpoint=srv.url),
            )
        )
        await session.connect()
        await session.send_pcm(_speech_pcm())
        await session.rotate_task(require_consumed=False)

        events = []
        while not session.events.empty():
            events.append(session.events.get_nowait())
        await session.aclose()

        assert [event.event for event in events] == ["task-started", "task-finished"]
        assert srv.requests == 1
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_rescue_timeout_never_raises(
    silent_funasr: MockFunASRServer,
) -> None:
    srv = MockSenseVoiceServer(scenario="happy", delay_s=0.5)
    srv.start()
    try:
        session = FunASRSession(
            FunASRConfig(
                api_key="test",
                ws_url=silent_funasr.ws_url,
                rescue_config=SenseVoiceRescueConfig(endpoint=srv.url, timeout_s=0.1),
            )
        )
        await session.connect()
        await session.send_pcm(_speech_pcm())
        await session.rotate_task(require_consumed=False)

        events = []
        while not session.events.empty():
            events.append(session.events.get_nowait())
        await session.aclose()

        assert [event.event for event in events] == ["task-started", "task-finished"]
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_rescue_after_emptyaudio_synthesizes_boundary(
    rescue_server: MockSenseVoiceServer,
) -> None:
    srv = MockFunASRServer(scenario="emptyaudio")
    srv.start()
    try:
        session = FunASRSession(
            FunASRConfig(
                api_key="test",
                ws_url=srv.ws_url,
                rescue_config=SenseVoiceRescueConfig(endpoint=rescue_server.url),
            )
        )
        await session.connect()
        task_id = session.task_id
        await session.send_pcm(_speech_pcm())

        # Drain the startup and the provider's task-failed(EmptyAudio).
        kinds: list[str] = []
        for _ in range(4):
            try:
                event = await asyncio.wait_for(session.events.get(), timeout=2)
            except TimeoutError:
                break
            kinds.append(event.event)
        assert kinds == ["task-started", "task-failed"]
        assert session.failed is True

        await session.rotate_task(require_consumed=False)

        events = []
        while not session.events.empty():
            events.append(session.events.get_nowait())
        assert [event.event for event in events] == ["result-generated", "task-finished"]
        final = events[0].sentence
        assert final is not None
        assert final.sentence_end is True
        assert final.text == "兜底识别成功。"
        assert events[0].task_id == task_id
        assert session._task_boundary_observed() is True
        assert rescue_server.requests == 1

        # The failed provider task is still replaceable on the next PCM frame.
        await session.send_pcm(_speech_pcm())
        assert session.task_id != task_id
        assert session.failed is False
        await session.aclose()
    finally:
        srv.stop()


@pytest.mark.asyncio
async def test_recognize_stream_rescue_final_precedes_end_of_speech(
    silent_funasr: MockFunASRServer,
) -> None:
    # The offline rescue is slower than the post-boundary tail grace; the
    # consumer must keep the boundary window open until the rescue settles.
    srv = MockSenseVoiceServer(scenario="happy", delay_s=0.3)
    srv.start()
    try:
        plugin = FunASRSTT(
            FunASRConfig(
                api_key="test",
                ws_url=silent_funasr.ws_url,
                post_finish_tail_grace_s=0.05,
                rescue_config=SenseVoiceRescueConfig(endpoint=srv.url),
            )
        )
        stream = plugin.stream()

        async def _collect() -> list[stt.SpeechEvent]:
            return [event async for event in stream]

        collector = asyncio.create_task(_collect())
        samples = 4000
        stream.push_frame(
            rtc.AudioFrame(
                data=_speech_pcm(samples),
                sample_rate=16000,
                num_channels=1,
                samples_per_channel=samples,
            )
        )
        plugin.flush_speech_segment()
        for _ in range(200):
            if srv.requests >= 1:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)
        stream.end_input()

        events = await asyncio.wait_for(collector, timeout=10)
        relevant = [
            event.type
            for event in events
            if event.type
            in {
                stt.SpeechEventType.START_OF_SPEECH,
                stt.SpeechEventType.FINAL_TRANSCRIPT,
                stt.SpeechEventType.END_OF_SPEECH,
            }
        ]
        assert relevant == [
            stt.SpeechEventType.START_OF_SPEECH,
            stt.SpeechEventType.FINAL_TRANSCRIPT,
            stt.SpeechEventType.END_OF_SPEECH,
        ]
        finals = [
            event.alternatives[0].text
            for event in events
            if event.type is stt.SpeechEventType.FINAL_TRANSCRIPT and event.alternatives
        ]
        assert finals == ["兜底识别成功。"]
        assert srv.received_bytes == samples * 2
        await plugin.aclose()
    finally:
        srv.stop()
        await plugin.aclose()


@pytest.mark.asyncio
async def test_mid_segment_rescue_emits_final_before_vad_end(
    silent_funasr: MockFunASRServer,
    rescue_server: MockSenseVoiceServer,
) -> None:
    session = FunASRSession(
        FunASRConfig(
            api_key="test",
            ws_url=silent_funasr.ws_url,
            rescue_config=SenseVoiceRescueConfig(endpoint=rescue_server.url),
        )
    )
    await session.connect()
    samples = 16_000 * 5
    chunk = 320
    for start in range(0, samples, chunk):
        await session.send_pcm(
            _speech_pcm(chunk),
            capture_start_sample=start,
        )
    await asyncio.sleep(0.05)
    finals: list[str] = []
    while not session.events.empty():
        event = session.events.get_nowait()
        if event.event == "result-generated" and event.sentence is not None:
            finals.append(event.sentence.text)
    await session.aclose()
    assert finals == ["兜底识别成功。"]
    assert rescue_server.requests == 1


@pytest.mark.asyncio
async def test_media_adapter_finalize_returns_rescue_final(
    silent_funasr: MockFunASRServer,
    rescue_server: MockSenseVoiceServer,
) -> None:
    """The bridge/edge drain path commits the rescue final like any provider final."""

    class NoopLM:
        def stream(self, request: Any) -> AsyncIterator[str]:
            _ = request

            async def tokens() -> AsyncIterator[str]:
                return
                yield ""

            return tokens()

        async def start_delegation(self, *args: Any, **kwargs: Any) -> None:
            return None

        async def accept_output_intent(self, *args: Any, **kwargs: Any) -> bool:
            return False

    adapter = ExistingVoiceProviderAdapter(
        asr_session_factory=cast(
            Any,
            lambda: FunASRSession(
                FunASRConfig(
                    api_key="test",
                    ws_url=silent_funasr.ws_url,
                    rescue_config=SenseVoiceRescueConfig(endpoint=rescue_server.url),
                )
            ),
        ),
        language_model=cast(Any, NoopLM()),
        speech_synthesis=cast(
            Any,
            SimpleNamespace(synthesize=None),
        ),
    )
    identity = SessionIdentity("rescue-edge", stream_epoch=1)
    samples = 4000
    frame = AudioFrame(
        identity=identity,
        sequence=1,
        capture_start_sample=0,
        frame_samples=samples,
        payload=_speech_pcm(samples),
    )
    await adapter.ingest_audio(identity, frame)
    results = await adapter.finalize_speech_segment(identity)
    await adapter.close(identity)

    finals = [result for result in results if result.is_final]
    assert finals and finals[-1].text == "兜底识别成功。"
    assert finals[-1].capture_end_sample == samples
    assert rescue_server.requests == 1


def test_segment_pcm_truncation_keeps_tail() -> None:
    session = FunASRSession(
        FunASRConfig(
            api_key="test",
            ws_url="ws://unused",
            rescue_config=SenseVoiceRescueConfig(
                endpoint="http://127.0.0.1:9/asr",
                max_audio_s=0.5,
            ),
        )
    )
    for index in range(3):
        session._remember_segment_pcm(
            b"\x01\x00" * 4000,
            start_sample=index * 4000,
            end_sample=(index + 1) * 4000,
        )
    assert session._segment_pcm_samples == 8000
    assert session._segment_pcm_start_sample == 4000
    assert session._segment_pcm_end_sample == 12000


def test_funasr_config_from_env_parses_rescue() -> None:
    cfg = FunASRConfig.from_env(
        {
            "DASHSCOPE_API_KEY": "k",
            "DASHSCOPE_WS_URL": "wss://example.invalid",
            "SENSEVOICE_URL": "http://sensevoice:8000/asr",
            "SENSEVOICE_TIMEOUT_S": "1.5",
            "SENSEVOICE_MIN_RMS": "150",
            "SENSEVOICE_MIN_PEAK_ABS": "400",
        }
    )
    assert cfg.rescue_config is not None
    assert cfg.rescue_config.endpoint == "http://sensevoice:8000/asr"
    assert cfg.rescue_config.timeout_s == 1.5
    assert cfg.rescue_config.min_rms == 150
    assert cfg.rescue_config.min_peak_abs == 400

    disabled = FunASRConfig.from_env({"DASHSCOPE_API_KEY": "k"})
    assert disabled.rescue_config is None
