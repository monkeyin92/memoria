"""Identity-backed read grants for production MemoryScope retrieval.

Only explicit, active relationship permissions are projected.  A family,
device-admin, payer or account-owner relationship never implies memory read
authority.  The adapter returns owner ids only; MemoryScope still applies its
scope-specific repository/RLS fence before exposing a record.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from services.identity.domain import Relationship, RelationshipStatus


class _IdentityRelationshipSource(Protocol):
    async def list_relationships(
        self,
        *,
        person_id: str,
        statuses: tuple[RelationshipStatus, ...] | None = None,
        actor_person_id: str | None = None,
    ) -> tuple[Relationship, ...]: ...


class IdentityRelationshipGrantResolver:
    """Resolve guardian-summary and legacy grants from Identity authority."""

    def __init__(self, identity: _IdentityRelationshipSource) -> None:
        self._identity = identity

    async def guardian_of(
        self,
        *,
        actor_subject_id: str,
        now: datetime,
    ) -> frozenset[str]:
        relationships = await self._identity.list_relationships(
            person_id=actor_subject_id,
            statuses=("active",),
            actor_person_id=actor_subject_id,
        )
        wards: set[str] = set()
        for relationship in relationships:
            if not _active_at(relationship.valid_from, relationship.valid_until, now):
                continue
            if "memory.guardian.summary" not in relationship.permissions:
                continue
            if (
                relationship.relation_type == "guardian_of"
                and relationship.source_person_id == actor_subject_id
            ):
                wards.add(relationship.target_person_id)
            elif (
                relationship.relation_type == "ward_of"
                and relationship.target_person_id == actor_subject_id
            ):
                wards.add(relationship.source_person_id)
        return frozenset(wards)

    async def legacy_grants_for(
        self,
        *,
        actor_subject_id: str,
        now: datetime,
    ) -> frozenset[str]:
        relationships = await self._identity.list_relationships(
            person_id=actor_subject_id,
            statuses=("active",),
            actor_person_id=actor_subject_id,
        )
        owners: set[str] = set()
        for relationship in relationships:
            if (
                "legacy.access" not in relationship.permissions
                or not _active_at(
                    relationship.valid_from,
                    relationship.valid_until,
                    now,
                )
            ):
                continue
            if relationship.source_person_id == actor_subject_id:
                owners.add(relationship.target_person_id)
            elif relationship.target_person_id == actor_subject_id:
                owners.add(relationship.source_person_id)
        return frozenset(owners)


def _active_at(
    valid_from: datetime,
    valid_until: datetime | None,
    now: datetime,
) -> bool:
    return valid_from <= now and (valid_until is None or now < valid_until)


__all__ = ["IdentityRelationshipGrantResolver"]
