"""The control path confirms the one person a one-to-one binding serves.

Product decision (2026-09-25): a device serves exactly one bound person and
voiceprint is out, so the app/control Session confirms that person itself
instead of leaving every mini program profile ``unconfirmed``/``unknown_safe``.
The confirmation is recorded honestly (``sole_bound_subject``), never as an
app confirmation or a voice match, and Policy/Consent still decide every
capability: the app user only acts for the subject when they are the subject.

These tests drive the real Session, Consent and Policy authorities in
PostgreSQL and record the resulting profile for each binding mode.
"""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    RuntimeProfileSignedV2,
)
from services.consent.bound_subject import (
    GUARDIAN_MEMORY_CAPABILITIES,
    MEMORY_CAPABILITIES,
    MINOR_SESSION_CAPABILITIES,
    BoundSubjectGrant,
)
from services.consent.evidence import ConsentParams
from services.consent.tests.test_bound_subject_postgres import _attest, _consent_service
from services.control_api.app.multi_subject_runtime import (
    PostgresMultiSubjectRuntimeControl,
)
from services.identity.domain import BindingManifest, ManifestRole
from services.policy.engine import PolicyEngine
from services.session_runtime.postgres_store import PostgresSessionRuntimeStore
from services.session_runtime.service import build_postgres_session_runtime_service
from services.session_runtime.subject_resolver import (
    SOLE_BOUND_SUBJECT_REASON,
    BindingSnapshot,
    sole_bound_subject_id,
)
from services.session_runtime.tests.test_postgres_store import (
    _SIGNING_KEY,
    _seed_binding,
    _seed_delegated_binding,
    _seed_verified_device,
    postgres_runtime,  # noqa: F401 - pytest discovers imported fixtures by name
    postgres_runtime_with_consent,  # noqa: F401 - pytest discovers imported fixtures by name
)

requires_postgres = pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the real Session authority",
)

#: The mini program's gated entries (utils/device-binding.js SENSITIVE_ENTRIES).
_GATED = frozenset(
    {
        "memory_recall_private",
        "digital_self_preview",
        "guardian_summary_view",
        "raw_audio_retention",
        "voice_profile_create",
        "voice_clone_use",
    }
)


def _snapshot(mode: str, primary: tuple[str, ...]) -> BindingSnapshot:
    return BindingSnapshot(
        binding_id="binding",
        device_id="device",
        binding_version=1,
        declared_mode=mode,  # type: ignore[arg-type]
        primary_subject_ids=primary,
        member_subject_ids=primary,
    )


def test_only_one_to_one_bindings_have_a_sole_subject() -> None:
    assert sole_bound_subject_id(_snapshot("self_use", ("me",))) == "me"
    assert sole_bound_subject_id(_snapshot("parent_for_child", ("kid",))) == "kid"
    assert sole_bound_subject_id(_snapshot("child_for_parent", ("elder",))) == "elder"
    assert sole_bound_subject_id(_snapshot("family_shared", ("kid",))) is None
    assert sole_bound_subject_id(_snapshot("parent_for_child", ("a", "b"))) is None
    assert sole_bound_subject_id(_snapshot("self_use", ())) is None


def _manifest(
    *,
    mode: str,
    owner: str,
    subject: str,
    device_id: str,
    binding_id: str,
    guardian: bool = False,
) -> BindingManifest:
    now = datetime.now(UTC)
    roles = [
        ManifestRole(owner, "account_owner", ("binding.manage",)),
        ManifestRole(owner, "device_admin", ("binding.manage",)),
        ManifestRole(subject, "primary_subject", ("content.read",)),
    ]
    if subject != owner:
        roles.append(ManifestRole(subject, "member", ("family.shared.read",)))
    if guardian:
        roles.append(ManifestRole(owner, "guardian", ("guardian.manage",)))
    return BindingManifest(
        binding_id=binding_id,
        device_id=device_id,
        declared_mode=mode,  # type: ignore[arg-type]
        binding_version=1,
        status="active",
        reason="create",
        supersedes_binding_id=None,
        family_space_id=f"family-{binding_id}",
        account_owner_id=owner,
        device_admin_ids=(owner,),
        primary_subject_ids=(subject,),
        guardian_ids=(owner,) if guardian else (),
        delegate_ids=(),
        emergency_contact_ids=(),
        member_ids=() if subject == owner else (subject,),
        roles=tuple(roles),
        service_profile_version=f"{mode}-v1",
        policy_bundle_version="multi-subject-v2",
        consent_snapshot_id=None,
        persona_assignment_id="starlight:v1",
        valid_from=now,
        valid_until=None,
        created_at=now,
    )


