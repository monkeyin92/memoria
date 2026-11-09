"""Session runtime Postgres: consent discovery, action authorization and durable effect commit."""
# ruff: noqa: F811 - imported pytest fixtures are shadowed by test injection

import asyncio
import hashlib
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import asyncpg
import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    PolicyReceiptV2,
)
from services.consent.evidence import (
    ConsentEvidence,
    ConsentSnapshot,
)
from services.policy.action_fence import (
    build_action_resource_fence,
)
from services.policy.context import PolicyContext
from services.policy.engine import PolicyEngine
from services.session_runtime.postgres_store import (
    PostgresSessionRuntimeStore,
    SessionRuntimeConflict,
)
from services.session_runtime.tests.postgres_store_test_support import (
    _SIGNING_KEY,
    _dsn_with,
    _seed_self_consent,
    _seed_verified_device,
    _session_fence_receipt,
    postgres_runtime,  # noqa: F401 - pytest discovers imported fixtures by name
    postgres_runtime_with_consent,  # noqa: F401 - pytest discovers imported fixtures by name
)


async def _revoke_self_consent(
    connection: asyncpg.Connection,
    *,
    evidence: ConsentEvidence,
    snapshot: ConsentSnapshot,
    now: datetime,
) -> None:
    revoked_snapshot_id = f"{snapshot.snapshot_id}-revoked"
    revoked = replace(
        evidence,
        version=evidence.version + 1,
        snapshot_id=revoked_snapshot_id,
        status="revoked",
        evidence_id=f"{evidence.evidence_id}-revoked",
        idempotency_key=None,
        canonical_hash="",
    )
    revoked_snapshot = ConsentSnapshot(
        snapshot_id=revoked_snapshot_id,
        version=snapshot.version + 1,
        subject_id=snapshot.subject_id,
        binding_id=snapshot.binding_id,
        binding_version=snapshot.binding_version,
        policy_version=snapshot.policy_version,
        created_at=now,
        grants=(),
        relationships=snapshot.relationships,
        binding=snapshot.binding,
    )
    await connection.execute(
        """
        INSERT INTO consent_evidence (
            consent_id, version, actor_id, subject_id, binding_id,
            binding_version, status, evidence_json
        ) VALUES ($1, $2, $3, $4, $5, $6, 'revoked', $7::jsonb)
        """,
        revoked.consent_id,
        revoked.version,
        revoked.actor_id,
        revoked.subject_id,
        revoked.binding_id,
        revoked.binding_version,
        json.dumps(revoked.to_canonical_dict()),
    )
    await connection.execute(
        """
        INSERT INTO consent_snapshot (
            snapshot_id, version, actor_id, subject_id, binding_id,
            binding_version, snapshot_json
        ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb)
        """,
        revoked_snapshot.snapshot_id,
        revoked_snapshot.version,
        revoked.actor_id,
        revoked_snapshot.subject_id,
        revoked_snapshot.binding_id,
        revoked_snapshot.binding_version,
        json.dumps(revoked_snapshot.to_canonical_dict()),
    )
    await connection.execute(
        """
        UPDATE consent_evidence_head
        SET current_consent_id = $1,
            current_revision = $2,
            current_hash = $3,
            updated_at = $4
        WHERE actor_id = $5
          AND subject_id = $6
          AND binding_id = $7
          AND binding_version = $8
          AND capability = $9
          AND purpose = $10
        """,
        revoked.consent_id,
        revoked.version,
        revoked.canonical_hash,
        now,
        revoked.actor_id,
        revoked.subject_id,
        revoked.binding_id,
        revoked.binding_version,
        revoked.capability,
        revoked.purpose,
    )
    await connection.execute(
        """
        UPDATE consent_snapshot_head
        SET current_snapshot_id = $1,
            current_revision = $2,
            current_hash = $3,
            updated_at = $4
        WHERE subject_id = $5
          AND binding_id = $6
          AND binding_version = $7
        """,
        revoked_snapshot.snapshot_id,
        revoked_snapshot.version,
        revoked_snapshot.canonical_hash,
        now,
        revoked_snapshot.subject_id,
        revoked_snapshot.binding_id,
        revoked_snapshot.binding_version,
    )


