"""A deleted, unbound subject keeps no name — only content-free audit."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.identity.authority import DeterministicConsentSnapshotResolver
from services.identity.domain import IdentityAccessDeniedError, RelationshipLifecycleError
from services.identity.in_memory_store import InMemoryIdentityStore
from services.identity.service import REDACTED_DISPLAY_NAME, IdentityService
from services.identity.tests.test_service import _TEST_AUTHORITY

NOW = datetime.now(UTC)


async def _bound_child(
    tmp_path: Path, store: InMemoryIdentityStore | None = None
) -> tuple[IdentityService, str, str]:
    identity = IdentityService(
        store or InMemoryIdentityStore(),
        transfer_verifier=_TEST_AUTHORITY,
        consent_resolver=DeterministicConsentSnapshotResolver(),
    )
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
        display_name="小明",
        timezone="Asia/Shanghai",
        subject_category="minor",
        age_band="under_14",
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
        consent_offer_ids=("offer_minor_voice_session_v1",),
        actor_person_id="parent",
        now=NOW,
    )
    return identity, child.person_id, manifest.device_id


@pytest.mark.asyncio
async def test_redaction_waits_until_no_device_serves_the_person(tmp_path: Path) -> None:
    store = InMemoryIdentityStore()
    identity, child_id, device_id = await _bound_child(tmp_path, store)

    with pytest.raises(RelationshipLifecycleError, match="still serves"):
        await identity.redact_bound_subject(person_id=child_id, actor_person_id="parent", now=NOW)

    await identity.revoke_binding(device_id=device_id, actor_person_id="parent", reason="unbind")
    redacted = await identity.redact_bound_subject(
        person_id=child_id, actor_person_id="parent", now=NOW
    )

    assert (redacted.display_name, redacted.status) == (REDACTED_DISPLAY_NAME, "disabled")
    stored = await identity.get_person(child_id, actor_person_id="parent")
    assert stored.display_name == REDACTED_DISPLAY_NAME
    payloads = [
        event.payload
        for event in store.audit_events()
        if child_id in (event.person_id, event.subject_person_id)
    ]
    assert payloads, "the audit trail itself is kept"
    assert all(item.get("display_name", REDACTED_DISPLAY_NAME) != "小明" for item in payloads)


@pytest.mark.asyncio
async def test_only_the_attesting_owner_may_redact(tmp_path: Path) -> None:
    identity, child_id, device_id = await _bound_child(tmp_path)
    await identity.revoke_binding(device_id=device_id, actor_person_id="parent", reason="unbind")

    with pytest.raises(IdentityAccessDeniedError):
        await identity.redact_bound_subject(
            person_id=child_id, actor_person_id="stranger", now=NOW
        )
