from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from services.identity.domain import (
    Permission,
    Relationship,
    RelationshipStatus,
    RelationType,
)
from services.memory_scope.domain import MemoryOutboxEvent
from services.memory_scope.redis_outbox import RedisMemoryOutboxDispatcher
from services.memory_scope.relationship_grants import IdentityRelationshipGrantResolver


class _Identity:
    def __init__(self, relationships: tuple[Relationship, ...]) -> None:
        self.relationships = relationships
        self.calls: list[
            tuple[
                str,
                tuple[RelationshipStatus, ...] | None,
                str | None,
            ]
        ] = []

    async def list_relationships(
        self,
        *,
        person_id: str,
        statuses: tuple[RelationshipStatus, ...] | None = None,
        actor_person_id: str | None = None,
    ) -> tuple[Relationship, ...]:
        self.calls.append((person_id, statuses, actor_person_id))
        return self.relationships


def _relationship(
    *,
    relation_type: RelationType,
    source: str,
    target: str,
    permissions: frozenset[Permission],
    valid_until: datetime | None = None,
) -> Relationship:
    now = datetime.now(UTC)
    return Relationship(
        relationship_id=f"{relation_type}:{source}:{target}",
        source_person_id=source,
        target_person_id=target,
        relation_type=relation_type,
        status="active",
        valid_from=now - timedelta(minutes=1),
        valid_until=valid_until,
        established_evidence_id="relationship-evidence-1",
        confirmed_by_source_at=now - timedelta(minutes=1),
        confirmed_by_target_at=now - timedelta(minutes=1),
        permissions=permissions,
    )


@pytest.mark.asyncio
async def test_identity_grants_require_explicit_scope_permission_and_direction() -> None:
    now = datetime.now(UTC)
    identity = _Identity(
        (
            _relationship(
                relation_type="guardian_of",
                source="guardian",
                target="ward",
                permissions=frozenset(("memory.guardian.summary",)),
            ),
            _relationship(
                relation_type="guardian_of",
                source="guardian",
                target="ungranted-ward",
                permissions=frozenset(),
            ),
            _relationship(
                relation_type="family_member_of",
                source="guardian",
                target="legacy-owner",
                permissions=frozenset(("legacy.access",)),
            ),
            _relationship(
                relation_type="family_member_of",
                source="guardian",
                target="expired-owner",
                permissions=frozenset(("legacy.access",)),
                valid_until=now - timedelta(seconds=1),
            ),
        )
    )
    resolver = IdentityRelationshipGrantResolver(identity)

    assert await resolver.guardian_of(actor_subject_id="guardian", now=now) == frozenset(
        ("ward",)
    )
    assert await resolver.legacy_grants_for(
        actor_subject_id="guardian", now=now
    ) == frozenset(("legacy-owner",))


class _Redis:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, int, tuple[object, ...]]] = []
        self.closed = False

    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object:
        self.calls.append((script, numkeys, keys_and_args))
        if self.fail:
            raise RuntimeError("redis unavailable")
        return 1

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_redis_dispatch_is_atomic_idempotent_and_failure_is_not_acknowledged() -> None:
    event = MemoryOutboxEvent(
        outbox_id="outbox-1",
        event_id="record-1:captured",
        topic="memory.record.captured",
        payload={"record_id": "record-1", "subject_id": "person-a"},
    )
    redis = _Redis()
    dispatcher = RedisMemoryOutboxDispatcher(redis)

    assert await dispatcher.dispatch(event) is True
    assert len(redis.calls) == 1
    script, numkeys, args = redis.calls[0]
    assert numkeys == 2
    assert "SET" in script and "XADD" in script
    assert args[0] == "memoria:memory:events"
    assert args[1] == "memoria:memory:events:dedup:record-1:captured"
    assert '"subject_id":"person-a"' in str(args)
    await dispatcher.close()
    assert redis.closed is True

    failed = RedisMemoryOutboxDispatcher(_Redis(fail=True))
    assert await failed.dispatch(event) is False