@pytest.mark.asyncio
async def test_authorize_action_reuses_same_transaction_consent_discovery(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.commands import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.service import (
        PersistentSessionDenied,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        evidence, snapshot = await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
        policy=PolicyEngine(receipt_ttl=timedelta(seconds=30)),
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-action-consent",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-action-consent",
            now=now,
            requested_capabilities=("chat", "voice_clone_use"),
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
    _current_profile, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )

    receipt = await service.authorize_action(
        actor_id="actor-a",
        session_id=profile.session_id,
        runtime_profile_id=profile.runtime_profile_id,
        capability="voice_clone_use",
        session_epoch=current.session_epoch,
        generation_id=current.generation_id + 1,
        turn_id=current.turn_id + 1,
        tool_epoch=current.tool_epoch,
        data_classification="biometric",
        safety_state="normal",
        now=now + timedelta(seconds=2),
    )
    replayed = await service.authorize_action(
        actor_id="actor-a",
        session_id=profile.session_id,
        runtime_profile_id=profile.runtime_profile_id,
        capability="voice_clone_use",
        session_epoch=current.session_epoch,
        generation_id=current.generation_id + 1,
        turn_id=current.turn_id + 1,
        tool_epoch=current.tool_epoch,
        data_classification="biometric",
        safety_state="normal",
        now=now + timedelta(seconds=2),
    )

    assert receipt.effect.value == "allow_with_obligations"
    assert receipt.reason_code == "adult_subject_authorized"
    assert receipt.consent_snapshot_ids == (snapshot.snapshot_id,)
    assert replayed == receipt
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _revoke_self_consent(
            admin,
            evidence=evidence,
            snapshot=snapshot,
            now=now + timedelta(seconds=3),
        )
    finally:
        await admin.close()
    with pytest.raises(PersistentSessionDenied, match="current Consent"):
        await service.authorize_action(
            actor_id="actor-a",
            session_id=profile.session_id,
            runtime_profile_id=profile.runtime_profile_id,
            capability="voice_clone_use",
            session_epoch=current.session_epoch,
            generation_id=current.generation_id + 1,
            turn_id=current.turn_id + 1,
            tool_epoch=current.tool_epoch,
            data_classification="biometric",
            safety_state="normal",
            now=now + timedelta(seconds=4),
        )
    with pytest.raises(PersistentSessionDenied, match="receipt is expired"):
        await service.authorize_action(
            actor_id="actor-a",
            session_id=profile.session_id,
            runtime_profile_id=profile.runtime_profile_id,
            capability="voice_clone_use",
            session_epoch=current.session_epoch,
            generation_id=current.generation_id + 1,
            turn_id=current.turn_id + 1,
            tool_epoch=current.tool_epoch,
            data_classification="biometric",
            safety_state="normal",
            now=now + timedelta(seconds=40),
        )


@pytest.mark.asyncio
async def test_profile_issue_and_decide_discover_current_consent(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.commands import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.service import build_postgres_session_runtime_service

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="memory_recall_private",
            purpose="memory_recall",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-consented-memory-recall",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-consented-memory-recall",
            now=now,
            requested_capabilities=("chat", "memory_recall_private"),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(milliseconds=100),
            requested_capabilities=("chat", "memory_recall_private"),
        )
    )

    assert "memory_recall_private" in {
        capability.value for capability in profile.capabilities
    }
    decision = await service.decide(
        runtime_profile_id=profile.runtime_profile_id,
        capability="memory_recall_private",
        actor_id="actor-a",
        data_classification="private",
        safety_state="normal",
        now=now + timedelta(seconds=1),
    )
    assert decision.effect.value == "allow_with_obligations"
    assert decision.reason_code == "private_memory_subject_authorized"


