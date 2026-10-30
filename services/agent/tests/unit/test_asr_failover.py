"""Failover ASR chain: Volc primary, SenseVoice backup, Bailian tertiary."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.observability.metrics import MetricsRegistry
from services.agent.src.providers import asr_failover
from services.agent.src.providers.asr_failover import (
    ASRFailoverConfig,
    FailoverASRSession,
    ProcessASRBreaker,
    build_asr_session_factory,
)
from services.agent.src.providers.funasr_protocol import FunASRSentence, FunASRServerEvent
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession


def _pcm(samples: int = 1600, amplitude: int = 200) -> bytes:
    return bytes((amplitude & 0xFF, (amplitude >> 8) & 0xFF)) * samples


def _silence(samples: int = 1600) -> bytes:
    return b"\x00\x00" * samples


def _sentence(text: str, *, final: bool = True) -> FunASRServerEvent:
    return FunASRServerEvent(
        event="result-generated",
        task_id="primary-task",
        sentence=FunASRSentence(
            sentence_id=1,
            text=text,
            begin_ms=0,
            end_ms=100,
            sentence_end=final,
            heartbeat=False,
            words=(),
        ),
    )


def _drain(queue: asyncio.Queue[FunASRServerEvent]) -> list[FunASRServerEvent]:
    events: list[FunASRServerEvent] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


class FakeSession:
    def __init__(
        self,
        *,
        text: str | None = None,
        interim: str | None = None,
        fail_connect: bool = False,
        fail_rotate: bool = False,
        fail_event_on_send: bool = False,
        final_on_rotate: bool = True,
        rotate_blocks: float = 0.0,
    ) -> None:
        self.config = SimpleNamespace(post_finish_tail_grace_s=0.0)
        self.events: asyncio.Queue[FunASRServerEvent] = asyncio.Queue()
        self.task_id = "primary-task"
        self.task_epoch = 1
        self.task_sample_origin = 0
        self.rotation_pending = False
        self.text = text
        self.interim = interim
        self.fail_connect = fail_connect
        self.fail_rotate = fail_rotate
        self.fail_event_on_send = fail_event_on_send
        self.final_on_rotate = final_on_rotate
        self.rotate_blocks = rotate_blocks
        self.sent: list[bytes] = []
        self.closed = False
        self.rotated = 0
        self.commits: list[int] = []

    async def connect(self) -> None:
        if self.fail_connect:
            raise RuntimeError("connect refused")

    async def send_pcm(self, pcm: bytes, *, capture_start_sample: int | None = None) -> None:
        self.sent.append(pcm)
        if self.interim:
            self.events.put_nowait(_sentence(self.interim, final=False))
        if self.fail_event_on_send:
            self.events.put_nowait(
                FunASRServerEvent(
                    event="task-failed",
                    task_id="primary-task",
                    error_code="55000031",
                )
            )

    async def rotate_task(self, *, require_consumed: bool = True) -> None:
        self.rotated += 1
        try:
            await asyncio.sleep(self.rotate_blocks)
        except asyncio.CancelledError:
            # Mirrors VolcASRSession, which aborts its task in a finally block.
            self.rotation_pending = True
            raise
        if self.fail_rotate:
            raise RuntimeError("rotation failed")
        if self.text and self.final_on_rotate:
            self.events.put_nowait(_sentence(self.text))
        self.events.put_nowait(
            FunASRServerEvent(event="task-finished", task_id="primary-task")
        )
        self.rotation_pending = True
        self.task_epoch += 1

    def mark_committed_sample(self, sample: int) -> None:
        self.commits.append(sample)

    async def reconnect_with_replay(self) -> None:
        pass

    async def aclose(self) -> None:
        self.closed = True


class RecoveringSession:
    """Primary that loses its task once, then accepts a replay and answers."""

    def __init__(self, *, fail_first_send: bool = False, text: str | None = None) -> None:
        self.config = SimpleNamespace(post_finish_tail_grace_s=0.0)
        self.events: asyncio.Queue[FunASRServerEvent] = asyncio.Queue()
        self.task_id = "recovering-task"
        self.task_epoch = 1
        self.task_sample_origin = 0
        self.rotation_pending = False
        self.fail_next = fail_first_send
        self.text = text
        self.sent: list[bytes] = []
        self.closed = False
        self.rotated = 0

    async def connect(self) -> None:
        return None

    async def send_pcm(self, pcm: bytes, *, capture_start_sample: int | None = None) -> None:
        if self.fail_next:
            self.fail_next = False
            self.events.put_nowait(
                FunASRServerEvent(
                    event="task-failed",
                    task_id=self.task_id,
                    error_code="55000031",
                    error_message="idle closed",
                )
            )
            return
        self.sent.append(pcm)
        if self.text:
            self.events.put_nowait(_sentence(self.text))

    async def rotate_task(self, *, require_consumed: bool = True) -> None:
        self.rotation_pending = True
        self.rotated += 1
        self.events.put_nowait(
            FunASRServerEvent(event="task-finished", task_id=self.task_id)
        )
        self.task_epoch += 1
        self.fail_next = True

    def mark_committed_sample(self, sample: int) -> None:
        return None

    async def reconnect_with_replay(self) -> None:
        return None

    async def aclose(self) -> None:
        self.closed = True


class FakeSenseVoice:
    def __init__(self, text: str | None = None, *, raises: bool = False) -> None:
        self.text = text
        self.raises = raises
        self.requests: list[tuple[bytes, int, str]] = []

    def accepts_text(self, text: str) -> bool:
        return len(text.strip()) >= 2

    def accepts_energy(self, *, rms: int, peak_abs: int) -> bool:
        return rms >= 100 or peak_abs >= 350

    async def transcribe(
        self, pcm: bytes, *, sample_rate: int, language: str
    ) -> str | None:
        self.requests.append((pcm, sample_rate, language))
        if self.raises:
            raise RuntimeError("sensevoice down")
        return self.text


def _config(**overrides: Any) -> ASRFailoverConfig:
    values: dict[str, Any] = {
        "final_result_timeout_s": 1.0,
        "connect_timeout_s": 0.2,
        "backup_timeout_s": 0.5,
        "max_segment_s": 12.0,
    }
    values.update(overrides)
    return ASRFailoverConfig(**values)


@pytest.mark.asyncio
async def test_primary_connect_failure_reidentifies_whole_segment() -> None:
    sensevoice = FakeSenseVoice("兜底整句")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(fail_connect=True),
        sensevoice=sensevoice,
    )
    await session.connect()
    assert session.degraded is True
    assert session.degrade_reason == "connect_failed"

    pcm = _pcm()
    previous_task_id = session.task_id
    await session.send_pcm(pcm, capture_start_sample=8_000)
    assert session.task_sample_origin == 8_000
    await session.rotate_task(require_consumed=False)

    events = _drain(session.events)
    assert previous_task_id is not None
    assert all(event.task_id == previous_task_id for event in events)
    results = [event for event in events if event.event == "result-generated"]
    assert len(results) == 1
    assert results[0].sentence is not None
    assert results[0].sentence.text == "兜底整句"
    assert results[0].sentence.sentence_end is True
    assert events[-1].event == "task-finished"
    assert sensevoice.requests == [(pcm, 16_000, "zh")]


@pytest.mark.asyncio
async def test_streaming_for_minutes_is_not_a_failure() -> None:
    """The budget bounds the finalize wait only.

    Volc's first partial arrives at p50 3.4 s, so a clock that started at
    speech onset failed ordinary speech. A primary that is still streaming is
    never late.
    """

    now = [0.0]
    sensevoice = FakeSenseVoice("不应调用")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(text="主用结果"),
        sensevoice=sensevoice,
        clock=lambda: now[0],
    )
    await session.connect()
    start = 0
    for _ in range(30):
        await session.send_pcm(_pcm(), capture_start_sample=start)
        start += 1600
        now[0] += 2.0
    assert session.degraded is False
    assert sensevoice.requests == []

    await session.rotate_task(require_consumed=False)
    events = _drain(session.events)
    assert any(
        event.sentence is not None and event.sentence.text == "主用结果"
        for event in events
        if event.event == "result-generated"
    )


@pytest.mark.asyncio
async def test_finalize_timeout_switches_to_backup() -> None:
    """A primary that ignores the negative packet past the budget is replaced.

    The fake never returns from ``rotate_task``, so the failover has to come
    from the budget, not from the primary's own error path.
    """

    sensevoice = FakeSenseVoice("超时兜底")
    session = FailoverASRSession(
        config=_config(final_result_timeout_s=0.05),
        primary_factory=lambda: FakeSession(text="永不返回", rotate_blocks=5.0),
        sensevoice=sensevoice,
        clock=lambda: 0.0,
    )
    await session.connect()
    await session.send_pcm(_pcm())
    await session.rotate_task(require_consumed=False)

    assert session.degraded is True
    assert session.degrade_reason == "final_result_timeout"
    assert session._primary is None
    events = _drain(session.events)
    assert any(
        event.sentence is not None and event.sentence.text == "超时兜底"
        for event in events
        if event.event == "result-generated"
    )


@pytest.mark.asyncio
async def test_a_final_within_the_budget_is_forwarded_without_backup() -> None:
    sensevoice = FakeSenseVoice("不应调用")
    session = FailoverASRSession(
        config=_config(final_result_timeout_s=0.5),
        primary_factory=lambda: FakeSession(text="准时结果"),
        sensevoice=sensevoice,
        clock=lambda: 0.0,
    )
    await session.connect()
    await session.send_pcm(_pcm())
    await session.rotate_task(require_consumed=False)

    assert session.degraded is False
    assert sensevoice.requests == []
    events = _drain(session.events)
    assert any(
        event.sentence is not None and event.sentence.text == "准时结果"
        for event in events
        if event.event == "result-generated"
    )


@pytest.mark.asyncio
async def test_degradation_covers_one_segment_and_the_next_retries_primary() -> None:
    now = [0.0]
    primaries: list[FakeSession] = []

    def primary_factory() -> FakeSession:
        # The first segment's primary never answers past the budget; the
        # retried primary does.
        session = FakeSession(
            text="主用第二段", rotate_blocks=5.0 if not primaries else 0.0
        )
        primaries.append(session)
        return session

    sensevoice = FakeSenseVoice("兜底第一段")
    session = FailoverASRSession(
        config=_config(final_result_timeout_s=0.05),
        primary_factory=primary_factory,
        sensevoice=sensevoice,
        clock=lambda: now[0],
    )
    await session.connect()
    await session.send_pcm(_pcm())
    await session.rotate_task(require_consumed=False)
    assert session.degraded is True
    assert session.degrade_reason == "final_result_timeout"
    _drain(session.events)

    # The next segment starts: the primary is retried and the session is no
    # longer pinned to the backup.
    await session.send_pcm(_pcm(), capture_start_sample=4800)
    assert session.degraded is False
    assert session.degrade_reason is None
    assert len(primaries) == 2
    assert primaries[-1].sent, "the retried primary must receive the segment"

    await session.rotate_task(require_consumed=False)
    events = _drain(session.events)
    assert any(
        event.sentence is not None and event.sentence.text == "主用第二段"
        for event in events
        if event.event == "result-generated"
    )
    assert [request[2] for request in sensevoice.requests] == ["zh"]


@pytest.mark.asyncio
async def test_open_breaker_keeps_the_next_segment_on_the_backup() -> None:
    now = [0.0]
    breaker = ProcessASRBreaker(failures=1, open_s=30, clock=lambda: now[0])
    breaker.record_failure()
    called: list[int] = []

    def primary_factory() -> FakeSession:
        called.append(1)
        return FakeSession()

    sensevoice = FakeSenseVoice("熔断兜底")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=primary_factory,
        sensevoice=sensevoice,
        breaker=breaker,
        clock=lambda: now[0],
    )
    await session.connect()
    assert called == []
    await session.send_pcm(_pcm())
    await session.rotate_task(require_consumed=False)
    assert any(
        event.sentence is not None and event.sentence.text == "熔断兜底"
        for event in _drain(session.events)
        if event.event == "result-generated"
    )

    # The breaker is still open, so the next segment stays on the backup and
    # no primary connection is attempted.
    await session.send_pcm(_pcm(), capture_start_sample=1600)
    assert session.degraded is True
    assert session.degrade_reason == "breaker_open"
    assert called == []
    await session.rotate_task(require_consumed=False)
    _drain(session.events)
    # The window has passed, so the following segment probes the primary again.
    now[0] = 31.0
    await session.send_pcm(_pcm(), capture_start_sample=3200)
    assert called == [1]
    assert session.degraded is False


@pytest.mark.asyncio
async def test_healthy_primary_final_is_forwarded_without_backup() -> None:
    primary = FakeSession(text="主用结果")
    sensevoice = FakeSenseVoice("不应调用")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: primary,
        sensevoice=sensevoice,
    )
    await session.connect()
    await session.send_pcm(_pcm())
    previous_task_id = session.task_id
    await session.rotate_task(require_consumed=False)
    events = _drain(session.events)
    assert [event.event for event in events] == ["result-generated", "task-finished"]
    assert events[0].sentence is not None
    assert events[0].sentence.text == "主用结果"
    assert all(event.task_id == previous_task_id for event in events)
    assert session.degraded is False
    assert sensevoice.requests == []


@pytest.mark.asyncio
async def test_healthy_primary_empty_retries_backup_with_the_same_pcm() -> None:
    primary = FakeSession()
    sensevoice = FakeSenseVoice("重试听出的句子")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: primary,
        sensevoice=sensevoice,
    )
    await session.connect()
    await session.send_pcm(_pcm())

    await session.rotate_task(require_consumed=False)

    events = _drain(session.events)
    assert any(
        event.sentence is not None and event.sentence.text == "重试听出的句子"
        for event in events
        if event.event == "result-generated"
    )
    assert len(sensevoice.requests) == 1
    assert session.last_segment_had_speech is True
    assert session.last_segment_provider_failed is False
    assert session.last_segment_backup_retried is True


@pytest.mark.asyncio
async def test_healthy_primary_empty_retry_stops_at_its_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowSenseVoice(FakeSenseVoice):
        async def transcribe(
            self, pcm: bytes, *, sample_rate: int, language: str
        ) -> str | None:
            await asyncio.sleep(0.2)
            return "太晚的结果"

    monkeypatch.setattr(asr_failover, "_HEALTHY_EMPTY_RETRY_BUDGET_S", 0.02)
    sensevoice = SlowSenseVoice()
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(),
        sensevoice=sensevoice,
    )
    await session.connect()
    await session.send_pcm(_pcm())

    started = asyncio.get_running_loop().time()
    await session.rotate_task(require_consumed=False)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.15
    assert not any(
        event.event == "result-generated" for event in _drain(session.events)
    )
    assert session.last_segment_had_speech is True
    assert session.last_segment_provider_failed is False
    assert session.last_segment_backup_retried is True


@pytest.mark.asyncio
async def test_healthy_primary_empty_silence_skips_backup_and_fallback() -> None:
    sensevoice = FakeSenseVoice("不应调用")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(),
        sensevoice=sensevoice,
    )
    await session.connect()
    await session.send_pcm(_silence())

    await session.rotate_task(require_consumed=False)

    assert sensevoice.requests == []
    assert session.last_segment_had_speech is False
    assert session.last_segment_provider_failed is False
    assert session.last_segment_backup_retried is True


@pytest.mark.asyncio
async def test_provider_task_failure_uses_backup_without_raising() -> None:
    sensevoice = FakeSenseVoice("故障兜底")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(fail_event_on_send=True),
        sensevoice=sensevoice,
    )
    await session.connect()
    await session.send_pcm(_pcm())
    assert session.degraded is True
    assert session.degrade_reason == "provider_error"
    await session.rotate_task(require_consumed=False)
    assert any(
        event.sentence is not None and event.sentence.text == "故障兜底"
        for event in _drain(session.events)
        if event.event == "result-generated"
    )


@pytest.mark.asyncio
async def test_provider_error_mid_segment_recovers_and_replays_before_vad_end() -> None:
    primaries: list[RecoveringSession] = []

    def primary_factory() -> RecoveringSession:
        primary = RecoveringSession(
            fail_first_send=not primaries,
            text="恢复结果",
        )
        primaries.append(primary)
        return primary

    sensevoice = FakeSenseVoice("不应调用")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=primary_factory,
        sensevoice=sensevoice,
    )
    await session.connect()
    frame = _pcm(1600)
    await session.send_pcm(frame, capture_start_sample=0)

    assert session.degraded is False
    assert [primary.closed for primary in primaries] == [True, False]
    assert primaries[1].sent == [frame]
    assert sensevoice.requests == []

    await session.rotate_task(require_consumed=False)
    events = _drain(session.events)
    assert [
        event.sentence.text
        for event in events
        if event.event == "result-generated" and event.sentence is not None
    ] == ["恢复结果"]


@pytest.mark.asyncio
async def test_primary_text_resets_breaker_failures() -> None:
    breaker = ProcessASRBreaker(failures=2, open_s=30)
    breaker.record_failure()
    primary = RecoveringSession(text="主用恢复")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: primary,
        sensevoice=FakeSenseVoice("不应调用"),
        breaker=breaker,
    )
    await session.connect()
    await session.send_pcm(_pcm())
    assert breaker.state == "closed"

    breaker.record_failure()
    assert breaker.state == "closed"

    breaker.record_failure()
    assert breaker.state == "open"


@pytest.mark.asyncio
async def test_three_idle_evictions_recover_without_opening_breaker() -> None:
    breaker = ProcessASRBreaker(failures=3, open_s=30)
    primaries: list[RecoveringSession] = []

    def primary_factory() -> RecoveringSession:
        primary = RecoveringSession(
            fail_first_send=not primaries,
            text="恢复结果",
        )
        primaries.append(primary)
        return primary

    session = FailoverASRSession(
        config=_config(),
        primary_factory=primary_factory,
        sensevoice=FakeSenseVoice("不应调用"),
        breaker=breaker,
    )
    await session.connect()
    start = 0
    for _ in range(3):
        await session.send_pcm(_pcm(), capture_start_sample=start)
        start += 1600
        assert session.degraded is False
        await session.rotate_task(require_consumed=False)
        texts = [
            event.sentence.text
            for event in _drain(session.events)
            if event.event == "result-generated" and event.sentence is not None
        ]
        assert texts == ["恢复结果"]
    assert breaker.state == "closed"
    assert len(primaries) == 4


@pytest.mark.asyncio
async def test_reset_for_gap_discards_the_old_task_and_arms_the_next() -> None:
    primaries: list[FakeSession] = []

    def primary_factory() -> FakeSession:
        primary = FakeSession(text="下一句")
        primaries.append(primary)
        return primary

    session = FailoverASRSession(
        config=_config(),
        primary_factory=primary_factory,
        sensevoice=FakeSenseVoice("不应调用"),
    )
    await session.connect()
    await session.send_pcm(_pcm(), capture_start_sample=0)
    await session.reset_for_gap()
    assert primaries[0].closed is True
    assert session.degraded is False

    await session.send_pcm(_pcm(), capture_start_sample=5 * 16_000)
    assert len(primaries) == 2
    assert primaries[1].sent == [_pcm()]
    await session.rotate_task(require_consumed=False)
    texts = [
        event.sentence.text
        for event in _drain(session.events)
        if event.event == "result-generated" and event.sentence is not None
    ]
    assert texts == ["下一句"]


@pytest.mark.asyncio
async def test_task_failure_first_seen_at_vad_end_recovers_on_the_new_task() -> None:
    primaries: list[RecoveringSession] = []

    def primary_factory() -> RecoveringSession:
        primary = RecoveringSession(text="句末恢复" if primaries else None)
        primaries.append(primary)
        return primary

    session = FailoverASRSession(
        config=_config(),
        primary_factory=primary_factory,
        sensevoice=FakeSenseVoice("不应调用"),
    )
    await session.connect()
    await session.send_pcm(_pcm(), capture_start_sample=0)
    primaries[0].events.put_nowait(
        FunASRServerEvent(
            event="task-failed",
            task_id=primaries[0].task_id,
            error_code="55000031",
            error_message="idle closed",
        )
    )

    await session.rotate_task(require_consumed=False)

    assert primaries[0].rotated == 0
    assert primaries[1].rotated == 1
    texts = [
        event.sentence.text
        for event in _drain(session.events)
        if event.event == "result-generated" and event.sentence is not None
    ]
    assert texts == ["句末恢复"]


@pytest.mark.asyncio
async def test_backup_receives_the_segment_from_its_first_speech_frame() -> None:
    """Leading silence must not push a normal utterance past the segment cap."""

    sensevoice = FakeSenseVoice("短句")
    session = FailoverASRSession(
        config=_config(max_segment_s=1.0),
        primary_factory=lambda: FakeSession(fail_connect=True),
        sensevoice=sensevoice,
    )
    await session.connect()
    # 20 s of silence, then one second of speech: the segment itself is longer
    # than the cap, but the speech is not.
    start = 0
    for _ in range(10):
        await session.send_pcm(_silence(16_000), capture_start_sample=start)
        start += 16_000
    await session.send_pcm(_pcm(16_000), capture_start_sample=start)
    await session.rotate_task(require_consumed=False)
    assert len(sensevoice.requests) == 1
    assert len(sensevoice.requests[0][0]) == 16_000 * 2


@pytest.mark.asyncio
async def test_backup_starts_at_vad_start_not_earlier_noise() -> None:
    sensevoice = FakeSenseVoice("短句")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(fail_connect=True),
        sensevoice=sensevoice,
    )
    await session.connect()
    await session.send_pcm(_pcm(16_000, 100), capture_start_sample=0)
    session.note_vad_start(16_000)
    speech = _pcm(16_000, 200)
    await session.send_pcm(speech, capture_start_sample=16_000)
    await session.rotate_task(require_consumed=False)
    assert sensevoice.requests[0][0] == speech


@pytest.mark.asyncio
async def test_backup_truncates_to_the_last_twelve_seconds() -> None:
    sensevoice = FakeSenseVoice("截断兜底")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(fail_connect=True),
        sensevoice=sensevoice,
    )
    await session.connect()
    for second in range(27):
        await session.send_pcm(
            _pcm(16_000, 1000 + second),
            capture_start_sample=second * 16_000,
        )
    await session.rotate_task(require_consumed=False)
    pcm = sensevoice.requests[0][0]
    assert len(pcm) == 12 * 16_000 * 2
    assert int.from_bytes(pcm[:2], "little") == 1015
    final = next(
        event.sentence
        for event in _drain(session.events)
        if event.event == "result-generated" and event.sentence is not None
    )
    assert (final.begin_ms, final.end_ms) == (15_000, 27_000)


@pytest.mark.asyncio
async def test_sensevoice_failure_falls_through_to_bailian_tertiary() -> None:
    sensevoice = FakeSenseVoice(raises=True)
    tertiary = FakeSession(text="百炼识别")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(fail_rotate=True),
        sensevoice=sensevoice,
        tertiary_factory=lambda: tertiary,
    )
    await session.connect()
    await session.send_pcm(_pcm())
    await session.rotate_task(require_consumed=False)
    events = _drain(session.events)
    texts = [
        event.sentence.text
        for event in events
        if event.event == "result-generated" and event.sentence is not None
    ]
    assert texts == ["百炼识别"]
    assert sensevoice.requests
    assert tertiary.closed is True


@pytest.mark.asyncio
async def test_both_backups_failing_emits_boundary_and_metric() -> None:
    metrics = MetricsRegistry()
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(fail_rotate=True),
        sensevoice=FakeSenseVoice(None),
        tertiary_factory=lambda: FakeSession(fail_connect=True),
        metrics=metrics,
    )
    await session.connect()
    await session.send_pcm(_pcm())
    await session.rotate_task(require_consumed=False)
    events = _drain(session.events)
    assert [event.event for event in events] == ["task-finished"]
    assert metrics.get("asr_both_failed_total") == 1.0


@pytest.mark.asyncio
async def test_empty_primary_result_is_not_a_failure() -> None:
    now = [0.0]
    sensevoice = FakeSenseVoice("不应调用")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(),
        sensevoice=sensevoice,
        clock=lambda: now[0],
    )
    await session.connect()
    # A silent segment may run far past the first-result budget without being
    # charged against the primary.
    for start in range(0, 16_000 * 5, 1600):
        await session.send_pcm(_silence(), capture_start_sample=start)
        now[0] += 0.2
    await session.rotate_task(require_consumed=False)
    assert session.degraded is False
    assert sensevoice.requests == []
    assert [event.event for event in _drain(session.events)] == ["task-finished"]


@pytest.mark.asyncio
async def test_repeated_timeouts_open_breaker_for_the_next_session() -> None:
    now = [0.0]
    breaker = ProcessASRBreaker(failures=2, open_s=30, clock=lambda: now[0])
    called: list[int] = []

    def primary_factory() -> FakeSession:
        called.append(1)
        return FakeSession(rotate_blocks=5.0)

    session = FailoverASRSession(
        config=_config(final_result_timeout_s=0.05),
        primary_factory=primary_factory,
        sensevoice=FakeSenseVoice("兜底"),
        breaker=breaker,
        clock=lambda: now[0],
    )
    await session.connect()
    start = 0
    # Two segments, each timing out: the breaker opens after the second.
    for _ in range(2):
        await session.send_pcm(_pcm(), capture_start_sample=start)
        start += 1600
        await session.rotate_task(require_consumed=False)
        assert session.degrade_reason == "final_result_timeout"
        _drain(session.events)
        now[0] += 0.1
    await session.aclose()

    skipped = FailoverASRSession(
        config=_config(final_result_timeout_s=0.05),
        primary_factory=primary_factory,
        sensevoice=FakeSenseVoice("兜底"),
        breaker=breaker,
        clock=lambda: now[0],
    )
    await skipped.connect()
    assert skipped.degrade_reason == "breaker_open"
    assert len(called) == 2


def test_breaker_merges_half_open_probes() -> None:
    now = [0.0]
    breaker = ProcessASRBreaker(failures=1, open_s=10, clock=lambda: now[0])
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.allow() is False
    now[0] = 11.0
    assert breaker.allow() is True
    assert breaker.allow() is False
    breaker.record_success()
    assert breaker.state == "closed"
    assert breaker.allow() is True


@pytest.mark.asyncio
async def test_closing_a_probe_session_releases_the_half_open_lease() -> None:
    """A closed probe must not disable the primary for the whole process."""

    now = [0.0]
    breaker = ProcessASRBreaker(failures=1, open_s=10, clock=lambda: now[0])
    breaker.record_failure()
    now[0] = 11.0

    probe = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(),
        sensevoice=FakeSenseVoice("兜底"),
        breaker=breaker,
        clock=lambda: now[0],
    )
    await probe.connect()
    assert probe.degraded is False
    assert breaker.probe_in_flight is True
    # The device disconnects before the probe ever rotates.
    await probe.aclose()

    assert breaker.probe_in_flight is False
    assert breaker.allow() is True
    breaker.release_probe()


def test_abandoned_probe_lease_expires_without_a_verdict() -> None:
    now = [0.0]
    breaker = ProcessASRBreaker(failures=1, open_s=10, probe_timeout_s=5, clock=lambda: now[0])
    breaker.record_failure()
    now[0] = 11.0
    assert breaker.allow() is True
    assert breaker.allow() is False
    now[0] = 15.0
    assert breaker.allow() is False
    now[0] = 16.5
    # The probing session died without a verdict; the lease expires and the
    # next request may probe again.
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.state == "open"


def test_a_session_cannot_release_a_probe_it_does_not_own() -> None:
    now = [0.0]
    breaker = ProcessASRBreaker(failures=1, open_s=10, clock=lambda: now[0])
    breaker.record_failure()
    now[0] = 11.0
    assert breaker.allow() is True
    owner = breaker.probe_owner
    assert owner is not None
    # Another session reporting an unrelated verdict must not free the lease.
    breaker.release_probe(owner + 1)
    assert breaker.probe_in_flight is True
    breaker.release_probe(owner)
    assert breaker.probe_in_flight is False


def test_funasr_selector_builds_the_plain_session_and_reads_no_chain_keys() -> None:
    """``ASR_PROVIDER=funasr`` must not need any Volc or backup config."""

    factory = build_asr_session_factory(
        asr_provider="funasr",
        funasr_config=FunASRConfig(api_key="key", ws_url="ws://example.invalid"),
        env={},
    )
    assert isinstance(factory(), FunASRSession)


def test_volc_selector_requires_sensevoice_endpoint() -> None:
    with pytest.raises(ValueError, match="SENSEVOICE_URL"):
        build_asr_session_factory(
            asr_provider="volc",
            funasr_config=FunASRConfig(api_key="key", ws_url="ws://example.invalid"),
            env={},
        )


def test_volc_selector_requires_volc_credentials() -> None:
    """A deploy that selected Volc without credentials must fail at build time."""

    with pytest.raises(ValueError, match="VOLC_ASR_APP_ID"):
        build_asr_session_factory(
            asr_provider="volc",
            funasr_config=FunASRConfig(api_key="key", ws_url="ws://example.invalid"),
            env={"SENSEVOICE_URL": "http://127.0.0.1:9/transcribe"},
        )


def test_volc_selector_builds_the_failover_session() -> None:
    factory = build_asr_session_factory(
        asr_provider="volc",
        funasr_config=FunASRConfig(api_key="key", ws_url="ws://example.invalid"),
        env={
            "SENSEVOICE_URL": "http://127.0.0.1:9/transcribe",
            "VOLC_ASR_APP_ID": "app",
            "VOLC_ASR_ACCESS_TOKEN": "token",
            "VOLC_ASR_RESOURCE_ID": "resource",
        },
    )
    assert isinstance(factory(), FailoverASRSession)


@pytest.mark.asyncio
async def test_failover_logs_reason_without_transcript(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    secret = "孩子的秘密转写"
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(fail_connect=True),
        sensevoice=FakeSenseVoice(secret),
    )
    await session.connect()
    await session.send_pcm(_pcm())
    await session.rotate_task(require_consumed=False)
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "connect_failed" in messages
    assert secret not in messages


@pytest.mark.asyncio
async def test_segment_too_long_is_truncated_to_the_last_cap() -> None:
    config = replace(_config(), max_segment_s=0.01)
    sensevoice = FakeSenseVoice("截断兜底")
    session = FailoverASRSession(
        config=config,
        primary_factory=lambda: FakeSession(fail_connect=True),
        sensevoice=sensevoice,
    )
    await session.connect()
    await session.send_pcm(_pcm())
    await session.rotate_task(require_consumed=False)
    assert len(sensevoice.requests) == 1
    assert len(sensevoice.requests[0][0]) == int(0.01 * 16_000) * 2
