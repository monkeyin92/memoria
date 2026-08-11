"""FastAPI and lifecycle wiring for the production MemoryScope stack.

The HTTP boundary accepts only business input. Authentication comes from the
Control API Bearer dependency and every operation resolves one explicit voice
``session_id`` into a single Session + Identity authority snapshot. Subject,
family, consent, policy receipt and write-fence claims are never accepted from
the client.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
)
from services.memory_scope.capture_policy import (
    MemoryCapturePolicyContextBuilderPort,
)
from services.memory_scope.domain import (
    ConsentSnapshotInput,
    CoSubjectContext,
    MemoryScope,
    MemoryWriteDraft,
    PolicyDecisionInput,
    ResolutionContext,
    SubjectContext,
)
from services.memory_scope.production import (
    MemoryCaptureAdapter,
    MemoryCaptureConflictError,
    MemoryCaptureDeniedError,
    MemoryOutboxWorker,
    MemoryProductionSettings,
    MemoryProductionStack,
    MemoryProductionUnavailableError,
    MemoryRecallAdapter,
    MemorySharedLifecycleAdapter,
    build_production_memory_stack,
    initialize_production_memory_stack,
)
from services.memory_scope.repository import (
    ConsentSnapshotVerifier,
    FamilyMembershipVerifier,
    MemoryAuthoritySnapshot,
    MemoryOutboxDispatcherPort,
    MemorySessionAuthorityPort,
    PolicyReceiptVerifier,
    RelationshipGrantResolver,
)
from services.memory_scope.service import MemoryScopeService
from services.memory_scope.shared_actions import (
    FamilySharedActionConflictError,
    FamilySharedActionDeniedError,
    FamilySharedActionExecutorPort,
    FamilySharedActionInput,
)
from services.policy.production_wiring import SensitiveWriteService

LOGGER = logging.getLogger(__name__)


class _CaptureRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    requested_scope: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=32_768)
    source_evidence_ids: tuple[str, ...] = Field(default=(), max_length=64)


class _RecallQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)


class _SharedProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=32_768)
    source_evidence_ids: tuple[str, ...] = Field(default=(), max_length=64)
    co_subject_ids: tuple[str, ...] = Field(default=(), max_length=32)


class _SharedCommandRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)


@dataclass
class MemoryProductionWiring:
    """Own the MemoryScope adapters, authority and worker lifecycle."""

    service: MemoryScopeService
    capture: MemoryCaptureAdapter
    recall: MemoryRecallAdapter
    shared: FamilySharedActionExecutorPort
    authority: MemorySessionAuthorityPort
    outbox_worker: MemoryOutboxWorker | None
    router: APIRouter
    _settings: MemoryProductionSettings = field(init=False)
    _stack: MemoryProductionStack = field(init=False)
    _started: bool = field(init=False, default=False)
    _outbox_task: asyncio.Task[None] | None = field(init=False, default=None)

    async def start(self) -> None:
        if self._started:
            return
        try:
            await initialize_production_memory_stack(self._stack, self._settings)
            shared_initialize = getattr(self.shared, "initialize", None)
            if callable(shared_initialize):
                await shared_initialize()
            if self.outbox_worker is not None:
                self._outbox_task = asyncio.create_task(
                    self.outbox_worker.run_forever(),
                    name="memory-scope-outbox",
                )
        except Exception:
            self._started = False
            raise
        self._started = True

    async def close(self) -> None:
        if self.outbox_worker is not None:
            self.outbox_worker.stop()
        if self._outbox_task is not None:
            self._outbox_task.cancel()
            try:
                await asyncio.wait_for(self._outbox_task, timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
            self._outbox_task = None
        await self._stack.close()
        shared_close = getattr(self.shared, "close", None)
        if callable(shared_close):
            await shared_close()
        self._started = False

    @property
    def ready(self) -> bool:
        if not self._started or self._stack.outbox_dispatcher is None:
            return False
        if self._stack.sensitive_executor is None:
            return False
        if isinstance(self.shared, MemorySharedLifecycleAdapter):
            if not self.shared.available:
                return False
        elif not callable(getattr(self.shared, "execute", None)):
            return False
        if self.outbox_worker is None or self._outbox_task is None:
            return False
        if self._outbox_task.done():
            return False
        return True

    async def project_capture_evidence(
        self,
        *,
        event_id: str,
        content_sha256: str,
        occurred_at: datetime,
        candidate: dict[str, object],
    ) -> bool:
        executor = self._stack.sensitive_executor
        if executor is None:
            raise MemoryProductionUnavailableError(
                "memory capture evidence projector is unavailable (503)"
            )
        return await executor.project_capture_evidence(
            event_id=event_id,
            content_sha256=content_sha256,
            occurred_at=occurred_at,
            candidate=candidate,
        )


def build_memory_router() -> APIRouter:
    """Build a static router.

    The router is safe to register during ``create_app``. The production
    lifespan later installs ``app.state.memory_wiring`` before traffic is
    accepted, avoiding FastAPI's unsupported dynamic-router mutation.
    """

    router = APIRouter(prefix="/v1/production/memories", tags=["memories"])

    def _wiring(request: Request) -> MemoryProductionWiring:
        wiring = getattr(request.app.state, "memory_wiring", None)
        if not isinstance(wiring, MemoryProductionWiring):
            raise HTTPException(status_code=503, detail="memory scope unavailable")
        return wiring

    async def _authorize(
        request: Request,
        user: AuthenticatedUser,
        session_id: str,
    ) -> tuple[MemoryProductionWiring, MemoryAuthoritySnapshot]:
        wiring = _wiring(request)
        try:
            snapshot = await wiring.authority.current(
                actor_id=user.user_id,
                session_id=session_id,
                now=datetime.now(UTC),
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="memory authority denied") from exc
        except Exception as exc:
            LOGGER.exception("memory authority resolution failed")
            raise HTTPException(status_code=503, detail="memory authority unavailable") from exc
        if snapshot is None:
            raise HTTPException(status_code=403, detail="no current voice session")
        return wiring, snapshot

    @router.post("", status_code=201)
    async def capture_memory(
        payload: _CaptureRequest,
        request: Request,
        user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    ) -> dict[str, str]:
        wiring, snapshot = await _authorize(request, user, payload.session_id)
        try:
            context = _context_from_request(payload, snapshot)
            record_id = await wiring.capture.capture(
                context=context,
                draft=MemoryWriteDraft(
                    content=payload.content,
                    source_evidence_ids=payload.source_evidence_ids,
                ),
                actor_subject_id=user.user_id,
                snapshot=snapshot,
            )
            return {"record_id": record_id}
        except MemoryProductionUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except MemoryCaptureDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except MemoryCaptureConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except FamilySharedActionDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FamilySharedActionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("memory capture failed")
            raise HTTPException(status_code=500, detail="internal error") from exc

    @router.get("/healthz")
    async def healthz(request: Request) -> dict[str, str]:
        wiring = _wiring(request)
        if not wiring.ready:
            raise HTTPException(status_code=503, detail="not ready")
        return {"status": "ok", "ready": "true"}

    @router.get("/{record_id}")
    async def recall_memory(
        record_id: str,
        request: Request,
        query: Annotated[_RecallQuery, Query()],
        user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    ) -> object:
        wiring, snapshot = await _authorize(request, user, query.session_id)
        record = await wiring.recall.get(
            record_id,
            user.user_id,
            actor_family_space_id=snapshot.family_space_id,
        )
        if record is None:
            raise HTTPException(status_code=404, detail="not found")
        return record

    @router.post("/shared-proposals", status_code=201)
    async def propose_shared(
        payload: _SharedProposalRequest,
        request: Request,
        user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    ) -> dict[str, str]:
        wiring, snapshot = await _authorize(request, user, payload.session_id)
        try:
            result = await _shared_executor(wiring).execute(
                snapshot=snapshot,
                actor_subject_id=user.user_id,
                action=FamilySharedActionInput.propose(
                    title=payload.title,
                    content=payload.content,
                    source_evidence_ids=payload.source_evidence_ids,
                    co_subject_ids=payload.co_subject_ids,
                ),
            )
            return {"proposal_id": result.proposal_id, "status": result.status}
        except MemoryProductionUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except FamilySharedActionDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except FamilySharedActionConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("shared memory propose failed")
            raise HTTPException(status_code=500, detail="internal error") from exc

    async def _shared_command(
        *,
        operation: str,
        proposal_id: str,
        payload: _SharedCommandRequest,
        request: Request,
        user: AuthenticatedUser,
    ) -> dict[str, str]:
        wiring, snapshot = await _authorize(request, user, payload.session_id)
        try:
            if operation not in ("confirm", "object", "withdraw"):
                raise ValueError(f"unknown shared operation: {operation}")
            result = await _shared_executor(wiring).execute(
                snapshot=snapshot,
                actor_subject_id=user.user_id,
                action=FamilySharedActionInput.command(
                    cast(
                        Literal["confirm", "object", "withdraw"],
                        operation,
                    ),
                    proposal_id=proposal_id,
                ),
            )
            return {"status": result.status}
        except MemoryProductionUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            LOGGER.exception("shared memory %s failed", operation)
            raise HTTPException(status_code=500, detail="internal error") from exc

    @router.post("/shared-proposals/{proposal_id}/confirm")
    async def confirm_shared(
        proposal_id: str,
        payload: _SharedCommandRequest,
        request: Request,
        user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    ) -> dict[str, str]:
        return await _shared_command(
            operation="confirm",
            proposal_id=proposal_id,
            payload=payload,
            request=request,
            user=user,
        )

    @router.post("/shared-proposals/{proposal_id}/object")
    async def object_shared(
        proposal_id: str,
        payload: _SharedCommandRequest,
        request: Request,
        user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    ) -> dict[str, str]:
        return await _shared_command(
            operation="object",
            proposal_id=proposal_id,
            payload=payload,
            request=request,
            user=user,
        )

    @router.post("/shared-proposals/{proposal_id}/withdraw")
    async def withdraw_shared(
        proposal_id: str,
        payload: _SharedCommandRequest,
        request: Request,
        user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    ) -> dict[str, str]:
        return await _shared_command(
            operation="withdraw",
            proposal_id=proposal_id,
            payload=payload,
            request=request,
            user=user,
        )

    return router


def _shared_executor(
    wiring: MemoryProductionWiring,
) -> FamilySharedActionExecutorPort:
    """Return the shared-action port or preserve the 503 boundary."""
    executor = getattr(wiring, "shared", None)
    if executor is None or not callable(getattr(executor, "execute", None)):
        raise MemoryProductionUnavailableError(
            "family shared action executor is not wired (503)"
        )
    return cast(FamilySharedActionExecutorPort, executor)


def install_memory_production(
    app: object,
    settings: MemoryProductionSettings,
    *,
    receipt_verifier: PolicyReceiptVerifier | None,
    family_membership_verifier: FamilyMembershipVerifier | None,
    consent_verifier: ConsentSnapshotVerifier | None,
    grant_resolver: RelationshipGrantResolver | None,
    authority: MemorySessionAuthorityPort,
    outbox_worker: MemoryOutboxWorker | None = None,
    stack: MemoryProductionStack | None = None,
    shared_action_executor: FamilySharedActionExecutorPort | None = None,
    sensitive_write: SensitiveWriteService | None = None,
    context_builder: MemoryCapturePolicyContextBuilderPort | None = None,
    outbox_dispatcher: MemoryOutboxDispatcherPort | None = None,
    include_router: bool = True,
) -> MemoryProductionWiring:
    """Build and install the real stack.

    ``include_router=False`` is used by Control because its app factory
    registers the static router before lifespan startup.
    """

    from fastapi import FastAPI

    if stack is None:
        stack = build_production_memory_stack(
            settings,
            receipt_verifier=receipt_verifier,
            family_membership_verifier=family_membership_verifier,
            consent_verifier=consent_verifier,
            grant_resolver=grant_resolver,
            sensitive_write=sensitive_write,
            context_builder=context_builder,
            outbox_dispatcher=outbox_dispatcher,
        )
    if outbox_worker is None and stack.outbox_dispatcher is not None:
        outbox_worker = MemoryOutboxWorker(stack)
    capture = MemoryCaptureAdapter(stack)
    recall = MemoryRecallAdapter(stack)
    shared = MemorySharedLifecycleAdapter(
        stack,
        executor=shared_action_executor,
    )
    router = build_memory_router()
    assert isinstance(app, FastAPI)
    if include_router:
        app.include_router(router)
    wiring = MemoryProductionWiring(
        service=stack.service,
        capture=capture,
        recall=recall,
        shared=shared,
        authority=authority,
        outbox_worker=outbox_worker,
        router=router,
    )
    wiring._settings = settings
    wiring._stack = stack
    app.state.memory_wiring = wiring
    return wiring


def _context_from_request(
    payload: _CaptureRequest,
    snapshot: MemoryAuthoritySnapshot,
) -> ResolutionContext:
    requested = MemoryScope.from_value(payload.requested_scope)
    if requested is None:
        raise ValueError("requested_scope is not a canonical memory scope")
    receipt_id = (
        snapshot.policy_receipt_ids[-1] if snapshot.policy_receipt_ids else None
    )
    return ResolutionContext(
        subject=SubjectContext(
            active_subject_id=snapshot.active_subject_id,
            subject_category=snapshot.subject_category,
            speaker_state=snapshot.speaker_state,
            speaker_confidence=snapshot.speaker_confidence,
            registered=snapshot.registered,
            family_space_id=snapshot.family_space_id,
        ),
        policy=PolicyDecisionInput(receipt_id=receipt_id),
        consent=ConsentSnapshotInput(
            snapshot_id=snapshot.consent_snapshot_id,
            granted=snapshot.consent_snapshot_id is not None,
            scope="memory",
            covers_subjects=(snapshot.active_subject_id,),
        ),
        co_subjects=CoSubjectContext(),
        requested_scope=requested,
        fence=snapshot.fence,
    )
