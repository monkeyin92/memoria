from __future__ import annotations

from pathlib import Path

import numpy as np
import numpy.typing as npt
import pytest
from services.archive.turbovec_experiment import (
    TurboVecBenchmarkResult,
    TurboVecEvaluationIndex,
    TurboVecGateThresholds,
    TurboVecIdCollisionError,
    VectorDocument,
    VectorQuery,
    benchmark_turbovec,
    decide_turbovec_gate,
)


class FakeIdMapIndex:
    def __init__(self, dimensions: int, bit_width: int) -> None:
        del bit_width
        self.dimensions = dimensions
        self.vectors: dict[int, npt.NDArray[np.float32]] = {}

    def add_with_ids(
        self,
        vectors: npt.NDArray[np.float32],
        ids: npt.NDArray[np.uint64],
    ) -> None:
        for vector, external_id in zip(vectors, ids, strict=True):
            self.vectors[int(external_id)] = vector.copy()

    def search(
        self,
        query: npt.NDArray[np.float32],
        *,
        k: int,
        allowlist: npt.NDArray[np.uint64] | None = None,
    ) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.uint64]]:
        allowed = (
            {int(value) for value in allowlist}
            if allowlist is not None
            else set(self.vectors)
        )
        candidates = [
            (external_id, vector)
            for external_id, vector in self.vectors.items()
            if external_id in allowed
        ]
        rows_scores: list[list[float]] = []
        rows_ids: list[list[int]] = []
        for query_vector in query:
            scored = sorted(
                (
                    (
                        float(
                            np.dot(query_vector, vector)
                            / (
                                max(float(np.linalg.norm(query_vector)), 1e-9)
                                * max(float(np.linalg.norm(vector)), 1e-9)
                            )
                        ),
                        external_id,
                    )
                    for external_id, vector in candidates
                ),
                reverse=True,
            )[:k]
            rows_scores.append([score for score, _ in scored])
            rows_ids.append([external_id for _, external_id in scored])
        return (
            np.asarray(rows_scores, dtype=np.float32),
            np.asarray(rows_ids, dtype=np.uint64),
        )

    def remove(self, external_id: int) -> None:
        self.vectors.pop(external_id, None)

    def write(self, path: str) -> None:
        Path(path).write_bytes(b"x" * max(1, len(self.vectors)))


class FailingRemoveIndex(FakeIdMapIndex):
    def remove(self, external_id: int) -> None:
        del external_id
        raise RuntimeError("remove failed")


def _factory(dimensions: int, bit_width: int) -> FakeIdMapIndex:
    return FakeIdMapIndex(dimensions, bit_width)


def _failing_remove_factory(
    dimensions: int,
    bit_width: int,
) -> FailingRemoveIndex:
    return FailingRemoveIndex(dimensions, bit_width)


def test_evaluation_index_supports_allowlist_delete_and_collision_detection() -> None:
    index = TurboVecEvaluationIndex(
        dimensions=2,
        index_factory=_factory,
    )
    index.add(
        (
            VectorDocument("a", (1.0, 0.0)),
            VectorDocument("b", (0.0, 1.0)),
        )
    )

    assert index.search((1.0, 0.0), k=2)[0][0] == "a"
    assert index.search((1.0, 0.0), k=2, allowed_item_ids=("b",)) == (
        ("b", 0.0),
    )
    index.remove("a")
    assert index.search((1.0, 0.0), k=2)[0][0] == "b"

    collision = TurboVecEvaluationIndex(
        dimensions=2,
        index_factory=_factory,
        id_mapper=lambda _: 7,
    )
    collision.add((VectorDocument("first", (1.0, 0.0)),))
    with pytest.raises(TurboVecIdCollisionError):
        collision.add((VectorDocument("second", (0.0, 1.0)),))


def test_failed_index_delete_preserves_the_external_id_mapping_for_retry() -> None:
    index = TurboVecEvaluationIndex(
        dimensions=2,
        index_factory=_failing_remove_factory,
    )
    index.add((VectorDocument("a", (1.0, 0.0)),))

    with pytest.raises(RuntimeError, match="remove failed"):
        index.remove("a")

    assert index.search((1.0, 0.0), k=1)[0][0] == "a"