@pytest.mark.asyncio
async def test_action_receipt_subject_must_match_current_active_subject(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.commands import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.service import build_postgres_session_runtime_service

    store, _bootstrap_dsn = postgres_runtime
    now = datetime.now(UTC)
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-subject-receipt-fence",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-subject-receipt-fence",
            now=now,
            requested_capabilities=("chat",),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(milliseconds=100),
            requested_capabilities=("chat",),
        )
    )
    current_profile, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=1),
    )
    forged_profile = current_profile.model_copy(
        update={"active_subject_id": "actor-b"}
    )
    forged_receipt = _session_fence_receipt(
        receipt_id="receipt-forged-cross-subject",
        profile=forged_profile,
        generation_id=current.generation_id,
        turn_id=current.turn_id,
        tool_epoch=current.tool_epoch,
        issued_at=now + timedelta(seconds=1),
    )

    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
        subject_id="actor-a",
    ) as connection:
        await connection.execute(
            "SELECT set_config('app.authenticated_subject', $1, true)",
            "actor-b",
        )
        with pytest.raises(asyncpg.PostgresError) as raised:
            await connection.fetchval(
                "SELECT action_policy_insert_receipt($1::jsonb)",
                json.dumps(forged_receipt.model_dump(mode="json"), default=str),
            )
    assert raised.value.sqlstate in {"SR403", "SR412"}


@pytest.mark.asyncio
async def test_concurrent_authorize_actions_have_one_fence_winner(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.commands import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.service import (
        SessionRuntimeConflict,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-concurrent-actions",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-concurrent-actions",
            now=now,
            requested_capabilities=("chat",),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(milliseconds=100),
            requested_capabilities=("chat",),
        )
    )
    _profile, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=1),
    )

    async def attempt(safety_state: str) -> object:
        try:
            return await service.authorize_action(
                actor_id="actor-a",
                session_id=profile.session_id,
                runtime_profile_id=profile.runtime_profile_id,
                capability="voice_clone_use",
                session_epoch=current.session_epoch,
                generation_id=current.generation_id + 1,
                turn_id=current.turn_id + 1,
                tool_epoch=current.tool_epoch,
                data_classification="biometric",
                safety_state=safety_state,
                now=now + timedelta(seconds=1),
            )
        except Exception as exc:  # collected for exact outcome assertions
            return exc

    results = await asyncio.gather(
        attempt("normal"),
        attempt("self_crisis"),
    )
    receipts = [item for item in results if isinstance(item, PolicyReceiptV2)]
    conflicts = [item for item in results if isinstance(item, SessionRuntimeConflict)]

    assert len(receipts) == 1, repr(results)
    assert len(conflicts) == 1, repr(results)


