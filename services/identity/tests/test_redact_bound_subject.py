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
    # No age band survives either, and the row no longer reads as a child.
    assert (redacted.subject_category, redacted.age_band) == ("unknown", "unknown")
    assert redacted.age_evidence_status == "unverified"
    stored = await identity.get_person(child_id, actor_person_id="parent")
    assert stored.display_name == REDACTED_DISPLAY_NAME
    assert (stored.subject_category, stored.age_band) == ("unknown", "unknown")
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


@pytest.mark.asyncio
async def test_redaction_never_clears_an_adult_member(tmp_path: Path) -> None:
    """Only the child a device serves is cleared, never a verified adult."""

    store = InMemoryIdentityStore()
    identity = IdentityService(
        store,
        transfer_verifier=_TEST_AUTHORITY,
        consent_resolver=DeterministicConsentSnapshotResolver(),
    )
    owner = await identity.register_person(
        person_id="owner",
        display_name="家长",
        timezone="Asia/Shanghai",
        subject_category="adult",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="fixture-owner",
        now=NOW,
    )
    elder = await identity.register_person(
        person_id="elder",
        display_name="外婆",
        timezone="Asia/Shanghai",
        subject_category="adult",
        age_band="adult",
        age_evidence_status="verified",
        age_evidence_id="fixture-elder",
        actor_person_id=owner.person_id,
        now=NOW,
    )
    await identity.attest_binding_relationship(
        source_person_id=owner.person_id,
        target_person_id=elder.person_id,
        relation_type="guardian_of",
        actor_person_id=owner.person_id,
        now=NOW,
    )

    with pytest.raises(IdentityAccessDeniedError, match="adult"):
        await identity.redact_bound_subject(
            person_id=elder.person_id, actor_person_id=owner.person_id, now=NOW
        )

    unchanged = await identity.get_person(elder.person_id, actor_person_id=owner.person_id)
    assert (unchanged.display_name, unchanged.subject_category) == ("外婆", "adult")
    assert (unchanged.age_band, unchanged.status) == ("adult", "active")


@pytest.mark.asyncio
async def test_redaction_is_idempotent_and_keeps_audit_content_free(tmp_path: Path) -> None:
    store = InMemoryIdentityStore()
    identity, child_id, device_id = await _bound_child(tmp_path, store)
    await identity.revoke_binding(device_id=device_id, actor_person_id="parent", reason="unbind")

    first = await identity.redact_bound_subject(
        person_id=child_id, actor_person_id="parent", now=NOW
    )
    second = await identity.redact_bound_subject(
        person_id=child_id, actor_person_id="parent", now=NOW
    )

    assert first == second
    audit = [
        event
        for event in store.audit_events()
        if child_id in (event.person_id, event.subject_person_id)
    ]
    assert audit, "the audit trail itself is kept"
    # The name is scrubbed wherever the trail carried it; the binding rows
    # keep only ids and times. Older snapshots are reported, never rewritten
    # (issue #283), so this asserts the name alone.
    for event in audit:
        assert "小明" not in str(event.payload)
    binding_rows = [event for event in audit if event.binding_id is not None]
    assert binding_rows, "binding audit rows survive the unbind"
    for event in binding_rows:
        # Ids and times only: no name and no age band in the binding trail.
        assert "display_name" not in event.payload
        assert "age_band" not in event.payload
        assert "subject_category" not in event.payload


@pytest.mark.asyncio
async def test_a_child_a_second_binding_still_serves_is_not_cleared(tmp_path: Path) -> None:
    """The name is still needed while any binding of the account serves them."""

    store = InMemoryIdentityStore()
    identity, child_id, first_device = await _bound_child(tmp_path, store)
    second_device = await identity.create_binding(
        device_id="device-2",
        declared_mode="parent_for_child",
        account_owner_person_id="parent",
        primary_subject_ids=(child_id,),
        roles=(("parent", "guardian"), ("parent", "device_admin")),
        service_profile_version="parent_for_child-v1",
        policy_bundle_version="multi-subject-v1",
        consent_offer_ids=("offer_minor_voice_session_v1",),
        actor_person_id="parent",
        now=NOW,
    )
    await identity.revoke_binding(
        device_id=first_device, actor_person_id="parent", reason="unbind"
    )

    # One binding still serves them, so the name is still needed.
    with pytest.raises(RelationshipLifecycleError, match="still serves"):
        await identity.redact_bound_subject(
            person_id=child_id, actor_person_id="parent", now=NOW
        )
    assert (
        await identity.get_person(child_id, actor_person_id="parent")
    ).display_name == "小明"

    await identity.revoke_binding(
        device_id=second_device.device_id, actor_person_id="parent", reason="unbind"
    )
    cleared = await identity.redact_bound_subject(
        person_id=child_id, actor_person_id="parent", now=NOW
    )
    assert cleared.display_name == REDACTED_DISPLAY_NAME
