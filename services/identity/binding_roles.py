"""Relationship checks behind parent_for_child binding roles."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Protocol


class _RelationshipReader(Protocol):
    async def has_active_relationship(
        self, *, source_person_id: str, target_person_id: str, relation_type: str, at: datetime
    ) -> bool: ...

    async def has_source_confirmed_relationship(
        self, *, source_person_id: str, target_person_id: str, relation_type: str, at: datetime
    ) -> bool: ...


async def parent_emergency_contact_verified(
    store: _RelationshipReader,
    *,
    emergency_id: str,
    account_owner_id: str,
    guardian_ids: set[str],
    subject_ids: Iterable[str],
    at: datetime,
) -> bool:
    """Whether ``emergency_id`` may hold the emergency_contact role.

    An active emergency_contact_for relationship with a primary subject (in
    either direction) always counts. So does the account owner's own
    ``guardian_of`` declaration when the owner is also the binding's guardian:
    that owner is already the account-less child's crisis contact (P0-04 D7,
    ``_CRISIS_CONTACT_LINKS``). Requiring a second emergency_contact_for
    relationship made every parent_for_child binding that accepted the
    emergency-contact offer fail with a binding conflict.
    """
    for subject_id in subject_ids:
        for source, target in ((emergency_id, subject_id), (subject_id, emergency_id)):
            if await store.has_active_relationship(
                source_person_id=source,
                target_person_id=target,
                relation_type="emergency_contact_for",
                at=at,
            ):
                return True
        if (
            emergency_id == account_owner_id
            and emergency_id in guardian_ids
            and await store.has_source_confirmed_relationship(
                source_person_id=emergency_id,
                target_person_id=subject_id,
                relation_type="guardian_of",
                at=at,
            )
        ):
            return True
    return False
