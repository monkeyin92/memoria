"""P1-03: persona learning follows the person using the device."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from services.archive.domain import EvidenceEvent
from services.control_api.app.persona_learning import (
    observe_persona,
    schedule_persona_observation,
)


@asynccontextmanager
async def _no_fence(_request: Any, _account_id: str) -> Any:
    yield


class _Tasks:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    def add_task(self, func: Any, *args: Any, **kwargs: Any) -> None:
        self.calls.append((func, args, kwargs))


def _event(subject_id: str | None) -> EvidenceEvent:
    return EvidenceEvent(
        event_id="persona-turn",
        account_id="account-holder",
        subject_id=subject_id,
        session_id="session-1",
        turn_id=1,
        generation_id=1,
        event_type="speech.utterance_finalized",
        occurred_at=datetime.now(UTC),
        speaker_class="owner",
        source="test",
        payload={"text": "我觉得先想清楚。", "persona_eligible": True},
    )


def _request() -> Any:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(persona_engine=object(), memory_store=None))
    )


@pytest.mark.parametrize(
    ("subject_id", "grant", "scheduled", "subject_learning"),
    [
        # The account holder keeps learning under the account's persona consent.
        ("account-holder", False, True, False),
        # A child or elder learns their own persona under the binding grant...
        ("person-child", True, True, True),
        # ...and nothing without it.
        ("person-child", False, False, None),
        # A turn without a subject is nobody's persona.
        (None, True, False, None),
    ],
)
def test_learning_is_scheduled_for_the_person_using_the_device(
    subject_id: str | None, grant: bool, scheduled: bool, subject_learning: bool | None
) -> None:
    tasks = _Tasks()

    schedule_persona_observation(
        _request(),
        tasks,  # type: ignore[arg-type]
        event=_event(subject_id),
        duplicate=False,
        account_write=_no_fence,
        subject_learning_allowed=grant,
    )

    assert bool(tasks.calls) is scheduled
    if scheduled:
        func, _args, kwargs = tasks.calls[0]
        assert func is observe_persona
        assert kwargs["account_id"] == "account-holder"
        assert kwargs["source_event_id"] == "persona-turn"
        assert kwargs["subject_learning_allowed"] is subject_learning


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("subject_learning", "account_consent", "expected"),
    [
        (True, False, True),
        (False, True, True),
        (False, False, False),
    ],
)
async def test_observe_uses_the_binding_grant_or_the_account_consent(
    subject_learning: bool, account_consent: bool, expected: bool
) -> None:
    engine = SimpleNamespace(
        learning_allowed=AsyncMock(return_value=account_consent),
        observe=AsyncMock(),
    )

    await observe_persona(
        _request(),
        engine,  # type: ignore[arg-type]
        account_write=_no_fence,
        account_id="account-holder",
        source_event_id="persona-turn",
        speech_duration_ms=900,
        pause_ratio=0.2,
        quality_score=0.9,
        subject_learning_allowed=subject_learning,
    )

    evidence = engine.observe.await_args.args[0]
    assert evidence.learning_allowed is expected
    assert evidence.account_id == "account-holder"
    if subject_learning:
        # Another subject never borrows the account holder's persona consent.
        engine.learning_allowed.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("style_only", [False, True])
async def test_a_minor_subject_is_scheduled_for_style_only_learning(style_only: bool) -> None:
    """P0-04 D2: the archive route marks a minor's turns style-only."""

    tasks = _Tasks()
    schedule_persona_observation(
        _request(),
        tasks,  # type: ignore[arg-type]
        event=_event("person-child"),
        duplicate=False,
        account_write=_no_fence,
        subject_learning_allowed=True,
        style_only=style_only,
    )
    func, args, kwargs = tasks.calls[0]
    engine = SimpleNamespace(learning_allowed=AsyncMock(return_value=False), observe=AsyncMock())

    await func(args[0], engine, **kwargs)

    assert engine.observe.await_args.args[0].style_only is style_only
