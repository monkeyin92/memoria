"""Failover ASR chain: Volc primary, SenseVoice backup, Bailian tertiary."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.observability.metrics import MetricsRegistry
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
        self.sent: list[bytes] = []
        self.closed = False

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
        pass

    async def reconnect_with_replay(self) -> None:
        pass

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

    async def transcribe(self, pcm: bytes, *, sample_rate: int, language: str) -> str | None:
        self.requests.append((pcm, sample_rate, language))
        if self.raises:
            raise RuntimeError("sensevoice down")
        return self.text


def _config(**overrides: Any) -> ASRFailoverConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "first_result_timeout_s": 1.5,
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
async def test_first_result_timeout_switches_to_backup() -> None:
    now = [0.0]
    sensevoice = FakeSenseVoice("超时兜底")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(),
        sensevoice=sensevoice,
        clock=lambda: now[0],
    )
    await session.connect()
    await session.send_pcm(_pcm())
    now[0] = 2.0
    await session.send_pcm(_pcm(), capture_start_sample=1600)
    assert session.degraded is True
    assert session.degrade_reason == "first_result_timeout"
    assert session._primary is None

    await session.rotate_task(require_consumed=False)
    events = _drain(session.events)
    assert any(
        event.sentence is not None and event.sentence.text == "超时兜底"
        for event in events
        if event.event == "result-generated"
    )


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
    sensevoice = FakeSenseVoice("不应调用")
    session = FailoverASRSession(
        config=_config(),
        primary_factory=lambda: FakeSession(),
        sensevoice=sensevoice,
    )
    await session.connect()
    await session.send_pcm(_pcm())
    await session.rotate_task(require_consumed=False)
    assert session.degraded is False
    assert sensevoice.requests == []
    assert [event.event for event in _drain(session.events)] == ["task-finished"]


@pytest.mark.asyncio
async def test_open_breaker_skips_primary_entirely() -> None:
    breaker = ProcessASRBreaker(failures=1, open_s=30)
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
    )
    await session.connect()
    assert called == []
    assert session.degrade_reason == "breaker_open"
    await session.send_pcm(_pcm())
    await session.rotate_task(require_consumed=False)
    assert any(
        event.sentence is not None and event.sentence.text == "熔断兜底"
        for event in _drain(session.events)
        if event.event == "result-generated"
    )


@pytest.mark.asyncio
async def test_repeated_timeouts_open_breaker_for_the_next_session() -> None:
    now = [0.0]
    breaker = ProcessASRBreaker(failures=2, open_s=30, clock=lambda: now[0])
    called: list[int] = []

    def primary_factory() -> FakeSession:
        called.append(1)
        return FakeSession()

    for _ in range(2):
        session = FailoverASRSession(
            config=_config(),
            primary_factory=primary_factory,
            sensevoice=FakeSenseVoice("兜底"),
            breaker=breaker,
            clock=lambda: now[0],
        )
        await session.connect()
        await session.send_pcm(_pcm())
        now[0] += 2.0
        await session.send_pcm(_pcm(), capture_start_sample=1600)
        assert session.degrade_reason == "first_result_timeout"
        await session.rotate_task(require_consumed=False)
        _drain(session.events)
        await session.aclose()

    skipped = FailoverASRSession(
        config=_config(),
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


def test_config_is_off_by_default_and_factory_is_unchanged() -> None:
    assert ASRFailoverConfig.from_env({}).enabled is False
    factory = build_asr_session_factory(
        funasr_config=FunASRConfig(api_key="key", ws_url="ws://example.invalid"),
        env={},
    )
    assert isinstance(factory(), FunASRSession)


def test_enabled_factory_requires_sensevoice_endpoint() -> None:
    with pytest.raises(ValueError, match="SENSEVOICE_URL"):
        build_asr_session_factory(
            funasr_config=FunASRConfig(api_key="key", ws_url="ws://example.invalid"),
            env={"ASR_FAILOVER_ENABLED": "true"},
        )


def test_enabled_factory_builds_failover_session() -> None:
    factory = build_asr_session_factory(
        funasr_config=FunASRConfig(api_key="key", ws_url="ws://example.invalid"),
        env={
            "ASR_FAILOVER_ENABLED": "true",
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
async def test_segment_too_long_is_not_sent_to_backup() -> None:
    config = replace(_config(), max_segment_s=0.01)
    sensevoice = FakeSenseVoice("不应调用")
    session = FailoverASRSession(
        config=config,
        primary_factory=lambda: FakeSession(fail_connect=True),
        sensevoice=sensevoice,
    )
    await session.connect()
    await session.send_pcm(_pcm())
    await session.rotate_task(require_consumed=False)
    assert sensevoice.requests == []
    assert [event.event for event in _drain(session.events)] == ["task-finished"]
