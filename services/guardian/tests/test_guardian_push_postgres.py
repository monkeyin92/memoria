from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from services.guardian.domain import GuardianAccessDeniedError
from services.guardian.postgres_store import PostgresGuardianStore
from services.guardian.push import CrisisPushContent, CrisisPushWorker, PushSendResult
from services.guardian.tests.test_guardian_postgres_store import (
    _ensure_roles,
    _postgres_dsn,
    _role_dsn,
)

TEMPLATE = "pg-crisis-template"
OPENID = "pg-guardian-openid"

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
        reason="set MEMORIA_TEST_POSTGRES_DSN for the PostgreSQL guardian contract",
    ),
]


class _Sender:
    def __init__(self, result: PushSendResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str, CrisisPushContent]] = []

    async def send_crisis_alert(
        self,
        *,
        openid: str,
        template_id: str,
        content: CrisisPushContent,
    ) -> PushSendResult:
        self.calls.append((openid, template_id, content))
        return self.result


async def _name(guardian_user_id: str, minor_user_id: str) -> str | None:
    return "小红"


@asynccontextmanager
async def _guardian_database() -> AsyncIterator[tuple[PostgresGuardianStore, str, str]]:
    admin_dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    database = f"memoria_guardian_push_{uuid.uuid4().hex[:10]}"
    admin = await asyncpg.connect(admin_dsn)
    created = (False, False, False)
    store: PostgresGuardianStore | None = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        dsn = _postgres_dsn(admin_dsn, database=database)
        created = await _ensure_roles(admin, database=database)
        store = PostgresGuardianStore(
            dsn=_role_dsn(
                admin_dsn, database=database, role="memoria_guardian",
                password="api-role-password",
            ),
            bootstrap_dsn=dsn,
            maintenance_dsn=_role_dsn(
                admin_dsn, database=database, role="memoria_guardian_maintenance",
                password="maintenance-role-password",
            ),
            worker_dsn=_role_dsn(
                admin_dsn, database=database, role="memoria_guardian_worker",
                password="worker-role-password",
            ),
        )
        await store.initialize()
        api_dsn = _role_dsn(
            admin_dsn, database=database, role="memoria_guardian",
            password="api-role-password",
        )
        yield store, dsn, api_dsn
    finally:
        if store is not None:
            await store.close()
        try:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1",
                database,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        finally:
            role_created, maintenance_created, worker_created = created
            if maintenance_created:
                await admin.execute("DROP ROLE IF EXISTS memoria_guardian_maintenance")
            if worker_created:
                await admin.execute("DROP ROLE IF EXISTS memoria_guardian_worker")
            if role_created:
                await admin.execute("DROP ROLE IF EXISTS memoria_guardian")
            await admin.close()


async def _crisis(store: PostgresGuardianStore, *, turn_id: int) -> None:
    receipt = await store.enqueue_crisis_event(
        crisis_event_id=str(uuid.uuid4()),
        evidence_event_id=f"guardian-crisis:pg-{turn_id}",
        minor_user_id="pg-minor",
        occurred_at=datetime.now(UTC),
        script_version="crisis-transfer-draft-v1",
    )
    assert receipt.notification_count == 1


async def _link(store: PostgresGuardianStore) -> None:
    now = datetime.now(UTC)
    digest = hashlib.sha256(b"pg-push-binding").hexdigest()
    link = await store.create_link(
        guardian_user_id="pg-guardian",
        minor_user_id="pg-minor",
        relation="parent",
        verified_via="wechat_identity",
        binding_code_hash=digest,
        binding_expires_at=now + timedelta(minutes=15),
        now=now,
    )
    await store.confirm_link(
        link_id=link.link_id,
        minor_user_id="pg-minor",
        binding_code_hash=digest,
        now=now,
    )


async def _remaining(store: PostgresGuardianStore) -> int:
    subscription = await store.push_subscription(
        guardian_user_id="pg-guardian", template_id=TEMPLATE
    )
    return subscription.remaining if subscription is not None else 0


