"""FastAPI install hook contract: the production router is installed
directly into a real app and the crisis enqueue route drives the
coordinator -> service -> store chain (in-memory adapter here; the real
PostgreSQL chain is covered by test_production_wiring)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from services.notification.domain import NotificationFence
from services.notification.in_memory_store import InMemoryNotificationStore
from services.notification.production import (
    NotificationProductionSettings,
    NotificationProductionStack,
)
from services.notification.repository import (
    ChannelResult,
    DeliveryAttempt,
    InMemoryNotificationReceiptVerifier,
    InMemoryOperatorAuthorizationPort,
    InMemoryRelationshipResolver,
    RecipientBinding,
)
from services.notification.service import NotificationService
from services.notification.tests.receipt_helpers import make_notification_receipt
from services.notification.wiring import install_notification_production


class FakePrincipal:
    """Test principal: header ``x-test-actor`` wins; ``None`` otherwise."""

    async def authenticate(
        self,
        *,
        headers: dict[str, str],
        now: datetime,
    ) -> str | None:
        return headers.get("x-test-actor")


class FakeSessionAuthority:
    def __init__(
        self,
        fence: NotificationFence,
        *,
        safety_event: tuple[str, datetime] | None = None,
    ) -> None:
        self._fence = fence
        self._safety_event = safety_event or (
            "crisis-event-wiring",
            datetime.now(UTC),
        )

    async def current_fence(
        self,
        *,
        actor_person_id: str,
        now: datetime,
    ) -> NotificationFence | None:
        return self._fence

    async def current_active_subject(
        self,
        *,
        actor_person_id: str,
        now: datetime,
    ) -> str | None:
        return self._fence.subject_person_id

    async def current_safety_event(
        self,
        *,
        actor_person_id: str,
        now: datetime,
    ) -> tuple[str, datetime] | None:
        return self._safety_event


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


@pytest.mark.asyncio
async def test_crisis_enqueue_route_drives_coordinator() -> None:
    store = InMemoryNotificationStore()
    await store.initialize()
    verifier = InMemoryNotificationReceiptVerifier()
    resolver = InMemoryRelationshipResolver()
    fence = NotificationFence(
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
    from services.notification.domain import RelationshipSnapshot

    snapshot = RelationshipSnapshot(
        relationship_id="rel-1",
        subject_person_id="minor-1",
        person_id="guardian-1",
        role="guardian",  # type: ignore[arg-type]
        status="active",  # type: ignore[arg-type]
        snapshot_id="rel-snap-rel-1",
        revision=1,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    verifier.register(
        make_notification_receipt(
            receipt_id="policy-receipt-wiring",
            actor_person_id="actor-1",
            subject_person_id="minor-1",
            fence=fence,
            relationship_snapshot_ids=(snapshot.snapshot_id,),
        )
    )
    resolver.register(snapshot)
    service = NotificationService(
        store,
        receipt_verifier=verifier,
        relationship_resolver=resolver,
        operator_authorizer=InMemoryOperatorAuthorizationPort(),
        send_timeout_seconds=1.0,
    )
    stack = NotificationProductionStack(
        api_store=store,
        worker_store=store,
        api_service=service,
        worker_service=service,
        sensitive_write=object(),
    )
    settings = NotificationProductionSettings(
        api_dsn="postgresql://memoria_notification_api@localhost/x",
        worker_dsn="postgresql://memoria_notification_worker@localhost/x",
    )
    app = FastAPI()
    wiring = install_notification_production(
        app,
        settings,
        receipt_verifier=verifier,
        relationship_resolver=resolver,
        operator_authorizer=InMemoryOperatorAuthorizationPort(),
        principal=FakePrincipal(),
        session=FakeSessionAuthority(fence),
        channels={"wechat_subscription": FakeChannel()},
        stack=stack,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        payload = {
            "policy_receipt_id": "policy-receipt-wiring",
            "subject_person_id": "minor-1",
            "intent_kind": "crisis_safety",
            "template_key": "crisis_safety_notice",
            "template_params": {
                "role_label": "孩子",
                "reason_code": "safety_concern",
                "action_hint": "联系监护人",
            },
            "reason_code": "safety_concern",
            "script_version": "2026-08-09.1",
            "source_event_id": "crisis-event-wiring",
            "recipients": [{"relationship_id": "rel-1"}],
            "relationship_snapshot_refs": [
                {"snapshot_id": "rel-snap-rel-1", "revision": 1}
            ],
        }
        response = await client.post(
            "/v1/production/notifications/crisis-intents",
            headers={"x-test-actor": "actor-1"},
            json=payload,
        )
        assert response.status_code == 201, response.text
        intent_id = response.json()["intent_id"]
        # A2: the idempotency key is SERVER-derived - a second POST with
        # the same receipt + source event (even a different client-chosen
        # key) returns the SAME intent and creates NO second notification.
        replay = dict(payload)
        replay["idempotency_key"] = "client-chosen-different-key"
        response = await client.post(
            "/v1/production/notifications/crisis-intents",
            headers={"x-test-actor": "actor-1"},
            json=replay,
        )
        assert response.status_code == 201, response.text
        assert response.json()["intent_id"] == intent_id
        assert len(store.intents_by_key()) == 1
        intent = await service.get_intent(intent_id, actor_person_id="minor-1")
        assert intent is not None and intent.status == "pending"
    await wiring.close()


@pytest.mark.asyncio
async def test_forged_actor_and_drifted_relationship_403_503() -> None:
    """Wiring P0: the actor comes from the HTTP principal (a body actor
    cannot override it - 401 without credentials), the active subject must
    match the session (403), and a drifted relationship revision fails
    closed (503).  The route never writes anything silently."""
    store = InMemoryNotificationStore()
    await store.initialize()
    verifier = InMemoryNotificationReceiptVerifier()
    resolver = InMemoryRelationshipResolver()
    fence = NotificationFence(
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
    from services.notification.domain import RelationshipSnapshot

    snapshot = RelationshipSnapshot(
        relationship_id="rel-1",
        subject_person_id="minor-1",
        person_id="guardian-1",
        role="guardian",  # type: ignore[arg-type]
        status="active",  # type: ignore[arg-type]
        snapshot_id="rel-snap-rel-1",
        revision=2,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    verifier.register(
        make_notification_receipt(
            receipt_id="policy-receipt-wiring",
            actor_person_id="actor-1",
            subject_person_id="minor-1",
            fence=fence,
            relationship_snapshot_ids=(snapshot.snapshot_id,),
            relationship_snapshot_revisions=(2,),
        )
    )
    resolver.register(snapshot)
    service = NotificationService(
        store,
        receipt_verifier=verifier,
        relationship_resolver=resolver,
        operator_authorizer=InMemoryOperatorAuthorizationPort(),
        send_timeout_seconds=1.0,
    )
    stack = NotificationProductionStack(
        api_store=store,
        worker_store=store,
        api_service=service,
        worker_service=service,
        sensitive_write=object(),
    )
    settings = NotificationProductionSettings(
        api_dsn="postgresql://memoria_notification_api@localhost/x",
        worker_dsn="postgresql://memoria_notification_worker@localhost/x",
    )
    app = FastAPI()
    wiring = install_notification_production(
        app,
        settings,
        receipt_verifier=verifier,
        relationship_resolver=resolver,
        operator_authorizer=InMemoryOperatorAuthorizationPort(),
        principal=FakePrincipal(),
        session=FakeSessionAuthority(
            fence, safety_event=("crisis-event-wiring-3", datetime.now(UTC))
        ),
        channels={"wechat_subscription": FakeChannel()},
        stack=stack,
    )
    base_payload = {
        "policy_receipt_id": "policy-receipt-wiring",
        "subject_person_id": "minor-1",
        "intent_kind": "crisis_safety",
        "template_key": "crisis_safety_notice",
        "template_params": {
            "role_label": "孩子",
            "reason_code": "safety_concern",
            "action_hint": "联系监护人",
        },
        "reason_code": "safety_concern",
        "script_version": "2026-08-09.1",
        "source_event_id": "crisis-event-wiring-3",
        "recipients": [{"relationship_id": "rel-1"}],
        "relationship_snapshot_refs": [
            {"snapshot_id": "rel-snap-rel-1", "revision": 1}
        ],
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        # No credentials -> 401 (the body cannot authenticate the caller).
        response = await client.post(
            "/v1/production/notifications/crisis-intents", json=base_payload
        )
        assert response.status_code == 401, response.text
        # Authenticated actor but body subject does not match the session
        # -> 403 (the caller can never claim another subject).
        forged = dict(base_payload)
        forged["subject_person_id"] = "other-minor"
        response = await client.post(
            "/v1/production/notifications/crisis-intents",
            headers={"x-test-actor": "actor-1"},
            json=forged,
        )
        assert response.status_code == 403, response.text
        # A3: a body claiming a DIFFERENT source event than the session is
        # rejected - the receipt cannot be replayed for another event.
        forged_event = dict(base_payload)
        forged_event["source_event_id"] = "forged-event-999"
        response = await client.post(
            "/v1/production/notifications/crisis-intents",
            headers={"x-test-actor": "actor-1"},
            json=forged_event,
        )
        assert response.status_code == 403, response.text
        # Snapshot revision drift (resolver now at revision 2, caller refs
        # revision 1) -> 503; nothing is written.
        stale = dict(base_payload)
        response = await client.post(
            "/v1/production/notifications/crisis-intents",
            headers={"x-test-actor": "actor-1"},
            json=stale,
        )
        assert response.status_code == 503, response.text
    # Healthz reports not-ready before start.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/v1/production/notifications/healthz"
        )
        assert response.status_code == 503, response.text
    await wiring.close()