class _IdentityView:
    """Identity as production exposes it: FORCE RLS, no actor means no rows."""

    def __init__(self, manifest: BindingManifest) -> None:
        self.manifest = manifest

    async def get_active_manifest(
        self,
        device_id: str,
        _now: datetime | None = None,
        actor_person_id: str | None = None,
    ) -> BindingManifest | None:
        if actor_person_id is None or device_id != self.manifest.device_id:
            return None
        return self.manifest

    async def list_persona_assignments(
        self,
        *,
        binding_id: str,
        actor_person_id: str | None = None,
    ) -> tuple[object, ...]:
        return ()


def _control(
    store: PostgresSessionRuntimeStore, manifest: BindingManifest
) -> PostgresMultiSubjectRuntimeControl:
    return PostgresMultiSubjectRuntimeControl(
        identity=_IdentityView(manifest),  # type: ignore[arg-type]
        sessions=build_postgres_session_runtime_service(
            store=store,
            signing_key=_SIGNING_KEY,
            policy=PolicyEngine(),
        ),
    )


def _shape(profile: RuntimeProfileSignedV2) -> dict[str, Any]:
    return {
        "active_subject_id": profile.active_subject_id,
        "speaker_state": profile.speaker_state.value,
        "service_mode": profile.service_mode.value,
        "capabilities": sorted(item.value for item in profile.capabilities),
    }


async def _event_reasons(bootstrap_dsn: str, session_id: str) -> list[str]:
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        rows = await admin.fetch(
            "SELECT COALESCE(payload_json -> 'payload' ->> 'reason_code', "
            "payload_json ->> 'reason_code') AS reason "
            "FROM session_runtime_events WHERE session_id = $1 "
            "ORDER BY event_sequence",
            session_id,
        )
    finally:
        await admin.close()
    return [str(row["reason"]) for row in rows]


async def _read(
    control: PostgresMultiSubjectRuntimeControl,
    manifest: BindingManifest,
    *,
    now: datetime | None = None,
) -> RuntimeProfileSignedV2:
    return await control.ensure_profile(
        device_id=manifest.device_id,
        session_id=None,
        actor_id=manifest.account_owner_id,
        now=now or datetime.now(UTC),
    )


@requires_postgres
@pytest.mark.asyncio
async def test_self_use_owner_is_confirmed_and_policy_decides_memory(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811
) -> None:
    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        # A verified adult (WeChat phone registration) and a device whose
        # certificate + attestation are current.
        await _seed_binding(admin, actor_id="owner-1", device_id="dev-self", binding_id="b-self")
        await _seed_verified_device(admin, device_id="dev-self", binding_id="b-self", now=now)
    finally:
        await admin.close()
    manifest = _manifest(
        mode="self_use",
        owner="owner-1",
        subject="owner-1",
        device_id="dev-self",
        binding_id="b-self",
    )
    control = _control(store, manifest)

    no_consent = await _read(control, manifest)
    assert _shape(no_consent) == {
        "active_subject_id": "owner-1",
        "speaker_state": "confirmed",
        "service_mode": "adult_companion",
        "capabilities": ["chat", "english_practice", "tutor"],
    }
    assert await _event_reasons(bootstrap_dsn, no_consent.session_id) == [SOLE_BOUND_SUBJECT_REASON]

    consent, consent_store = await _consent_service(bootstrap_dsn)
    try:
        # offer_self_memory_retention_v1 accepted at binding.
        await consent.grant(
            BoundSubjectGrant(
                actor_person_id="owner-1",
                subject_person_id="owner-1",
                binding_id="b-self",
                kind="subject",
                capabilities=MEMORY_CAPABILITIES,
                source_key="snapshot-self",
            )
        )
    finally:
        await consent_store.close()

    # The standing profile is reused inside its TTL; the renewal re-confirms
    # the owner and re-derives capabilities, now including private memory.
    renewed = await _read(control, manifest, now=now + timedelta(minutes=6))
    assert renewed.session_id == no_consent.session_id
    assert renewed.session_epoch == no_consent.session_epoch + 1
    assert _shape(renewed) == {
        "active_subject_id": "owner-1",
        "speaker_state": "confirmed",
        "service_mode": "adult_companion",
        "capabilities": ["chat", "english_practice", "memory_recall_private", "tutor"],
    }
    assert set(_shape(renewed)["capabilities"]) & _GATED == {"memory_recall_private"}
    assert (await _event_reasons(bootstrap_dsn, renewed.session_id))[-1] == (
        SOLE_BOUND_SUBJECT_REASON
    )


