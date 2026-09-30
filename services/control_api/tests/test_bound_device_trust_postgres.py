"""A bound device with no hardware attestation, against the real authorities.

Production's device has a certificate and an onboarding binding but no
attestation, so Device trust answers ``untrusted`` and Policy withholds every
sensitive capability, memory included.  ``MEMORIA_BOUND_DEVICE_TRUST_ENABLED``
lets onboarding's bound link stand for the memory capabilities and nothing
else.  These tests drive the real Session, Consent and Policy authorities in
PostgreSQL with the switch off and on: off must be exactly today, on must add
memory (and the guardian's summary) and stop there.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from services.consent.bound_subject import (
    GUARDIAN_MEMORY_CAPABILITIES,
    MEMORY_CAPABILITIES,
    MINOR_SESSION_CAPABILITIES,
    BoundSubjectGrant,
)
from services.consent.tests.test_bound_subject_postgres import _attest, _consent_service
from services.control_api.tests.test_control_bound_subject_postgres import (
    _GATED,
    _control,
    _manifest,
    _read,
    _shape,
    requires_postgres,
)
from services.policy.engine import PolicyEngine
from services.session_runtime.postgres_store import PostgresSessionRuntimeStore
from services.session_runtime.service import (
    PersistentSessionDenied,
    StartPersistentSessionCommand,
    SwitchPersistentSubjectCommand,
    build_postgres_session_runtime_service,
)
from services.session_runtime.tests.test_postgres_store import (
    _DEVICE_ONBOARDING_SCHEMA_SQL,
    _SIGNING_KEY,
    _seed_binding,
    _seed_delegated_binding,
    _seed_self_consent,
    postgres_runtime,  # noqa: F401 - pytest discovers imported fixtures by name
    postgres_runtime_with_consent,  # noqa: F401 - pytest discovers imported fixtures by name
)


async def _onboard(
    admin: asyncpg.Connection,
    *,
    device_id: str,
    binding_id: str,
    actor_id: str,
    binding_version: int = 1,
) -> None:
    """Onboarding's record of the device: activated with its key, bound, no attestation."""

    now = datetime.now(UTC)
    # Installed after the Session schema, as on a fresh volume: this file's own
    # grant is what lets the trust port read the row.
    await admin.execute(_DEVICE_ONBOARDING_SCHEMA_SQL)
    await admin.execute("RESET ROLE")
    await admin.execute(
        """
        INSERT INTO device_onboarding_devices (
            device_id, certificate_id, public_key_b64, product_model,
            hardware_revision, firmware_version, firmware_security_version,
            capability_manifest_hash, minimum_firmware_security_version,
            lifecycle_status, binding_id, binding_version, actor_id,
            activation_version, created_at, updated_at
        ) VALUES ($1, $2, $3, 'memoria-esp-vocat', 'r1', '2.4.2', 1, $4, 1,
                  'bound', $5, $6, $7, 3, $8, $8)
        """,
        device_id,
        f"cert-{device_id}",
        "A" * 43,
        hashlib.sha256(f"manifest:{device_id}".encode()).hexdigest(),
        binding_id,
        binding_version,
        actor_id,
        now,
    )


@requires_postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("opted_in", [False, True])
async def test_an_adults_bound_device_gets_memory_only_when_the_deployment_opted_in(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811
    opted_in: bool,
) -> None:
    store, bootstrap_dsn = postgres_runtime_with_consent
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_binding(admin, actor_id="owner-b1", device_id="dev-b1", binding_id="b-b1")
        await _onboard(admin, device_id="dev-b1", binding_id="b-b1", actor_id="owner-b1")
    finally:
        await admin.close()
    consent, consent_store = await _consent_service(bootstrap_dsn)
    try:
        await consent.grant(
            BoundSubjectGrant(
                actor_person_id="owner-b1",
                subject_person_id="owner-b1",
                binding_id="b-b1",
                kind="subject",
                capabilities=MEMORY_CAPABILITIES,
                source_key="snapshot-b1",
            )
        )
    finally:
        await consent_store.close()
    manifest = _manifest(
        mode="self_use", owner="owner-b1", subject="owner-b1",
        device_id="dev-b1", binding_id="b-b1",
    )

    profile = await _read(
        _control(store, manifest, accept_bound_device_trust=opted_in), manifest
    )

    shape = _shape(profile)
    assert shape["active_subject_id"] == "owner-b1"
    assert shape["capabilities"] == (
        ["chat", "english_practice", "memory_recall_private", "tutor"]
        if opted_in
        else ["chat", "english_practice", "tutor"]
    )
    assert set(shape["capabilities"]) & _GATED <= {"memory_recall_private"}