def test_benchmark_reports_recall_sync_rebuild_and_serialized_ram() -> None:
    benchmark = benchmark_turbovec(
        documents=(
            VectorDocument("a", (1.0, 0.0)),
            VectorDocument("b", (0.0, 1.0)),
            VectorDocument("c", (-1.0, 0.0)),
        ),
        queries=(
            VectorQuery("qa", (1.0, 0.0), ("a",)),
            VectorQuery("qb", (0.0, 1.0), ("b",)),
        ),
        repeats=2,
        index_factory=_factory,
    )

    assert benchmark.available is True
    assert benchmark.candidate_recall_at_10 == 1
    assert benchmark.candidate_ndcg_at_10 == 1
    assert benchmark.incremental_sync_consistent is True
    assert benchmark.rebuild_consistent is True
    assert benchmark.candidate_ram_bytes < benchmark.baseline_ram_bytes


def test_proxy_storage_size_cannot_satisfy_the_resident_ram_gate() -> None:
    benchmark = benchmark_turbovec(
        documents=(
            VectorDocument("a", (1.0, 0.0)),
            VectorDocument("b", (0.0, 1.0)),
        ),
        queries=(VectorQuery("qa", (1.0, 0.0), ("a",)),),
        index_factory=_factory,
    )

    decision = decide_turbovec_gate(benchmark)

    assert decision.enabled is False
    assert "resident_ram_measurement_required" in decision.reasons


def _passing_benchmark(**changes: object) -> TurboVecBenchmarkResult:
    values: dict[str, object] = {
        "available": True,
        "baseline_recall_at_10": 1.0,
        "candidate_recall_at_10": 0.99,
        "baseline_ndcg_at_10": 1.0,
        "candidate_ndcg_at_10": 0.99,
        "baseline_p95_ms": 10.0,
        "candidate_p95_ms": 7.0,
        "baseline_ram_bytes": 1000,
        "candidate_ram_bytes": 500,
        "ram_measurement": "resident",
        "incremental_sync_ms": 10.0,
        "incremental_sync_consistent": True,
        "rebuild_seconds": 20.0,
        "rebuild_consistent": True,
        "dependency_error": None,
    }
    values.update(changes)
    return TurboVecBenchmarkResult(**values)  # type: ignore[arg-type]


def test_gate_enables_only_when_every_metric_passes() -> None:
    decision = decide_turbovec_gate(
        _passing_benchmark(),
        TurboVecGateThresholds(),
    )

    assert decision.enabled is True
    assert decision.reasons == ()


@pytest.mark.parametrize(
    ("changes", "reason"),
    (
        ({"candidate_recall_at_10": 0.8}, "recall_loss_exceeds_threshold"),
        ({"candidate_ndcg_at_10": 0.8}, "ndcg_loss_exceeds_threshold"),
        ({"candidate_p95_ms": 11.0}, "p95_does_not_improve"),
        ({"candidate_ram_bytes": 900}, "ram_reduction_below_threshold"),
        ({"incremental_sync_consistent": False}, "incremental_sync_gate_failed"),
        ({"rebuild_consistent": False}, "rebuild_gate_failed"),
    ),
)
def test_any_failed_metric_keeps_turbovec_disabled(
    changes: dict[str, object],
    reason: str,
) -> None:
    decision = decide_turbovec_gate(_passing_benchmark(**changes))

    assert decision.enabled is False
    assert reason in decision.reasons


def test_missing_dependency_is_a_disabled_diagnostic_not_a_production_fallback() -> None:
    def unavailable(_: int, __: int) -> FakeIdMapIndex:
        raise ModuleNotFoundError("turbovec")

    benchmark = benchmark_turbovec(
        documents=(VectorDocument("a", (1.0, 0.0)),),
        queries=(VectorQuery("qa", (1.0, 0.0), ("a",)),),
        index_factory=unavailable,
    )
    decision = decide_turbovec_gate(benchmark)

    assert benchmark.available is False
    assert decision.enabled is False
    assert "turbovec" in " ".join(decision.reasons).lower()
