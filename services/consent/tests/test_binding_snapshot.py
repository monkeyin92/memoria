from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from services.consent.binding_snapshot import (
    BINDING_OFFER_CATALOG,
    BindingConsentAuthority,
    BindingConsentCommand,
    BindingConsentRole,
    BindingConsentValidationError,
    BindingRelationshipEvidence,
    SqliteBindingConsentStore,
)

NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


def _command(
    *,
    mode: str = "self_use",
    offer_ids: tuple[str, ...] = ("offer_self_memory_retention_v1",),
    preferences: dict[str, object] | None = None,
    relationships: tuple[BindingRelationshipEvidence, ...] = (),
) -> BindingConsentCommand:
    return BindingConsentCommand(
        actor_person_id="person-owner",
        account_owner_person_id="person-owner",
        primary_subject_ids=("person-subject",),
        device_id="device-1",
        binding_id="binding-real-1",
        binding_version=1,
        declared_mode=mode,
        roles=(
            BindingConsentRole(
                person_id="person-owner",
                role="account_owner",
                permissions=("binding.manage", "billing.manage"),
            ),
            BindingConsentRole(
                person_id="person-subject",
                role="primary_subject",
                permissions=("content.read", "memory.private.read"),
            ),
        ),
        consent_offer_ids=offer_ids,
        service_preferences=preferences or {"memory_level": "personal"},
        relationship_evidence=relationships,
        service_profile_version=f"{mode}-v1",
        policy_bundle_version="multi-subject-v1",
        issued_at=NOW,
    )


@pytest.mark.parametrize(
    ("offer_ids", "message"),
    [
        (
            ("offer_unknown_v1",),
            "consent offers are not valid",
        ),
        (
            (
                "offer_self_memory_retention_v1",
                "offer_self_memory_retention_v1",
            ),
            "consent_offer_ids must be unique",
        ),
        (
            (),
            "required consent offers are missing",
        ),
    ],
)
def test_catalog_rejects_unknown_duplicate_and_missing_required_offers(
    offer_ids: tuple[str, ...],
    message: str,
) -> None:
    with pytest.raises(BindingConsentValidationError, match=message):
        BINDING_OFFER_CATALOG.validate(
            declared_mode="self_use",
            consent_offer_ids=offer_ids,
            service_preferences={"memory_level": "personal"},
        )


@pytest.mark.asyncio
async def test_snapshot_binds_real_binding_roles_preferences_and_relationship_revision(
    tmp_path,
) -> None:
    store = SqliteBindingConsentStore(str(tmp_path / "binding-consent.sqlite3"))
    await store.initialize()
    relationship = BindingRelationshipEvidence(
        relationship_id="relationship-1",
        source_person_id="person-owner",
        target_person_id="person-subject",
        relation_type="guardian_of",
        status="active",
        established_evidence_id="relationship-evidence-1",
        revision_at=NOW - timedelta(minutes=1),
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=30),
    )
    authority = BindingConsentAuthority(store)
    command = _command(
        mode="parent_for_child",
        offer_ids=(
            "offer_minor_voice_session_v1",
            "offer_minor_memory_retention_v1",
        ),
        preferences={"memory_level": "growth_summary"},
        relationships=(relationship,),
    )

    snapshot_id = await authority.resolve(command=command)
    snapshot = await authority.get(snapshot_id)

    assert snapshot.binding_id == "binding-real-1"
    assert snapshot.binding_version == 1
    assert snapshot.device_id == "device-1"
    assert snapshot.actor_person_id == "person-owner"
    assert snapshot.account_owner_person_id == "person-owner"
    assert snapshot.primary_subject_ids == ("person-subject",)
    assert snapshot.roles == command.roles
    assert snapshot.consent_offer_ids == command.consent_offer_ids
    assert snapshot.service_preferences == command.service_preferences
    assert snapshot.relationship_evidence == (relationship,)
    assert snapshot.catalog_version == "binding-offers-v1"
    assert snapshot.canonical_hash
    await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "offer_ids", "preferences"),
    [
        (
            "self_use",
            ("offer_self_memory_retention_v1",),
            {"memory_level": "personal"},
        ),
        (
            "parent_for_child",
            ("offer_minor_voice_session_v1",),
            {"memory_level": "ephemeral"},
        ),
        (
            "child_for_parent",
            ("offer_admin_device_management_v1",),
            {"memory_level": "ephemeral"},
        ),
        (
            "family_shared",
            ("offer_family_space_v1",),
            {"memory_level": "ephemeral"},
        ),
    ],
)
async def test_all_declared_modes_persist_replayable_snapshots(
    tmp_path,
    mode: str,
    offer_ids: tuple[str, ...],
    preferences: dict[str, object],
) -> None:
    store = SqliteBindingConsentStore(str(tmp_path / f"{mode}.sqlite3"))
    await store.initialize()
    authority = BindingConsentAuthority(store)
    command = _command(
        mode=mode,
        offer_ids=offer_ids,
        preferences=preferences,
    )

    first = await authority.resolve(command=command)
    replay = await authority.resolve(command=command)

    assert replay == first
    assert (await authority.get(first)).declared_mode == mode
    await store.close()


@pytest.mark.asyncio
async def test_snapshot_is_acceptance_evidence_not_a_capability_grant(tmp_path) -> None:
    store = SqliteBindingConsentStore(str(tmp_path / "no-grant.sqlite3"))
    await store.initialize()
    authority = BindingConsentAuthority(store)

    snapshot = await authority.get(await authority.resolve(command=_command()))

    payload = snapshot.to_canonical_dict()
    assert "grants" not in payload
    assert "capabilities" not in payload
    assert payload["schema_version"] == "binding-consent-snapshot-v1"
    await store.close()