@requires_postgres
@pytest.mark.asyncio
@pytest.mark.parametrize("opted_in", [False, True])
async def test_a_childs_device_and_the_guardians_app_follow_the_bound_link(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811
    opted_in: bool,
) -> None:
    # Production's shape: the parent's account owns the binding, the child is
    # the person the device serves, and the guardian ticked long-term memory.
    store, bootstrap_dsn = postgres_runtime_with_consent
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_delegated_binding(
            admin,
            actor_id="parent-b2",
            subject_id="child-b2",
            device_id="dev-b2",
            binding_id="b-b2",
            declared_mode="parent_for_child",
            subject_category="minor",
            age_band="under_14",
        )
        await _attest(admin, source="parent-b2", target="child-b2", relation_type="guardian_of")
        await _onboard(admin, device_id="dev-b2", binding_id="b-b2", actor_id="parent-b2")
    finally:
        await admin.close()
    consent, consent_store = await _consent_service(bootstrap_dsn)
    try:
        for capabilities, key in (
            (MINOR_SESSION_CAPABILITIES, "session"),
            (GUARDIAN_MEMORY_CAPABILITIES, "memory"),
        ):
            await consent.grant(
                BoundSubjectGrant(
                    actor_person_id="parent-b2",
                    subject_person_id="child-b2",
                    binding_id="b-b2",
                    kind="guardian",
                    capabilities=capabilities,
                    source_key=f"snapshot-b2-{key}",
                )
            )
    finally:
        await consent_store.close()

    # The robot's own session: it names the child it serves.
    now = datetime.now(UTC)  # after the seeds: a binding is not valid before it exists
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
        policy=PolicyEngine(),
        accept_bound_device_trust=opted_in,
    )
    device_profile = await service.start(
        StartPersistentSessionCommand(
            session_id="device-media-b2",
            actor_id="parent-b2",
            device_id="dev-b2",
            expected_binding_version=1,
            idempotency_key="device-media-b2",
            now=now,
            requested_capabilities=("chat", "memory_recall_private"),
            device_bound_subject_id="child-b2",
        )
    )
    device_caps = sorted(item.value for item in device_profile.capabilities)
    assert device_profile.active_subject_id == "child-b2"
    assert ("memory_recall_private" in device_caps) is opted_in

    # The guardian's app on the same device: the summary, never the child's memory.
    manifest = _manifest(
        mode="parent_for_child", owner="parent-b2", subject="child-b2",
        device_id="dev-b2", binding_id="b-b2", guardian=True,
    )
    app_profile = await _read(
        _control(store, manifest, accept_bound_device_trust=opted_in),
        manifest,
        now=now + timedelta(minutes=1),
    )
    app_caps = _shape(app_profile)["capabilities"]
    assert ("guardian_summary_view" in app_caps) is opted_in
    assert "memory_recall_private" not in app_caps
    assert set(app_caps) & _GATED <= {"guardian_summary_view"}


@requires_postgres
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "purpose", "opted_in", "allowed"),
    [
        # Memory rides on the bound link once the deployment opted in ...
        ("memory_capture", "memory_capture", True, True),
        ("memory_capture", "memory_capture", False, False),
        # ... and nothing else does, whatever consent the person gave.
        ("voice_clone_use", "voice_clone", True, False),
        ("voice_clone_use", "voice_clone", False, False),
    ],
)
async def test_action_time_authorization_keeps_hardware_backed_capabilities_verified_only(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],  # noqa: F811
    capability: str,
    purpose: str,
    opted_in: bool,
    allowed: bool,
) -> None:
    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _onboard(admin, device_id="device-a", binding_id="binding-a", actor_id="actor-a")
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability=capability,
            purpose=purpose,
            now=now,
        )
    finally:
        await admin.close()
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
        accept_bound_device_trust=opted_in,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id=f"session-bound-{capability}-{opted_in}",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key=f"start-bound-{capability}-{opted_in}",
            now=now,
            requested_capabilities=("chat",),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat",),
        )
    )
    _profile, current = await service.current(
        actor_id="actor-a", session_id=profile.session_id, now=now + timedelta(seconds=2)
    )
    request = {
        "actor_id": "actor-a",
        "session_id": profile.session_id,
        "runtime_profile_id": profile.runtime_profile_id,
        "capability": capability,
        "session_epoch": current.session_epoch,
        "generation_id": current.generation_id + 1,
        "turn_id": current.turn_id + 1,
        "tool_epoch": current.tool_epoch,
        "data_classification": "biometric" if capability == "voice_clone_use" else "private",
        "safety_state": "normal",
        "now": now + timedelta(seconds=2),
    }

    if allowed:
        receipt = await service.authorize_action(**request)  # type: ignore[arg-type]
        assert receipt.effect.value == "allow_with_obligations"
        # The receipt records the tier it relied on: a bound link, not hardware.
        assert receipt.device_trust.value == "trusted"
    else:
        with pytest.raises(PersistentSessionDenied, match="device_untrusted"):
            await service.authorize_action(**request)  # type: ignore[arg-type]
