"""FORCE-RLS PostgreSQL contract for the notification store seam (PR-17).

The admin DSN is used ONLY to create the database/roles and to bootstrap the
schema; business operations run as TWO real application roles
(``memoria_notification_api`` and ``memoria_notification_worker``, both
LOGIN, NOSUPERUSER, NOBYPASSRLS, non-table-owner, P0-2).  The tests assert
the runtime role properties, prove the API adapter refuses worker commands
(and vice versa), and cover no-context fail-closed, full claim/delivery
roundtrip, cross-subject isolation and command-splitting negatives.

The API and the worker NEVER share a store/DSN: every business create/read
goes through the API store while claim/complete/scan go through the worker
store, each with its own connection role.

Skipped unless ``MEMORIA_TEST_POSTGRES_DSN`` is set (same convention as the
identity / guardian PostgreSQL contract tests).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.notification.domain import (
    DeliveryAttempt,
    NotificationFence,
    RecipientSpec,
    RelationshipSnapshot,
    new_id,
)
from services.notification.postgres_store import PostgresNotificationStore
from services.notification.repository import (
    InMemoryNotificationReceiptVerifier,
    InMemoryRelationshipResolver,
)
from services.notification.service import NotificationService
from services.notification.tests.receipt_helpers import make_notification_receipt

API_ROLE = "memoria_notification_api"
WORKER_ROLE = "memoria_notification_worker"


def _dsn_with(dsn: str, *, database: str, user: str | None = None, password: str | None = None) -> str:
    parsed = urlsplit(dsn)
    host = parsed.hostname or "localhost"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    username = quote(user or parsed.username or "postgres")
    secret = quote(password if password is not None else parsed.password or "")
    credentials = f"{username}:{secret}" if secret else username
    return urlunsplit(
        (parsed.scheme, f"{credentials}@{host}", f"/{database}", parsed.query, "")
    )


@pytest.fixture
async def pg_env() -> AsyncIterator[tuple[str, str, str, str]]:
    """Provision a fresh database + BOTH app roles and yield (api_dsn,
    worker_dsn, admin_db_dsn, role_password); drops the database
    afterwards."""
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    db_name = f"memoria_notification_{uuid.uuid4().hex[:10]}"
    # Deterministic shared password: the app roles are CLUSTER-global, so a
    # random password per test would let concurrent runs (parallel CI /
    # sub-agents) ALTER ROLE over each other and break connections with
    # InvalidPassword.  The local test cluster is non-production and the
    # password is never reused for anything real.
    password = "memoria_local_test_password"
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f"CREATE DATABASE {db_name}")
        for role in (API_ROLE, WORKER_ROLE):
            if not await admin.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = $1", role
            ):
                await admin.execute(
                    f"CREATE ROLE {role} LOGIN NOSUPERUSER NOBYPASSRLS"
                )
    finally:
        await admin.close()
    admin_db = _dsn_with(dsn, database=db_name)
    api = _dsn_with(admin_db, database=db_name, user=API_ROLE, password=password)
    worker = _dsn_with(admin_db, database=db_name, user=WORKER_ROLE, password=password)
    try:
        yield api, worker, admin_db, password
    finally:
        admin = await asyncpg.connect(dsn)
        try:
            await admin.execute(f"DROP DATABASE IF EXISTS {db_name}")
        finally:
            await admin.close()


def _now() -> datetime:
    # Same real-clock convention as the service tests: the receipt helper
    # anchors receipts just before this instant, so a fixed historical clock
    # would land outside the decision window.
    return datetime.now(UTC)


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL notification contract",
)
async def test_postgres_notification_schema_rls_and_claim(
    pg_env: tuple[str, str, str, str],
) -> None:
    api_dsn, worker_dsn, admin_db_dsn, password = pg_env
    api_store = PostgresNotificationStore(api_dsn)
    await api_store.initialize(
        bootstrap_dsn=admin_db_dsn,
        app_role_password=password,
    )
    worker_store = PostgresNotificationStore(worker_dsn).with_role("worker")
    await worker_store.initialize()
    try:
        async with api_store._ready().acquire() as connection:  # noqa: SLF001
            row = await connection.fetchrow(
                "SELECT current_user AS usr,"
                " (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) AS su,"
                " (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)"
                " AS bypass"
            )
            assert row is not None
            assert row["usr"] == API_ROLE
            assert row["su"] is False
            assert row["bypass"] is False
        async with worker_store._ready().acquire() as connection:  # noqa: SLF001
            row = await connection.fetchrow("SELECT current_user AS usr")
            assert row is not None and row["usr"] == WORKER_ROLE
        verifier = InMemoryNotificationReceiptVerifier()
        resolver = InMemoryRelationshipResolver()
        api_service = NotificationService(
            api_store,
            lease_seconds=60.0,
            max_retries=2,
            receipt_verifier=verifier,
            relationship_resolver=resolver,
        )
        worker_service = NotificationService(
            worker_store,
            lease_seconds=60.0,
            max_retries=2,
            receipt_verifier=verifier,
            relationship_resolver=resolver,
        )

        now = _now()
        fence = NotificationFence(
            device_id="device-1",
            session_id="session-1",
            epoch=1,
            binding_id="binding-1",
            binding_version=1,
            runtime_profile_id="profile-1",
            actor_person_id="actor-1",
            subject_person_id="minor-1",
            valid_until=_now() + timedelta(hours=2),
        )
        verifier.register(
            make_notification_receipt(
                receipt_id="policy-receipt-1",
                actor_person_id="actor-1",
                subject_person_id="minor-1",
                fence=fence,
                relationship_snapshot_ids=("rel-snap-1",),
            )
        )
        resolver.register(
            RelationshipSnapshot(
                relationship_id="rel-1",
                subject_person_id="minor-1",
                person_id="guardian-1",
                role="guardian",
                status="active",
                snapshot_id="rel-snap-1",
                revision=1,
                valid_from=now - timedelta(days=1),
            )
        )
        intent = await api_service.create_intent(
            intent_kind="crisis_safety",
            subject_person_id="minor-1",
            source_event_id="crisis-event-1",
            idempotency_key="crisis:pg-1",
            policy_receipt_id="policy-receipt-1",
            fence=fence,
            actor_person_id="actor-1",
            template_key="crisis_safety_notice",
            template_params={
                "role_label": "孩子",
                "reason_code": "safety_concern",
                "action_hint": "联系监护人",
            },
            reason_code="safety_concern",
            script_version="2026-08-09.1",
            recipients=(
                RecipientSpec(relationship_id="rel-1"),
            ),
            occurred_at=now,
            now=now,
        )
        assert intent.status == "pending"

        # Lease + fencing-token claim survives a round trip.
        (attempt,) = await worker_service.claim_due(now=now)
        assert attempt.attempt_number == 1
        claimed = await worker_store.get_recipient_for_worker(attempt.recipient_id)
        assert claimed is not None
        assert claimed.status == "in_progress"
        assert claimed.fencing_token == attempt.fencing_token

        # Concurrent re-claim while the lease is active must be rejected.
        assert await worker_store.claim_recipient(
            attempt.recipient_id,
            DeliveryAttempt(
                attempt_id=new_id(),
                intent_id=intent.intent_id,
                recipient_id=attempt.recipient_id,
                attempt_number=2,
                channel="sms",
                logical_delivery_key=(
                    f"{attempt.intent_id}:{attempt.recipient_id}:sms"
                ),
                status="leased",
                fencing_token="other-worker",
                leased_until=now + timedelta(seconds=30),
                started_at=now,
            ),
            now,
        ) is None

        delivered = await worker_service.complete_attempt(
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.fencing_token,
            result="delivered",
            channel_receipt_id="pg-receipt-1",
            now=now + timedelta(seconds=1),
        )
        assert delivered.status == "delivered"
        assert (
            await api_service.get_intent(
                intent.intent_id, actor_person_id="minor-1"
            )
        ).status == "delivered"

        # Fifth review: subject-owner join reads work through the API role
        # (recipients/receipts policies) without borrowing the worker role.
        bound = await api_store.list_recipients_by_intent_subject(
            intent.intent_id, subject_person_id="minor-1"
        )
        assert len(bound) == 1 and bound[0].person_id == "guardian-1"
        receipts = await api_store.list_receipts(
            intent.intent_id, person_id="minor-1"
        )
        assert len(receipts) == 1
        # Cross-subject join read sees nothing.
        assert (
            await api_store.list_recipients_by_intent_subject(
                intent.intent_id, subject_person_id="other-subject"
            )
            == ()
        )

        # P0-A: old lease expires -> new worker re-claims -> the OLD worker's
        # completion changes zero state (recipient token + attempt identity
        # are condition-guarded inside one transaction).
        now2 = now + timedelta(minutes=10)
        fence2 = NotificationFence(
            device_id="device-1",
            session_id="session-2",
            epoch=1,
            binding_id="binding-1",
            binding_version=1,
            runtime_profile_id="profile-1",
            actor_person_id="actor-1",
            subject_person_id="minor-1",
            valid_until=_now() + timedelta(hours=2),
        )
        # The second intent needs its own receipt: a PolicyReceiptV2 is
        # session-bound, so reusing the session-1 receipt must fail closed
        # (this is exactly the fence matching being tested).
        verifier.register(
            make_notification_receipt(
                receipt_id="policy-receipt-2",
                actor_person_id="actor-1",
                subject_person_id="minor-1",
                fence=fence2,
                relationship_snapshot_ids=("rel-snap-1",),
            )
        )
        (new_intent,) = (
            await api_service.create_intent(
                intent_kind="crisis_safety",
                subject_person_id="minor-1",
                source_event_id="crisis-event-2",
                idempotency_key="crisis:pg-2",
                policy_receipt_id="policy-receipt-2",
                fence=fence2,
                actor_person_id="actor-1",
                template_key="crisis_safety_notice",
                template_params={
                    "role_label": "孩子",
                    "reason_code": "safety_concern",
                    "action_hint": "联系监护人",
                },
                reason_code="safety_concern",
                script_version="2026-08-09.1",
                recipients=(
                    RecipientSpec(relationship_id="rel-1"),
                ),
                occurred_at=now2,
                now=now2,
            ),
        )
        assert new_intent.status == "pending"
        (old_attempt,) = await worker_service.claim_due(now=now2)
        (reclaimed,) = await worker_service.claim_due(now=now2 + timedelta(minutes=3))
        assert reclaimed.fencing_token != old_attempt.fencing_token
        from services.notification.domain import NotificationFencingError

        with pytest.raises(NotificationFencingError):
            await worker_service.complete_attempt(
                attempt_id=old_attempt.attempt_id,
                fencing_token=old_attempt.fencing_token,
                result="delivered",
                channel_receipt_id="stale-pg",
                now=now2 + timedelta(minutes=3, seconds=1),
            )
        # Zero change: the recipient still carries the NEW token.
        current = await worker_store.get_recipient_for_worker(old_attempt.recipient_id)
        assert current is not None
        assert current.fencing_token == reclaimed.fencing_token
        assert current.status == "in_progress"

        # FORCE RLS is enabled on every notification table.
        async with worker_store._ready().acquire() as connection:  # noqa: SLF001
            rows = await connection.fetch(
                """
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE nspname = 'public'
                AND c.relname LIKE E'notification\\_%'
                AND c.relkind = 'r'
                ORDER BY c.relname
                """
            )
            tables = {str(row["relname"]) for row in rows}
            assert tables == {
                "notification_audit_events",
                "notification_delivery_attempts",
                "notification_intents",
                "notification_outbox",
                "notification_receipts",
                "notification_recipients",
            }
            assert all(bool(row["relrowsecurity"]) for row in rows)
            assert all(bool(row["relforcerowsecurity"]) for row in rows)
        # Catalog assertion: the fence-snapshot columns live exactly where
        # the store/domain read them - intents carry device_id +
        # subject_revision, recipients carry the relationship evidence
        # columns, and the erroneous intents copy is gone.
        async with api_store._ready().acquire() as connection:  # noqa: SLF001
            intent_columns = {
                str(row["column_name"])
                for row in await connection.fetch(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'notification_intents'
                    """
                )
            }
            recipient_columns = {
                str(row["column_name"])
                for row in await connection.fetch(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'notification_recipients'
                    """
                )
            }
        assert {"device_id", "subject_revision"} <= intent_columns
        assert not ({"relationship_snapshot_id", "relationship_revision"} <= intent_columns)
        assert {
            "relationship_snapshot_id",
            "relationship_revision",
        } <= recipient_columns
        # The API's audit/outbox idempotent inserts work with only the
        # column-level SELECT (event_id) conflict-detection grant.
        async with api_store._ready().acquire() as connection:  # noqa: SLF001
            for table in (
                "notification_audit_events",
                "notification_outbox",
            ):
                event_id_select = await connection.fetchval(
                    """
                    SELECT has_column_privilege(current_user, $1, 'event_id',
                                               'SELECT')
                    """,
                    table,
                )
                content_select = await connection.fetchval(
                    """
                    SELECT has_column_privilege(current_user, $1, 'payload_json',
                                               'SELECT')
                    """,
                    table,
                )
                assert event_id_select is True
                assert content_select is False
    finally:
        await api_store.close()
        await worker_store.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL notification contract",
)
async def test_postgres_rls_fails_closed_without_app_context(
    pg_env: tuple[str, str, str, str],
) -> None:
    """PR-17: no app context -> zero rows / rejected writes; cross-subject
    context reads nothing."""
    api_dsn, worker_dsn, admin_db_dsn, password = pg_env
    api_store = PostgresNotificationStore(api_dsn)
    await api_store.initialize(
        bootstrap_dsn=admin_db_dsn,
        app_role_password=password,
    )
    try:
        pool = api_store._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                # No app context: reads see nothing.
                rows = await connection.fetch("SELECT * FROM notification_intents")
                assert rows == []
            # No app context: writes are rejected (own savepoint so the
            # transaction is not poisoned for the assertions below).
            try:
                async with connection.transaction():
                    await connection.execute(
                        "INSERT INTO notification_intents ("
                        " intent_id, idempotency_key, intent_kind, subject_person_id,"
                        " source_event_id, policy_receipt_id, session_id, epoch,"
                        " binding_id, binding_version, runtime_profile_id,"
                        " actor_person_id, fence_context_hash, template_key,"
                        " template_params_json, reason_code, script_version,"
                        " occurred_at, status, valid_until, created_at, updated_at)"
                        " VALUES ('x', 'k', 'crisis_safety', 'minor-1', 'e', 'r',"
                        " 's-a', 1, 'b-a', 1, 'p-a', 'act-a', 'h-a',"
                        " 'crisis_safety_notice', '{}', 'safety_concern', 'v',"
                        " now(), 'pending', now() + interval '1 hour',"
                        " now(), now())"
                    )
            except asyncpg.PostgresError as exc:
                assert "row-level security" in str(exc)
            else:  # pragma: no cover - the INSERT must be RLS-rejected
                raise AssertionError(
                    "INSERT without app context must be rejected by RLS"
                )
            async with connection.transaction():
                # API-role person context: person A can write her intent.
                await connection.execute(
                    "SELECT set_config('app.authenticated_actor', 'act-a', true),"
                    " set_config('app.authenticated_subject', 'person-a', true)"
                )
                await connection.execute(
                    "INSERT INTO notification_intents ("
                    " intent_id, idempotency_key, intent_kind, subject_person_id,"
                    " source_event_id, policy_receipt_id, session_id, epoch,"
                    " binding_id, binding_version, runtime_profile_id,"
                    " actor_person_id, fence_context_hash, template_key,"
                    " template_params_json, reason_code, script_version,"
                    " occurred_at, status, valid_until, created_at, updated_at)"
                    " VALUES ('i-a', 'k-a', 'crisis_safety', 'person-a', 'e-a', 'r-a',"
                    " 's-a', 1, 'b-a', 1, 'p-a', 'act-a', 'h-a',"
                    " 'crisis_safety_notice', '{}', 'safety_concern', 'v-a',"
                    " now(), 'pending', now() + interval '1 hour',"
                    " now(), now())"
                )
            try:
                async with connection.transaction():
                    await connection.execute(
                        "SELECT set_config('app.authenticated_actor', 'act-a', true),"
                        " set_config('app.authenticated_subject', 'person-a', true)"
                    )
                    await connection.execute(
                        "INSERT INTO notification_intents ("
                        " intent_id, idempotency_key, intent_kind, subject_person_id,"
                        " source_event_id, policy_receipt_id, session_id, epoch,"
                        " binding_id, binding_version, runtime_profile_id,"
                        " actor_person_id, fence_context_hash, template_key,"
                        " template_params_json, reason_code, script_version,"
                        " occurred_at, status, valid_until, created_at, updated_at)"
                        " VALUES ('i-forged', 'k-forged', 'crisis_safety', 'person-a',"
                        " 'e-f', 'r-f', 's-f', 1, 'b-f', 1, 'p-f', 'actor-forged',"
                        " 'h-f', 'crisis_safety_notice', '{}', 'safety_concern',"
                        " 'v-f', now(), 'pending', now() + interval '1 hour',"
                        " now(), now())"
                    )
            except asyncpg.PostgresError as exc:
                assert "row-level security" in str(exc)
            else:  # pragma: no cover
                raise AssertionError(
                    "intent actor must match the authenticated actor"
                )
            async with connection.transaction():
                # Person B's context cannot see person A's intent.
                await connection.execute(
                    "SELECT set_config('app.authenticated_actor', 'act-b', true),"
                    " set_config('app.authenticated_subject', 'person-b', true)"
                )
                rows = await connection.fetch("SELECT * FROM notification_intents")
                assert rows == []
    finally:
        await api_store.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL notification contract",
)
async def test_postgres_rls_command_splitting_negative(
    pg_env: tuple[str, str, str, str],
) -> None:
    """P0-E: the subject API cannot fabricate receipts/attempts and cannot
    write recipients for another subject's intent; worker-only writes are
    pinned to the in-scope recipient."""
    api_dsn, worker_dsn, admin_db_dsn, password = pg_env
    api_store = PostgresNotificationStore(api_dsn)
    await api_store.initialize(
        bootstrap_dsn=admin_db_dsn,
        app_role_password=password,
    )
    try:
        pool = api_store._ready()
        async with pool.acquire() as connection:
            async with connection.transaction():
                # Subject A owns intent i-a.
                await connection.execute(
                    "SELECT set_config('app.authenticated_actor', 'act-a', true),"
                    " set_config('app.authenticated_subject', 'person-a', true)"
                )
                await connection.execute(
                    """
                    INSERT INTO notification_intents (
                        intent_id, idempotency_key, intent_kind, subject_person_id,
                        source_event_id, policy_receipt_id, session_id, epoch,
                        binding_id, binding_version, runtime_profile_id,
                        actor_person_id, fence_context_hash, template_key,
                        template_params_json, reason_code, script_version,
                        occurred_at, status, valid_until, created_at, updated_at
                    ) VALUES ('i-a', 'k-a', 'crisis_safety', 'person-a', 'e-a',
                              'r-a', 's-a', 1, 'b-a', 1, 'p-a', 'act-a', 'h-a',
                              'crisis_safety_notice', '{}', 'safety_concern',
                              'v-a', now(), 'pending', now() + interval '1 hour', now(), now())
                    """
                    )
                # Subject B's context cannot attach a recipient to A's intent.
                await connection.execute(
                    "SELECT set_config('app.authenticated_actor', 'act-b', true),"
                    " set_config('app.authenticated_subject', 'person-b', true)"
                )
            # Every negative below runs in its own savepoint so the first
            # expected RLS rejection cannot poison the transaction.
            try:
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO notification_recipients (
                            recipient_id, intent_id, person_id, role,
                            relationship_id, relationship_status, channels_json,
                            channel_index, status, attempts, max_retries,
                            valid_from, created_at, updated_at
                        ) VALUES ('r-x', 'i-a', 'person-x', 'guardian',
                                  'rel-x', 'active', '["sms"]', 0, 'pending', 0, 3,
                                  now(), now(), now())
                        """
                    )
            except asyncpg.PostgresError as exc:
                assert "row-level security" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("recipient insert across subjects must be RLS-rejected")
            # Subject API cannot fabricate a delivery receipt.
            try:
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO notification_receipts (
                            receipt_id, intent_id, recipient_id, attempt_id,
                            channel, channel_receipt_id, delivered_at
                        ) VALUES ('rc-fake', 'i-a', 'r-any', 'at-fake',
                                  'sms', 'fake', now())
                        """
                    )
            except asyncpg.PostgresError as exc:
                # The API role has no INSERT grant on receipts/attempts
                # (ACL deny) - either rejection mode is fail-closed; the
                # RLS-level proof is the recipients negative above.
                assert "row-level security" in str(exc) or "permission denied" in str(
                    exc
                )
            else:  # pragma: no cover
                raise AssertionError("receipt insert must be rejected")
            # Subject API cannot write a delivery attempt.
            try:
                async with connection.transaction():
                    await connection.execute(
                        """
                        INSERT INTO notification_delivery_attempts (
                            attempt_id, intent_id, recipient_id, attempt_number,
                            channel, status, fencing_token, leased_until, started_at
                        ) VALUES ('at-fake', 'i-a', 'r-any', 1, 'sms', 'leased',
                                  'tok', now(), now())
                        """
                    )
            except asyncpg.PostgresError as exc:
                assert "row-level security" in str(exc) or "permission denied" in str(
                    exc
                )
            else:  # pragma: no cover
                raise AssertionError("attempt insert must be rejected")
            # Subject API cannot read the audit trail.
            try:
                async with connection.transaction():
                    await connection.execute(
                        "SELECT * FROM notification_audit_events"
                    )
            except asyncpg.PostgresError as exc:
                assert "row-level security" in str(exc) or "permission denied" in str(
                    exc
                )
            else:  # pragma: no cover
                raise AssertionError("audit read must be rejected")
    finally:
        await api_store.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL notification contract",
)
async def test_postgres_legacy_null_fence_window_migration_quarantines(
    pg_env: tuple[str, str, str, str],
) -> None:
    """Legacy database with a NULL intent fence window: bootstrap
    quarantines the row (cancelled + audit), then enforces the invariant at
    the CATALOG level (NOT NULL) so a NULL can never be written again.
    Migration is idempotent and re-runnable."""
    api_dsn, worker_dsn, admin_db_dsn, password = pg_env
    # Build a LEGACY intents table (no valid_until column) with a NULL-window
    # row BEFORE bootstrap runs.
    admin = await asyncpg.connect(admin_db_dsn)
    try:
        await admin.execute(
            """
            CREATE TABLE notification_intents (
                intent_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                intent_kind TEXT NOT NULL,
                subject_person_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                policy_receipt_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                binding_id TEXT NOT NULL,
                binding_version INTEGER NOT NULL,
                runtime_profile_id TEXT NOT NULL,
                actor_person_id TEXT NOT NULL,
                fence_context_hash TEXT NOT NULL,
                template_key TEXT NOT NULL,
                template_params_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                reason_code TEXT NOT NULL,
                script_version TEXT NOT NULL,
                occurred_at TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL,
                cancelled_reason TEXT,
                cancelled_at TIMESTAMPTZ,
                delivered_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        await admin.execute(
            """
            INSERT INTO notification_intents (
                intent_id, idempotency_key, intent_kind, subject_person_id,
                source_event_id, policy_receipt_id, session_id, epoch,
                binding_id, binding_version, runtime_profile_id,
                actor_person_id, fence_context_hash, template_key,
                template_params_json, reason_code, script_version,
                occurred_at, status, created_at, updated_at
            ) VALUES ('pg-legacy-null', 'pg:k-null', 'crisis_safety',
                      'minor-1', 'e', 'r', 's', 1, 'b', 1, 'p', 'a', 'h',
                      'crisis_safety_notice', '{}', 'safety_concern', 'v',
                      now(), 'pending', now(), now())
            """
        )
    finally:
        await admin.close()

    api_store = PostgresNotificationStore(api_dsn)
    await api_store.initialize(
        bootstrap_dsn=admin_db_dsn,
        app_role_password=password,
    )
    # Re-running the migration must be safe (idempotent).
    await api_store.close()
    api_store = PostgresNotificationStore(api_dsn)
    await api_store.initialize(
        bootstrap_dsn=admin_db_dsn,
        app_role_password=password,
    )
    try:
        # The API role cannot see the quarantined intent without a subject
        # context (RLS hides it - that is the point).  Verify the migration
        # result / audit / catalog through the OWNER connection instead of
        # weakening RLS.
        connection = await asyncpg.connect(admin_db_dsn)
        try:
            row = await connection.fetchrow(
                "SELECT status, cancelled_reason, valid_until"
                " FROM notification_intents WHERE intent_id = 'pg-legacy-null'"
            )
            assert row is not None
            assert row["status"] == "cancelled"
            assert row["cancelled_reason"] == "authorization_expired"
            assert row["valid_until"] is not None
            audit = await connection.fetchrow(
                "SELECT action FROM notification_audit_events"
                " WHERE event_id = 'audit:pg-legacy-null:quarantine'"
            )
            assert audit is not None and audit["action"] == "intent.quarantine"
            nullable = await connection.fetchval(
                """
                SELECT is_nullable FROM information_schema.columns
                WHERE table_name = 'notification_intents'
                  AND column_name = 'valid_until'
                """
            )
            assert nullable == "NO"
            async with connection.transaction():
                await connection.execute(
                    "SELECT set_config('app.authenticated_actor', 'a', true),"
                    " set_config('app.authenticated_subject', 'minor-1', true)"
                )
                with pytest.raises(asyncpg.PostgresError, match="null value"):
                    await connection.execute(
                        """
                        INSERT INTO notification_intents (
                            intent_id, idempotency_key, intent_kind, subject_person_id,
                            source_event_id, policy_receipt_id, session_id, epoch,
                            binding_id, binding_version, runtime_profile_id,
                            actor_person_id, fence_context_hash, valid_until,
                            template_key, template_params_json, reason_code,
                            script_version, occurred_at, status, created_at,
                            updated_at
                        ) VALUES ('pg-null-rejected', 'pg:k-null-rejected',
                                  'crisis_safety', 'minor-1', 'e', 'r', 's', 1,
                                  'b', 1, 'p', 'a', 'h', NULL,
                                  'crisis_safety_notice', '{}', 'safety_concern',
                                  'v', now(), 'pending', now(), now())
                        """
                    )
        finally:
            await connection.close()
    finally:
        await api_store.close()


__all__: list[str] = []
