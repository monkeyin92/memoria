from __future__ import annotations

import asyncio
import collections
import logging

import pytest
from livekit.agents import APIConnectionError, stt
from livekit.agents.utils import aio
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.orchestration.stable_prefix import StablePrefixTracker
from services.agent.src.providers import funasr_stt
from services.agent.src.providers.funasr_protocol import FunASRSentence, FunASRServerEvent
from services.agent.src.providers.funasr_stt import (
    _WS_TRACE_MAX_PER_WINDOW,
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
    assert session.task_audio_start_sample == 0
    assert session.task_audio_end_sample == 1
    assert session.task_audio_send_count == 1


@pytest.mark.asyncio
async def test_funasr_first_pcm_replaces_failed_provider_task_with_bounded_replay() -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[object] = []
            self.closed = False

        async def send(self, payload: object) -> None:
            self.sent.append(payload)

        async def close(self) -> None:
            self.closed = True

    old_websocket = FakeWebSocket()
    replacement = FakeWebSocket()
    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session._ws = old_websocket  # type: ignore[assignment]
    session.task_id = "task-idle"
    session._task_epoch = 1
    session._task_sample_origin = 320
    session._last_sent_sample = 324
    preroll = b"\x02\x00" * 4
    session._push_ring(preroll, start_sample=320)
    session._record_task_audio_send(pcm=preroll, start_sample=320, end_sample=324)
    session._failed = True
    session._replaceable_provider_failure = True
    session._task_failed_event.set()
    session.events.put_nowait(
        funasr_stt.FunASRServerEvent(
            event="task-failed",
            task_id="task-idle",
            error_code="CLIENT_ERROR",
            error_message="request timeout after 23 seconds",
        )
    )

    async def open_replacement() -> tuple[FakeWebSocket, str, tuple[object, ...]]:
        return replacement, "task-replacement", ()

    async def receive_until_closed() -> None:
        await session._idle_terminal_event.wait()

    session._open_with_retry = open_replacement  # type: ignore[method-assign]
    session._recv_loop = receive_until_closed  # type: ignore[method-assign]

    await session.send_pcm(b"\x01\x00" * 4, capture_start_sample=324)

    assert old_websocket.closed is True
    assert replacement.sent == [preroll, b"\x01\x00" * 4]
    assert session.task_id == "task-replacement"
    assert session.task_epoch == 2
    assert session.task_sample_origin == 320
    assert session.last_sent_sample == 328
    assert session.task_audio_send_count == 2
    assert session.failed is False
    assert session._replaceable_provider_failure is False
    assert session._task_failed_event.is_set() is False
    assert session.events.empty()
    await session.aclose()


@pytest.mark.asyncio
async def test_funasr_empty_audio_boundary_replaces_without_replaying_discarded_pcm() -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[object] = []
            self.closed = False

        async def send(self, payload: object) -> None:
            self.sent.append(payload)

        async def close(self) -> None:
            self.closed = True

    old_websocket = FakeWebSocket()
    replacement = FakeWebSocket()
    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session._ws = old_websocket  # type: ignore[assignment]
    session.task_id = "task-empty"
    session._task_epoch = 1
    discarded = b"\x00\x00" * 4
    session._last_sent_sample = 4
    session._push_ring(discarded, start_sample=0)
    session._record_task_audio_send(pcm=discarded, start_sample=0, end_sample=4)
    session._failed = True
    session._replaceable_provider_failure = True
    session._task_failed_event.set()
    session._record_task_failure(
        task_id="task-empty",
        error_code="EmptyAudio",
        error_message="No effective audio received",
    )

    async def open_replacement() -> tuple[FakeWebSocket, str, tuple[object, ...]]:
        return replacement, "task-replacement", ()

    async def receive_until_closed() -> None:
        await session._idle_terminal_event.wait()

    session._open_with_retry = open_replacement  # type: ignore[method-assign]
    session._recv_loop = receive_until_closed  # type: ignore[method-assign]

    await session.rotate_task(require_consumed=True)
    assert session.rotation_pending is True

    next_turn = b"\x02\x00" * 4
    await session.send_pcm(next_turn, capture_start_sample=4)

    assert old_websocket.closed is True
    assert replacement.sent == [next_turn]
    assert session.task_id == "task-replacement"
    assert session.task_sample_origin == 4
    assert session.last_sent_sample == 8
    assert session.failed is False
    assert session._task_failed_event.is_set() is False
    await session.aclose()


@pytest.mark.asyncio
async def test_funasr_does_not_replace_internal_failed_task_fence() -> None:
    class FakeWebSocket:
        async def send(self, _payload: object) -> None:
            return None

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session._ws = FakeWebSocket()  # type: ignore[assignment]
    session.task_id = "task-with-audio"
    session._task_epoch = 1
    session._failed = True
    session._task_failed_event.set()

    with pytest.raises(APIConnectionError, match="FunASR cannot send PCM"):
        await session.send_pcm(b"\x01\x00", capture_start_sample=320)

    assert session.task_id == "task-with-audio"
    assert session.last_sent_sample == 0


@pytest.mark.asyncio
async def test_funasr_failed_send_without_replay_does_not_claim_provider_pcm() -> None:
    class FailingWebSocket:
        async def send(self, _payload: object) -> None:
            raise RuntimeError("send failed")

        async def close(self) -> None:
            return None

    class RecoveredWebSocket:
        def __init__(self) -> None:
            self.sent: list[object] = []

        async def send(self, payload: object) -> None:
            self.sent.append(payload)

        async def close(self) -> None:
            return None

    recovered = RecoveredWebSocket()
    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session._ws = FailingWebSocket()  # type: ignore[assignment]
    session._ready.set()

    async def open_recovered() -> tuple[RecoveredWebSocket, str, tuple[object, ...]]:
        return recovered, "task-recovered", ()

    session._open_with_retry = open_recovered  # type: ignore[method-assign]

    # A replayed frame is deliberately not in the bounded ring. Recovery has
    # no bytes to replay, so the failed send must not become provider evidence.
    await session.send_pcm(
        b"\x01\x00" * 4,
        replayed=True,
        capture_start_sample=100,
    )

    assert recovered.sent == []
    assert session.task_audio_start_sample is None
    assert session.task_audio_end_sample is None
    assert session.task_audio_send_count == 0


@pytest.mark.asyncio
async def test_funasr_replay_send_failure_clears_provider_pcm_evidence() -> None:
    class FailingWebSocket:
        async def send(self, _payload: object) -> None:
            raise RuntimeError("send failed")

        async def close(self) -> None:
            return None

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session._task_sample_origin = 0
    session._last_sent_sample = 4
    session._push_ring(b"\x01\x00" * 4)
    session._ws = FailingWebSocket()  # type: ignore[assignment]
    session._ready.set()

    async def open_recovered() -> tuple[FailingWebSocket, str, tuple[object, ...]]:
        return FailingWebSocket(), "task-recovered", ()

    session._open_with_retry = open_recovered  # type: ignore[method-assign]

    with pytest.raises(APIConnectionError, match="reconnect failed"):
        await session.send_pcm(b"\x02\x00", capture_start_sample=4)

    assert session.task_audio_start_sample is None
    assert session.task_audio_end_sample is None
    assert session.task_audio_send_count == 0


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
    assert session.task_audio_start_sample == 320
    assert session.task_audio_end_sample == 322
    assert session.task_audio_send_count == 1
    with pytest.raises(ValueError, match="moved backwards"):
        await session.send_pcm(b"\x00\x00", capture_start_sample=321)


@pytest.mark.asyncio
async def test_funasr_send_pcm_fails_closed_if_boundary_arrives_during_send() -> None:
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    class RacingWebSocket:
        def __init__(self) -> None:
            self.closed = False
            self.sent: list[object] = []

        async def send(self, payload: object) -> None:
            self.sent.append(payload)
            if payload == b"\x01\x00":
                send_started.set()
                await release_send.wait()
                session._finished_task_id = session.task_id
                session._ready.clear()

        async def close(self) -> None:
            self.closed = True

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    websocket = RacingWebSocket()
    session._ws = websocket  # type: ignore[assignment]
    session.task_id = "task-1"
    session._task_epoch = 1
    session._ready.set()

    sending = asyncio.create_task(
        session.send_pcm(b"\x01\x00", capture_start_sample=0)
    )
    await asyncio.wait_for(send_started.wait(), timeout=1)
    release_send.set()

    with pytest.raises(APIConnectionError, match="task boundary race"):
        await sending

    assert session.failed is True
    assert session._ready.is_set() is False
    assert session.last_sent_sample == 0
    assert session.task_audio_send_count == 0
    assert session.task_pcm_sample_count == 0
    assert session._pcm_ring_bytes == 0
    assert websocket.closed is True


@pytest.mark.asyncio
async def test_funasr_send_pcm_fails_closed_if_boundary_arrives_during_ready_wait() -> None:
    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[object] = []
            self.closed = False

        async def send(self, payload: object) -> None:
            self.sent.append(payload)

        async def close(self) -> None:
            self.closed = True

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    websocket = FakeWebSocket()
    session._ws = websocket  # type: ignore[assignment]
    session.task_id = "task-1"
    session._task_epoch = 1
    session._ready.set()

    async def observe_boundary(_operation: str) -> None:
        session._finished_task_id = session.task_id
        session._ready.clear()

    session._wait_until_ready = observe_boundary  # type: ignore[method-assign]

    with pytest.raises(APIConnectionError, match="task boundary race"):
        await session.send_pcm(b"\x01\x00", capture_start_sample=0)

    assert websocket.sent == []
    assert session.failed is True
    assert session.last_sent_sample == 0
    assert session._pcm_ring_bytes == 0


@pytest.mark.asyncio
async def test_funasr_lazy_task_start_failure_cannot_be_revived_by_late_task_started() -> None:
    class SilentWebSocket:
        def __init__(self) -> None:
            self.sent: list[object] = []
            self.closed = False
            self.messages: list[object] = []

        def __aiter__(self) -> SilentWebSocket:
            return self

        async def __anext__(self) -> object:
            if not self.messages:
                raise StopAsyncIteration
            return self.messages.pop(0)

        async def send(self, payload: object) -> None:
            self.sent.append(payload)

        async def close(self) -> None:
            self.closed = True

    session = FunASRSession(
        FunASRConfig(
            api_key="test",
            ws_url="ws://unused",
            connect_timeout_s=0.01,
        )
    )
    websocket = SilentWebSocket()
    session._ws = websocket  # type: ignore[assignment]
    session.task_id = "task-1"
    session._task_epoch = 1
    session._finished_task_id = "task-1"
    session._rotation_pending = True
    session._last_sent_sample = 320
    session._ready.clear()

    with pytest.raises(APIConnectionError, match="reused task failed to start"):
        await session.send_pcm(b"\x02\x00", capture_start_sample=320)

    assert session.failed is True
    assert session._ready.is_set() is False
    assert session._ws is None
    assert session.last_sent_sample == 320
    assert session._pcm_ring_bytes == 0

    # A task-started packet from the failed handoff must not clear the failure
    # fence or make the session ready again.
    websocket.messages.append(
        {
            "header": {"event": "task-started", "task_id": "task-2"},
            "payload": {},
        }
    )
    session._ws = websocket  # type: ignore[assignment]
    session.task_id = "task-2"
    session._task_epoch = 2
    session._task_failed_event.set()
    await session._recv_loop()

    assert session.failed is True
    assert session._ready.is_set() is False


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

    async def start_reused_task(*, next_origin: int | None = None) -> None:
        assert next_origin == 320
        calls.append("start")
        session._finishing = False
        session._rotation_pending = False
        session._ready.set()

    session.finish = finish  # type: ignore[method-assign]
    session.wait_for_task_finished = wait_for_task_finished  # type: ignore[method-assign]
    session._start_reused_task = start_reused_task  # type: ignore[method-assign]

    await session.rotate_task()
    await session.rotate_task()

    assert calls == ["finish", "finished"]
    assert session._last_rotation_sample == 320
    assert session.rotation_pending is True

    await session.send_pcm(b"\x00\x00", capture_start_sample=320)

    assert calls == ["finish", "finished", "start"]


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
async def test_funasr_rotate_task_arms_lazy_reuse_when_boundary_already_observed() -> None:
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

    async def start_reused_task(*, next_origin: int | None = None) -> None:
        assert next_origin == 320
        calls.append("start")
        session.task_id = "task-2"
        session._task_epoch += 1
        session._rotation_pending = False
        session._ready.set()

    session._start_reused_task = start_reused_task  # type: ignore[method-assign]

    await session.rotate_task(require_consumed=False)

    assert calls == []
    assert session._last_rotation_sample == 320
    assert session._finished_task_id == "task-1"
    assert session.rotation_pending is True

    await session.send_pcm(b"\x00\x00", capture_start_sample=320)

    assert calls == ["start"]
    assert session.task_id == "task-2"


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

    async def start_reused_task(*, next_origin: int | None = None) -> None:
        assert next_origin == 320
        calls.append("start")
        session.task_id = "task-2"
        session._task_epoch += 1
        session._task_sample_origin = next_origin
        session._rotation_pending = False
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

    assert calls == []
    assert session.task_id == "task-1"
    assert session.task_epoch == 0
    assert session.task_sample_origin == 0
    assert session.rotation_pending is True

    await session.send_pcm(b"\x00\x00", capture_start_sample=320)

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
@pytest.mark.parametrize(
    "event_order",
    (
        ("result-generated", "task-finished"),
        ("task-finished", "result-generated"),
    ),
)
async def test_funasr_recv_loop_preserves_final_on_both_sides_of_task_finished(
    event_order: tuple[str, str],
) -> None:
    def message(event: str) -> dict[str, object]:
        if event == "task-finished":
            return {
                "header": {"event": event, "task_id": "task-1"},
                "payload": {},
            }
        return {
            "header": {"event": event, "task_id": "task-1"},
            "payload": {
                "output": {
                    "sentence": {
                        "sentence_id": 7,
                        "text": "尾包完整",
                        "begin_time": 0,
                        "end_time": 20,
                        "sentence_end": True,
                        "heartbeat": False,
                        "words": [],
                    }
                }
            },
        }

    class EndingWebSocket:
        def __init__(self) -> None:
            self.messages = [message(event) for event in event_order]

        def __aiter__(self) -> EndingWebSocket:
            return self

        async def __anext__(self) -> object:
            if self.messages:
                return self.messages.pop(0)
            session._closed = True
            raise StopAsyncIteration

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session._ws = EndingWebSocket()  # type: ignore[assignment]
    session.task_id = "task-1"
    session._task_epoch = 3
    session._segment_epoch = 5
    session._task_sample_origin = 320
    session._task_audio_end_sample = 640
    session._last_sent_sample = 640
    session._ready.set()

    await session._recv_loop()

    queued = []
    while not session.events.empty():
        queued.append(session.events.get_nowait())
    assert [event.event for event in queued] == list(event_order)
    final = next(event for event in queued if event.event == "result-generated")
    context = session.task_event_context(final.task_id)
    assert context is not None
    assert (context.task_epoch, context.segment_epoch, context.sample_origin) == (3, 5, 320)
    assert context.boundary_observed is True
    assert session.last_emitted_final_sample == 640


@pytest.mark.asyncio
async def test_funasr_recv_events_waits_for_late_final_after_prior_final() -> None:
    config = FunASRConfig(
        api_key="test",
        ws_url="ws://unused",
        post_finish_tail_grace_s=0.01,
    )
    # Construct the stream without starting RecognizeStream's background
    # network task; this test drives only the provider event consumer.
    stream = object.__new__(funasr_stt.FunASRRecognizeStream)
    stream._config = config
    stream._stt_instance = FunASRSTT(config)
    stream._event_ch = aio.Chan()
    stream._prefix_tracker = StablePrefixTracker()
    stream._speaking = False
    stream._final_sentence_ids = set()
    stream._sentence_revisions = {}
    stream._provider_task_id = ""
    stream._provider_task_epoch = 0
    stream._asr_results = collections.deque(maxlen=64)
    stream._stream_epoch = 1
    stream._last_emitted_final_sample = 0
    session = FunASRSession(config)
    session.task_id = "task-1"
    session._task_epoch = 1
    session._segment_epoch = 1
    session._task_sample_origin = 0
    session._task_audio_end_sample = 640
    session._last_sent_sample = 640
    session._rotation_pending = True
    session._idle_terminal_event.set()

    def final(sentence_id: int, text: str) -> FunASRServerEvent:
        return FunASRServerEvent(
            event="result-generated",
            task_id="task-1",
            sentence=FunASRSentence(
                sentence_id=sentence_id,
                text=text,
                begin_ms=0,
                end_ms=sentence_id * 20,
                sentence_end=True,
                heartbeat=False,
                words=(),
            ),
        )

    # The second final models the provider tail observed after task-finished.
    session.events.put_nowait(final(1, "今天"))
    session.events.put_nowait(FunASRServerEvent(event="task-finished", task_id="task-1"))
    session.events.put_nowait(final(2, "今天星期几"))

    await stream._recv_events(session)
    events = [
        await asyncio.wait_for(stream._event_ch.recv(), timeout=0.5)
        for _ in range(4)
    ]

    assert [event.type for event in events] == [
        stt.SpeechEventType.START_OF_SPEECH,
        stt.SpeechEventType.FINAL_TRANSCRIPT,
        stt.SpeechEventType.FINAL_TRANSCRIPT,
        stt.SpeechEventType.END_OF_SPEECH,
    ]
    assert [event.alternatives[0].text for event in events[1:3]] == ["今天", "今天星期几"]


@pytest.mark.asyncio
async def test_funasr_recv_events_treats_terminal_empty_audio_as_clean_no_speech() -> None:
    config = FunASRConfig(api_key="test", ws_url="ws://unused")
    stream = object.__new__(funasr_stt.FunASRRecognizeStream)
    stream._config = config
    stream._stt_instance = FunASRSTT(config)
    stream._event_ch = aio.Chan()
    stream._prefix_tracker = StablePrefixTracker()
    stream._speaking = False
    stream._final_sentence_ids = set()
    stream._sentence_revisions = {}
    stream._provider_task_id = ""
    stream._provider_task_epoch = 0
    stream._asr_results = collections.deque(maxlen=64)
    stream._stream_epoch = 1
    stream._last_emitted_final_sample = 0
    session = FunASRSession(config)
    session.task_id = "task-empty"
    session._finishing = True
    session._terminal_finishing = True
    session.events.put_nowait(
        FunASRServerEvent(
            event="task-failed",
            task_id="task-empty",
            error_code="EmptyAudio",
            error_message="No effective audio received",
        )
    )

    await asyncio.wait_for(stream._recv_events(session), timeout=0.5)

    assert stream._speaking is False
    assert stream._event_ch.empty()


@pytest.mark.asyncio
async def test_funasr_recv_loop_keeps_old_final_after_new_task_starts() -> None:
    old_final = {
        "header": {"event": "result-generated", "task_id": "task-1"},
        "payload": {
            "output": {
                "sentence": {
                    "sentence_id": 8,
                    "text": "旧段尾包",
                    "begin_time": 0,
                    "end_time": 20,
                    "sentence_end": True,
                    "heartbeat": False,
                    "words": [],
                }
            }
        },
    }

    class EndingWebSocket:
        def __init__(self) -> None:
            self.messages = [old_final]

        def __aiter__(self) -> EndingWebSocket:
            return self

        async def __anext__(self) -> object:
            if self.messages:
                return self.messages.pop(0)
            session._closed = True
            raise StopAsyncIteration

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session.task_id = "task-1"
    session._task_epoch = 1
    session._segment_epoch = 1
    session._task_sample_origin = 0
    session._task_audio_end_sample = 320
    session._finished_task_id = "task-1"
    session._remember_current_task_event_context()
    session.task_id = "task-2"
    session._task_epoch = 2
    session._segment_epoch = 2
    session._task_sample_origin = 320
    session._task_audio_end_sample = 640
    session._finished_task_id = None
    session._remember_current_task_event_context()
    session._ws = EndingWebSocket()  # type: ignore[assignment]
    session._ready.set()

    await session._recv_loop()

    event = session.events.get_nowait()
    assert event.task_id == "task-1"
    context = session.task_event_context(event.task_id)
    assert context is not None
    assert (context.task_epoch, context.segment_epoch, context.sample_origin) == (1, 1, 0)
    assert session.last_emitted_final_sample == 320


@pytest.mark.asyncio
async def test_funasr_recv_loop_ignores_old_failure_after_new_task_starts() -> None:
    class EndingWebSocket:
        def __init__(self) -> None:
            self.messages = [
                {
                    "header": {
                        "event": "task-failed",
                        "task_id": "task-1",
                        "error_code": "CLIENT_ERROR",
                        "error_message": "late timeout",
                    },
                    "payload": {},
                }
            ]

        def __aiter__(self) -> EndingWebSocket:
            return self

        async def __anext__(self) -> object:
            if self.messages:
                return self.messages.pop(0)
            session._closed = True
            raise StopAsyncIteration

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session.task_id = "task-1"
    session._task_epoch = 1
    session._finished_task_id = "task-1"
    session._remember_current_task_event_context()
    session.task_id = "task-2"
    session._task_epoch = 2
    session._segment_epoch = 2
    session._finished_task_id = None
    session._remember_current_task_event_context()
    session._ws = EndingWebSocket()  # type: ignore[assignment]
    session._ready.set()

    await session._recv_loop()

    assert session.failed is False
    assert session.last_task_failure is None
    assert session._task_failed_event.is_set() is False
    assert session._ready.is_set() is True
    assert session.events.empty()


@pytest.mark.asyncio
async def test_funasr_empty_audio_failure_is_replaceable_without_tripping_breaker() -> None:
    class EmptyAudioWebSocket:
        def __init__(self) -> None:
            self.messages = [
                {
                    "header": {
                        "event": "task-failed",
                        "task_id": "task-empty",
                        "error_code": "EmptyAudio",
                        "error_message": "No effective audio received",
                    },
                    "payload": {},
                }
            ]

        def __aiter__(self) -> EmptyAudioWebSocket:
            return self

        async def __anext__(self) -> object:
            if not self.messages:
                raise StopAsyncIteration
            return self.messages.pop(0)

        async def close(self) -> None:
            return None

    session = FunASRSession(FunASRConfig(api_key="test", ws_url="ws://unused"))
    session._ws = EmptyAudioWebSocket()  # type: ignore[assignment]
    session.task_id = "task-empty"
    session._task_epoch = 2

    await session._recv_loop()

    assert session.failed is True
    assert session._replaceable_provider_failure is True
    assert session._task_failed_event.is_set() is True
    assert session._breaker.consecutive_failures == 0
    failure = session.last_task_failure
    assert failure is not None
    assert failure.error_code == "EmptyAudio"


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


def test_ws_trace_disabled_emits_nothing(caplog: pytest.LogCaptureFixture) -> None:
    session = FunASRSession(
        FunASRConfig(api_key="test", ws_url="ws://unused", ws_trace=False)
    )
    caplog.set_level(logging.INFO, logger="services.agent.src.providers.funasr_stt")

    session._trace_ws("tx run-task task_id=t1 origin=connect")

    assert "funasr_ws_trace" not in caplog.text


def test_ws_trace_rate_limits_and_flushes_suppression_counter(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = FunASRSession(
        FunASRConfig(api_key="test", ws_url="ws://unused", ws_trace=True)
    )
    caplog.set_level(logging.INFO, logger="services.agent.src.providers.funasr_stt")
    now = [100.0]
    monkeypatch.setattr(funasr_stt, "monotonic", lambda: now[0])

    for index in range(_WS_TRACE_MAX_PER_WINDOW + 5):
        session._trace_ws(f"msg-{index}")

    traced = [
        record.message
        for record in caplog.records
        if record.message.startswith("funasr_ws_trace ")
    ]
    assert len(traced) == _WS_TRACE_MAX_PER_WINDOW
    assert traced[0] == "funasr_ws_trace msg-0"
    assert traced[-1] == f"funasr_ws_trace msg-{_WS_TRACE_MAX_PER_WINDOW - 1}"
    assert session._trace_suppressed_total == 5

    caplog.clear()
    now[0] += 1.0
    session._trace_ws(f"msg-{_WS_TRACE_MAX_PER_WINDOW + 5}")

    flushed = [
        record.message
        for record in caplog.records
        if record.message.startswith("funasr_ws_trace")
    ]
    assert flushed[0] == "funasr_ws_trace suppressed_total=5 task_id=unknown"
    assert flushed[-1] == f"funasr_ws_trace msg-{_WS_TRACE_MAX_PER_WINDOW + 5}"
    assert session._trace_suppressed_total == 0


def _final_sentence(text: str) -> FunASRSentence:
    return FunASRSentence(
        sentence_id=1,
        text=text,
        begin_ms=0,
        end_ms=100,
        sentence_end=True,
        heartbeat=False,
        words=(),
    )


@pytest.mark.asyncio
async def test_wait_for_nonempty_final_accepts_text_after_vad_end() -> None:
    plugin = FunASRSTT(FunASRConfig(api_key="test", ws_url="ws://unused"))
    plugin.trace_result(_final_sentence("旧句"), task_epoch=1)
    since = plugin._last_nonempty_at + 0.001
    plugin.trace_result(_final_sentence(""), task_epoch=2)

    waiting = asyncio.create_task(plugin.wait_for_nonempty_final(since=since, timeout=0.2))
    await asyncio.sleep(0)
    assert not waiting.done()

    plugin.trace_result(_final_sentence("今天星期几"), task_epoch=3)
    assert await waiting is True


@pytest.mark.asyncio
async def test_wait_for_nonempty_final_times_out_without_text() -> None:
    plugin = FunASRSTT(FunASRConfig(api_key="test", ws_url="ws://unused"))
    since = funasr_stt.monotonic()
    plugin.trace_result(_final_sentence(""), task_epoch=1)

    assert await plugin.wait_for_nonempty_final(since=since, timeout=0.05) is False
