"""MemoryScope HTTP authority, validation and lifecycle gates."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi import FastAPI
from services.control_api.app.config import ControlSettings
from services.control_api.app.security import mint_memoria_access_token
from services.memory_scope.domain import (
    MemoryWriteDraft,
    ResolutionContext,
    WriteFence,
)
from services.memory_scope.in_memory_store import InMemoryMemoryStore
from services.memory_scope.production import (
    MemoryProductionSettings,
    MemoryProductionStack,
    MemorySharedLifecycleAdapter,
)
from services.memory_scope.repository import (
    InMemoryConsentSnapshotVerifier,
    InMemoryFamilyMembershipVerifier,
    InMemoryPolicyReceiptVerifier,
    InMemoryRelationshipGrantResolver,
    MemoryAuthoritySnapshot,
)
from services.memory_scope.service import MemoryScopeService
from services.memory_scope.shared_actions import (
    FamilySharedActionInput,
    FamilySharedActionResult,
)
from services.memory_scope.tests.receipt_helpers import make_memory_receipt
from services.memory_scope.wiring import (
    MemoryProductionWiring,
    install_memory_production,
)


class FakeSensitiveWriteExecutor:
    def __init__(self, service: MemoryScopeService) -> None:
        self._service = service
        self.calls = 0

    async def execute(
        self,
        context: ResolutionContext,
        draft: MemoryWriteDraft,
        actor_subject_id: str,
        *,
        snapshot: MemoryAuthoritySnapshot,
    ) -> str:
        assert snapshot.active_subject_id == actor_subject_id
        self.calls += 1
        record = await self._service.capture(
            context,
            draft,
            actor_subject_id=actor_subject_id,
        )
        return record.record_id

    async def close(self) -> None:
        return None


class FakeAuthority:
    def __init__(
        self,
        snapshot: MemoryAuthoritySnapshot | None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def current(
        self,
        *,
        actor_id: str,
        session_id: str,
        now: datetime,
    ) -> MemoryAuthoritySnapshot | None:
        self.calls.append((actor_id, session_id))
        if self.error is not None:
            raise self.error
        return self.snapshot


class FakeShared:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, FamilySharedActionInput]] = []

    async def execute(
        self,
        *,
        snapshot: MemoryAuthoritySnapshot,
        actor_subject_id: str,
        action: FamilySharedActionInput,
    ) -> FamilySharedActionResult:
        self.calls.append(
            (action.name, actor_subject_id, snapshot.fence.session_id, action)
        )
        return FamilySharedActionResult(
            proposal_id=action.proposal_id or "proposal-1",
            status={
                "propose": "pending",
                "confirm": "confirmed",
                "object": "frozen",
                "withdraw": "withdrawn",
            }[action.name],
        )


def _fence() -> WriteFence:
    return WriteFence(
        session_id="voice-session-1",
        epoch=1,
        binding_id="binding-1",
        binding_role="primary_subject",
        runtime_profile_id="profile-1",
        actor_subject_id="person-a",
        active_subject_id="person-a",
        binding_version=1,
        device_id="device-1",
        subject_revision=3,
        family_space_id="family-1",
        generation_id="7",
        turn_id=8,
        valid_until=datetime.now(UTC) + timedelta(minutes=5),
    )


def _snapshot() -> MemoryAuthoritySnapshot:
    return MemoryAuthoritySnapshot(
        fence=_fence(),
        active_subject_id="person-a",
        subject_category="adult",
        age_band="adult",
        speaker_state="confirmed",
        speaker_confidence=0.99,
        registered=True,
        actor_binding_role="primary_subject",
        actor_binding_roles=("primary_subject",),
        family_space_id="family-1",
        consent_snapshot_id="consent-1",
        profile_revision=4,
        generation_id=7,
        turn_id=8,
        tool_epoch=2,
        policy_bundle_version="policy-v2",
        policy_receipt_ids=("receipt-1",),
        service_mode="adult_companion",
    )


async def _build() -> tuple[
    MemoryProductionStack,
    MemoryProductionSettings,
    MemoryScopeService,
]:
    store = InMemoryMemoryStore()
    await store.initialize()
    verifier = InMemoryPolicyReceiptVerifier()
    consent = InMemoryConsentSnapshotVerifier()
    membership = InMemoryFamilyMembershipVerifier()
    service = MemoryScopeService(
        store,
        receipt_verifier=verifier,
        family_membership_verifier=membership,
        consent_verifier=consent,
        grant_resolver=InMemoryRelationshipGrantResolver(),
    )
    await verifier.register(
        make_memory_receipt(
            receipt_id="receipt-1",
            actor_subject_id="person-a",
            subject_id="person-a",
            fence=_fence(),
        )
    )
    await consent.register("consent-1", frozenset(("person-a",)))
    settings = MemoryProductionSettings(
        api_dsn="postgresql://memoria_memory_api@localhost/x",
        worker_dsn="postgresql://memoria_memory_worker@localhost/x",
        action_executor_dsn="postgresql://memoria_action_executor@localhost/x",
        schema_managed_externally=True,
    )
    stack = MemoryProductionStack(
        api_store=cast(object, store),
        worker_store=cast(object, store),
        service=service,
        sensitive_executor=cast(object, FakeSensitiveWriteExecutor(service)),
    )
    return stack, settings, service


def _authenticated_app() -> tuple[FastAPI, dict[str, str]]:
    app = FastAPI()
    settings = ControlSettings(_env_file=None)
    app.state.settings = settings
    token, _ttl = mint_memoria_access_token(
        settings,
        user_id="person-a",
        session_id="login-session-not-voice-session",
    )
    return app, {"Authorization": f"Bearer {token}"}


def test_readiness_requires_capture_and_family_shared_action_executors() -> None:
    stack = cast(object, type("Stack", (), {})())
    stack.outbox_dispatcher = object()
    stack.sensitive_executor = object()
    wiring = MemoryProductionWiring(
        service=cast(object, object()),
        shared=MemorySharedLifecycleAdapter(cast(object, stack)),
        authority=FakeAuthority(_snapshot()),
        outbox_worker=cast(object, object()),
    )
    wiring._stack = stack
    wiring._settings = cast(object, object())
    wiring._started = True
    wiring._outbox_task = cast(object, type("Task", (), {"done": lambda self: False})())

    assert wiring.ready is False


@pytest.mark.asyncio
async def test_injected_worker_is_preserved_and_dead_task_is_not_ready() -> None:
    stack, settings, service = await _build()

    class Dispatcher:
        async def dispatch(self, event: object) -> bool:
            return True

    stack = MemoryProductionStack(
        api_store=stack.api_store,
        worker_store=stack.worker_store,
        service=stack.service,
        sensitive_executor=stack.sensitive_executor,
        outbox_dispatcher=cast(object, Dispatcher()),
    )

    class Worker:
        def stop(self) -> None:
            pass

        async def run_forever(self) -> None:
            return

    worker = Worker()
    app, _headers = _authenticated_app()
    wiring = install_memory_production(
        app,
        settings,
        receipt_verifier=service.receipt_verifier,
        family_membership_verifier=service.family_membership_verifier,
        consent_verifier=service.consent_verifier,
        grant_resolver=service.grant_resolver,
        authority=FakeAuthority(_snapshot()),
        outbox_worker=cast(object, worker),
        stack=stack,
    )
    assert wiring.outbox_worker is worker
    wiring._started = True
    wiring._outbox_task = asyncio.create_task(worker.run_forever())
    await wiring._outbox_task
    assert wiring.ready is False
    await wiring.close()


@pytest.mark.asyncio
async def test_close_closes_the_complete_stack() -> None:
    class Stack:
        def __init__(self) -> None:
            self.closed = 0
            self.outbox_dispatcher = object()

        async def close(self) -> None:
            self.closed += 1

    stack = Stack()
    wiring = MemoryProductionWiring(
        service=cast(object, object()),
        shared=cast(object, object()),
        authority=FakeAuthority(_snapshot()),
        outbox_worker=None,
    )
    wiring._stack = cast(object, stack)
    wiring._settings = cast(object, object())
    wiring._started = True
    await wiring.close()
    assert stack.closed == 1
    assert wiring.ready is False
