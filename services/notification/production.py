"""Production wiring for the multi-role notification stack (PR-15/§11.6).

Control/main consumes THIS module instead of the legacy guardian outbox
path: the API service (subject-scoped create/cancel/replay) and the worker
service (claim/dispatch/reconcile) are built from TWO SEPARATE PostgreSQL
DSNs with the REAL ``memoria_notification_api`` / ``memoria_notification_worker``
roles (P0-2: command authority comes from the database role, never from
GUCs).  Crisis enqueue goes through :class:`CrisisEnqueueCoordinator`,
which requires the canonical ``PolicyReceiptV2`` + ``NotificationFence`` +
authoritative relationship evidence: a missing authority fails closed with
``NotificationProductionUnavailableError`` (503 semantics) and NEVER
silently falls back to the legacy outbox.  The worker is a first-class
lifecycle object (``NotificationDeliveryWorker.run_once`` /
``run_forever``), not a user-facing drain.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from services.notification.domain import (
    Channel,
    IntentKind,
    NotificationFence,
    RecipientSpec,
    TemplateKey,
)
from services.notification.postgres_store import PostgresNotificationStore
from services.notification.repository import (
    DeliveryChannelPort,
    NotificationReceiptVerifier,
    NotificationStore,
    OperatorAuthorizationPort,
    RelationshipResolver,
)
from services.notification.service import NotificationService
from services.policy.production_wiring import SensitiveWriteService

LOGGER = logging.getLogger(__name__)


class NotificationProductionUnavailableError(RuntimeError):
    """503 semantics: a required authority (policy receipt verifier,
    relationship resolver, operator authorizer) or the production
    PostgreSQL stack is not wired.  The caller must fail closed and MUST
    NOT silently fall back to the legacy outbox path."""


@dataclass(frozen=True, slots=True)
class NotificationProductionSettings:
    """DSN/role settings for the production notification stack.

    ``api_dsn`` and ``worker_dsn`` MUST be different connections/roles:
    the API role can never execute worker commands and vice versa (P0-2).
    ``bootstrap_dsn`` + ``app_role_password`` are used ONLY for schema /
    role bootstrap at startup; they are never used for business traffic.
    """

    api_dsn: str
    worker_dsn: str
    api_role: str = "memoria_notification_api"
    worker_role: str = "memoria_notification_worker"
    bootstrap_dsn: str | None = None
    app_role_password: str | None = None
    lease_seconds: float = 120.0
    send_timeout_seconds: float = 30.0
    reconcile_timeout_seconds: float = 5.0
    max_retries: int = 3

    def __post_init__(self) -> None:
        for name, value in (
            ("api_dsn", self.api_dsn),
            ("worker_dsn", self.worker_dsn),
        ):
            if not value or not value.strip():
                raise ValueError(f"{name} must not be empty")
        if not 0 < self.send_timeout_seconds < self.lease_seconds:
            raise ValueError(
                "send_timeout_seconds must be smaller than lease_seconds"
            )
        if not 0 < self.reconcile_timeout_seconds < self.lease_seconds:
            raise ValueError(
                "reconcile_timeout_seconds must be smaller than lease_seconds"
            )


@dataclass(frozen=True, slots=True)
class NotificationProductionStack:
    """API + worker stores/services wired from the real PostgreSQL roles."""

    api_store: NotificationStore
    worker_store: NotificationStore
    api_service: NotificationService
    worker_service: NotificationService
    #: Policy SensitiveWriteService (same-connection seam): without it
    #: crisis enqueue fails closed (503) - the separate in-process
    #: verifier is never the production write authority.
    sensitive_write: SensitiveWriteService | None = None

    async def close(self) -> None:
        await self.api_store.close()
        await self.worker_store.close()


def build_production_notification_stack(
    settings: NotificationProductionSettings,
    *,
    receipt_verifier: NotificationReceiptVerifier,
    relationship_resolver: RelationshipResolver,
    operator_authorizer: OperatorAuthorizationPort,
    sensitive_write: SensitiveWriteService | None = None,
    initialize: bool = False,
) -> NotificationProductionStack:
    """Build the production stack.  The authorities are REQUIRED: without
    them the service fails closed at every sensitive operation - passing
    ``None`` here is never allowed (use the coordinator's 503 path
    instead of constructing a half-wired service)."""
    if (
        receipt_verifier is None
        or relationship_resolver is None
        or operator_authorizer is None
    ):
        raise NotificationProductionUnavailableError(
            "production notification stack requires the policy receipt "
            "verifier, relationship resolver and operator authorizer; "
            "a half-wired service is never constructed (503)"
        )
    api_store = PostgresNotificationStore(settings.api_dsn).with_role("api")
    worker_store = PostgresNotificationStore(settings.worker_dsn).with_role(
        "worker"
    )
    return NotificationProductionStack(
        api_store=api_store,
        worker_store=worker_store,
        api_service=NotificationService(
            api_store,
            lease_seconds=settings.lease_seconds,
            max_retries=settings.max_retries,
            receipt_verifier=receipt_verifier,
            relationship_resolver=relationship_resolver,
            operator_authorizer=operator_authorizer,
            send_timeout_seconds=settings.send_timeout_seconds,
            reconcile_timeout_seconds=settings.reconcile_timeout_seconds,
        ),
        worker_service=NotificationService(
            worker_store,
            lease_seconds=settings.lease_seconds,
            max_retries=settings.max_retries,
            receipt_verifier=receipt_verifier,
            relationship_resolver=relationship_resolver,
            operator_authorizer=operator_authorizer,
            send_timeout_seconds=settings.send_timeout_seconds,
            reconcile_timeout_seconds=settings.reconcile_timeout_seconds,
        ),
        sensitive_write=sensitive_write,
    )


async def initialize_production_notification_stack(
    stack: NotificationProductionStack,
    settings: NotificationProductionSettings,
) -> None:
    """Bootstrap the schema/roles through the OWNER DSN (never used for
    business traffic) and initialize both role adapters; a missing
    bootstrap authority fails closed."""
    if not settings.bootstrap_dsn or not settings.app_role_password:
        raise NotificationProductionUnavailableError(
            "notification production bootstrap requires the owner DSN and "
            "app role password; refusing to run half-initialized (503)"
        )
    await cast(PostgresNotificationStore, stack.api_store).initialize(
        bootstrap_dsn=settings.bootstrap_dsn,
        app_role_password=settings.app_role_password,
    )
    await cast(PostgresNotificationStore, stack.worker_store).initialize()


class CrisisEnqueueInput(Protocol):
    """One crisis notification request.  The actor, fence and active
    subject are derived by the WIRING layer from the HTTP principal +
    runtime session authority (never from the caller); relationship
    evidence is referenced by id/revision refs only and RE-READ from the
    authoritative resolver at enqueue time."""

    fence: NotificationFence
    policy_receipt_id: str
    subject_person_id: str
    actor_person_id: str
    intent_kind: IntentKind
    template_key: TemplateKey
    template_params: dict[str, str]
    reason_code: str
    script_version: str
    source_event_id: str
    idempotency_key: str
    occurred_at: datetime
    recipients: tuple[RecipientSpec, ...]
    relationship_snapshot_refs: tuple[tuple[str, int | None], ...]


class CrisisEnqueueCoordinator:
    """Production crisis enqueue: writes a NEW NotificationIntent through
    the API service with the canonical receipt/fence/relationship evidence.
    Missing authority (no verifier / resolver / operator authorizer) fails
    closed with 503 - the legacy outbox is NEVER written behind the
    caller's back."""

    def __init__(self, stack: NotificationProductionStack) -> None:
        self._stack = stack

    async def enqueue_crisis(
        self, request: CrisisEnqueueInput
    ) -> str:
        service = self._stack.api_service
        if self._stack.sensitive_write is None:
            raise NotificationProductionUnavailableError(
                "crisis enqueue requires the Policy SensitiveWriteService "
                "(same-connection seam); the separate in-process receipt "
                "verifier is NOT production authority (503)"
            )
        if service.receipt_verifier is None:
            raise NotificationProductionUnavailableError(
                "crisis enqueue requires the policy receipt verifier (503)"
            )
        if service.relationship_resolver is None:
            raise NotificationProductionUnavailableError(
                "crisis enqueue requires the authoritative relationship "
                "resolver (503)"
            )
        if not request.recipients:
            raise NotificationProductionUnavailableError(
                "crisis enqueue requires at least one recipient (503)"
            )
        # Authoritative data flow (P0-5): relationships are referenced by
        # id (plus an optional id/revision REF - never a full snapshot from
        # the caller).  The resolver's CURRENT snapshot is the only
        # authority: a missing/revoked relationship, a snapshot-id drift or
        # a revision drift fails closed (503) and the service persists the
        # recipient rows from the resolver's snapshots.
        resolver = service.relationship_resolver
        assert resolver is not None
        now = request.occurred_at
        ref_by_id = {
            snapshot_id: revision
            for snapshot_id, revision in request.relationship_snapshot_refs
        }
        for spec in request.recipients:
            current = await resolver.get_relationship(
                spec.relationship_id, now=now
            )
            if current is None or not current.is_active(now):
                raise NotificationProductionUnavailableError(
                    f"relationship {spec.relationship_id} is not active at "
                    "enqueue time; "
                    "refusing to notify (503)"
                )
            ref_revision = ref_by_id.get(current.snapshot_id)
            if ref_revision is not None and ref_revision != current.revision:
                raise NotificationProductionUnavailableError(
                    f"relationship {spec.relationship_id} snapshot "
                    f"{current.snapshot_id} revision drifted "
                    f"({ref_revision} -> {current.revision}); refusing to "
                    "notify (503)"
                )
        intent = await service.create_intent(
            intent_kind=request.intent_kind,
            subject_person_id=request.subject_person_id,
            source_event_id=request.source_event_id,
            idempotency_key=request.idempotency_key,
            policy_receipt_id=request.policy_receipt_id,
            fence=request.fence,
            actor_person_id=request.actor_person_id,
            template_key=request.template_key,
            template_params=request.template_params,
            reason_code=request.reason_code,
            script_version=request.script_version,
            recipients=request.recipients,
            occurred_at=request.occurred_at,
        )
        return intent.intent_id


