from __future__ import annotations

import pytest
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.providers.funasr_stt import (
    FunASRConfig,
    FunASRSession,
    FunASRSTT,
    resample_pcm_16le,
)


@pytest.mark.asyncio
async def test_unconnected_session_control_methods_and_ring_bound() -> None:
    session = FunASRSession(
        FunASRConfig(
            api_key="test",
            ws_url="ws://unused",
            sample_rate=1000,
            reconnect_audio_ms=100,
        )
    )
    await session.update_context(({"role": "user", "text": "术语"},))
    await session.finish()
    with pytest.raises(RuntimeError):
        await session.send_pcm(b"\x00\x00")

    session._push_ring(b"a" * 120)
    session._push_ring(b"b" * 120)
    assert session._pcm_ring_bytes <= session._max_ring_bytes
    assert b"".join(session._pcm_ring) == b"b" * 120
    await session.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await session.connect()


def test_session_tracks_sample_watermarks_for_bounded_replay() -> None:
    session = FunASRSession(
        FunASRConfig(
            api_key="test",
            ws_url="ws://unused",
            sample_rate=1000,
            reconnect_audio_ms=100,
        )
    )
    session._last_sent_sample = 1000
    session._last_provider_acked_sample = 700
    session.mark_committed_sample(800)

    assert session.replay_start_sample() == 900
    assert session.last_committed_sample == 800
    with pytest.raises(ValueError):
        session.mark_committed_sample(-1)


def test_resample_pcm_and_stt_does_not_forward_chat_history() -> None:
    pcm = b"\x00\x00" * 160
    assert resample_pcm_16le(pcm, src_rate=16000, dst_rate=16000) is pcm
    downsampled = resample_pcm_16le(pcm, src_rate=16000, dst_rate=8000)
    assert 0 < len(downsampled) < len(pcm)

    plugin = FunASRSTT(FunASRConfig(api_key="test", ws_url="ws://unused"))

    class Item:
        role = "assistant"

        @staticmethod
        def text_content() -> str:
            return "已经听到的内容"

    class Event:
        item = Item()

    plugin._push_conversation_item(Event())
    plugin.push_conversation_item({"role": "invalid", "text": "ignored"})
    session = plugin.create_session()
    assert session._context == ()
    assert plugin.provider == "alibaba_model_studio"
    assert plugin.model == "fun-asr-realtime"


@pytest.mark.asyncio
async def test_funasr_active_connections_aggregate_and_recovery_failure_decrements_once() -> None:
    class FakeWebSocket:
        async def close(self) -> None:
            return None

        async def send(self, _payload: object) -> None:
            return None

    metrics = MetricsRegistry()
    config = FunASRConfig(api_key="test", ws_url="ws://unused")
    first = FunASRSession(config, metrics=metrics)
    second = FunASRSession(config, metrics=metrics)
    first._mark_ws_connected()
    second._mark_ws_connected()
    assert metrics.get("provider_ws_active", {"provider": "asr"}) == 2

    failed_ws = FakeWebSocket()
    first._ws = failed_ws  # type: ignore[assignment]

    async def fail_open():
        raise RuntimeError("provider unavailable")

    first._open_with_retry = fail_open  # type: ignore[method-assign]
    assert not await first._recover(failed_ws)  # type: ignore[arg-type]
    assert metrics.get("provider_ws_active", {"provider": "asr"}) == 1

    await first.aclose()
    await second.aclose()
    assert metrics.get("provider_ws_active", {"provider": "asr"}) == 0


@pytest.mark.asyncio
async def test_funasr_send_lag_uses_websocket_send_elapsed_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent.src.providers import funasr_stt as funasr_module

    class FakeWebSocket:
        async def send(self, _payload: object) -> None:
            return None

    ticks = iter((10.0, 10.025))
    monkeypatch.setattr(funasr_module, "monotonic", lambda: next(ticks))
    metrics = MetricsRegistry()
    session = FunASRSession(
        FunASRConfig(api_key="test", ws_url="ws://unused"),
        metrics=metrics,
    )
    session._ws = FakeWebSocket()  # type: ignore[assignment]
    session._ready.set()

    await session.send_pcm(b"\x00\x00", capture_start_sample=0)

    assert metrics.get("asr_send_lag_ms") == pytest.approx(25.0)
