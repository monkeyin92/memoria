from __future__ import annotations

import asyncio
import logging

import pytest
from livekit.agents import APIConnectionError
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


@pytest.mark.asyncio
async def test_funasr_first_frame_adopts_origin_and_backward_sample_is_rejected() -> None:
    class FakeWebSocket:
        async def send(self, _payload: object) -> None:
            return None

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session._ws = FakeWebSocket()  # type: ignore[assignment]
    session._ready.set()

    await session.send_pcm(b"\x00\x00" * 2, capture_start_sample=320)

    assert session.task_sample_origin == 320
    with pytest.raises(ValueError, match="moved backwards"):
        await session.send_pcm(b"\x00\x00", capture_start_sample=321)


@pytest.mark.asyncio
async def test_funasr_task_rotation_is_idempotent_without_new_audio() -> None:
    class FakeWebSocket:
        async def send(self, _payload: object) -> None:
            return None

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session._ws = FakeWebSocket()  # type: ignore[assignment]
    session.task_id = "task-1"
    session._ready.set()
    session._last_sent_sample = 320
    calls: list[str] = []

    async def finish(*, terminal: bool = True) -> None:
        assert terminal is False
        calls.append("finish")
        session._finishing = True

    async def wait_for_task_finished(*, require_consumed: bool = False) -> None:
        assert require_consumed is True
        calls.append("finished")

    async def start_reused_task() -> None:
        calls.append("start")
        session._finishing = False
        session._ready.set()

    session.finish = finish  # type: ignore[method-assign]
    session.wait_for_task_finished = wait_for_task_finished  # type: ignore[method-assign]
    session._start_reused_task = start_reused_task  # type: ignore[method-assign]

    await session.rotate_task()
    await session.rotate_task()

    assert calls == ["finish", "finished", "start"]
    assert session._last_rotation_sample == 320


@pytest.mark.asyncio
async def test_funasr_wait_for_task_finished_returns_when_boundary_observed() -> None:
    session = FunASRSession(
        FunASRConfig(api_key="test", ws_url="ws://unused", result_timeout_s=30)
    )
    session.task_id = "task-1"
    session._finished_task_id = "task-1"

    await session.wait_for_task_finished()


@pytest.mark.asyncio
async def test_funasr_observed_boundary_waits_for_consumer_when_required() -> None:
    session = FunASRSession(
        FunASRConfig(api_key="test", ws_url="ws://unused", result_timeout_s=1.0)
    )
    session.task_id = "task-1"
    session._finished_task_id = "task-1"
    session._task_finished_received.set()

    waiter = asyncio.create_task(
        session.wait_for_task_finished(require_consumed=True)
    )
    await asyncio.sleep(0)

    assert waiter.done() is False
    session.acknowledge_task_finished("task-1")
    await waiter


@pytest.mark.asyncio
async def test_funasr_wait_for_task_finished_fails_fast_on_task_failed() -> None:
    session = FunASRSession(
        FunASRConfig(api_key="test", ws_url="ws://unused", result_timeout_s=30)
    )
    session.task_id = "task-1"
    session._failed = True
    session._task_failed_event.set()

    started = asyncio.get_running_loop().time()
    with pytest.raises(APIConnectionError, match="task failed before task boundary"):
        await session.wait_for_task_finished()
    assert asyncio.get_running_loop().time() - started < 1.0


@pytest.mark.asyncio
async def test_funasr_wait_for_task_finished_wakes_on_task_failed_during_wait() -> None:
    session = FunASRSession(
        FunASRConfig(api_key="test", ws_url="ws://unused", result_timeout_s=30)
    )
    session.task_id = "task-1"
    waiter = asyncio.create_task(session.wait_for_task_finished())
    await asyncio.sleep(0)
    session._task_failed_event.set()

    started = asyncio.get_running_loop().time()
    with pytest.raises(APIConnectionError, match="task failed before task boundary"):
        await waiter
    assert asyncio.get_running_loop().time() - started < 1.0


@pytest.mark.asyncio
async def test_funasr_finish_preserves_observed_boundary() -> None:
    class FakeWebSocket:
        async def send(self, _payload: object) -> None:
            return None

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session._ws = FakeWebSocket()  # type: ignore[assignment]
    session.task_id = "task-1"
    session._finished_task_id = "task-1"
    session._task_finished_received.set()
    session._ready.set()

    await session.finish(terminal=False)

    assert session._finished_task_id == "task-1"
    assert session._task_finished_received.is_set()
    assert session._finishing is True
    assert session._terminal_finishing is False


