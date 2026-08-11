"""FastAPI wiring for the production notification stack (PR-15).

This module is the SINGLE install/start/close hook for Control main: it
builds the API/worker stores + services from the real PostgreSQL roles,
installs a dedicated router (crisis enqueue + healthz) and owns the worker
lifecycle.  It never touches ``main.py``/``config.py`` - an integration
agent wires ONE call after the Session sub-agent closes those files.

Settings declaration for main/config (to be wired by the integration
agent)::

    NOTIFICATION_API_DSN          (str, required)
    NOTIFICATION_WORKER_DSN       (str, required)
    NOTIFICATION_BOOTSTRAP_DSN    (str, owner-only bootstrap; required at
                                   startup, never used for traffic)
    NOTIFICATION_APP_ROLE_PASSWORD(str, bootstrap only)
    NOTIFICATION_LEASE_SECONDS    (float, default 120)
    NOTIFICATION_SEND_TIMEOUT     (float, default 30, < lease)
    NOTIFICATION_RECONCILE_TIMEOUT(float, default 5, < lease)
    NOTIFICATION_MAX_RETRIES      (int, default 3)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from fastapi import APIRouter, HTTPException, Request

from services.notification.domain import Channel, RecipientSpec
from services.notification.production import (
    CrisisEnqueueCoordinator,
    CrisisEnqueueInput,
    NotificationDeliveryWorker,
    NotificationProductionSettings,
    NotificationProductionStack,
    NotificationProductionUnavailableError,
    build_production_notification_stack,
    initialize_production_notification_stack,
)
from services.notification.repository import (
    DeliveryChannelPort,
    NotificationReceiptVerifier,
    OperatorAuthorizationPort,
    PrincipalAuthorityPort,
    RelationshipResolver,
    RuntimeSessionAuthorityPort,
)

LOGGER = logging.getLogger(__name__)


@dataclass
class NotificationProductionWiring:
    """Installed wiring: owns the stack + coordinator + worker lifecycle.
    ``install`` returns this; ``start`` bootstraps + starts the worker;
    ``close`` drains detached sends and closes the stores."""

    settings: NotificationProductionSettings
    stack: NotificationProductionStack
    coordinator: CrisisEnqueueCoordinator
    worker: NotificationDeliveryWorker
    router: APIRouter
    _started: bool = False
    _worker_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Idempotent start: bootstrap (owner DSN) + own the worker loop as
        a tracked task.  Any failure leaves readiness False."""
        if self._started:
            return
        try:
            await initialize_production_notification_stack(self.stack, self.settings)
            self._worker_task = asyncio.create_task(self.worker.run_forever())
        except Exception:
            self._started = False
            raise
        self._started = True

    async def close(self) -> None:
        self.worker.stop()
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await asyncio.wait_for(self._worker_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
            self._worker_task = None
        await self.worker.shutdown(drain_timeout=5.0)
        await self.stack.close()
        self._started = False

    @property
    def ready(self) -> bool:
        return (
            self._started
            and self._worker_task is not None
            and not self._worker_task.done()
        )


def build_notification_router(
    coordinator: CrisisEnqueueCoordinator,
    *,
    principal: PrincipalAuthorityPort,
    session: RuntimeSessionAuthorityPort,
) -> APIRouter:
    router = APIRouter(prefix="/v1/production/notifications", tags=["notifications"])

    @router.post("/crisis-intents", status_code=201)
    async def enqueue_crisis(request: Request) -> dict[str, str]:
        payload = await request.json()
        now = datetime.now(UTC)
        actor = await principal.authenticate(
            headers=dict(request.headers), now=now
        )
        if actor is None:
            raise HTTPException(status_code=401, detail="unauthenticated")
        # The fence / active subject come from the SESSION authority - the
        # body can never override them.
        fence = await session.current_fence(actor_person_id=actor, now=now)
        if fence is None:
            raise HTTPException(status_code=403, detail="no current session fence")
        subject = await session.current_active_subject(
            actor_person_id=actor, now=now
        )
        if subject is None or subject != payload.get("subject_person_id"):
            raise HTTPException(
                status_code=403, detail="active subject does not match session"
            )
        # A3: the source event + occurred_at come from the authoritative
        # session safety event - the body can never attach a receipt to an
        # arbitrary event.
        safety_event = await session.current_safety_event(
            actor_person_id=actor, now=now
        )
        if safety_event is None:
            raise HTTPException(status_code=403, detail="no current safety event")
        source_event_id, occurred_at = safety_event
        if payload.get("source_event_id") != source_event_id:
            raise HTTPException(
                status_code=403, detail="source_event_id does not match the session"
            )
        try:
            spec_payloads = payload["recipients"]
            recipients = tuple(
                RecipientSpec(
                    relationship_id=item["relationship_id"],
                    channels=tuple(item.get("channels", ())),
                )
                for item in spec_payloads
            )
            # Callers may only reference relationships by id (+ optional
            # id/revision refs); the authoritative snapshot is re-read from
            # the resolver by the coordinator.
            snapshot_refs = tuple(
                (item["snapshot_id"], item.get("revision"))
                for item in payload.get("relationship_snapshot_refs", ())
            )

            # A2: the idempotency key is SERVER-DERIVED from the receipt +
            # source event + recipients - a caller can never swap in a new
            # key to replay the same receipt as a second side effect.
            server_idempotency_key = (
                f"{payload['policy_receipt_id']}:{source_event_id}:"
                + ":".join(
                    sorted(spec.relationship_id for spec in recipients)
                )
            )
            intent_id = await coordinator.enqueue_crisis(
                cast(
                    "CrisisEnqueueInput",
                    _CrisisRequest(
                    fence=fence,
                    actor_person_id=actor,
                    subject_person_id=subject,
                    policy_receipt_id=payload["policy_receipt_id"],
                    intent_kind=payload["intent_kind"],
                    template_key=payload["template_key"],
                    template_params=payload["template_params"],
                    reason_code=payload["reason_code"],
                    script_version=payload["script_version"],
                    source_event_id=source_event_id,
                    idempotency_key=server_idempotency_key,
                    occurred_at=occurred_at,
                    recipients=recipients,
                    relationship_snapshot_refs=snapshot_refs,
                    ),
                )
            )
            return {"intent_id": intent_id}
        except NotificationProductionUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("crisis enqueue failed")
            raise HTTPException(status_code=500, detail="internal error") from exc

    @router.get("/healthz")
    async def healthz(request: Request) -> dict[str, str]:
        wiring = getattr(request.app.state, "notification_wiring", None)
        if wiring is None or not wiring.ready:
            raise HTTPException(status_code=503, detail="not ready")
        return {"status": "ok", "ready": "true"}

    return router


def install_notification_production(
    app: object,
    settings: NotificationProductionSettings,
    *,
    receipt_verifier: NotificationReceiptVerifier,
    relationship_resolver: RelationshipResolver,
    operator_authorizer: OperatorAuthorizationPort,
    principal: PrincipalAuthorityPort,
    session: RuntimeSessionAuthorityPort,
    channels: dict[str, DeliveryChannelPort],
    stack: NotificationProductionStack | None = None,
) -> NotificationProductionWiring:
    """Single install hook: builds the stack, wires the router into the
    FastAPI app and returns the lifecycle handle (start/close)."""
    from fastapi import FastAPI

    if stack is None:
        stack = build_production_notification_stack(
            settings,
            receipt_verifier=receipt_verifier,
            relationship_resolver=relationship_resolver,
            operator_authorizer=operator_authorizer,
    )
    coordinator = CrisisEnqueueCoordinator(stack)
    router = build_notification_router(
        coordinator,
        principal=principal,
        session=session,
    )
    worker = NotificationDeliveryWorker(
        stack.worker_service,
        cast("Mapping[Channel, DeliveryChannelPort]", channels),
    )
    assert isinstance(app, FastAPI)
    app.include_router(router)
    wiring = NotificationProductionWiring(
        settings=settings,
        stack=stack,
        coordinator=coordinator,
        worker=worker,
        router=router,
    )
    app.state.notification_wiring = wiring
    return wiring


class _CrisisRequest:
    """Concrete adapter filling ``CrisisEnqueueInput`` from the HTTP
    payload (kept in this file so the router stays a thin shim)."""

    def __init__(self, **values: object) -> None:
        for name, value in values.items():
            setattr(self, name, value)
