"""Production wiring contract: the Control main path must consume the
multi-role stack (API/worker DSNs + real roles), enqueue crisis intents
through the coordinator (canonical receipt + fence + authoritative
relationships; missing authority -> 503) and run the worker as a first-
class lifecycle.  The legacy outbox path is never an authority."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg
import pytest
from services.notification.domain import (
    NotificationFence,
    RecipientSpec,
    RelationshipSnapshot,
)
from services.notification.production import (
    CrisisEnqueueCoordinator,
    NotificationDeliveryWorker,
    NotificationProductionSettings,
    NotificationProductionUnavailableError,
    build_production_notification_stack,
    initialize_production_notification_stack,
    legacy_outbox_write_prohibited,
)
from services.notification.repository import (
    ChannelResult,
    DeliveryAttempt,
    InMemoryNotificationReceiptVerifier,
    InMemoryOperatorAuthorizationPort,
    InMemoryRelationshipResolver,
    RecipientBinding,
)
from services.notification.tests.receipt_helpers import make_notification_receipt

API_ROLE = "memoria_notification_api"
WORKER_ROLE = "memoria_notification_worker"


def _dsn_with(
    dsn: str, *, database: str, user: str | None = None, password: str | None = None
) -> str:
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
    dsn = os.environ["MEMORIA_TEST_POSTGRES_DSN"]
    db_name = f"notification_prod_{uuid.uuid4().hex[:8]}"
    password = "memoria_local_test_password"
    admin = await asyncpg.connect(dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
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
            await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await admin.close()


class FakeChannel:
    channel = "wechat_subscription"

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(
        self,
        *,
        recipient: RecipientBinding,
        content: str,
        attempt: DeliveryAttempt,
    ) -> ChannelResult:
        self.sent.append(attempt.logical_delivery_key)
        return ChannelResult(ok=True, channel_receipt_id=f"ext-{attempt.attempt_id}")

    async def reconcile(
        self,
        *,
        logical_delivery_key: str,
        channel_receipt_id: str | None = None,
    ) -> ChannelResult | None:
        return None


def _fence() -> NotificationFence:
    return NotificationFence(
        device_id="device-1",
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_version=1,
        runtime_profile_id="profile-1",
        actor_person_id="actor-1",
        subject_person_id="minor-1",
        valid_until=datetime.now(UTC) + timedelta(hours=2),
    )


def _snapshot() -> RelationshipSnapshot:
    return RelationshipSnapshot(
        relationship_id="rel-1",
        subject_person_id="minor-1",
        person_id="guardian-1",
        role="guardian",  # type: ignore[arg-type]
        status="active",  # type: ignore[arg-type]
        snapshot_id="rel-snap-rel-1",
        revision=1,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to run the PostgreSQL contract",
)
async def test_production_enqueue_dispatch_and_roles(
    pg_env: tuple[str, str, str, str],
) -> None:
    api_dsn, worker_dsn, admin_db_dsn, password = pg_env
    settings = NotificationProductionSettings(
        api_dsn=api_dsn,
        worker_dsn=worker_dsn,
        bootstrap_dsn=admin_db_dsn,
        app_role_password=password,
        lease_seconds=60.0,
        send_timeout_seconds=10.0,
        reconcile_timeout_seconds=2.0,
    )
    verifier = InMemoryNotificationReceiptVerifier()
    resolver = InMemoryRelationshipResolver()
    authorizer = InMemoryOperatorAuthorizationPort()
    stack = build_production_notification_stack(
        settings,
        receipt_verifier=verifier,
        relationship_resolver=resolver,
        operator_authorizer=authorizer,
        sensitive_write=object(),
    )
    await initialize_production_notification_stack(stack, settings)
    try:
        # Real-role assertions (P0-2): command authority comes from the DB
        # role, never from GUCs.
        async with stack.api_store._ready().acquire() as connection:  # noqa: SLF001
            row = await connection.fetchrow(
                "SELECT current_user AS usr,"
                " (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
                " AS su,"
                " (SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user)"
                " AS bypass"
            )
            assert row is not None
            assert row["usr"] == API_ROLE
            assert row["su"] is False and row["bypass"] is False
        async with stack.worker_store._ready().acquire() as connection:  # noqa: SLF001
            row = await connection.fetchrow(
                "SELECT current_user AS usr,"
                " (SELECT rolsuper FROM pg_roles WHERE rolname = current_user)"
                " AS su"
            )
            assert row is not None
            assert row["usr"] == WORKER_ROLE
            assert row["su"] is False
        # Authoritative evidence: receipt + relationship.
        fence = _fence()
        snapshot = _snapshot()
        verifier.register(
            make_notification_receipt(
                receipt_id="policy-receipt-prod",
                actor_person_id="actor-1",
                subject_person_id="minor-1",
                fence=fence,
                relationship_snapshot_ids=(snapshot.snapshot_id,),
            )
        )
        resolver.register(snapshot)
        coordinator = CrisisEnqueueCoordinator(stack)
        intent_id = await coordinator.enqueue_crisis(_Request(fence, snapshot))
        channel = FakeChannel()
        worker = NotificationDeliveryWorker(
            stack.worker_service, {"wechat_subscription": channel}
        )
        await worker.run_once()
        assert len(channel.sent) == 1
        intent = await stack.api_service.get_intent(intent_id, actor_person_id="minor-1")
        assert intent is not None and intent.status == "delivered"
    finally:
        await stack.close()


class _Request:
    def __init__(
        self, fence: NotificationFence, snapshot: RelationshipSnapshot
    ) -> None:
        self.fence = fence
        self.policy_receipt_id = "policy-receipt-prod"
        self.subject_person_id = "minor-1"
        self.actor_person_id = "actor-1"
        self.intent_kind = "crisis_safety"  # type: ignore[assignment]
        self.template_key = "crisis_safety_notice"  # type: ignore[assignment]
        self.template_params = {
            "role_label": "孩子",
            "reason_code": "safety_concern",
            "action_hint": "联系监护人",
        }
        self.reason_code = "safety_concern"
        self.script_version = "2026-08-09.1"
        self.source_event_id = "crisis-event-prod"
        self.idempotency_key = "crisis:prod-1"
        self.occurred_at = datetime.now(UTC)
        self.recipients = (RecipientSpec(relationship_id="rel-1"),)  # type: ignore[arg-type]
        self.relationship_snapshot_refs = (
            (snapshot.snapshot_id, snapshot.revision),
        )


def test_build_requires_authorities() -> None:
    settings = NotificationProductionSettings(
        api_dsn="postgresql://memoria_notification_api@localhost/x",
        worker_dsn="postgresql://memoria_notification_worker@localhost/x",
    )
    with pytest.raises(NotificationProductionUnavailableError, match="half-wired"):
        build_production_notification_stack(
            settings,
            receipt_verifier=None,  # type: ignore[arg-type]
            relationship_resolver=None,  # type: ignore[arg-type]
            operator_authorizer=None,  # type: ignore[arg-type]
        )


def test_legacy_outbox_write_prohibited() -> None:
    with pytest.raises(NotificationProductionUnavailableError, match="NOT the production"):
        legacy_outbox_write_prohibited()