@requires_postgres
@pytest.mark.asyncio
async def test_self_use_memory_needs_a_trusted_device(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811
) -> None:
    store, bootstrap_dsn = postgres_runtime_with_consent
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_binding(
            admin, actor_id="owner-2", device_id="dev-untrusted", binding_id="b-untrusted"
        )
    finally:
        await admin.close()
    consent, consent_store = await _consent_service(bootstrap_dsn)
    try:
        await consent.grant(
            BoundSubjectGrant(
                actor_person_id="owner-2",
                subject_person_id="owner-2",
                binding_id="b-untrusted",
                kind="subject",
                capabilities=MEMORY_CAPABILITIES,
                source_key="snapshot-untrusted",
            )
        )
    finally:
        await consent_store.close()
    manifest = _manifest(
        mode="self_use",
        owner="owner-2",
        subject="owner-2",
        device_id="dev-untrusted",
        binding_id="b-untrusted",
    )
    profile = await _read(_control(store, manifest), manifest)
    # Confirmed, but Policy withholds private memory without device attestation.
    assert _shape(profile) == {
        "active_subject_id": "owner-2",
        "speaker_state": "confirmed",
        "service_mode": "adult_companion",
        "capabilities": ["chat", "english_practice", "tutor"],
    }


@requires_postgres
@pytest.mark.asyncio
async def test_self_use_owner_without_verified_age_stays_unconfirmed(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811
) -> None:
    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_binding(
            admin, actor_id="owner-3", device_id="dev-unknown", binding_id="b-unknown"
        )
        await _seed_verified_device(admin, device_id="dev-unknown", binding_id="b-unknown", now=now)
        # No verified adult evidence (e.g. never completed WeChat phone login).
        await admin.execute(
            "UPDATE identity_persons SET subject_category = 'unknown', "
            "age_band = 'unknown', age_evidence_status = 'unverified' "
            "WHERE person_id = 'owner-3'"
        )
    finally:
        await admin.close()
    manifest = _manifest(
        mode="self_use",
        owner="owner-3",
        subject="owner-3",
        device_id="dev-unknown",
        binding_id="b-unknown",
    )
    profile = await _read(_control(store, manifest), manifest)
    assert _shape(profile) == {
        "active_subject_id": None,
        "speaker_state": "unconfirmed",
        "service_mode": "unknown_safe",
        "capabilities": ["chat", "english_practice", "tutor"],
    }
    assert await _event_reasons(bootstrap_dsn, profile.session_id) == ["subject_facts_unverified"]


@requires_postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_params", "expected_limits"),
    [
        # No bind-form values: the policy defaults.
        (None, (1800, ["21:30", "06:30"])),
        # The parent's session length and quiet hours (P0-04 D3).
        ((2700, ("21:00", "07:00")), (2700, ["21:00", "07:00"])),
    ],
)
async def test_parent_for_child_opens_the_summary_but_the_guardian_never_reads_memory(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811
    session_params: tuple[int, tuple[str, str]] | None,
    expected_limits: tuple[int, list[str]],
) -> None:
    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id="parent-1",
            subject_id="child-1",
            device_id="dev-child",
            binding_id="b-child",
            declared_mode="parent_for_child",
            subject_category="minor",
            age_band="under_14",
        )
        await _attest(admin, source="parent-1", target="child-1", relation_type="guardian_of")
        await _seed_verified_device(admin, device_id="dev-child", binding_id="b-child", now=now)
    finally:
        await admin.close()
    now = datetime.now(UTC)
    manifest = _manifest(
        mode="parent_for_child",
        owner="parent-1",
        subject="child-1",
        device_id="dev-child",
        binding_id="b-child",
        guardian=True,
    )
    control = _control(store, manifest)

    # No guardian consent: the child is confirmed, and Policy grants nothing.
    without = await _read(control, manifest, now=now)
    assert _shape(without) == {
        "active_subject_id": "child-1",
        "speaker_state": "confirmed",
        "service_mode": "student_minor",
        "capabilities": [],
    }

    consent, consent_store = await _consent_service(bootstrap_dsn)
    try:
        grant = BoundSubjectGrant(
            actor_person_id="parent-1",
            subject_person_id="child-1",
            binding_id="b-child",
            kind="guardian",
            capabilities=MINOR_SESSION_CAPABILITIES,
            source_key="snapshot-child",
            params=(
                ConsentParams(
                    max_session_seconds=session_params[0], quiet_hours=session_params[1]
                )
                if session_params
                else ConsentParams()
            ),
        )
        await consent.grant(grant)
        # Ticking long-term memory for a child also grants the weekly summary.
        await consent.grant(replace(grant, capabilities=GUARDIAN_MEMORY_CAPABILITIES))
    finally:
        await consent_store.close()

    with_consent = await _read(control, manifest, now=now + timedelta(minutes=6))
    # The guardian's app is not the child talking: the guardian's consent
    # lifts chat/practice and the summary, never the child's tutoring or
    # private memory.
    assert _shape(with_consent) == {
        "active_subject_id": "child-1",
        "speaker_state": "confirmed",
        "service_mode": "student_minor",
        "capabilities": ["chat", "english_practice", "guardian_summary_view"],
    }
    assert set(_shape(with_consent)["capabilities"]) & _GATED == {"guardian_summary_view"}
    limits = {
        item.code.value: item.params for item in with_consent.obligations
        if item.code.value in {"MAX_SESSION_SECONDS", "QUIET_HOURS"}
    }
    assert limits["MAX_SESSION_SECONDS"].max_session_seconds == expected_limits[0]
    assert list(limits["QUIET_HOURS"].quiet_hours) == expected_limits[1]


