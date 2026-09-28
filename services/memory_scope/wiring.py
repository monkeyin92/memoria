"""Lifecycle wiring for the production MemoryScope stack.

Control installs the stack at startup, keeps the outbox worker running and
reads ``ready`` for readiness. Capture evidence reaches the stack through
``project_capture_evidence``; there is no MemoryScope HTTP router.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from services.memory_scope.capture_policy import (
    MemoryCapturePolicyContextBuilderPort,
)
from services.memory_scope.production import (
    MemoryOutboxWorker,
    MemoryProductionSettings,
    MemoryProductionStack,
    MemoryProductionUnavailableError,
    MemorySharedLifecycleAdapter,
    build_production_memory_stack,
    initialize_production_memory_stack,
)
from services.memory_scope.repository import (
    ConsentSnapshotVerifier,
    FamilyMembershipVerifier,
    MemoryOutboxDispatcherPort,
    MemorySessionAuthorityPort,
    PolicyReceiptVerifier,
    RelationshipGrantResolver,
)
from services.memory_scope.service import MemoryScopeService
from services.memory_scope.shared_actions import (
    FamilySharedActionExecutorPort,
)
from services.policy.production_wiring import SensitiveWriteService

LOGGER = logging.getLogger(__name__)


@dataclass
class MemoryProductionWiring:
    """Own the MemoryScope adapters, authority and worker lifecycle."""

    service: MemoryScopeService
    shared: FamilySharedActionExecutorPort
    authority: MemorySessionAuthorityPort
    outbox_worker: MemoryOutboxWorker | None
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
) -> MemoryProductionWiring:
    """Build and install the real stack on ``app.state.memory_wiring``."""

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
    shared = MemorySharedLifecycleAdapter(
        stack,
        executor=shared_action_executor,
    )
    assert isinstance(app, FastAPI)
    wiring = MemoryProductionWiring(
        service=stack.service,
        shared=shared,
        authority=authority,
        outbox_worker=outbox_worker,
    )
    wiring._settings = settings
    wiring._stack = stack
    app.state.memory_wiring = wiring
    return wiring
