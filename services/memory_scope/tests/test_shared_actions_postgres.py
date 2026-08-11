"""Real PostgreSQL checks for the narrow PR-12 capture ports.

The test deliberately uses the existing cross-domain Policy V2 bridge: the
receipt is minted and inserted by ``SensitiveWriteService`` on the same
caller-owned transaction that invokes the Memory SECURITY DEFINER ports.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Final, cast

import asyncpg
import pytest
from services.memory_scope.tests.test_cross_domain_sensitive_write import (
    TestActionAdapter,
    TestBindingAdapter,
    TestCaptureAdapter,
    TestConsentAdapter,
    TestPrincipalAdapter,
    _bridge_lock_receipt,
    _BridgePolicyReceiptRepository,
    _dsn_with,
    _install_schemas,
    _make_context,
)
from services.policy.action_authorizer import (
    ActionResourceAuthorityAdapter,
    PostgresActionAuthorizer,
    WriteCallback,
)
from services.policy.context import PolicyContext
from services.policy.production_wiring import (
    ProductionAuthorityAdapters,
    SensitiveWriteService,
    build_composite_action_authority,
)
from services.policy.receipts import PolicyReceiptV2

pytest_plugins = ("services.memory_scope.tests.test_cross_domain_sensitive_write",)

API_ROLE: Final[str] = "memoria_memory_api"


def _context_for_action(
    canonical_action_sha256: str,
    *,
    idempotency_key: str,
) -> PolicyContext:
    return _make_context(
        action_resource_id=f"capture:{canonical_action_sha256}",
        idempotency_key=idempotency_key,
    )


async def _set_action_context(connection: asyncpg.Connection) -> None:
    for name, value in (
        ("app.memory.actor_subject_id", "person-a"),
        ("app.memory.subject_id", "person-a"),
        ("app.session_actor", "person-a"),
        ("app.authenticated_actor", "person-a"),
        ("app.authenticated_subject", "person-a"),
        ("app.authenticated_device", "device-1"),
        ("app.authenticated_binding", "binding-1"),
    ):
        await connection.execute("SELECT set_config($1, $2, true)", name, value)


def _capture_payload(
    *,
    receipt: PolicyReceiptV2,
    record_id: str,
    content_sha256: str,
) -> dict[str, object]:
    fence = receipt.action_resource_fence.model_dump(mode="json")
    ttl_values = [
        item.params.retention_ttl_seconds
        for item in receipt.obligations
        if item.code == "RETENTION_TTL"
    ]
    assert len(ttl_values) == 1 and ttl_values[0] is not None
    retention_expires_at = receipt.created_at + timedelta(seconds=ttl_values[0])
    canonical_action_sha256 = str(fence["action_resource_id"])[len("capture:") :]
    return {
        "record_id": record_id,
        "scope": "personal_private",
        "subject_id": "person-a",
        "resource_owner_id": "person-a",
        "family_space_id": None,
        "co_subject_ids": [],
        "source_evidence_ids": ["evidence-1"],
        "policy_receipt_id": receipt.receipt_id,
        "promotion_receipt_id": "",
        "promotion_fence_context_hash": "",
        "approval_evidence_refs": [],
        "consent_snapshot_id": "consent-snap-1",
        "memory_type": "semantic",
        "confidence": 0.5,
        "retention": "ttl",
        "retention_expires_at": retention_expires_at.isoformat(),
        "payload": {
            "content_sha256": content_sha256,
            "text": "capture-e2e",
        },
        "created_by_actor_id": "person-a",
        "created_at": receipt.created_at.isoformat(),
        "shared_proposal_id": "",
        "status": "confirmed",
        "content_sha256": content_sha256,
        "canonical_action_sha256": canonical_action_sha256,
        "action_resource_id": fence["action_resource_id"],
        "action_resource_fence": fence,
    }


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL capture contract",
)
async def test_postgres_capture_ports_are_receipt_bound_and_atomic(
    cross_db: tuple[str, str, str, str],
) -> None:
    executor_dsn, admin_db_dsn, password, _db_name = cross_db
    await _install_schemas(admin_db_dsn, password)
    canonical_action_sha256 = hashlib.sha256(b"canonical-capture-e2e").hexdigest()
    content_sha256 = hashlib.sha256(b"capture-e2e").hexdigest()
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        await admin.execute(
            """
            INSERT INTO memory_capture_evidence (
                evidence_id, revision, canonical_hash, status, subject_id,
                binding_id, binding_version, valid_from, valid_until
            ) VALUES (
                'evidence-1', 1, $1, 'active', 'person-a', 'binding-1', 1,
                now() - interval '1 minute', now() + interval '1 hour'
            )
            """,
            "e" * 64,
        )
    finally:
        await admin.close()

    service = SensitiveWriteService(
        authorizer=PostgresActionAuthorizer(
            authority=build_composite_action_authority(
                ProductionAuthorityAdapters(
                    principal=TestPrincipalAdapter(),
                    binding=TestBindingAdapter(),
                    action=cast(ActionResourceAuthorityAdapter, TestActionAdapter()),
                    consent=TestConsentAdapter(),
                    capture=TestCaptureAdapter(),
                )
            ),
            receipt_locker=_bridge_lock_receipt,
        ),
        repository=_BridgePolicyReceiptRepository(),
    )
    executor = await asyncpg.connect(executor_dsn)
    try:
        successful_receipts: list[PolicyReceiptV2] = []
        async with executor.transaction():
            await _set_action_context(executor)
            shared_lock = await executor.fetchval(
                """
                SELECT memory_shared_lock_capture_evidence(
                    $1::text[], $2, $3, $4, $5
                )
                """,
                ["evidence-1"],
                "person-a",
                "binding-1",
                1,
                datetime.now(UTC),
            )
            assert shared_lock
            context = _context_for_action(
                canonical_action_sha256, idempotency_key="capture:e2e:success"
            )

            async def commit(
                connection: asyncpg.Connection,
                receipt: PolicyReceiptV2,
            ) -> str:
                successful_receipts.append(receipt)
                payload = _capture_payload(
                    receipt=receipt,
                    record_id="capture-record-1",
                    content_sha256=content_sha256,
                )
                assert await connection.fetchval(
                    """
                    SELECT memory_capture_lock_action_resource(
                        $1, $2, $3, $4::text[], $5, $6
                    )
                    """,
                    "memory_capture",
                    f"capture:{canonical_action_sha256}",
                    "person-a",
                    ["evidence-1"],
                    content_sha256,
                    1,
                )
                return str(
                    await connection.fetchval(
                        "SELECT memory_capture_commit($1::jsonb)",
                        json.dumps(payload),
                    )
                )

            assert (
                await service.execute(
                    executor,
                    context,
                    cast(WriteCallback[str], commit),
                )
                == "capture-record-1"
            )

        admin = await asyncpg.connect(admin_db_dsn)
        try:
            counts = await admin.fetchrow(
                """
                SELECT
                    (SELECT count(*) FROM policy_receipts_v2) AS receipts,
                    (SELECT count(*) FROM memory_records
                     WHERE record_id = 'capture-record-1') AS records,
                    (SELECT count(*) FROM memory_status_events
                     WHERE record_id = 'capture-record-1') AS statuses,
                    (SELECT count(*) FROM memory_outbox
                     WHERE event_id = 'capture-record-1:captured') AS outbox,
                    (SELECT count(*) FROM memory_audit_events
                     WHERE record_id = 'capture-record-1') AS audits
                """
            )
            assert counts is not None
            assert tuple(counts) == (1, 1, 1, 1, 1)
            record = await admin.fetchrow(
                "SELECT confidence, retention, retention_expires_at "
                "FROM memory_records WHERE record_id = 'capture-record-1'"
            )
            assert record is not None
            assert tuple(record)[:2] == (0.5, "ttl")
            assert record["retention_expires_at"] is not None
            assert await admin.fetchval(
                "SELECT status FROM memory_status_events "
                "WHERE record_id = 'capture-record-1'"
            ) == "confirmed"
        finally:
            await admin.close()

        assert successful_receipts
        for forbidden_scope in (
            "session_ephemeral",
            "family_shared",
            "guardian_summary",
        ):
            with pytest.raises(asyncpg.PostgresError, match="record authority fields"):
                async with executor.transaction():
                    await _set_action_context(executor)
                    forbidden_payload = _capture_payload(
                        receipt=successful_receipts[0],
                        record_id=f"capture-record-{forbidden_scope}",
                        content_sha256=content_sha256,
                    )
                    forbidden_payload["scope"] = forbidden_scope
                    await executor.fetchval(
                        "SELECT memory_capture_commit($1::jsonb)",
                        json.dumps(forbidden_payload),
                    )

        # The resource lock is a real first-write CAS, not a mere payload
        # check.  A second action with the same canonical resource is rejected.
        async with executor.transaction():
            await _set_action_context(executor)
            with pytest.raises(asyncpg.PostgresError):
                await executor.fetchval(
                    """
                    SELECT memory_capture_lock_action_resource(
                        'memory_capture', $1, 'person-a', ARRAY['evidence-1'], $2, 1
                    )
                    """,
                    f"capture:{canonical_action_sha256}",
                    content_sha256,
                )

        # API/worker roles cannot invoke either narrow capture port.
        api_dsn = _dsn_with(
            admin_db_dsn,
            database=_db_name,
            user=API_ROLE,
            password=password,
        )
        api = await asyncpg.connect(api_dsn)
        try:
            with pytest.raises(asyncpg.PostgresError, match="permission denied"):
                await api.fetchval(
                    "SELECT memory_capture_commit('{}'::jsonb)"
                )
        finally:
            await api.close()

        # Receipt and all business rows share the caller-owned transaction:
        # an exception after commit removes both the freshly inserted receipt
        # and the capture record plus its append-only projections.
        rollback_canonical_sha256 = hashlib.sha256(
            b"canonical-capture-e2e-rollback"
        ).hexdigest()
        rollback_sha256 = hashlib.sha256(b"capture-e2e-rollback").hexdigest()
        with pytest.raises(RuntimeError, match="injected"):
            async with executor.transaction():
                await _set_action_context(executor)
                context = _context_for_action(
                    rollback_canonical_sha256,
                    idempotency_key="capture:e2e:rollback",
                )

                async def failing_commit(
                    connection: asyncpg.Connection,
                    receipt: PolicyReceiptV2,
                ) -> None:
                    payload = _capture_payload(
                        receipt=receipt,
                        record_id="capture-record-rollback",
                        content_sha256=rollback_sha256,
                    )
                    await connection.fetchval(
                        "SELECT memory_capture_commit($1::jsonb)",
                        json.dumps(payload),
                    )
                    raise RuntimeError("injected")

                await service.execute(
                    executor,
                    context,
                    cast(WriteCallback[None], failing_commit),
                )

        admin = await asyncpg.connect(admin_db_dsn)
        try:
            assert await admin.fetchval(
                "SELECT count(*) FROM policy_receipts_v2"
            ) == 1
            assert await admin.fetchval(
                "SELECT count(*) FROM memory_records "
                "WHERE record_id = 'capture-record-rollback'"
            ) == 0
        finally:
            await admin.close()
    finally:
        await executor.close()
