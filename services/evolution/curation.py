"""Offline evolution orchestration: collect, diagnose, validate, canary and curate."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Literal

from services.evolution.account_fence import AccountWriteBlockedError, AccountWriteGuard
from services.evolution.diagnosis import (
    CandidateGenerator,
    FailureCluster,
    aggregate_failure_clusters,
)
from services.evolution.domain import CandidateArtifact, LearningSignal, ValidationReport
from services.evolution.release_policy import EvolutionReleasePolicy
from services.evolution.store import EvolutionStore, EvolutionTransitionError
from services.evolution.verifier import TrajectoryObservation, VerificationReport, verify_trajectory

_HARNESS_FAILURE_CODES = frozenset(
    {
        "privacy_violation",
        "authorization_violation",
        "stale_fence",
        "tool_fence_mismatch",
        "commitment_action_mismatch",
    }
)
_HARNESS_FAILURE_PREFIXES = ("forbidden_tool:", "tool_not_allowlisted:")


@dataclass(frozen=True, slots=True)
class SleepLearningPolicy:
    min_new_signals: int = 10
    min_failure_support: int = 2
    stale_after_days: int = 30

    def __post_init__(self) -> None:
        if self.min_new_signals < 1 or self.min_failure_support < 2 or self.stale_after_days < 1:
            raise ValueError(
                "sleep learning requires positive thresholds and at least two failure signals"
            )


@dataclass(frozen=True, slots=True)
class SleepCycleReport:
    ran: bool
    signal_count: int
    new_signal_count: int
    clusters: tuple[FailureCluster, ...]
    candidates: tuple[CandidateArtifact, ...]
    curation: dict[str, int]
    checkpoint: str | None


class EvolutionControlPlane:
    """Single decision point for the offline learning loop.

    This class never changes stable prompt, tool, validator or release files. It
    only persists evidence and candidate manifests until an external validator
    records a passed report and an explicit lifecycle transition is requested.
    """

    def __init__(
        self,
        store: EvolutionStore,
        *,
        trusted_root_sha256: str,
        policy: SleepLearningPolicy | None = None,
        release_policy: EvolutionReleasePolicy | None = None,
        account_write_guard: AccountWriteGuard | None = None,
    ) -> None:
        self.store = store
        self.policy = policy or SleepLearningPolicy()
        self.release_policy = release_policy or EvolutionReleasePolicy()
        self._trusted_root_sha256 = trusted_root_sha256
        self._generator = CandidateGenerator(trusted_root_sha256=trusted_root_sha256)
        self._sleep_lock = RLock()
        self._account_write_guard = account_write_guard or _unguarded_account_write

    def observe(self, observation: TrajectoryObservation) -> VerificationReport:
        report = verify_trajectory(observation)
        self.append_signal(report.learning_signal())
        return report

    def append_signal(self, signal: LearningSignal) -> LearningSignal:
        with self._account_write(signal.scope, signal.account_id):
            return self.store.append_signal(signal)

    def create_candidate(self, candidate: CandidateArtifact) -> CandidateArtifact:
        with self._account_write(candidate.scope, candidate.account_id):
            return self._create_candidate_unlocked(candidate)

    def _create_candidate_unlocked(self, candidate: CandidateArtifact) -> CandidateArtifact:
        if candidate.trusted_root_sha256 != self._trusted_root_sha256:
            raise ValueError("candidate trusted root does not match the running control plane")
        signals = tuple(self.store.get_signal(signal_id) for signal_id in candidate.source_signal_ids)
        if len(signals) < 2 or any(not signal.failed for signal in signals):
            raise ValueError("candidate requires at least two failed source trajectories")
        if any(
            signal.task_family != candidate.task_family
            or signal.scope != candidate.scope
            or signal.account_id != candidate.account_id
            for signal in signals
        ):
            raise ValueError("candidate source trajectory scope does not match the artifact")
        refuting_ids = candidate.payload.get("refuting_signal_ids", [])
        if (
            not isinstance(refuting_ids, list)
            or len(refuting_ids) > 100
            or any(
                not isinstance(signal_id, str)
                or not signal_id.strip()
                or len(signal_id) > 128
                for signal_id in refuting_ids
            )
            or len(set(refuting_ids)) != len(refuting_ids)
            or set(refuting_ids) & set(candidate.source_signal_ids)
        ):
            raise ValueError("candidate refuting signal ids are invalid")
        refuting = tuple(self.store.get_signal(signal_id) for signal_id in refuting_ids)
        if any(
            signal.failed
            or signal.task_family != candidate.task_family
            or signal.scope != candidate.scope
            or signal.account_id != candidate.account_id
            for signal in refuting
        ):
            raise ValueError("candidate refuting trajectories must be successful and scope-matched")
        return self.store.create_candidate(candidate)

    def sleep_cycle(
        self,
        *,
        now: datetime | None = None,
        force: bool = False,
        candidate_kind: Literal["knowledge", "prompt", "skill", "harness", "parameter"] = "prompt",
    ) -> SleepCycleReport:
        with self._sleep_lock:
            return self._run_sleep_cycle(
                now=now,
                force=force,
                candidate_kind=candidate_kind,
            )

    def _run_sleep_cycle(
        self,
        *,
        now: datetime | None,
        force: bool,
        candidate_kind: Literal["knowledge", "prompt", "skill", "harness", "parameter"],
    ) -> SleepCycleReport:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        signals = self.store.list_signals(failed_only=False)
        checkpoint = _read_checkpoint(self.store.get_control_state("sleep_checkpoint"))
        new_signals = self.store.unprocessed_signals()
        if not force and len(new_signals) < self.policy.min_new_signals:
            return SleepCycleReport(
                False,
                len(signals),
                len(new_signals),
                (),
                (),
                {},
                _checkpoint_text(checkpoint),
            )
        clusters = aggregate_failure_clusters(
            signals,
            min_support=self.policy.min_failure_support,
        )
        candidates: list[CandidateArtifact] = []
        for cluster in clusters:
            effective_kind = _candidate_kind_for_cluster(cluster, candidate_kind)
            existing = self.store.list_candidates(
                task_family=cluster.task_family,
                scope=cluster.scope,
                account_id=cluster.account_id,
            )
            matching = [
                candidate
                for candidate in existing
                if candidate.kind == effective_kind
                and set(candidate.source_signal_ids) == set(cluster.signal_ids)
            ]
            if matching:
                continue
            next_version = max(
                (candidate.version for candidate in existing if candidate.kind == effective_kind),
                default=0,
            ) + 1
            candidate = self._generator.from_cluster(
                cluster,
                kind=effective_kind,
                version=next_version,
                expected_behavior=(
                    f"reduce {cluster.failure_code} for task family {cluster.task_family} "
                    "without weakening privacy, fence or allowlist guards"
                ),
                regression_guards=(
                    "privacy_leakage_zero",
                    "stale_fence_rejection_preserved",
                    "old_task_retention_non_regressing",
                ),
                risk=(
                    "high"
                    if effective_kind in {"harness", "parameter"}
                    else "low"
                    if effective_kind == "prompt"
                    and cluster.task_family.casefold()
                    in self.release_policy.runtime_prompt_families
                    else "medium"
                ),
            )
            with self._account_write(cluster.scope, cluster.account_id):
                candidates.append(self._create_candidate_unlocked(candidate))
        if new_signals:
            self.store.mark_signals_processed(
                tuple(signal.signal_id for signal in new_signals),
                processed_at=current,
            )
        curation = self.store.curate(
            stale_before=current - timedelta(days=self.policy.stale_after_days),
            now=current,
        )
        next_checkpoint = max((_signal_key(signal) for signal in signals), default=checkpoint)
        if next_checkpoint is not None:
            self.store.set_control_state(
                "sleep_checkpoint",
                {
                    "created_at": next_checkpoint[0],
                    "signal_id": next_checkpoint[1],
                },
                updated_at=current,
            )
        return SleepCycleReport(
            True,
            len(signals),
            len(new_signals),
            clusters,
            tuple(candidates),
            curation,
            _checkpoint_text(next_checkpoint),
        )

    def record_validation(self, report: ValidationReport) -> ValidationReport:
        candidate = self.store.get_candidate(report.candidate_id)
        with self._account_write(candidate.scope, candidate.account_id):
            return self.store.record_validation(report)

    def record_activation(
        self,
        *,
        candidate_id: str,
        task_id: str,
        activated: bool,
        adhered: bool,
        outcome_passed: bool,
        evidence_event_id: str,
        created_at: datetime | None = None,
    ) -> str:
        candidate = self.store.get_candidate(candidate_id)
        with self._account_write(candidate.scope, candidate.account_id):
            return self.store.record_activation(
                candidate_id=candidate_id,
                task_id=task_id,
                activated=activated,
                adhered=adhered,
                outcome_passed=outcome_passed,
                evidence_event_id=evidence_event_id,
                created_at=created_at,
            )

    def transition(
        self,
        candidate_id: str,
        target: Literal["validated", "canary", "stable", "rejected", "retired"],
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> CandidateArtifact:
        candidate = self.store.get_candidate(candidate_id)
        with self._account_write(candidate.scope, candidate.account_id):
            if target in {"canary", "stable"} and candidate.kind != "prompt":
                raise EvolutionTransitionError(
                    f"{candidate.kind} candidates require a separately released runtime adapter"
                )
            if target in {"canary", "stable"}:
                self.release_policy.require_runtime_release(candidate)
            return self.store.transition_candidate(candidate_id, target, reason=reason, now=now)

    def rollback(
        self,
        candidate_id: str,
        *,
        reason: str = "",
        now: datetime | None = None,
    ) -> CandidateArtifact:
        """Restore a superseded prompt only through the audited store transition."""

        candidate = self.store.get_candidate(candidate_id)
        with self._account_write(candidate.scope, candidate.account_id):
            if candidate.kind != "prompt":
                raise EvolutionTransitionError(
                    f"{candidate.kind} candidates require a separately released runtime adapter"
                )
            self.release_policy.require_runtime_release(candidate)
            if candidate.trusted_root_sha256 != self._trusted_root_sha256:
                raise EvolutionTransitionError(
                    "rollback target trusted root does not match the running control plane"
                )
            return self.store.rollback_to(candidate_id, reason=reason, now=now)

    @contextmanager
    def _account_write(
        self,
        scope: str,
        account_id: str | None,
    ) -> Iterator[None]:
        if scope != "owner_private":
            yield
            return
        if not account_id:
            raise ValueError("owner-private evolution writes require an account")
        try:
            with self._account_write_guard(account_id):
                # This check is intentionally inside the in-process lease. The
                # database trigger remains the cross-process atomic backstop.
                self.store.assert_account_writable(account_id)
                yield
        except Exception as exc:
            # A marker can be committed by another worker after the durable
            # check above but before the adapter INSERT. Normalize both
            # SQLite and PostgreSQL trigger errors at this seam so telemetry
            # remains best-effort and API callers get one failure mode.
            if "owner-private evolution write blocked by account deletion" in str(exc):
                raise AccountWriteBlockedError("account deletion is in progress") from exc
            raise


__all__ = [
    "EvolutionControlPlane",
    "SleepCycleReport",
    "SleepLearningPolicy",
]


def _unguarded_account_write(account_id: str) -> AbstractContextManager[None]:
    del account_id
    return nullcontext()


def _signal_key(signal: LearningSignal) -> tuple[str, str]:
    return signal.created_at.astimezone(UTC).isoformat(), signal.signal_id


def _read_checkpoint(value: dict[str, object] | None) -> tuple[str, str] | None:
    if value is None:
        return None
    created_at = value.get("created_at")
    signal_id = value.get("signal_id")
    if not isinstance(created_at, str) or not isinstance(signal_id, str):
        raise ValueError("sleep checkpoint is invalid")
    parsed = datetime.fromisoformat(created_at)
    if parsed.tzinfo is None or not signal_id.strip():
        raise ValueError("sleep checkpoint is invalid")
    return parsed.astimezone(UTC).isoformat(), signal_id


def _checkpoint_text(value: tuple[str, str] | None) -> str | None:
    return None if value is None else f"{value[0]}|{value[1]}"


def _candidate_kind_for_cluster(
    cluster: FailureCluster,
    requested: Literal["knowledge", "prompt", "skill", "harness", "parameter"],
) -> Literal["knowledge", "prompt", "skill", "harness", "parameter"]:
    failure_code = cluster.failure_code
    if failure_code in _HARNESS_FAILURE_CODES or failure_code.startswith(
        _HARNESS_FAILURE_PREFIXES
    ):
        return "harness"
    return requested