@pytest.mark.asyncio
async def test_funasr_rotate_task_reuses_task_when_boundary_already_observed() -> None:
    class FakeWebSocket:
        async def send(self, _payload: object) -> None:
            return None

    session = FunASRSession(
        FunASRConfig(api_key="test", ws_url="ws://unused", result_timeout_s=0.5)
    )
    session._ws = FakeWebSocket()  # type: ignore[assignment]
    session.task_id = "task-1"
    session._finished_task_id = "task-1"
    session._ready.set()
    session._last_sent_sample = 320
    calls: list[str] = []

    async def start_reused_task() -> None:
        calls.append("start")
        session._ready.set()

    session._start_reused_task = start_reused_task  # type: ignore[method-assign]

    await session.rotate_task(require_consumed=False)

    assert calls == ["start"]
    assert session._last_rotation_sample == 320
    assert session._finished_task_id == "task-1"


@pytest.mark.asyncio
async def test_funasr_legacy_rotation_does_not_advance_context_before_boundary_consumed() -> None:
    class FakeWebSocket:
        async def send(self, _payload: object) -> None:
            return None

    session = FunASRSession(
        FunASRConfig(api_key="test", ws_url="ws://unused", result_timeout_s=1.0)
    )
    session._ws = FakeWebSocket()  # type: ignore[assignment]
    session.task_id = "task-1"
    session._finished_task_id = "task-1"
    session._task_finished_received.set()
    session._ready.set()
    session._last_sent_sample = 320
    calls: list[str] = []

    async def start_reused_task() -> None:
        calls.append("start")
        session.task_id = "task-2"
        session._task_epoch += 1
        session._task_sample_origin = session._last_sent_sample
        session._ready.set()

    session._start_reused_task = start_reused_task  # type: ignore[method-assign]

    rotation = asyncio.create_task(session.rotate_task(require_consumed=True))
    await asyncio.sleep(0)

    assert rotation.done() is False
    assert calls == []
    assert session.task_id == "task-1"
    assert session.task_epoch == 0
    assert session.task_sample_origin == 0

    session.acknowledge_task_finished("task-1")
    await rotation

    assert calls == ["start"]
    assert session.task_id == "task-2"
    assert session.task_epoch == 1
    assert session.task_sample_origin == 320


@pytest.mark.asyncio
async def test_funasr_recv_loop_survives_task_finished_without_rotation() -> None:
    class EndingWebSocket:
        def __init__(self, messages: list[object]) -> None:
            self._messages = messages

        def __aiter__(self) -> EndingWebSocket:
            return self

        async def __anext__(self) -> object:
            if not self._messages:
                raise StopAsyncIteration
            return self._messages.pop(0)

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session._ws = EndingWebSocket(  # type: ignore[assignment]
        [{"header": {"event": "task-finished", "task_id": "task-1"}, "payload": {}}]
    )
    session.task_id = "task-1"
    session._ready.set()

    async def no_recover(_failed_ws: object) -> bool:
        return False

    session._recover = no_recover  # type: ignore[method-assign]

    await session._recv_loop()

    assert session._finished_task_id == "task-1"
    assert session._task_finished_received.is_set()
    # The loop must have survived the boundary and only ended on the
    # deliberately failed recovery; the old code returned at task-finished
    # and never reached the failure path.
    assert session.failed is True
    assert session._task_failed_event.is_set()
    queued = []
    while not session.events.empty():
        queued.append(session.events.get_nowait())
    assert any(event.event == "task-failed" for event in queued)


@pytest.mark.asyncio
async def test_funasr_task_failure_records_safe_context_and_logs_all_fences(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingWebSocket:
        def __init__(self) -> None:
            self._messages = [
                {
                    "header": {
                        "event": "task-failed",
                        "task_id": "task-1",
                        "error_code": "InvalidParameter",
                        "error_message": "request failed for 13812345678\n",
                    },
                    "payload": {},
                }
            ]

        def __aiter__(self) -> FailingWebSocket:
            return self

        async def __anext__(self) -> object:
            if not self._messages:
                raise StopAsyncIteration
            return self._messages.pop(0)

        async def close(self) -> None:
            return None

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session._ws = FailingWebSocket()  # type: ignore[assignment]
    session.task_id = "task-1"
    session._task_epoch = 4
    session._segment_epoch = 9
    caplog.set_level(logging.WARNING)

    await session._recv_loop()

    failure = session.last_task_failure
    assert failure is not None
    assert failure.task_id == "task-1"
    assert failure.task_epoch == 4
    assert failure.segment_epoch == 9
    assert failure.error_code == "InvalidParameter"
    assert failure.error_message == "request failed for [手机号]"
    assert "task_id=task-1" in caplog.text
    assert "task_epoch=4" in caplog.text
    assert "segment_epoch=9" in caplog.text
    assert "error_code=InvalidParameter" in caplog.text
    assert "error_message=request failed for [手机号]" in caplog.text
    assert "13812345678" not in caplog.text

    with pytest.raises(APIConnectionError, match="error_code=InvalidParameter"):
        await session.wait_for_task_finished()