async def test_postgres_crisis_push_delivery_is_worker_only_and_idempotent() -> None:
    async with _guardian_database() as (store, bootstrap_dsn, api_dsn):
        await _link(store)
        await _crisis(store, turn_id=1)
        for _ in range(3):
            await store.record_push_subscription(
                guardian_user_id="pg-guardian",
                template_id=TEMPLATE,
                result="accept",
                openid=OPENID,
                now=datetime.now(UTC),
            )
        assert await _remaining(store) == 3

        # The API role sees only its own ledger and can never claim or settle.
        api = await asyncpg.connect(api_dsn)
        try:
            async with api.transaction():
                await api.execute(
                    "SELECT set_config('memoria.guardian_actor_id', 'pg-other', true),"
                    " set_config('memoria.guardian_subject_id', 'pg-other', true)"
                )
                assert await api.fetchval(
                    "SELECT count(*) FROM guardian_push_subscriptions"
                ) == 0
            for statement in (
                "SELECT * FROM guardian_crisis_push_claim("
                "'api', now(), 10, 60, 5, 86400)",
                "SELECT guardian_crisis_push_reserve("
                "gen_random_uuid(), 'api', 'pg-crisis-template', now())",
            ):
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await api.fetch(statement)
            async with api.transaction():
                await api.execute(
                    "SELECT set_config('memoria.guardian_actor_id', 'pg-guardian', true),"
                    " set_config('memoria.guardian_subject_id', 'pg-guardian', true)"
                )
                with pytest.raises(asyncpg.InsufficientPrivilegeError):
                    await api.execute(
                        "UPDATE guardian_notification_outbox SET status = 'delivered',"
                        " delivered_at = now()"
                    )
        finally:
            await api.close()

        # Concurrent claimers never hold the same notification.
        claims = await asyncio.gather(
            *(
                store.claim_crisis_pushes(
                    worker_id=f"pg-worker-{index}",
                    now=datetime.now(UTC),
                    limit=10,
                    lease_s=60,
                    max_attempts=5,
                    max_age_s=86_400,
                )
                for index in range(4)
            )
        )
        held = [(index, push) for index, batch in enumerate(claims) for push in batch]
        assert len(held) == 1
        holder, push = held[0]
        with pytest.raises(GuardianAccessDeniedError):
            await store.reserve_crisis_push_subscription(
                notification_id=push.notification_id,
                worker_id="pg-intruder",
                template_id=TEMPLATE,
                now=datetime.now(UTC),
            )
        assert await store.reserve_crisis_push_subscription(
            notification_id=push.notification_id,
            worker_id=f"pg-worker-{holder}",
            template_id=TEMPLATE,
            now=datetime.now(UTC),
        ) == OPENID
        # Re-reserving the same claim reuses the reservation.
        assert await store.reserve_crisis_push_subscription(
            notification_id=push.notification_id,
            worker_id=f"pg-worker-{holder}",
            template_id=TEMPLATE,
            now=datetime.now(UTC),
        ) == OPENID
        assert await _remaining(store) == 2
        assert await store.complete_crisis_push(
            notification_id=push.notification_id,
            worker_id=f"pg-worker-{holder}",
            outcome="retry",
            error_code="wechat_-1",
            retry_delay_s=30,
            exhaust_subscription=False,
            now=datetime.now(UTC),
        ) is True
        assert await _remaining(store) == 3  # refunded
        backing_off = await store.claim_crisis_pushes(
            worker_id="pg-worker-x",
            now=datetime.now(UTC),
            limit=10,
            lease_s=60,
            max_attempts=5,
            max_age_s=86_400,
        )
        assert backing_off == ()

        # The worker delivers once the backoff elapsed.
        sender = _Sender(PushSendResult("delivered"))
        later = datetime.now(UTC) + timedelta(seconds=31)
        worker = CrisisPushWorker(
            store,
            sender,
            template_id=TEMPLATE,
            display_name=_name,
            clock=lambda: later,
        )
        assert await worker.run_once() == 1
        notifications = await store.guardian_notifications(guardian_user_id="pg-guardian")
        assert [(item.status, item.attempts) for item in notifications] == [("delivered", 2)]
        assert await _remaining(store) == 2
        assert sender.calls[0][0] == OPENID
        assert sender.calls[0][2].child_display_name == "小红"

        # WeChat refusing the send exhausts the ledger.
        await _crisis(store, turn_id=2)
        refused = CrisisPushWorker(
            store,
            _Sender(PushSendResult("no_subscription", "wechat_43101")),
            template_id=TEMPLATE,
            display_name=_name,
        )
        assert await refused.run_once() == 1
        assert await _remaining(store) == 0
        statuses = sorted(
            item.status
            for item in await store.guardian_notifications(guardian_user_id="pg-guardian")
        )
        assert statuses == ["delivered", "no_subscription"]

        exported = await store.export_for_account(account_id="pg-guardian")
        ledger = exported["guardian_push_subscriptions"]
        assert isinstance(ledger, list) and len(ledger) == 1
        assert ledger[0]["openid_on_file"] is True
        assert OPENID not in repr(exported)
        assert (await store.delete_for_account(account_id="pg-guardian"))[
            "guardian_push_subscriptions"
        ] == 1
        assert "guardian_push_subscriptions" not in await store.remaining_account_rows(
            account_id="pg-guardian"
        )

        bootstrap = await asyncpg.connect(bootstrap_dsn)
        try:
            owners = {
                str(row["proname"]): str(row["rolname"])
                for row in await bootstrap.fetch(
                    """
                    SELECT p.proname, r.rolname FROM pg_proc p
                    JOIN pg_roles r ON r.oid = p.proowner
                    WHERE p.proname LIKE 'guardian_crisis_push_%'
                    """
                )
            }
            assert owners == {
                "guardian_crisis_push_claim": "memoria_guardian_maintenance",
                "guardian_crisis_push_reserve": "memoria_guardian_maintenance",
                "guardian_crisis_push_complete": "memoria_guardian_maintenance",
            }
            for function in owners:
                assert await bootstrap.fetchval(
                    "SELECT has_function_privilege('memoria_guardian_worker', p.oid, 'EXECUTE')"
                    " FROM pg_proc p WHERE p.proname = $1",
                    function,
                )
                assert not await bootstrap.fetchval(
                    "SELECT has_function_privilege('memoria_guardian', p.oid, 'EXECUTE')"
                    " FROM pg_proc p WHERE p.proname = $1",
                    function,
                )
        finally:
            await bootstrap.close()