class NotificationDeliveryWorker:
    """First-class worker lifecycle: claim -> dispatch -> reconcile under
    bounded leases; ``run_forever`` owns the loop and shuts down cleanly
    (draining detached sends / late reconcile callbacks)."""

    def __init__(
        self,
        service: NotificationService,
        channels: Mapping[Channel, DeliveryChannelPort],
        *,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        self._service = service
        self._channels = channels
        self._poll_interval_seconds = poll_interval_seconds
        self._stop = asyncio.Event()

    async def run_once(self, *, limit: int = 10) -> None:
        await self._service.dispatch_due(self._channels, limit=limit)

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                LOGGER.exception("notification worker cycle failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()

    async def shutdown(self, *, drain_timeout: float = 5.0) -> None:
        self.stop()
        await self._service.shutdown_detached_sends(timeout=drain_timeout)


#: Legacy migration marker: the old ``guardian_notification_outbox`` path
#: is NOT the production multi-subject authority.  Control wiring MUST use
#: ``CrisisEnqueueCoordinator``; the legacy path is only a read-only
#: compatibility view for historical rows and any write attempt fails
#: closed (503).
LEGACY_OUTBOX_IS_AUTHORITY: bool = False


def legacy_outbox_write_prohibited() -> None:
    """Fail closed instead of silently writing the legacy outbox: the
    production path is the NotificationIntent state machine (PR-15)."""
    raise NotificationProductionUnavailableError(
        "legacy guardian_notification_outbox is NOT the production "
        "authority; use CrisisEnqueueCoordinator (503)"
    )