@pytest.mark.asyncio
async def test_authorize_action_replays_terminal_deny_after_consent_is_added(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.commands import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.service import (
        PersistentSessionDenied,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-action-deny-replay",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-action-deny-replay",
            now=now,
            requested_capabilities=("chat", "voice_clone_use"),
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
    _current_profile, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )
    request = {
        "actor_id": "actor-a",
        "session_id": profile.session_id,
        "runtime_profile_id": profile.runtime_profile_id,
        "capability": "voice_clone_use",
        "session_epoch": current.session_epoch,
        "generation_id": current.generation_id + 1,
        "turn_id": current.turn_id + 1,
        "tool_epoch": current.tool_epoch,
        "data_classification": "biometric",
        "safety_state": "normal",
        "now": now + timedelta(seconds=2),
    }

    with pytest.raises(PersistentSessionDenied, match="subject_consent_required"):
        await service.authorize_action(**request)  # type: ignore[arg-type]

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
        )
    finally:
        await admin.close()

    with pytest.raises(PersistentSessionDenied, match="subject_consent_required"):
        await service.authorize_action(**request)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_authorize_action_accepts_one_step_turn_interrupt_and_tool_fences(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.commands import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.service import build_postgres_session_runtime_service

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-action-fence-kinds",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-action-fence-kinds",
            now=now,
            requested_capabilities=("chat", "voice_clone_use"),
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
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )
    common = {
        "actor_id": "actor-a",
        "session_id": profile.session_id,
        "runtime_profile_id": profile.runtime_profile_id,
        "capability": "voice_clone_use",
        "session_epoch": current.session_epoch,
        "data_classification": "biometric",
        "safety_state": "normal",
    }

    turn = await service.authorize_action(
        **common,  # type: ignore[arg-type]
        generation_id=current.generation_id + 1,
        turn_id=current.turn_id + 1,
        tool_epoch=current.tool_epoch,
        now=now + timedelta(seconds=2),
    )
    interrupt = await service.authorize_action(
        **common,  # type: ignore[arg-type]
        generation_id=current.generation_id + 2,
        turn_id=current.turn_id + 1,
        tool_epoch=current.tool_epoch,
        now=now + timedelta(seconds=3),
    )
    tool = await service.authorize_action(
        **common,  # type: ignore[arg-type]
        generation_id=current.generation_id + 3,
        turn_id=current.turn_id + 1,
        tool_epoch=current.tool_epoch + 1,
        now=now + timedelta(seconds=4),
    )
    _latest_profile, latest = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=5),
    )

    assert {turn.effect.value, interrupt.effect.value, tool.effect.value} == {
        "allow_with_obligations"
    }
    assert (latest.generation_id, latest.turn_id, latest.tool_epoch) == (
        current.generation_id + 3,
        current.turn_id + 1,
        current.tool_epoch + 1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_status", ["revoked", "expired"])
async def test_authorize_action_denies_non_effective_current_consent(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
    evidence_status: str,
) -> None:
    from services.session_runtime.commands import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.service import (
        PersistentSessionDenied,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    valid_from = now - timedelta(hours=2)
    valid_until = (
        now - timedelta(hours=1)
        if evidence_status == "expired"
        else now + timedelta(hours=1)
    )
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
            evidence_status=evidence_status,
            evidence_valid_from=valid_from,
            evidence_valid_until=valid_until,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id=f"session-action-{evidence_status}-consent",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key=f"start-action-{evidence_status}-consent",
            now=now,
            requested_capabilities=("chat", "voice_clone_use"),
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
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )

    with pytest.raises(PersistentSessionDenied, match="subject_consent_required"):
        await service.authorize_action(
            actor_id="actor-a",
            session_id=profile.session_id,
            runtime_profile_id=profile.runtime_profile_id,
            capability="voice_clone_use",
            session_epoch=current.session_epoch,
            generation_id=current.generation_id + 1,
            turn_id=current.turn_id + 1,
            tool_epoch=current.tool_epoch,
            data_classification="biometric",
            safety_state="normal",
            now=now + timedelta(seconds=2),
        )


@pytest.mark.asyncio
async def test_authorize_action_rejects_resource_scoped_capability_without_resource(
    postgres_runtime: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.service import (
        PersistentSessionDenied,
        build_postgres_session_runtime_service,
    )

    store, _bootstrap_dsn = postgres_runtime
    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )

    with pytest.raises(
        PersistentSessionDenied,
        match="resource-scoped action requires explicit resource identity",
    ):
        await service.authorize_action(
            actor_id="actor-a",
            session_id="session-resource-missing",
            runtime_profile_id="profile-resource-missing",
            capability="memory_promotion",
            session_epoch=1,
            generation_id=1,
            turn_id=1,
            tool_epoch=0,
            data_classification="private",
            safety_state="normal",
            now=datetime.now(UTC),
        )


@pytest.mark.asyncio
async def test_authorize_action_rolls_back_fence_when_terminal_authority_fails(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.commands import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.service import (
        PersistentSessionUnavailable,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await service.start(
        StartPersistentSessionCommand(
            session_id="session-action-rollback",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-action-rollback",
            now=now,
            requested_capabilities=("chat", "voice_clone_use"),
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
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )
    request = {
        "actor_id": "actor-a",
        "session_id": profile.session_id,
        "runtime_profile_id": profile.runtime_profile_id,
        "capability": "voice_clone_use",
        "session_epoch": current.session_epoch,
        "generation_id": current.generation_id + 1,
        "turn_id": current.turn_id + 1,
        "tool_epoch": current.tool_epoch,
        "data_classification": "biometric",
        "safety_state": "normal",
        "now": now + timedelta(seconds=2),
    }
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "REVOKE EXECUTE ON FUNCTION "
            "session_runtime_action_receipt_authority(text, jsonb) "
            "FROM memoria_action_executor"
        )
    finally:
        await admin.close()

    with pytest.raises(PersistentSessionUnavailable, match="permission denied"):
        await service.authorize_action(**request)  # type: ignore[arg-type]

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await admin.execute(
            "GRANT EXECUTE ON FUNCTION "
            "session_runtime_action_receipt_authority(text, jsonb) "
            "TO memoria_action_executor"
        )
    finally:
        await admin.close()

    receipt = await service.authorize_action(**request)  # type: ignore[arg-type]

    assert receipt.effect.value == "allow_with_obligations"


@pytest.mark.asyncio
async def test_sensitive_write_discovers_consent_on_action_executor_transaction(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.policy.production_wiring import (
        PostgresCurrentConsentAuthorityAdapter,
        ProductionAuthorityAdapters,
        build_sensitive_write_service,
    )
    from services.session_runtime.commands import (
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.postgres_authorities import (
        _ActionExecutorReceiptRepository,
        _PostgresIdentityAuthority,
        _PostgresPrincipalAuthority,
    )
    from services.session_runtime.service import build_postgres_session_runtime_service

    class _ExactActionAuthority:
        async def lock_current(
            self,
            _connection: asyncpg.Connection,
            _receipt: PolicyReceiptV2,
            request: object,
        ) -> object:
            return request.context.action_resource_fence  # type: ignore[attr-defined]

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        _evidence, snapshot = await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="memory_capture",
            purpose="memory_capture",
            now=now,
        )
    finally:
        await admin.close()

    runtime_service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
    )
    initial = await runtime_service.start(
        StartPersistentSessionCommand(
            session_id="session-sensitive-write",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-sensitive-write",
            now=now,
            requested_capabilities=("chat",),
        )
    )
    profile = await runtime_service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=initial.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat",),
        )
    )
    _profile, current = await runtime_service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )

    identity = _PostgresIdentityAuthority()
    consent = PostgresCurrentConsentAuthorityAdapter()
    service = build_sensitive_write_service(
        ProductionAuthorityAdapters(
            principal=_PostgresPrincipalAuthority(),
            consent=consent,
            binding=identity,
            action=_ExactActionAuthority(),  # type: ignore[arg-type]
        ),
        repository=_ActionExecutorReceiptRepository(),  # type: ignore[arg-type]
    )
    async with store.action_transaction(
        actor_id="actor-a",
        device_id="device-a",
        subject_id="actor-a",
    ) as connection:
        binding = await identity.lock_binding(
            connection,
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            now=now,
        )
        action_fence = build_action_resource_fence(
            capability="memory_capture",
            purpose="memory_capture",
            action_resource_id="capture:actor-a:test",
            action_revision=1,
            consent_snapshot_id=snapshot.snapshot_id,
            consent_snapshot_revision=snapshot.version,
            consent_snapshot_hash=snapshot.canonical_hash,
            generation_id=current.generation_id,
            turn_id=current.turn_id,
            tool_epoch=current.tool_epoch,
            issued_at=now + timedelta(seconds=2),
            valid_until=now + timedelta(minutes=5),
        )
        context = PolicyContext(
            actor_id="actor-a",
            subject_id="actor-a",
            resource_owner_id="actor-a",
            device_id="device-a",
            capability="memory_capture",
            purpose="memory_capture",
            declared_device_mode="self_use",
            current_session_mode="adult_companion",
            subject_category="adult",
            age_band="adult",
            speaker_state="confirmed",
            speaker_confidence=1.0,
            device_trust="verified",
            safety_state="normal",
            jurisdiction="CN",
            data_classification="private",
            binding_id="binding-a",
            binding_version=1,
            session_id=profile.session_id,
            session_epoch=profile.session_epoch,
            runtime_profile_id=profile.runtime_profile_id,
            subject_revision=profile.subject_revision,
            evaluated_at=now + timedelta(seconds=2),
            idempotency_key="capture-sensitive-write",
            binding_evidence=binding.evidence,
            generation_id=current.generation_id,
            turn_id=current.turn_id,
            tool_epoch=current.tool_epoch,
            action_resource_fence=action_fence,
        )

        async def accepted(
            _connection: asyncpg.Connection,
            receipt: PolicyReceiptV2,
        ) -> PolicyReceiptV2:
            return receipt

        receipt = await service.execute(connection, context, accepted)

    assert receipt.effect.value == "allow_with_obligations"
    assert receipt.consent_snapshot_ids == (snapshot.snapshot_id,)