async def test_postgres_schema_upgrades_a_pre_delivery_outbox_in_place() -> None:
    async with _guardian_database() as (store, bootstrap_dsn, _api_dsn):
        await _link(store)
        await _crisis(store, turn_id=7)
        schema = (
            Path(__file__).resolve().parents[1] / "postgres_schema.sql"
        ).read_text(encoding="utf-8")
        bootstrap = await asyncpg.connect(bootstrap_dsn)
        try:
            # Recreate the previous shape: old status CHECK, no delivery columns.
            await bootstrap.execute(
                """
                DROP POLICY IF EXISTS guardian_crisis_push_delivery
                    ON guardian_notification_outbox;
                DROP FUNCTION guardian_crisis_push_claim(
                    TEXT, TIMESTAMPTZ, INTEGER, INTEGER, INTEGER, INTEGER);
                DROP FUNCTION guardian_crisis_push_reserve(UUID, TEXT, TEXT, TIMESTAMPTZ);
                DROP FUNCTION guardian_crisis_push_complete(
                    UUID, TEXT, TEXT, TEXT, INTEGER, BOOLEAN, TIMESTAMPTZ);
                DROP INDEX IF EXISTS idx_guardian_notification_push_claim;
                ALTER TABLE guardian_notification_outbox
                    DROP COLUMN claimed_by, DROP COLUMN lease_until,
                    DROP COLUMN next_attempt_at, DROP COLUMN reserved_template_id,
                    DROP CONSTRAINT guardian_notification_outbox_status_check,
                    ADD CONSTRAINT guardian_notification_outbox_status_check
                        CHECK (status IN ('pending', 'delivered', 'failed'));
                DROP TABLE guardian_push_subscriptions;
                """
            )
            await bootstrap.execute(schema)
            await bootstrap.execute(schema)  # repeat-upgrade stays idempotent
            definition = await bootstrap.fetchval(
                """
                SELECT pg_get_constraintdef(oid) FROM pg_constraint
                WHERE conname = 'guardian_notification_outbox_status_check'
                """
            )
            assert "no_subscription" in str(definition)
            columns = {
                str(row["column_name"])
                for row in await bootstrap.fetch(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'guardian_notification_outbox'
                    """
                )
            }
            assert {
                "claimed_by",
                "lease_until",
                "next_attempt_at",
                "reserved_template_id",
            } <= columns
        finally:
            await bootstrap.close()
        notifications = await store.guardian_notifications(guardian_user_id="pg-guardian")
        assert [item.status for item in notifications] == ["pending"]
        worker = CrisisPushWorker(
            store,
            _Sender(PushSendResult("delivered")),
            template_id=TEMPLATE,
            display_name=_name,
        )
        assert await worker.run_once() == 1
        notifications = await store.guardian_notifications(guardian_user_id="pg-guardian")
        assert [item.status for item in notifications] == ["no_subscription"]
