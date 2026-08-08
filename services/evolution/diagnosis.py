"""Cross-trajectory diagnosis and conservative candidate generation."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from services.evolution.domain import CandidateArtifact, LearningSignal, SignalScope

_TASK_FAMILY_MATCH_TERMS: dict[str, tuple[str, ...]] = {
    # These are routing labels, not learned instructions. Keeping them
    # deterministic prevents a raw diagnosis or transcript from becoming a
    # cross-account retrieval key.
    "weather": ("天气", "天气预报", "气温", "预报"),
}


@dataclass(frozen=True, slots=True)
class FailureCluster:
    cluster_key: str
    task_family: str
    failure_code: str
    scope: SignalScope
    account_id: str | None
    signal_ids: tuple[str, ...]
    refuting_signal_ids: tuple[str, ...]
    environments: tuple[str, ...]
    diagnosis: str

    @property
    def support_count(self) -> int:
        return len(self.signal_ids)


def aggregate_failure_clusters(
    signals: Iterable[LearningSignal],
    *,
    min_support: int = 2,
) -> tuple[FailureCluster, ...]:
    if min_support < 1:
        raise ValueError("min_support must be positive")
    grouped: dict[tuple[str, str, SignalScope, str | None], list[LearningSignal]] = defaultdict(list)
    by_family: dict[tuple[str, SignalScope, str | None], list[LearningSignal]] = defaultdict(list)
    for signal in signals:
        family_key = (signal.task_family, signal.scope, signal.account_id)
        by_family[family_key].append(signal)
        if not signal.failed:
            continue
        failure_code = signal.failure_code or "unspecified_failure"
        grouped[(signal.task_family, failure_code, signal.scope, signal.account_id)].append(signal)
    clusters: list[FailureCluster] = []
    for (task_family, failure_code, scope, account_id), failures in grouped.items():
        if len(failures) < min_support:
            continue
        family_signals = by_family[(task_family, scope, account_id)]
        failure_ids = {signal.signal_id for signal in failures}
        refuting = tuple(
            signal.signal_id
            for signal in family_signals
            if signal.signal_id not in failure_ids and not signal.failed
        )
        cluster_key = hashlib.sha256(
            "\0".join(
                (
                    task_family,
                    failure_code,
                    scope,
                    account_id or "global",
                )
            ).encode("utf-8")
        ).hexdigest()
        diagnoses = tuple(dict.fromkeys(signal.diagnosis for signal in failures if signal.diagnosis))
        clusters.append(
            FailureCluster(
                cluster_key=cluster_key,
                task_family=task_family,
                failure_code=failure_code,
                scope=scope,
                account_id=account_id,
                signal_ids=tuple(signal.signal_id for signal in failures),
                refuting_signal_ids=refuting,
                environments=tuple(dict.fromkeys(signal.environment_version for signal in failures)),
                diagnosis=";".join(diagnoses)[:2000],
            )
        )
    return tuple(sorted(clusters, key=lambda cluster: cluster.cluster_key))


class CandidateGenerator:
    """Create non-executable candidate manifests from supported failure clusters."""

    def __init__(self, *, trusted_root_sha256: str) -> None:
        if len(trusted_root_sha256) != 64:
            raise ValueError("trusted_root_sha256 must be a sha256 digest")
        self._trusted_root_sha256 = trusted_root_sha256

    def from_cluster(
        self,
        cluster: FailureCluster,
        *,
        kind: Literal["knowledge", "prompt", "skill", "harness", "parameter"],
        expected_behavior: str,
        regression_guards: tuple[str, ...],
        payload: Mapping[str, object] | None = None,
        risk: Literal["low", "medium", "high"] = "medium",
        version: int = 1,
    ) -> CandidateArtifact:
        if cluster.support_count < 2:
            raise ValueError("a candidate requires support from at least two trajectories")
        body: dict[str, object] = {
            "cluster_key": cluster.cluster_key,
            "failure_code": cluster.failure_code,
            "environments": list(cluster.environments),
            "support_count": cluster.support_count,
            "refuting_signal_ids": list(cluster.refuting_signal_ids),
            "proposal": dict(payload or {}),
        }
        # A global-redacted manifest may be reused by every account.  Keep
        # free-form diagnosis text only in owner-private candidates; the
        # structured failure code and source ids remain sufficient provenance
        # for global curation without risking transcript leakage.
        if cluster.scope == "owner_private":
            body["diagnosis"] = cluster.diagnosis
        if kind == "prompt" and not body["proposal"]:
            match_terms = _TASK_FAMILY_MATCH_TERMS.get(
                cluster.task_family.casefold(),
                (cluster.task_family, cluster.failure_code),
            )
            body["proposal"] = {
                "instruction": (
                    f"在任务族 {cluster.task_family} 中，避免失败条件 {cluster.failure_code}；"
                    "不得削弱安全、隐私、权限、generation fence 或工具白名单约束。"
                ),
                "match_terms": list(match_terms),
            }
        identity = json.dumps(
            {
                "cluster": cluster.cluster_key,
                "kind": kind,
                "version": version,
                "payload": body,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memoria:evolution:{identity}"))
        now = datetime.now(UTC)
        return CandidateArtifact(
            candidate_id=candidate_id,
            task_family=cluster.task_family,
            kind=kind,
            scope=cluster.scope,
            account_id=cluster.account_id,
            version=version,
            payload=body,
            source_signal_ids=cluster.signal_ids,
            expected_behavior=expected_behavior,
            regression_guards=regression_guards,
            risk=risk,
            trusted_root_sha256=self._trusted_root_sha256,
            created_at=now,
            updated_at=now,
        )


__all__ = [
    "CandidateGenerator",
    "FailureCluster",
    "aggregate_failure_clusters",
]