@pytest.mark.asyncio
async def test_tool_effect_commit_replay_conflict_and_reconcile_are_durable(
    postgres_runtime_with_consent: tuple[PostgresSessionRuntimeStore, str],
) -> None:
    from services.session_runtime.commands import (
        CommitToolEffectCommand,
        StartPersistentSessionCommand,
        SwitchPersistentSubjectCommand,
    )
    from services.session_runtime.service import (
        PersistentSessionDenied,
        build_postgres_session_runtime_service,
    )

    store, bootstrap_dsn = postgres_runtime_with_consent
    now = datetime.now(UTC)
    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        await _seed_verified_device(
            admin,
            device_id="device-a",
            binding_id="binding-a",
            now=now,
        )
        await _seed_self_consent(
            admin,
            actor_id="actor-a",
            device_id="device-a",
            binding_id="binding-a",
            capability="voice_clone_use",
            purpose="voice_clone",
            now=now,
        )
    finally:
        await admin.close()

    service = build_postgres_session_runtime_service(
        store=store,
        signing_key=_SIGNING_KEY,
        policy=PolicyEngine(receipt_ttl=timedelta(seconds=30)),
    )
    started = await service.start(
        StartPersistentSessionCommand(
            session_id="session-tool-effect",
            actor_id="actor-a",
            device_id="device-a",
            expected_binding_version=1,
            idempotency_key="start-tool-effect",
            now=now,
            requested_capabilities=("chat", "voice_clone_use"),
        )
    )
    profile = await service.switch_subject(
        SwitchPersistentSubjectCommand(
            session_id=started.session_id,
            actor_id="actor-a",
            subject_id="actor-a",
            now=now + timedelta(seconds=1),
            requested_capabilities=("chat",),
        )
    )
    _current, current = await service.current(
        actor_id="actor-a",
        session_id=profile.session_id,
        now=now + timedelta(seconds=2),
    )
    receipt = await service.authorize_action(
        actor_id="actor-a",
        session_id=profile.session_id,
        runtime_profile_id=profile.runtime_profile_id,
        capability="voice_clone_use",
        session_epoch=current.session_epoch,
        generation_id=current.generation_id + 1,
        turn_id=current.turn_id + 1,
        tool_epoch=current.tool_epoch,
        data_classification="biometric",
        safety_state="normal",
        now=now + timedelta(seconds=2),
    )
    payload = {"provider": "test", "voice": "resource"}
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    generation_id = current.generation_id + 1
    turn_id = current.turn_id + 1
    fence = {
        "session_id": profile.session_id,
        "session_epoch": current.session_epoch,
        "generation_id": generation_id,
        "turn_id": turn_id,
        "tool_epoch": current.tool_epoch,
    }
    fence_fingerprint = hashlib.sha256(
        json.dumps(
            fence,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    command = CommitToolEffectCommand(
        actor_id="actor-a",
        session_id=profile.session_id,
        runtime_profile=profile,
        policy_receipt=receipt,
        session_epoch=current.session_epoch,
        generation_id=generation_id,
        turn_id=turn_id,
        tool_epoch=current.tool_epoch,
        capability=receipt.capability.value,
        purpose=receipt.purpose.value,
        resource_id=receipt.action_resource_fence.action_resource_id,
        evidence_refs=(receipt.receipt_id,),
        idempotency_key="effect-key-1",
        intent="voice_clone_use",
        payload=payload,
        payload_sha256=hashlib.sha256(payload_bytes).hexdigest(),
        fence_fingerprint=fence_fingerprint,
        now=now + timedelta(seconds=2),
    )

    committed = await service.commit_tool_effect(command)
    replayed = await service.commit_tool_effect(command)
    assert replayed == committed
    assert committed["idempotency_key"] == "effect-key-1"
    assert committed["intent_sha256"] == command.payload_sha256

    reconciled = await service.reconcile_tool_effect(idempotency_key="effect-key-1")
    assert reconciled["state"] == "committed"
    assert reconciled["receipt"] == committed

    conflict_payload = {"provider": "forged"}
    conflict_bytes = json.dumps(
        conflict_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    conflicting = replace(
        command,
        payload=conflict_payload,
        payload_sha256=hashlib.sha256(conflict_bytes).hexdigest(),
    )
    with pytest.raises(SessionRuntimeConflict, match="idempotency"):
        await service.commit_tool_effect(conflicting)

    def action_fingerprint(generation: int, turn: int) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "session_id": profile.session_id,
                    "turn_id": turn,
                    "generation_id": generation,
                    "tool_epoch": current.tool_epoch,
                    "session_epoch": current.session_epoch,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    stale = replace(
        command,
        generation_id=command.generation_id - 1,
        turn_id=command.turn_id - 1,
        fence_fingerprint=action_fingerprint(
            command.generation_id - 1,
            command.turn_id - 1,
        ),
    )
    with pytest.raises(SessionRuntimeConflict, match="fence"):
        await service.commit_tool_effect(stale)

    expired_receipt = receipt.model_copy(
        update={
            "expires_at": now + timedelta(seconds=1),
        }
    )
    with pytest.raises(PersistentSessionDenied, match="current evidence"):
        await service.commit_tool_effect(
            replace(command, policy_receipt=expired_receipt)
        )

    with pytest.raises(PersistentSessionDenied, match="payload mismatch"):
        await service.commit_tool_effect(replace(command, actor_id="actor-b"))

    assert (
        await service.reconcile_tool_effect(idempotency_key="effect-never-committed")
    ) == {"state": "not_found"}

    admin = await asyncpg.connect(bootstrap_dsn)
    try:
        assert await admin.fetchval(
            "SELECT count(*) FROM session_runtime_tool_effect_intents "
            "WHERE idempotency_key = $1",
            "effect-key-1",
        ) == 1
        assert await admin.fetchval(
            "SELECT count(*) FROM session_runtime_tool_effect_outbox "
            "WHERE intent_id = $1",
            committed["intent_id"],
        ) == 1
        worker_password = uuid.uuid4().hex
        await admin.execute(
            f"ALTER ROLE memoria_session_worker PASSWORD '{worker_password}'"
        )
    finally:
        await admin.close()

    worker_dsn = _dsn_with(
        bootstrap_dsn,
        database=urlsplit(bootstrap_dsn).path.lstrip("/"),
        user="memoria_session_worker",
        password=worker_password,
    )
    worker = await asyncpg.connect(worker_dsn)
    try:
        claimed_rows = await worker.fetch(
            "SELECT * FROM session_runtime_tool_effect_outbox_claim($1, $2, $3)",
            "worker-tool-effect",
            10,
            60,
        )
        assert len(claimed_rows) == 1
        claimed = claimed_rows[0][0]
        if isinstance(claimed, str):
            claimed = json.loads(claimed)
        assert isinstance(claimed, dict)
        completed = await worker.fetchval(
            "SELECT session_runtime_tool_effect_outbox_complete($1, $2, $3, $4)",
            claimed["outbox_id"],
            "worker-tool-effect",
            "delivered",
            None,
        )
        if isinstance(completed, str):
            completed = json.loads(completed)
        assert isinstance(completed, dict)
        with pytest.raises(asyncpg.PostgresError) as worker_read:
            await worker.fetchval(
                "SELECT count(*) FROM session_runtime_tool_effect_outbox"
            )
        assert worker_read.value.sqlstate == "42501"
    finally:
        await worker.close()
