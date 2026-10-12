"""Subject-keyed session recall isolation (P0-04 offline slice)."""

from __future__ import annotations

import pytest
from services.memory_scope.subject_session_recall import (
    InMemorySubjectSessionRecallStore,
    SubjectSessionRecallDenied,
)


@pytest.fixture
def store() -> InMemorySubjectSessionRecallStore:
    return InMemorySubjectSessionRecallStore()


@pytest.mark.asyncio
async def test_recall_is_scoped_to_subject_session_and_epoch(
    store: InMemorySubjectSessionRecallStore,
) -> None:
    await store.append(
        subject_id="person-a",
        session_id="session-1",
        session_epoch=1,
        turn_id=1,
        text="第一轮说到苏州。",
        actor_subject_id="person-a",
    )
    await store.append(
        subject_id="person-b",
        session_id="session-1",
        session_epoch=1,
        turn_id=1,
        text="另一人只说北京。",
        actor_subject_id="person-b",
    )
    a_items = await store.list_recent(
        subject_id="person-a",
        session_id="session-1",
        session_epoch=1,
        actor_subject_id="person-a",
        limit=4,
    )
    b_items = await store.list_recent(
        subject_id="person-b",
        session_id="session-1",
        session_epoch=1,
        actor_subject_id="person-b",
        limit=4,
    )
    assert a_items == ("第一轮说到苏州。",)
    assert b_items == ("另一人只说北京。",)


@pytest.mark.asyncio
async def test_subject_switch_epoch_closes_prior_recall(
    store: InMemorySubjectSessionRecallStore,
) -> None:
    await store.append(
        subject_id="person-child",
        session_id="session-1",
        session_epoch=1,
        turn_id=1,
        text="孩子 epoch1 的话。",
        actor_subject_id="person-child",
    )
    await store.append(
        subject_id="person-child",
        session_id="session-1",
        session_epoch=2,
        turn_id=2,
        text="孩子 epoch2 的话。",
        actor_subject_id="person-child",
    )
    epoch1 = await store.list_recent(
        subject_id="person-child",
        session_id="session-1",
        session_epoch=1,
        actor_subject_id="person-child",
        limit=4,
    )
    epoch2 = await store.list_recent(
        subject_id="person-child",
        session_id="session-1",
        session_epoch=2,
        actor_subject_id="person-child",
        limit=4,
    )
    assert epoch1 == ("孩子 epoch1 的话。",)
    assert epoch2 == ("孩子 epoch2 的话。",)


@pytest.mark.asyncio
async def test_foreign_actor_cannot_read_or_write(
    store: InMemorySubjectSessionRecallStore,
) -> None:
    with pytest.raises(SubjectSessionRecallDenied):
        await store.append(
            subject_id="person-child",
            session_id="session-1",
            session_epoch=1,
            turn_id=1,
            text="不应写入",
            actor_subject_id="person-other",
        )
    await store.append(
        subject_id="person-child",
        session_id="session-1",
        session_epoch=1,
        turn_id=1,
        text="私密一句",
        actor_subject_id="person-child",
    )
    with pytest.raises(SubjectSessionRecallDenied):
        await store.list_recent(
            subject_id="person-child",
            session_id="session-1",
            session_epoch=1,
            actor_subject_id="person-other",
            limit=4,
        )


@pytest.mark.asyncio
async def test_erase_subject_removes_every_epoch(
    store: InMemorySubjectSessionRecallStore,
) -> None:
    for epoch in (1, 2):
        await store.append(
            subject_id="person-a",
            session_id="session-1",
            session_epoch=epoch,
            turn_id=epoch,
            text=f"turn-{epoch}",
            actor_subject_id="person-a",
        )
    removed = await store.erase_subject(subject_id="person-a")
    assert removed == 2
    assert (
        await store.list_recent(
            subject_id="person-a",
            session_id="session-1",
            session_epoch=1,
            actor_subject_id="person-a",
            limit=4,
        )
    ) == ()
