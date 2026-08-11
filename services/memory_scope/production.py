"""Production wiring for the multi-role memory stack (PR-12/PR-14/§6-§13).

Control/main consumes THIS module for actual memory capture / recall /
family-shared lifecycle instead of the legacy single-user archive path:
the API store/service (subject-scoped capture/recall/shared proposals) and
the worker store (transactional outbox poll) run under the REAL
``memoria_memory_api`` / ``memoria_memory_worker`` PostgreSQL roles.
Every write requires a signed session fence (session/epoch/binding/
profile) + confirmed active subject + a verified PolicyReceiptV2 + consent
evidence; a missing authority fails closed with
``MemoryProductionUnavailableError`` (503) and never silently falls back to
the legacy SQLite/Archive path.  The family-shared PROMOTION additionally
requires the Policy same-connection authority seam - until it is wired the
finalizer fails closed (the service already enforces this).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import urlsplit

import asyncpg

from services.memory_scope.capture_policy import (
    MemoryCapturePolicyContextBuilderPort,
)
from services.memory_scope.domain import (
    MemoryRecord,
    MemoryScope,
    MemoryWriteDraft,
    ResolutionContext,
    RetentionPolicy,
    WriteFence,
)
from services.memory_scope.postgres_store import PostgresMemoryStore
from services.memory_scope.repository import (
    ConsentSnapshotVerifier,
    FamilyMembershipVerifier,
    MemoryAuthoritySnapshot,
    MemoryOutboxDispatcherPort,
    MemoryStore,
    PolicyReceiptVerifier,
    RelationshipGrantResolver,
)
from services.memory_scope.service import MemoryScopeService
from services.memory_scope.shared_actions import (
    FamilySharedActionExecutorPort,
    FamilySharedActionInput,
    FamilySharedActionResult,
)
from services.policy.action_authorizer import ActionAuthorizationError
from services.policy.production_wiring import SensitiveWriteService
from services.policy.receipts import PolicyReceiptConflictError, PolicyReceiptV2

LOGGER = logging.getLogger(__name__)


class MemoryProductionUnavailableError(RuntimeError):
    """503 semantics: a required authority (policy receipt verifier,
    consent/membership verifier, promotion commit authority) or the
    production PostgreSQL stack is not wired.  Callers must fail closed
    and MUST NOT silently fall back to the legacy SQLite/Archive path."""


class MemoryCaptureDeniedError(PermissionError):
    """The current actor/subject/policy authority denies capture."""


class MemoryCaptureConflictError(RuntimeError):
    """A current authority head no longer matches the capture fence."""


class RejectingMemoryAuthorities:
    """Explicit fail-closed placeholder for unavailable production evidence."""

    async def verify(
        self,
        receipt_id: str,
        *,
        fence: WriteFence,
        actor_subject_id: str,
        capability: str,
        now: datetime,
    ) -> None:
        return None

    async def verify_membership(
        self,
        *,
        family_space_id: str,
        subject_ids: tuple[str, ...],
        binding_version: int,
        now: datetime,
    ) -> bool:
        return False

    async def verify_consent(
        self,
        snapshot_id: str,
        *,
        scope: str,
        covers_subjects: tuple[str, ...],
        now: datetime,
    ) -> bool:
        return False

    def current_revision(self, *args: object) -> None:
        return None

    def current_hash(self, *args: object) -> None:
        return None

    async def guardian_of(
        self,
        *,
        actor_subject_id: str,
        now: datetime,
    ) -> frozenset[str]:
        return frozenset()

    async def legacy_grants_for(
        self,
        *,
        actor_subject_id: str,
        now: datetime,
    ) -> frozenset[str]:
        return frozenset()


@dataclass(frozen=True, slots=True)
class MemoryProductionSettings:
    """DSN/role settings for the production memory stack.  ``api_dsn`` and
    ``worker_dsn`` MUST be different connections/roles (P0-2)."""

    api_dsn: str
    worker_dsn: str
    #: Narrow action-executor DSN (P0/A): the SINGLE cross-domain login
    #: ``memoria_action_executor`` (Session schema owns it; Policy/
    #: Identity/Consent grant it EXECUTE on their narrow functions).
    #: NOBYPASSRLS, NO direct table
    #: grants - the ONLY privilege is EXECUTE on the SECURITY DEFINER
    #: ``memory_sensitive_commit``.  The Policy SensitiveWriteService runs
    #: its same-connection transaction on THIS role, never on the API role.
    action_executor_dsn: str = ""
    api_role: str = "memoria_memory_api"
    worker_role: str = "memoria_memory_worker"
    bootstrap_dsn: str | None = None
    app_role_password: str | None = None
    schema_managed_externally: bool = False

    def __post_init__(self) -> None:
        expected_roles = {
            "api_dsn": self.api_role,
            "worker_dsn": self.worker_role,
            "action_executor_dsn": "memoria_action_executor",
        }
        values = {
            "api_dsn": self.api_dsn,
            "worker_dsn": self.worker_dsn,
            "action_executor_dsn": self.action_executor_dsn,
        }
        for name, value in values.items():
            if not value or not value.strip():
                raise ValueError(f"{name} must not be empty")
            actual = urlsplit(value).username
            expected = expected_roles[name]
            if actual != expected:
                raise ValueError(
                    f"{name} must connect exactly as the {expected} role"
                )
        runtime_roles = {
            cast(str, urlsplit(value).username) for value in values.values()
        }
        if len(runtime_roles) != 3:
            raise ValueError(
                "memory API, worker and action executor must use independent roles"
            )
        if self.schema_managed_externally:
            if self.bootstrap_dsn or self.app_role_password:
                raise ValueError(
                    "external schema management cannot also configure bootstrap"
                )
        elif not self.bootstrap_dsn or not self.app_role_password:
            raise ValueError(
                "memory schema requires bootstrap_dsn + app_role_password or "
                "schema_managed_externally=True"
            )
        if self.bootstrap_dsn:
            bootstrap_role = urlsplit(self.bootstrap_dsn).username
            if not bootstrap_role or bootstrap_role in runtime_roles:
                raise ValueError(
                    "bootstrap_dsn must use an owner distinct from runtime roles"
                )


@dataclass(frozen=True, slots=True)
class MemoryProductionStack:
    api_store: PostgresMemoryStore
    worker_store: PostgresMemoryStore
    service: MemoryScopeService
    #: Policy's SensitiveWriteService (same-connection seam, A1): WITHOUT
    #: it the production capture/propose paths fail closed (503) - a
    #: separate in-process verifier is never called "production
    #: effective".
    sensitive_write: SensitiveWriteService | None = None
    #: PolicyContext builder (Policy side): turns the memory resolution
    #: context + draft into a canonical PolicyContext with an explicit
    #: action_resource_fence.  Required when ``sensitive_write`` is wired;
    #: missing builder fails closed (503).
    context_builder: MemoryCapturePolicyContextBuilderPort | None = None
    #: Transactional-outbox dispatcher (C8): only a successful dispatch
    #: acknowledges; an unconfigured dispatcher makes the worker not ready.
    outbox_dispatcher: MemoryOutboxDispatcherPort | None = None
    #: Caller-owned transaction executor (A1): None => capture/propose
    #: fail closed (503) - a separate in-process verifier is never the
    #: production write authority.
    sensitive_executor: MemorySensitiveWriteExecutor | None = None
    async def close(self) -> None:
        await self.api_store.close()
        await self.worker_store.close()
        executor = self.sensitive_executor
        if executor is not None:
            await executor.close()
        dispatcher_close = getattr(self.outbox_dispatcher, "close", None)
        if callable(dispatcher_close):
            result = dispatcher_close()
            if inspect.isawaitable(result):
                await result


def build_production_memory_stack(
    settings: MemoryProductionSettings,
    *,
    receipt_verifier: PolicyReceiptVerifier | None,
    family_membership_verifier: FamilyMembershipVerifier | None,
    consent_verifier: ConsentSnapshotVerifier | None,
    grant_resolver: RelationshipGrantResolver | None,
    sensitive_write: SensitiveWriteService | None = None,
    context_builder: MemoryCapturePolicyContextBuilderPort | None = None,
    outbox_dispatcher: MemoryOutboxDispatcherPort | None = None,
) -> MemoryProductionStack:
    """Build the production stack.  The authorities are REQUIRED: without
    them every durable write fails closed (the service never constructs a
    half-wired facade)."""
    if grant_resolver is None:
        raise MemoryProductionUnavailableError(
            "production memory reads require the Identity relationship grant "
            "resolver; a half-wired service is never constructed (503)"
        )
    api_store = PostgresMemoryStore(settings.api_dsn).with_role("api")
    worker_store = PostgresMemoryStore(settings.worker_dsn).with_role("worker")
    service = MemoryScopeService(
        cast(MemoryStore, api_store),
        receipt_verifier=receipt_verifier,
        family_membership_verifier=family_membership_verifier,
        consent_verifier=consent_verifier,
        grant_resolver=grant_resolver,
        # No promotion_commit_authority: family-shared final promotion
        # fails closed until the Policy same-connection seam is wired.
    )
    return MemoryProductionStack(
        api_store=api_store,
        worker_store=worker_store,
        service=service,
        sensitive_write=sensitive_write,
        context_builder=context_builder,
        outbox_dispatcher=outbox_dispatcher,
        sensitive_executor=(
            MemorySensitiveWriteExecutor(
                action_executor_store=PostgresMemoryStore(
                    settings.action_executor_dsn
                ).with_role("action_executor"),
                sensitive_write=sensitive_write,
                context_builder=context_builder,
            )
            if sensitive_write is not None
            else None
        ),
    )


async def initialize_production_memory_stack(
    stack: MemoryProductionStack,
    settings: MemoryProductionSettings,
) -> None:
    if settings.schema_managed_externally:
        await stack.api_store.initialize()
    else:
        if not settings.bootstrap_dsn or not settings.app_role_password:
            raise MemoryProductionUnavailableError(
                "memory production bootstrap requires the owner DSN and app "
                "role password; refusing to run half-initialized (503)"
            )
        await stack.api_store.initialize(
            bootstrap_dsn=settings.bootstrap_dsn,
            app_role_password=settings.app_role_password,
        )
    await stack.worker_store.initialize()
    executor = stack.sensitive_executor
    if executor is not None:
        await executor.initialize()


class MemoryCaptureAdapter:
    """Thin route adapter: capture one durable memory from a signed
    session fence + confirmed active subject + verified policy receipt.
    Unknown/guest subjects, missing fences or missing authorities fail
    closed (503) and NEVER write the legacy archive."""

    def __init__(self, stack: MemoryProductionStack) -> None:
        self._stack = stack

    async def capture(
        self,
        *,
        context: ResolutionContext,
        draft: MemoryWriteDraft,
        actor_subject_id: str,
        snapshot: MemoryAuthoritySnapshot | None = None,
    ) -> str:
        executor = self._stack.sensitive_executor
        if executor is None:
            raise MemoryProductionUnavailableError(
                "memory capture requires the Policy SensitiveWriteService "
                "(same-connection seam, A1); a separate "
                "in-process receipt verifier is NOT production authority "
                "(503)"
            )
        if context.fence is None or context.fence.is_expired(datetime.now(UTC)):
            raise MemoryProductionUnavailableError(
                "memory capture requires a signed, unexpired session fence "
                "(503)"
            )
        if not context.subject.active_subject_id:
            raise MemoryProductionUnavailableError(
                "memory capture requires a confirmed active subject (503)"
            )
        if context.subject.active_subject_id == "unknown":
            raise MemoryProductionUnavailableError(
                "unknown subject cannot write private memory (503)"
            )
        if context.requested_scope is MemoryScope.MEMORY_SCOPE_FAMILY_SHARED:
            raise MemoryCaptureDeniedError(
                "family-shared memory must use proposal and per-subject confirmation"
            )
        if context.requested_scope not in {
            MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
        }:
            raise MemoryCaptureDeniedError(
                "the requested memory scope is not a durable capture target"
            )
        if (
            context.requested_scope is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY
            and context.subject.subject_category != "minor"
        ):
            raise MemoryCaptureDeniedError(
                "guardian summaries are limited to a confirmed minor subject"
            )
        if snapshot is None:
            raise MemoryProductionUnavailableError(
                "memory capture requires the authoritative Session snapshot (503)"
            )
        return await executor.execute(
            context,
            draft,
            actor_subject_id,
            snapshot=snapshot,
        )


class MemorySensitiveWriteExecutor:
    """Caller-owned transaction path (A1): acquire ONE connection, open ONE
    transaction and let the Policy SensitiveWriteService decide -> insert
    the immutable receipt -> lock the current composite authorities ->
    validate -> invoke the write callback ON THE SAME CONNECTION.  Any
    failure rolls the whole transaction back; the separate in-process
    verifier is never used as the production write authority here."""

    def __init__(
        self,
        *,
        action_executor_store: PostgresMemoryStore,
        sensitive_write: SensitiveWriteService,
        context_builder: (
            MemoryCapturePolicyContextBuilderPort | None
        ),
    ) -> None:
        self._action_executor_store = action_executor_store
        self._sensitive_write = sensitive_write
        self._context_builder = context_builder

    async def initialize(self) -> None:
        await self._action_executor_store.initialize()
        pool = self._action_executor_store._require_pool()  # noqa: SLF001
        async with pool.acquire() as connection:
            for signature in (
                "memory_capture_commit(jsonb)",
                "memory_project_capture_evidence(jsonb)",
            ):
                available = await connection.fetchval(
                    """
                    SELECT to_regprocedure($1) IS NOT NULL
                       AND COALESCE(
                           has_function_privilege(
                               current_user, to_regprocedure($1), 'EXECUTE'
                           ), false
                       )
                    """,
                    signature,
                )
                if available is not True:
                    raise MemoryProductionUnavailableError(
                        f"memory action port is unavailable: {signature} (503)"
                    )

    async def close(self) -> None:
        await self._action_executor_store.close()

    async def execute(
        self,
        context: ResolutionContext,
        draft: MemoryWriteDraft,
        actor_subject_id: str,
        *,
        snapshot: MemoryAuthoritySnapshot,
    ) -> str:
        if self._context_builder is None:
            raise MemoryProductionUnavailableError(
                "sensitive write requires the PolicyContext builder "
                "(503)"
            )
        pool = self._action_executor_store._require_pool()
        try:
            async with pool.acquire() as connection:
                async with connection.transaction():
                    for name, value in (
                        ("app.authenticated_actor", actor_subject_id),
                        ("app.authenticated_subject", snapshot.active_subject_id),
                        ("app.authenticated_device", snapshot.fence.device_id),
                        ("app.authenticated_binding", snapshot.fence.binding_id),
                        ("app.session_actor", actor_subject_id),
                    ):
                        if not value:
                            raise MemoryProductionUnavailableError(
                                f"memory capture authority {name} is unavailable (503)"
                            )
                        await connection.execute(
                            "SELECT set_config($1, $2, true)",
                            name,
                            value,
                        )
                    await self._action_executor_store._scope(  # noqa: SLF001
                        connection,
                        actor_subject_id=actor_subject_id,
                        subject_id=context.subject.active_subject_id
                        or actor_subject_id,
                        family_space_id=context.subject.family_space_id,
                    )
                    policy_context = await self._context_builder.build(
                        connection,
                        snapshot=snapshot,
                        context=context,
                        draft=draft,
                        actor_subject_id=actor_subject_id,
                    )
                    action_fence = policy_context.action_resource_fence
                    if action_fence is None:
                        raise MemoryProductionUnavailableError(
                            "memory capture PolicyContext lacks an action fence (503)"
                        )
                    record = _record_from_context(
                        context,
                        draft,
                        actor_subject_id,
                        action_resource_id=action_fence.action_resource_id,
                    )

                    async def _write_callback(
                        conn: asyncpg.Connection, receipt: PolicyReceiptV2
                    ) -> str:
                        # The durable row is bound to the receipt minted and
                        # revalidated inside this exact transaction.
                        if not receipt.exact_fence:
                            raise MemoryProductionUnavailableError(
                                "sensitive write requires an exact-fence receipt "
                                "(503)"
                            )
                        if receipt.action_resource_fence is None:
                            raise MemoryProductionUnavailableError(
                                "sensitive write requires an explicit canonical "
                                "action fence (503)"
                            )
                        retention, retention_expires_at = _retention_from_receipt(
                            receipt,
                            now=policy_context.evaluated_at,
                        )
                        bound = replace(
                            record,
                            policy_receipt_id=receipt.receipt_id,
                            retention=retention,
                            retention_expires_at=retention_expires_at,
                        )
                        committed = await conn.fetchval(
                            "SELECT memory_capture_commit($1::jsonb)",
                            json.dumps(
                                _capture_commit_payload(
                                    bound,
                                    receipt=receipt,
                                    action_resource_fence=action_fence,
                                ),
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                default=str,
                            ),
                        )
                        if committed != bound.record_id:
                            raise MemoryProductionUnavailableError(
                                "memory capture commit did not return the bound record (503)"
                            )
                        return bound.record_id

                    committed_record_id: str = await self._sensitive_write.execute(
                        connection,
                        policy_context,
                        _write_callback,
                    )
                    if committed_record_id != record.record_id:
                        raise MemoryProductionUnavailableError(
                            "memory capture transaction returned a foreign record (503)"
                        )
                    return committed_record_id
        except ActionAuthorizationError as exc:
            message = str(exc).lower()
            if "changed" in message or "stale" in message or "expired" in message:
                raise MemoryCaptureConflictError(str(exc)) from exc
            if "denied" in message or "requires the authenticated" in message:
                raise MemoryCaptureDeniedError(str(exc)) from exc
            raise MemoryProductionUnavailableError(f"{exc} (503)") from exc
        except PolicyReceiptConflictError as exc:
            raise MemoryCaptureConflictError(
                "memory capture idempotency receipt conflicts with a prior action"
            ) from exc
        except asyncpg.PostgresError as exc:
            raise MemoryProductionUnavailableError(
                "memory capture PostgreSQL authority is unavailable (503)"
            ) from exc

    async def project_capture_evidence(
        self,
        *,
        event_id: str,
        content_sha256: str,
        occurred_at: datetime,
        candidate: dict[str, object],
    ) -> bool:
        """Revalidate one Archive candidate and project durable evidence.

        A candidate is not authority: the narrow PostgreSQL function locks
        the referenced Policy receipt, which asserts the current
        Session/Identity fence, before it can insert an evidence row.  Stale
        or forged candidates are ignored; infrastructure failures escape so
        the durable Archive outbox can retry them.
        """

        canonical = {
            "candidate": candidate,
            "content_sha256": content_sha256,
            "event_id": event_id,
            "occurred_at": occurred_at.astimezone(UTC).isoformat(),
        }
        canonical_hash = hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        payload: dict[str, object] = {
            **candidate,
            "canonical_hash": canonical_hash,
            "content_sha256": content_sha256,
            "event_id": event_id,
            "occurred_at": occurred_at.astimezone(UTC).isoformat(),
        }
        required = (
            "active_subject_id",
            "actor_id",
            "binding_id",
            "device_id",
        )
        if any(
            not isinstance(payload.get(name), str)
            or not cast(str, payload[name]).strip()
            for name in required
        ):
            return False
        pool = self._action_executor_store._require_pool()  # noqa: SLF001
        try:
            async with pool.acquire() as connection, connection.transaction():
                for name, value in (
                    ("app.authenticated_actor", cast(str, payload["actor_id"])),
                    (
                        "app.authenticated_subject",
                        cast(str, payload["active_subject_id"]),
                    ),
                    ("app.authenticated_device", cast(str, payload["device_id"])),
                    ("app.authenticated_binding", cast(str, payload["binding_id"])),
                    ("app.session_actor", cast(str, payload["actor_id"])),
                ):
                    await connection.execute(
                        "SELECT set_config($1, $2, true)", name, value
                    )
                await self._action_executor_store._scope(  # noqa: SLF001
                    connection,
                    actor_subject_id=cast(str, payload["actor_id"]),
                    subject_id=cast(str, payload["active_subject_id"]),
                    family_space_id=None,
                )
                projected = await connection.fetchval(
                    "SELECT memory_project_capture_evidence($1::jsonb)",
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
                return projected is True
        except asyncpg.PostgresError as exc:
            if exc.sqlstate in {"MC403", "SR403", "SR412"}:
                return False
            raise


def _record_from_context(
    context: ResolutionContext,
    draft: MemoryWriteDraft,
    actor_subject_id: str,
    *,
    action_resource_id: str,
) -> MemoryRecord:
    subject = context.subject.active_subject_id or actor_subject_id
    # Canonical ownership (P0): a private/guardian record belongs to the
    # AUTHORITATIVE active subject; a family-shared record belongs to the
    # family space.  A receipt id is NEVER an owner.
    if context.requested_scope == MemoryScope.MEMORY_SCOPE_FAMILY_SHARED:
        resource_owner = context.subject.family_space_id or subject
    else:
        resource_owner = subject
    record_payload = dict(draft.payload)
    if context.requested_scope is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY:
        record_payload["summary"] = draft.content
        record_payload.pop("content", None)
        record_payload.pop("raw_transcript", None)
    else:
        record_payload["content"] = draft.content
    return MemoryRecord(
        record_id=str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"https://memoria.local/memory-capture/{action_resource_id}",
            )
        ),
        scope=context.requested_scope,
        subject_id=subject,
        resource_owner_id=resource_owner,
        family_space_id=context.subject.family_space_id,
        source_evidence_ids=draft.source_evidence_ids,
        policy_receipt_id=context.policy.receipt_id or "",
        consent_snapshot_id=context.consent.snapshot_id or "",
        created_by_actor_id=actor_subject_id,
        memory_type=draft.memory_type,
        confidence=draft.confidence,
        status="confirmed",
        payload=record_payload,
    )


def _retention_from_receipt(
    receipt: PolicyReceiptV2,
    *,
    now: datetime,
) -> tuple[RetentionPolicy, datetime | None]:
    for obligation in receipt.obligations:
        if obligation.code != "RETENTION_TTL":
            continue
        ttl = obligation.params.retention_ttl_seconds
        if ttl is None or ttl <= 0:
            raise MemoryProductionUnavailableError(
                "RETENTION_TTL lacks a positive duration (503)"
            )
        return "ttl", now + timedelta(seconds=ttl)
    return "indefinite", None


def _capture_commit_payload(
    record: MemoryRecord,
    *,
    receipt: PolicyReceiptV2,
    action_resource_fence: object,
) -> dict[str, object]:
    from packages.contracts.generated.python.multi_subject_contracts import (
        PolicyActionResourceFence,
    )

    if not isinstance(action_resource_fence, PolicyActionResourceFence):
        raise MemoryProductionUnavailableError(
            "memory capture receipt/action fence is invalid (503)"
        )
    payload = record.to_dict()
    canonical_action_sha256 = action_resource_fence.action_resource_id.removeprefix(
        "capture:"
    )
    payload.update(
        {
            "policy_receipt_id": receipt.receipt_id,
            "action_resource_id": action_resource_fence.action_resource_id,
            "action_resource_fence": action_resource_fence.model_dump(mode="json"),
            "canonical_action_sha256": canonical_action_sha256,
            # Backward-compatible field name used by the narrow SQL port;
            # this is the whole canonical action digest, not raw content.
            "content_sha256": canonical_action_sha256,
        }
    )
    return payload


def _new_id() -> str:
    import uuid

    return str(uuid.uuid4())


class MemoryRecallAdapter:
    """Subject-scoped recall: a caller can only ever read through the
    authoritative actor/subject/family context (RLS + service gates)."""

    def __init__(self, stack: MemoryProductionStack) -> None:
        self._stack = stack

    async def get(
        self,
        record_id: str,
        actor_subject_id: str,
        *,
        actor_family_space_id: str | None = None,
    ) -> object | None:
        return await self._stack.service.get(
            record_id,
            actor_subject_id,
            actor_family_space_id=actor_family_space_id,
        )


class MemorySharedLifecycleAdapter:
    """Compatibility adapter for the deep shared-action port.

    The old adapter accepted caller-selected family, consent and policy
    receipt identifiers.  Those arguments are intentionally gone.  Until a
    resource-scoped Policy/Consent implementation is injected, this adapter
    fails closed; it never reuses ``MemoryAuthoritySnapshot`` profile
    receipts as resource receipts.
    """

    def __init__(
        self,
        stack: MemoryProductionStack,
        *,
        executor: FamilySharedActionExecutorPort | None = None,
    ) -> None:
        self._stack = stack
        self._executor = executor

    @property
    def available(self) -> bool:
        executor = self._executor
        if executor is None:
            return False
        configured = getattr(executor, "available", None)
        return bool(configured) if configured is not None else True

    async def initialize(self) -> None:
        executor = self._executor
        initialize = getattr(executor, "initialize", None)
        if callable(initialize):
            result = initialize()
            if inspect.isawaitable(result):
                await result

    async def close(self) -> None:
        executor = self._executor
        close = getattr(executor, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result

    async def execute(
        self,
        *,
        snapshot: MemoryAuthoritySnapshot,
        actor_subject_id: str,
        action: FamilySharedActionInput,
    ) -> FamilySharedActionResult:
        executor = self._executor
        if executor is None:
            raise MemoryProductionUnavailableError(
                "family shared action executor requires a resource-scoped "
                "Policy/Consent same-transaction implementation (503)"
            )
        return await executor.execute(
            snapshot=snapshot,
            actor_subject_id=actor_subject_id,
            action=action,
        )


class MemoryOutboxWorker:
    """First-class worker lifecycle for the transactional outbox (poll ->
    dispatch -> mark processed) under the worker role."""

    def __init__(
        self,
        stack: MemoryProductionStack,
        *,
        poll_interval_seconds: float = 5.0,
    ) -> None:
        self._stack = stack
        self._poll_interval_seconds = poll_interval_seconds
        self._stop = asyncio.Event()

    async def run_once(self, *, limit: int = 100) -> int:
        events = await self._stack.worker_store.list_pending_outbox(limit=limit)
        dispatcher = self._stack.outbox_dispatcher
        if dispatcher is None:
            # Fail closed: never ack events that nobody dispatched.
            return 0
        dispatched = 0
        for event in events:
            if await dispatcher.dispatch(event):
                await self._stack.worker_store.mark_outbox_processed(
                    event.outbox_id
                )
                dispatched += 1
        return dispatched

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception:
                LOGGER.exception("memory outbox worker cycle failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


#: Legacy migration marker: the old SQLite/Archive single-user memory path
#: is NOT the production multi-subject authority.  Production wiring MUST
#: use the stack above; the legacy path is read-only compatibility for
#: historical rows and any write attempt fails closed (503).
LEGACY_ARCHIVE_IS_AUTHORITY: bool = False


def legacy_archive_write_prohibited() -> None:
    """Fail closed instead of silently writing the legacy archive: the
    production path is the multi-subject MemoryScope state machine."""
    raise MemoryProductionUnavailableError(
        "legacy single-user archive is NOT the production memory "
        "authority; use the MemoryProductionStack (503)"
    )
