"""Shared fixtures and helpers for the split IdentityService tests.

``test_service_persons.py``, ``test_service_bindings.py`` and
``test_service_relationships_and_personas.py`` split the former
``test_service.py`` by behavior; each imports its common fixtures and
builders from here."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from services.identity.in_memory_store import InMemoryIdentityStore
from services.identity.service import IdentityService
from services.identity.testing_authorities import (
    TestTransferAuthority,
)

_TEST_AUTHORITY = TestTransferAuthority(b"test-secret-that-is-at-least-32-bytes")


@pytest.fixture
def store() -> InMemoryIdentityStore:
    # The PostgreSQL adapter has its own contract suite (test_postgres_store.py).
    return InMemoryIdentityStore()


@pytest.fixture
def service(store: InMemoryIdentityStore) -> IdentityService:
    from services.identity.authority import DeterministicConsentSnapshotResolver

    return IdentityService(
        store,
        transfer_verifier=_TEST_AUTHORITY,
        consent_resolver=DeterministicConsentSnapshotResolver(),
    )


def _now() -> datetime:
    return datetime(2026, 8, 9, 10, 0, tzinfo=UTC)


def _receipt(
    *,
    actor: str,
    subject: str,
    device: str,
    binding_id: str,
    binding_version: int,
    expires_at: datetime,
    nonce: str = "step-up-1",
) -> tuple[str, str]:
    return _TEST_AUTHORITY.mint(
        actor_person_id=actor,
        subject_person_id=subject,
        device_id=device,
        binding_id=binding_id,
        binding_version=binding_version,
        expires_at=expires_at,
        nonce=nonce,
    )


async def _register(
    service: IdentityService,
    *,
    name: str,
    category: str = "unknown",
    age_band: str = "unknown",
    evidence: str = "unverified",
    evidence_id: str | None = None,
    now: datetime,
) -> str:
    person = await service.register_person(
        display_name=name,
        timezone="Asia/Shanghai",
        subject_category=category,
        age_band=age_band,
        age_evidence_status=evidence,
        age_evidence_id=(
            evidence_id
            if evidence_id is not None
            else f"evidence-{name}" if category == "adult" else None
        ),
        now=now,
    )
    return person.person_id


async def _establish_guardian(
    service: IdentityService,
    *,
    guardian: str,
    ward: str,
    now: datetime,
) -> str:
    """Two-party confirmed guardian_of relationship (binding prerequisite)."""
    proposed = await service.propose_relationship(
        source_person_id=guardian,
        target_person_id=ward,
        relation_type="guardian_of",
        established_evidence_id="evidence-guardian",
        actor_person_id=guardian,
        now=now,
    )
    await service.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=guardian,
        now=now,
    )
    active = await service.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=ward,
        now=now,
    )
    return active.relationship_id


async def _establish_relationship(
    service: IdentityService,
    *,
    relation_type: str,
    source: str,
    target: str,
    now: datetime,
) -> str:
    proposed = await service.propose_relationship(
        source_person_id=source,
        target_person_id=target,
        relation_type=relation_type,  # type: ignore[arg-type]
        established_evidence_id=f"evidence-{relation_type}",
        actor_person_id=source,
        now=now,
    )
    await service.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=source,
        now=now,
    )
    active = await service.confirm_relationship(
        relationship_id=proposed.relationship_id,
        person_id=target,
        now=now,
    )
    return active.relationship_id


async def _adult(service: IdentityService, name: str, now: datetime) -> str:
    return await _register(
        service,
        name=name,
        category="adult",
        age_band="adult",
        evidence="verified",
        now=now,
    )


async def _minor(service: IdentityService, name: str, now: datetime) -> str:
    return await _register(
        service,
        name=name,
        category="minor",
        age_band="under_14",
        evidence="verified",
        now=now,
    )