@requires_postgres
@pytest.mark.asyncio
async def test_child_for_parent_confirms_the_elder_without_their_private_memory(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811
) -> None:
    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id="son-1",
            subject_id="elder-1",
            device_id="dev-elder",
            binding_id="b-elder",
            declared_mode="child_for_parent",
            subject_category="adult",
            age_band="adult",
        )
        await _attest(admin, source="son-1", target="elder-1", relation_type="delegate_for")
        await _seed_verified_device(admin, device_id="dev-elder", binding_id="b-elder", now=now)
    finally:
        await admin.close()
    consent, consent_store = await _consent_service(bootstrap_dsn)
    try:
        # offer_senior_memory_retention_v1: the adult child's proxy consent.
        await consent.grant(
            BoundSubjectGrant(
                actor_person_id="son-1",
                subject_person_id="elder-1",
                binding_id="b-elder",
                kind="delegate",
                capabilities=MEMORY_CAPABILITIES,
                source_key="snapshot-elder",
            )
        )
    finally:
        await consent_store.close()
    manifest = _manifest(
        mode="child_for_parent",
        owner="son-1",
        subject="elder-1",
        device_id="dev-elder",
        binding_id="b-elder",
    )
    profile = await _read(_control(store, manifest), manifest)
    assert _shape(profile) == {
        "active_subject_id": "elder-1",
        "speaker_state": "confirmed",
        "service_mode": "senior_companion",
        "capabilities": ["chat", "english_practice"],
    }


@requires_postgres
@pytest.mark.asyncio
async def test_family_shared_stays_unconfirmed(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811
) -> None:
    store, bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id="parent-2",
            subject_id="kid-2",
            device_id="dev-family",
            binding_id="b-family",
            declared_mode="family_shared",
            subject_category="minor",
            age_band="under_14",
        )
        await _seed_verified_device(admin, device_id="dev-family", binding_id="b-family", now=now)
    finally:
        await admin.close()
    now = datetime.now(UTC)
    manifest = _manifest(
        mode="family_shared",
        owner="parent-2",
        subject="kid-2",
        device_id="dev-family",
        binding_id="b-family",
        guardian=True,
    )
    control = _control(store, manifest)
    profile = await _read(control, manifest, now=now)
    assert _shape(profile) == {
        "active_subject_id": None,
        "speaker_state": "unconfirmed",
        "service_mode": "unknown_safe",
        "capabilities": ["chat", "english_practice", "tutor"],
    }
    renewed = await _read(control, manifest, now=now + timedelta(minutes=6))
    assert renewed.active_subject_id is None
    assert await _event_reasons(bootstrap_dsn, profile.session_id) == [
        "binding_not_one_to_one",
        "binding_not_one_to_one",
    ]


@requires_postgres
@pytest.mark.asyncio
async def test_a_proxy_without_an_active_relationship_leaves_the_subject_unconfirmed(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811
) -> None:
    store, bootstrap_dsn = postgres_runtime_with_consent
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        # A parent_for_child binding whose guardianship was never attested
        # (or is still a pending one-sided declaration).
        await _seed_delegated_binding(
            admin,
            actor_id="parent-9",
            subject_id="child-9",
            device_id="dev-unattested",
            binding_id="b-unattested",
            declared_mode="parent_for_child",
            subject_category="minor",
            age_band="under_14",
        )
        await _seed_verified_device(
            admin, device_id="dev-unattested", binding_id="b-unattested", now=datetime.now(UTC)
        )
    finally:
        await admin.close()
    manifest = _manifest(
        mode="parent_for_child",
        owner="parent-9",
        subject="child-9",
        device_id="dev-unattested",
        binding_id="b-unattested",
        guardian=True,
    )
    profile = await _read(_control(store, manifest), manifest)
    assert _shape(profile) == {
        "active_subject_id": None,
        "speaker_state": "unconfirmed",
        "service_mode": "unknown_safe",
        "capabilities": ["chat", "english_practice", "tutor"],
    }
    assert await _event_reasons(bootstrap_dsn, profile.session_id) == [
        "sole_bound_subject_relationship_missing"
    ]
