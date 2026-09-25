"""Bound-subject grants resolve their evidence from the real Identity service."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from services.consent.authority import SubjectProof
from services.consent.bound_subject import (
    MEMORY_CAPABILITIES,
    MINOR_SESSION_CAPABILITIES,
    BoundSubjectConsentService,
    BoundSubjectGrant,
    IdentityEvidenceResolver,
)
from services.consent.evidence import BindingEvidence
from services.consent.in_memory_store import InMemoryConsentStore
from services.identity.authority import DeterministicConsentSnapshotResolver
from services.identity.domain import AgeEvidenceError, IdentityAccessDeniedError
from services.identity.service import IdentityService
from services.identity.sqlite_store import SqliteIdentityStore
from services.identity.tests.test_service import _TEST_AUTHORITY

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)


class _AsyncUow:
    def __init__(self, uow: Any) -> None:
        self._uow = uow

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._uow, name)

        async def call(*args: Any, **kwargs: Any) -> Any:
            return method(*args, **kwargs)

        return call


class _AsyncStore:
    """The sync in-memory consent store behind the async authority port."""

    def __init__(self) -> None:
        self._store = InMemoryConsentStore()

    async def transaction(self) -> Any:
        return _AsyncUow(self._store.transaction())

    async def close(self) -> None:
        return None


async def _identity(tmp_path: Path) -> IdentityService:
    return IdentityService(
        SqliteIdentityStore(tmp_path / "identity.sqlite3"),
        transfer_verifier=_TEST_AUTHORITY,
        consent_resolver=DeterministicConsentSnapshotResolver(),
    )


async def _parent_and_child(identity: IdentityService) -> tuple[str, str, str]:
    await identity.register_person(
        person_id="parent",
        display_name="家长",
        timezone="Asia/Shanghai",
        subject_category="adult",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="fixture-parent",
        now=NOW,
    )
    child = await identity.register_person(
        display_name="孩子",
        timezone="Asia/Shanghai",
        subject_category="minor",
        age_band="under_14",
        age_evidence_status="unverified",
        actor_person_id="parent",
        now=NOW,
    )
    await identity.attest_binding_relationship(
        source_person_id="parent",
        target_person_id=child.person_id,
        relation_type="guardian_of",
        actor_person_id="parent",
        now=NOW,
    )
    manifest = await identity.create_binding(
        device_id="device-1",
        declared_mode="parent_for_child",
        account_owner_person_id="parent",
        primary_subject_ids=(child.person_id,),
        roles=(("parent", "guardian"), ("parent", "device_admin")),
        service_profile_version="parent_for_child-v1",
        policy_bundle_version="multi-subject-v1",
        consent_offer_ids=("offer_minor_voice_session_v1", "offer_minor_memory_retention_v1"),
        service_preferences={"memory_level": "growth_summary"},
        actor_person_id="parent",
        now=NOW,
    )
    return "parent", child.person_id, manifest.binding_id


@pytest.mark.asyncio
async def test_attestation_is_active_on_the_owner_side_only(tmp_path: Path) -> None:
    identity = await _identity(tmp_path)
    _, child_id, _ = await _parent_and_child(identity)

    (relationship,) = await identity.list_relationships(person_id=child_id)
    assert relationship.status == "active"
    assert relationship.confirmed_by_source_at is not None
    assert relationship.confirmed_by_target_at is None
    assert relationship.established_evidence_id == "guardian_attestation_v1:device_binding"
    # Crisis notification still reaches the guardian who attested.
    assert await identity.declared_guardians(subject_person_id=child_id, now=NOW) == ("parent",)


@pytest.mark.asyncio
async def test_only_a_verified_adult_may_attest_for_themself(tmp_path: Path) -> None:
    identity = await _identity(tmp_path)
    await identity.register_person(
        person_id="unverified",
        display_name="未核验",
        timezone="Asia/Shanghai",
        now=NOW,
    )
    target = await identity.register_person(
        display_name="孩子", timezone="Asia/Shanghai", actor_person_id="unverified", now=NOW
    )
    with pytest.raises(IdentityAccessDeniedError):
        await identity.attest_binding_relationship(
            source_person_id="unverified",
            target_person_id=target.person_id,
            relation_type="guardian_of",
            actor_person_id="someone-else",
            now=NOW,
        )
    with pytest.raises(AgeEvidenceError):
        await identity.attest_binding_relationship(
            source_person_id="unverified",
            target_person_id=target.person_id,
            relation_type="guardian_of",
            actor_person_id="unverified",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_resolver_rereads_identity_instead_of_trusting_the_candidate(
    tmp_path: Path,
) -> None:
    identity = await _identity(tmp_path)
    parent, child_id, binding_id = await _parent_and_child(identity)
    resolver = IdentityEvidenceResolver(identity, actor_person_id=parent)

    subject = await resolver.resolve_subject(
        SubjectProof(subject_id=child_id, subject_category="adult", age_evidence_status="verified")
    )
    assert (subject.subject_category, subject.age_evidence_status) == ("minor", "unverified")

    binding = await identity.get_binding(binding_id, actor_person_id=parent)
    candidate = BindingEvidence(
        binding_id=binding_id,
        version=binding.binding_version,
        device_id="device-1",
        status="active",
        declared_mode="self_use",
        valid_from=NOW,
        valid_until=NOW.replace(year=2030),
    )
    resolved = await resolver.resolve_binding(candidate, child_id)
    assert resolved.declared_mode == "parent_for_child"
    with pytest.raises(PermissionError):
        await resolver.resolve_binding(candidate, parent)

    relationships = await resolver.resolve_relationships((), parent, child_id, binding_id)
    assert [(item.relation_type, item.status) for item in relationships] == [
        ("guardian_of", "active")
    ]


@pytest.mark.asyncio
async def test_grant_replay_and_revoke_for_the_bound_child(tmp_path: Path) -> None:
    identity = await _identity(tmp_path)
    parent, child_id, binding_id = await _parent_and_child(identity)
    service = BoundSubjectConsentService(
        store=_AsyncStore(),  # type: ignore[arg-type]
        identity=identity,
        now=lambda: NOW,
    )
    grant = BoundSubjectGrant(
        actor_person_id=parent,
        subject_person_id=child_id,
        binding_id=binding_id,
        kind="guardian",
        capabilities=(*MINOR_SESSION_CAPABILITIES, *MEMORY_CAPABILITIES),
        source_key="snapshot-1",
    )
    granted = await service.grant(grant)
    replayed = await service.grant(grant)

    assert {item.capability for item in granted} == {
        *MINOR_SESSION_CAPABILITIES,
        *MEMORY_CAPABILITIES,
    }
    assert {item.consent_id for item in replayed} == {item.consent_id for item in granted}
    assert {item.actor_kind for item in granted} == {"guardian"}

    withdrawn = await service.revoke(
        actor_person_id=parent,
        subject_person_id=child_id,
        binding_id=binding_id,
        capabilities=MEMORY_CAPABILITIES,
        reason="guardian_toggle_off",
    )
    assert withdrawn == len(MEMORY_CAPABILITIES)
    remaining = await service.active(
        subject_person_id=child_id, binding_id=binding_id, binding_version=1
    )
    assert {item.capability for item in remaining} == set(MINOR_SESSION_CAPABILITIES)


def test_only_a_subject_grant_names_the_subject_as_actor() -> None:
    with pytest.raises(ValueError, match="subject grant"):
        BoundSubjectGrant(
            actor_person_id="parent",
            subject_person_id="parent",
            binding_id="binding",
            kind="guardian",
            capabilities=MEMORY_CAPABILITIES,
            source_key="key",
        )
    with pytest.raises(ValueError, match="at least one"):
        BoundSubjectGrant(
            actor_person_id="parent",
            subject_person_id="child",
            binding_id="binding",
            kind="guardian",
            capabilities=(),
            source_key="key",
        )
