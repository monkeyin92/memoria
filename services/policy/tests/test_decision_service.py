"""DecisionService returns decisions only after durable receipt insertion."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from services.policy.decision_service import DecisionService
from services.policy.engine import PolicyEngine
from services.policy.receipt_store import InMemoryPolicyReceiptRepository
from services.policy.receipts import PolicyReceiptConflictError, PolicyReceiptV2
from services.policy.tests.fakes import (
    make_binding,
    make_consent,
    make_context,
    make_relationship,
)

NOW = datetime(2026, 8, 9, 8, 0, tzinfo=UTC)


class FailingRepository:
    inserts = 0

    async def insert(self, receipt: PolicyReceiptV2) -> None:
        raise RuntimeError("persist failed")

    async def get(self, receipt_id: str) -> PolicyReceiptV2 | None:
        return None


class FailOnceRepository:
    def __init__(self) -> None:
        self._delegate = InMemoryPolicyReceiptRepository()
        self._failed = False

    async def insert(self, receipt: PolicyReceiptV2) -> None:
        if not self._failed:
            self._failed = True
            raise RuntimeError("transaction rolled back")
        await self._delegate.insert(receipt)

    async def get(self, receipt_id: str) -> PolicyReceiptV2 | None:
        return await self._delegate.get(receipt_id)

    def __len__(self) -> int:
        return len(self._delegate)


class ScopedCaptureRepository:
    def __init__(self) -> None:
        self.receipts: dict[str, PolicyReceiptV2] = {}
        self.scopes: list[tuple[str, str | None]] = []

    async def insert(self, receipt: PolicyReceiptV2) -> None:
        raise AssertionError("decision producer must use the scoped repository seam")

    async def get(self, receipt_id: str) -> PolicyReceiptV2 | None:
        raise AssertionError("decision producer must use the scoped repository seam")

    async def insert_scoped(
        self,
        receipt: PolicyReceiptV2,
        *,
        actor_id: str,
        subject_id: str | None,
    ) -> None:
        self.scopes.append((actor_id, subject_id))
        current = self.receipts.get(receipt.receipt_id)
        if current is not None and current != receipt:
            raise PolicyReceiptConflictError("immutable scoped receipt conflict")
        self.receipts[receipt.receipt_id] = receipt

    async def get_scoped(
        self,
        receipt_id: str,
        *,
        actor_id: str,
        subject_id: str | None,
    ) -> PolicyReceiptV2 | None:
        self.scopes.append((actor_id, subject_id))
        return self.receipts.get(receipt_id)


class ConcurrentRaceRepository:
    """Force two requests to observe the same initial get(None)."""

    def __init__(
        self,
        *,
        allow_must_insert_first: bool = False,
        first_created_at: datetime | None = None,
    ) -> None:
        self._delegate = InMemoryPolicyReceiptRepository()
        self._initial_gets = 0
        self._both_read_empty = asyncio.Event()
        self._allow_inserted = asyncio.Event()
        self._allow_must_insert_first = allow_must_insert_first
        self._first_created_at = first_created_at
        self._first_inserted = asyncio.Event()

    async def get(self, receipt_id: str) -> PolicyReceiptV2 | None:
        current = await self._delegate.get(receipt_id)
        if current is not None or self._initial_gets >= 2:
            return current
        self._initial_gets += 1
        if self._initial_gets == 2:
            self._both_read_empty.set()
        await asyncio.wait_for(self._both_read_empty.wait(), timeout=5)
        return None

    async def insert(self, receipt: PolicyReceiptV2) -> None:
        if self._allow_must_insert_first and receipt.effect == "deny":
            await asyncio.wait_for(self._allow_inserted.wait(), timeout=5)
        if (
            self._first_created_at is not None
            and receipt.created_at != self._first_created_at
        ):
            await asyncio.wait_for(self._first_inserted.wait(), timeout=5)
        try:
            await self._delegate.insert(receipt)
        finally:
            if receipt.effect != "deny":
                self._allow_inserted.set()
            if receipt.created_at == self._first_created_at:
                self._first_inserted.set()

    def __len__(self) -> int:
        return len(self._delegate)


def _context() -> object:
    consent = make_consent(
        capability="voice_clone_use", purpose="voice_clone", now=NOW
    )
    return make_context(
        capability="voice_clone_use",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )


async def test_decision_service_persists_allow_decision() -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)

    decision = await service.decide_and_persist(_context())  # type: ignore[arg-type]

    assert decision.effect == "allow_with_obligations"
    persisted = await repository.get(decision.receipt_id)
    assert persisted is not None
    assert persisted.effect == decision.effect
    assert persisted.context_hash == decision.context_hash
    assert persisted.receipt_id == decision.receipt_id


async def test_decision_producer_passes_authenticated_actor_subject_to_scoped_repository() -> None:
    repository = ScopedCaptureRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    context = make_context(
        actor_id="actor-scoped",
        subject_id="subject-scoped",
        resource_owner_id="subject-scoped",
        evaluated_at=NOW,
    )

    await service.decide_and_persist(context)

    assert repository.scopes == [("actor-scoped", "subject-scoped")]


async def test_decision_producer_preserves_unknown_subject_in_scoped_repository() -> None:
    repository = ScopedCaptureRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    context = make_context(
        actor_id="actor-unknown-safe",
        subject_id=None,
        resource_owner_id=None,
        capability="tutor",
        current_session_mode="unknown_safe",
        subject_category="unknown",
        age_band="unknown",
        speaker_state="unconfirmed",
        speaker_confidence=None,
        evaluated_at=NOW,
    )

    decision = await service.decide_and_persist(context)

    assert decision.effect == "deny"
    assert repository.scopes == [("actor-unknown-safe", None)]


async def test_decision_service_persists_deny_decision() -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    context = make_context(
        capability="voice_clone_use",
        consent_evidence=(),
        binding_evidence=None,
        evaluated_at=NOW,
    )

    decision = await service.decide_and_persist(context)

    assert decision.effect == "deny"
    persisted = await repository.get(decision.receipt_id)
    assert persisted is not None
    assert persisted.effect == "deny"


async def test_decision_service_failure_means_nothing_persisted() -> None:
    repository = FailingRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)

    with pytest.raises(RuntimeError, match="persist failed"):
        await service.decide_and_persist(_context())  # type: ignore[arg-type]

    assert repository.inserts == 0


async def test_same_idempotent_decision_replay_persists_one_receipt() -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    consent = make_consent(
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        idempotency_key="request-replay-1",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )

    first = await service.decide_and_persist(context)
    second = await service.decide_and_persist(
        replace(context, evaluated_at=NOW + timedelta(seconds=1))
    )

    assert first.receipt_id == second.receipt_id
    assert first == second
    assert len(repository) == 1


@pytest.mark.parametrize("case", ["non_exact_allow", "exact_allow", "deny"])
async def test_expired_receipt_is_never_replayed(case: str) -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    if case == "exact_allow":
        consent = make_consent(
            capability="voice_clone_use",
            purpose="voice_clone",
            now=NOW,
        )
        context = make_context(
            capability="voice_clone_use",
            purpose="voice_clone",
            idempotency_key=f"expired-{case}",
            consent_evidence=(consent,),
            binding_evidence=make_binding(now=NOW),
            evaluated_at=NOW,
        )
    elif case == "deny":
        context = make_context(
            subject_id=None,
            resource_owner_id=None,
            capability="tutor",
            current_session_mode="unknown_safe",
            subject_category="unknown",
            age_band="unknown",
            speaker_state="unconfirmed",
            speaker_confidence=None,
            idempotency_key=f"expired-{case}",
            evaluated_at=NOW,
        )
    else:
        context = make_context(
            capability="chat",
            idempotency_key=f"expired-{case}",
            evaluated_at=NOW,
        )

    first = await service.decide_and_persist(context)
    assert first.expires_at is not None

    with pytest.raises(PolicyReceiptConflictError, match="expired"):
        await service.decide_and_persist(
            replace(
                context,
                evaluated_at=first.expires_at + timedelta(seconds=1),
            )
        )

    assert len(repository) == 1


async def test_concurrent_same_outcome_replays_first_committed_receipt() -> None:
    repository = ConcurrentRaceRepository(first_created_at=NOW)
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    context = make_context(
        capability="chat",
        idempotency_key="concurrent-replay-1",
        evaluated_at=NOW,
    )

    first, second = await asyncio.gather(
        service.decide_and_persist(context),
        service.decide_and_persist(
            replace(context, evaluated_at=NOW + timedelta(milliseconds=1))
        ),
    )

    assert first == second
    assert first.effect == second.effect == "allow"
    assert len(repository) == 1


async def test_concurrent_insert_loser_cannot_replay_expired_winner() -> None:
    repository = ConcurrentRaceRepository(first_created_at=NOW)
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    context = make_context(
        capability="chat",
        idempotency_key="concurrent-expired-1",
        evaluated_at=NOW,
    )

    results = await asyncio.gather(
        service.decide_and_persist(context),
        service.decide_and_persist(
            replace(context, evaluated_at=NOW + timedelta(minutes=6))
        ),
        return_exceptions=True,
    )

    conflicts = [
        result for result in results if isinstance(result, PolicyReceiptConflictError)
    ]
    assert len(conflicts) == 1
    assert "expired" in str(conflicts[0])
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert len(repository) == 1


async def test_concurrent_different_authoritative_content_has_one_fail_closed() -> None:
    repository = ConcurrentRaceRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    context = make_context(
        capability="chat",
        data_classification="public",
        idempotency_key="concurrent-content-conflict-1",
        evaluated_at=NOW,
    )

    results = await asyncio.gather(
        service.decide_and_persist(context),
        service.decide_and_persist(
            replace(
                context,
                evaluated_at=NOW + timedelta(milliseconds=1),
                data_classification="private",
            )
        ),
        return_exceptions=True,
    )

    assert sum(isinstance(result, PolicyReceiptConflictError) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert len(repository) == 1


async def test_concurrent_revocation_never_replays_first_allow() -> None:
    repository = ConcurrentRaceRepository(allow_must_insert_first=True)
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    consent = make_consent(
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        idempotency_key="concurrent-revoke-1",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    revoked = replace(
        context,
        evaluated_at=NOW + timedelta(seconds=1),
        consent_evidence=(replace(consent, status="revoked"),),
    )

    allowed, denied = await asyncio.gather(
        service.decide_and_persist(context),
        service.decide_and_persist(revoked),
    )

    assert allowed.effect == "allow_with_obligations"
    assert denied.effect == "deny"
    assert denied.receipt_id != allowed.receipt_id
    assert len(repository) == 2


async def test_same_key_in_different_stable_scope_creates_distinct_receipts() -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    first_context = make_context(
        capability="chat",
        idempotency_key="request-conflict-1",
        evaluated_at=NOW,
    )
    second_context = make_context(
        capability="chat",
        purpose="runtime_sensitive_action",
        idempotency_key="request-conflict-1",
        evaluated_at=NOW,
    )

    first = await service.decide_and_persist(first_context)
    second = await service.decide_and_persist(second_context)

    assert first.receipt_id != second.receipt_id
    assert len(repository) == 2


async def test_revoked_consent_never_replays_allow_and_deny_version_is_replayable() -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    consent = make_consent(
        capability="voice_clone_use",
        purpose="voice_clone",
        data_classification="biometric",
        now=NOW,
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        data_classification="biometric",
        idempotency_key="request-revoked-1",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )

    allowed = await service.decide_and_persist(context)
    revoked_context = replace(
        context,
        evaluated_at=NOW + timedelta(seconds=1),
        data_classification="private",
        consent_evidence=(replace(consent, status="revoked"),),
    )
    denied = await service.decide_and_persist(revoked_context)
    replayed_deny = await service.decide_and_persist(
        replace(revoked_context, evaluated_at=NOW + timedelta(seconds=2))
    )

    assert allowed.effect == "allow_with_obligations"
    assert denied.effect == replayed_deny.effect == "deny"
    assert denied.receipt_id != allowed.receipt_id
    assert replayed_deny.receipt_id == denied.receipt_id
    assert len(repository) == 2


@pytest.mark.parametrize(
    ("field", "changed_value"),
    [
        ("data_classification", "private"),
        ("safety_state", "concern"),
        ("speaker_confidence", 0.1),
    ],
)
async def test_same_outcome_authoritative_context_probe_conflicts(
    field: str,
    changed_value: object,
) -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    consent = make_consent(
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        data_classification="biometric",
        idempotency_key=f"authoritative-probe-{field}",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    first = await service.decide_and_persist(context)
    stored_before = await repository.get(first.receipt_id)

    with pytest.raises(PolicyReceiptConflictError, match="immutable"):
        await service.decide_and_persist(
            replace(
                context,
                evaluated_at=NOW + timedelta(seconds=1),
                **{field: changed_value},
            )
        )

    assert len(repository) == 1
    assert await repository.get(first.receipt_id) == stored_before


async def test_runtime_change_conflicts_when_authoritative_evidence_is_still_valid() -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    consent = make_consent(
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        idempotency_key="request-runtime-change-1",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )

    await service.decide_and_persist(context)

    with pytest.raises(PolicyReceiptConflictError, match="immutable"):
        await service.decide_and_persist(
            replace(
                context,
                evaluated_at=NOW + timedelta(seconds=1),
                device_trust="untrusted",
            )
        )

    assert len(repository) == 1


async def test_relationship_revision_change_versions_receipt_and_replays_new_version() -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    relationship = make_relationship(
        snapshot_id="relationship-replay",
        revision=1,
        relation_type="emergency_contact_for",
        target_person_id="person-adult",
        now=NOW,
    )
    context = make_context(
        actor_id="safety-kernel",
        capability="crisis_notification",
        purpose="crisis_response",
        safety_state="self_crisis",
        data_classification="safety_minimum",
        idempotency_key="relationship-revision-1",
        relationship_evidence=(relationship,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )

    first = await service.decide_and_persist(context)
    revised_context = replace(
        context,
        evaluated_at=NOW + timedelta(seconds=1),
        relationship_evidence=(replace(relationship, revision=2),),
    )
    revised = await service.decide_and_persist(revised_context)
    replay = await service.decide_and_persist(
        replace(revised_context, evaluated_at=NOW + timedelta(seconds=2))
    )

    assert first.effect == revised.effect == "allow_with_obligations"
    assert revised.receipt_id != first.receipt_id
    assert replay == revised
    assert len(repository) == 2


async def test_repeated_evidence_invalidation_creates_bounded_generations() -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    consent = make_consent(
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        idempotency_key="multi-generation-1",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )

    base = await service.decide_and_persist(context)
    revoked_v1 = replace(
        context,
        evaluated_at=NOW + timedelta(seconds=1),
        consent_evidence=(replace(consent, status="revoked"),),
    )
    version_one = await service.decide_and_persist(revoked_v1)
    revoked_v2 = replace(
        revoked_v1,
        evaluated_at=NOW + timedelta(seconds=2),
        consent_evidence=(
            replace(consent, status="revoked", canonical_hash="d" * 64),
        ),
    )
    version_two = await service.decide_and_persist(revoked_v2)
    replay = await service.decide_and_persist(
        replace(revoked_v2, evaluated_at=NOW + timedelta(seconds=3))
    )

    assert base.effect == "allow_with_obligations"
    assert version_one.effect == version_two.effect == "deny"
    assert "-v1-" in version_one.receipt_id
    assert "-v2-" in version_two.receipt_id
    assert replay == version_two
    assert len(repository) == 3


async def test_expired_version_never_creates_another_generation() -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    consent = make_consent(
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        idempotency_key="expired-generation-1",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    await service.decide_and_persist(context)
    revoked = replace(
        context,
        evaluated_at=NOW + timedelta(seconds=1),
        consent_evidence=(replace(consent, status="revoked"),),
    )
    version_one = await service.decide_and_persist(revoked)
    assert version_one.expires_at is not None
    changed_after_expiry = replace(
        revoked,
        evaluated_at=version_one.expires_at + timedelta(seconds=1),
        consent_evidence=(
            replace(consent, status="revoked", canonical_hash="e" * 64),
        ),
    )

    with pytest.raises(PolicyReceiptConflictError, match="expired"):
        await service.decide_and_persist(changed_after_expiry)

    assert len(repository) == 2


async def test_version_generation_limit_fails_closed() -> None:
    repository = InMemoryPolicyReceiptRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    consent = make_consent(
        capability="voice_clone_use",
        purpose="voice_clone",
        now=NOW,
    )
    context = make_context(
        capability="voice_clone_use",
        purpose="voice_clone",
        idempotency_key="generation-limit-1",
        consent_evidence=(consent,),
        binding_evidence=make_binding(now=NOW),
        evaluated_at=NOW,
    )
    await service.decide_and_persist(context)

    for generation in range(1, 9):
        changed = replace(
            context,
            evaluated_at=NOW + timedelta(seconds=generation),
            consent_evidence=(
                replace(
                    consent,
                    status="revoked",
                    canonical_hash=f"{generation:x}" * 64,
                ),
            ),
        )
        decision = await service.decide_and_persist(changed)
        assert f"-v{generation}-" in decision.receipt_id

    overflow = replace(
        context,
        evaluated_at=NOW + timedelta(seconds=9),
        consent_evidence=(
            replace(consent, status="revoked", canonical_hash="a" * 64),
        ),
    )
    with pytest.raises(PolicyReceiptConflictError, match="generation limit"):
        await service.decide_and_persist(overflow)

    assert len(repository) == 9


async def test_failed_insert_retry_with_same_key_persists_exactly_once() -> None:
    repository = FailOnceRepository()
    service = DecisionService(engine=PolicyEngine(), repository=repository)
    context = make_context(
        capability="chat",
        idempotency_key="retry-after-rollback-1",
        evaluated_at=NOW,
    )

    with pytest.raises(RuntimeError, match="rolled back"):
        await service.decide_and_persist(context)
    assert len(repository) == 0

    decision = await service.decide_and_persist(context)

    assert len(repository) == 1
    assert await repository.get(decision.receipt_id) is not None
