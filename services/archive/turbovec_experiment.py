"""Evaluation-only TurboVec adapter and all-or-nothing production gate."""

from __future__ import annotations

import importlib
import json
import math
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import blake2b
from pathlib import Path
from time import perf_counter
from typing import Literal, Protocol, cast

import numpy as np
import numpy.typing as npt


class TurboVecUnavailableError(RuntimeError):
    """The optional TurboVec package is not installed or cannot initialize."""


class TurboVecIdCollisionError(RuntimeError):
    """Two external memory ids mapped to the same stable uint64."""


class TurboVecIndexPort(Protocol):
    def add_with_ids(
        self,
        vectors: npt.NDArray[np.float32],
        ids: npt.NDArray[np.uint64],
    ) -> None: ...

    def search(
        self,
        query: npt.NDArray[np.float32],
        *,
        k: int,
        allowlist: npt.NDArray[np.uint64] | None = None,
    ) -> tuple[npt.NDArray[np.float32], npt.NDArray[np.uint64]]: ...

    def remove(self, external_id: int) -> None: ...

    def write(self, path: str) -> None: ...


TurboVecIndexFactory = Callable[[int, int], TurboVecIndexPort]
ExternalIdMapper = Callable[[str], int]


@dataclass(frozen=True, slots=True)
class VectorDocument:
    item_id: str
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class VectorQuery:
    query_id: str
    vector: tuple[float, ...]
    relevant_item_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TurboVecBenchmarkResult:
    available: bool
    baseline_recall_at_10: float
    candidate_recall_at_10: float
    baseline_ndcg_at_10: float
    candidate_ndcg_at_10: float
    baseline_p95_ms: float
    candidate_p95_ms: float
    baseline_ram_bytes: int
    candidate_ram_bytes: int
    ram_measurement: Literal["serialized_proxy", "resident"]
    incremental_sync_ms: float
    incremental_sync_consistent: bool
    rebuild_seconds: float
    rebuild_consistent: bool
    dependency_error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "baseline_recall_at_10": self.baseline_recall_at_10,
            "candidate_recall_at_10": self.candidate_recall_at_10,
            "baseline_ndcg_at_10": self.baseline_ndcg_at_10,
            "candidate_ndcg_at_10": self.candidate_ndcg_at_10,
            "baseline_p95_ms": self.baseline_p95_ms,
            "candidate_p95_ms": self.candidate_p95_ms,
            "baseline_ram_bytes": self.baseline_ram_bytes,
            "candidate_ram_bytes": self.candidate_ram_bytes,
            "ram_measurement": self.ram_measurement,
            "incremental_sync_ms": self.incremental_sync_ms,
            "incremental_sync_consistent": self.incremental_sync_consistent,
            "rebuild_seconds": self.rebuild_seconds,
            "rebuild_consistent": self.rebuild_consistent,
            "dependency_error": self.dependency_error,
        }


@dataclass(frozen=True, slots=True)
class TurboVecGateThresholds:
    max_recall_loss: float = 0.02
    max_ndcg_loss: float = 0.02
    max_p95_ratio: float = 1.0
    min_ram_reduction_ratio: float = 0.25
    max_incremental_sync_ms: float = 50.0
    max_rebuild_seconds: float = 600.0

    def __post_init__(self) -> None:
        if not 0 <= self.max_recall_loss <= 1:
            raise ValueError("max_recall_loss must be between zero and one")
        if not 0 <= self.max_ndcg_loss <= 1:
            raise ValueError("max_ndcg_loss must be between zero and one")
        if self.max_p95_ratio <= 0:
            raise ValueError("max_p95_ratio must be positive")
        if not 0 <= self.min_ram_reduction_ratio <= 1:
            raise ValueError("min_ram_reduction_ratio must be between zero and one")
        if self.max_incremental_sync_ms <= 0 or self.max_rebuild_seconds <= 0:
            raise ValueError("TurboVec latency thresholds must be positive")


@dataclass(frozen=True, slots=True)
class TurboVecGateDecision:
    enabled: bool
    reasons: tuple[str, ...]
    benchmark: TurboVecBenchmarkResult

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "reasons": list(self.reasons),
            "benchmark": self.benchmark.as_dict(),
        }


class TurboVecEvaluationIndex:
    """Thin optional adapter; it is deliberately not a MemoryCatalog implementation."""

    def __init__(
        self,
        *,
        dimensions: int,
        bit_width: int = 4,
        index_factory: TurboVecIndexFactory | None = None,
        id_mapper: ExternalIdMapper | None = None,
    ) -> None:
        if dimensions < 1:
            raise ValueError("TurboVec dimensions must be positive")
        if bit_width not in (2, 3, 4):
            raise ValueError("TurboVec bit_width must be 2, 3 or 4")
        factory = index_factory or _default_index_factory
        try:
            self._index = factory(dimensions, bit_width)
        except (ImportError, ModuleNotFoundError) as exc:
            raise TurboVecUnavailableError(
                "TurboVec evaluation requires the optional `turbovec` package"
            ) from exc
        self.dimensions = dimensions
        self.bit_width = bit_width
        self._id_mapper = id_mapper or stable_external_id
        self._item_by_external_id: dict[int, str] = {}
        self._external_id_by_item: dict[str, int] = {}

    def add(self, documents: Sequence[VectorDocument]) -> None:
        if not documents:
            return
        vectors = _matrix([document.vector for document in documents], self.dimensions)
        external_ids = np.asarray(
            [self._register(document.item_id) for document in documents],
            dtype=np.uint64,
        )
        self._index.add_with_ids(vectors, external_ids)

    def search(
        self,
        vector: Sequence[float],
        *,
        k: int,
        allowed_item_ids: Sequence[str] = (),
    ) -> tuple[tuple[str, float], ...]:
        if k < 1:
            raise ValueError("TurboVec search k must be positive")
        query = _matrix([vector], self.dimensions)
        allowlist: npt.NDArray[np.uint64] | None = None
        if allowed_item_ids:
            allowlist = np.asarray(
                [
                    self._external_id_by_item[item_id]
                    for item_id in allowed_item_ids
                    if item_id in self._external_id_by_item
                ],
                dtype=np.uint64,
            )
        scores, ids = self._index.search(query, k=k, allowlist=allowlist)
        if ids.ndim != 2 or scores.ndim != 2:
            raise ValueError("TurboVec search must return two-dimensional arrays")
        result: list[tuple[str, float]] = []
        for score, external_id in zip(scores[0], ids[0], strict=True):
            item_id = self._item_by_external_id.get(int(external_id))
            if item_id is not None:
                result.append((item_id, float(score)))
        return tuple(result)

    def remove(self, item_id: str) -> None:
        external_id = self._external_id_by_item.get(item_id)
        if external_id is None:
            return
        self._index.remove(external_id)
        self._external_id_by_item.pop(item_id, None)
        self._item_by_external_id.pop(external_id, None)

    def serialized_bytes(self) -> int:
        with tempfile.TemporaryDirectory(prefix="memoria-turbovec-") as directory:
            path = Path(directory) / "index.tvim"
            self._index.write(str(path))
            return path.stat().st_size

    def _register(self, item_id: str) -> int:
        if not item_id.strip():
            raise ValueError("TurboVec item_id must not be blank")
        existing = self._external_id_by_item.get(item_id)
        if existing is not None:
            raise ValueError(f"TurboVec item_id already exists: {item_id}")
        external_id = self._id_mapper(item_id)
        collision = self._item_by_external_id.get(external_id)
        if collision is not None and collision != item_id:
            raise TurboVecIdCollisionError(
                f"stable uint64 collision between {collision!r} and {item_id!r}"
            )
        self._item_by_external_id[external_id] = item_id
        self._external_id_by_item[item_id] = external_id
        return external_id


def benchmark_turbovec(
    *,
    documents: Sequence[VectorDocument],
    queries: Sequence[VectorQuery],
    bit_width: int = 4,
    repeats: int = 5,
    index_factory: TurboVecIndexFactory | None = None,
    resident_ram_sampler: Callable[[TurboVecEvaluationIndex], int] | None = None,
) -> TurboVecBenchmarkResult:
    if not documents or not queries:
        raise ValueError("TurboVec benchmark requires documents and queries")
    if repeats < 1:
        raise ValueError("TurboVec benchmark repeats must be positive")
    dimensions = len(documents[0].vector)
    if dimensions < 1 or any(len(document.vector) != dimensions for document in documents):
        raise ValueError("TurboVec benchmark document dimensions must match")
    if any(len(query.vector) != dimensions for query in queries):
        raise ValueError("TurboVec benchmark query dimensions must match documents")
    item_ids = tuple(document.item_id for document in documents)
    if len(set(item_ids)) != len(item_ids):
        raise ValueError("TurboVec benchmark item ids must be unique")
    vectors = _matrix([document.vector for document in documents], dimensions)
    query_matrix = _matrix([query.vector for query in queries], dimensions)
    baseline_results, baseline_latencies = _exact_search(
        item_ids=item_ids,
        vectors=vectors,
        queries=query_matrix,
        k=10,
        repeats=repeats,
    )
    try:
        started = perf_counter()
        candidate = TurboVecEvaluationIndex(
            dimensions=dimensions,
            bit_width=bit_width,
            index_factory=index_factory,
        )
        candidate.add(documents)
        rebuild_seconds = perf_counter() - started
    except TurboVecUnavailableError as exc:
        return _unavailable_result(str(exc))

    candidate_results: list[tuple[str, ...]] = []
    candidate_latencies: list[float] = []
    for _ in range(repeats):
        current: list[tuple[str, ...]] = []
        for query in queries:
            started = perf_counter()
            hits = candidate.search(query.vector, k=10)
            candidate_latencies.append((perf_counter() - started) * 1000)
            current.append(tuple(item_id for item_id, _ in hits))
        candidate_results = current

    probe_id = "__memoria_turbovec_sync_probe__"
    probe_vector = tuple(float(value) for value in query_matrix[0])
    sync_started = perf_counter()
    candidate.add((VectorDocument(item_id=probe_id, vector=probe_vector),))
    sync_hits = candidate.search(
        probe_vector,
        k=min(10, len(documents) + 1),
    )
    candidate.remove(probe_id)
    incremental_sync_ms = (perf_counter() - sync_started) * 1000
    removed_hits = candidate.search(
        probe_vector,
        k=min(10, len(documents)),
    )
    incremental_sync_consistent = any(
        item_id == probe_id for item_id, _ in sync_hits
    ) and all(
        item_id != probe_id for item_id, _ in removed_hits
    )

    rebuild_started = perf_counter()
    rebuilt = TurboVecEvaluationIndex(
        dimensions=dimensions,
        bit_width=bit_width,
        index_factory=index_factory,
    )
    rebuilt.add(documents)
    rebuild_seconds = perf_counter() - rebuild_started
    rebuilt_results = tuple(
        tuple(item_id for item_id, _ in rebuilt.search(query.vector, k=10))
        for query in queries
    )
    rebuild_consistent = rebuilt_results == tuple(candidate_results)

    relevance = tuple(set(query.relevant_item_ids) for query in queries)
    baseline_recall, baseline_ndcg = _retrieval_metrics(
        baseline_results,
        relevance,
    )
    candidate_recall, candidate_ndcg = _retrieval_metrics(
        candidate_results,
        relevance,
    )
    candidate_ram_bytes = (
        resident_ram_sampler(candidate)
        if resident_ram_sampler is not None
        else candidate.serialized_bytes()
    )
    if candidate_ram_bytes <= 0:
        raise ValueError("TurboVec RAM measurement must be positive")
    return TurboVecBenchmarkResult(
        available=True,
        baseline_recall_at_10=baseline_recall,
        candidate_recall_at_10=candidate_recall,
        baseline_ndcg_at_10=baseline_ndcg,
        candidate_ndcg_at_10=candidate_ndcg,
        baseline_p95_ms=_percentile(baseline_latencies, 0.95),
        candidate_p95_ms=_percentile(candidate_latencies, 0.95),
        baseline_ram_bytes=vectors.nbytes,
        candidate_ram_bytes=candidate_ram_bytes,
        ram_measurement=(
            "resident"
            if resident_ram_sampler is not None
            else "serialized_proxy"
        ),
        incremental_sync_ms=incremental_sync_ms,
        incremental_sync_consistent=incremental_sync_consistent,
        rebuild_seconds=rebuild_seconds,
        rebuild_consistent=rebuild_consistent,
    )


def decide_turbovec_gate(
    benchmark: TurboVecBenchmarkResult,
    thresholds: TurboVecGateThresholds | None = None,
) -> TurboVecGateDecision:
    selected = thresholds or TurboVecGateThresholds()
    reasons: list[str] = []
    if not benchmark.available:
        reasons.append(benchmark.dependency_error or "turbovec_unavailable")
    if (
        benchmark.baseline_recall_at_10 - benchmark.candidate_recall_at_10
        > selected.max_recall_loss
    ):
        reasons.append("recall_loss_exceeds_threshold")
    if (
        benchmark.baseline_ndcg_at_10 - benchmark.candidate_ndcg_at_10
        > selected.max_ndcg_loss
    ):
        reasons.append("ndcg_loss_exceeds_threshold")
    if benchmark.baseline_p95_ms <= 0 or (
        benchmark.candidate_p95_ms
        > benchmark.baseline_p95_ms * selected.max_p95_ratio
    ):
        reasons.append("p95_does_not_improve")
    if benchmark.ram_measurement != "resident":
        reasons.append("resident_ram_measurement_required")
    ram_reduction = (
        (benchmark.baseline_ram_bytes - benchmark.candidate_ram_bytes)
        / benchmark.baseline_ram_bytes
        if benchmark.baseline_ram_bytes > 0
        else 0.0
    )
    if ram_reduction < selected.min_ram_reduction_ratio:
        reasons.append("ram_reduction_below_threshold")
    if (
        not benchmark.incremental_sync_consistent
        or benchmark.incremental_sync_ms > selected.max_incremental_sync_ms
    ):
        reasons.append("incremental_sync_gate_failed")
    if (
        not benchmark.rebuild_consistent
        or benchmark.rebuild_seconds > selected.max_rebuild_seconds
    ):
        reasons.append("rebuild_gate_failed")
    return TurboVecGateDecision(
        enabled=not reasons,
        reasons=tuple(reasons),
        benchmark=benchmark,
    )


def stable_external_id(item_id: str) -> int:
    return int.from_bytes(
        blake2b(item_id.encode(), digest_size=8, person=b"memoria").digest(),
        "little",
        signed=False,
    )


def decision_json(decision: TurboVecGateDecision) -> str:
    return json.dumps(
        decision.as_dict(),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _default_index_factory(dimensions: int, bit_width: int) -> TurboVecIndexPort:
    module = importlib.import_module("turbovec")
    index_type = getattr(module, "IdMapIndex", None)
    if index_type is None:
        raise TurboVecUnavailableError(
            "installed turbovec package does not expose IdMapIndex"
        )
    return cast(
        TurboVecIndexPort,
        index_type(dim=dimensions, bit_width=bit_width),
    )


def _matrix(
    values: Sequence[Sequence[float]],
    dimensions: int,
) -> npt.NDArray[np.float32]:
    matrix = np.ascontiguousarray(values, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] != dimensions:
        raise ValueError(f"vectors must have shape (n, {dimensions})")
    if not np.isfinite(matrix).all():
        raise ValueError("vectors must contain only finite values")
    return matrix


def _exact_search(
    *,
    item_ids: Sequence[str],
    vectors: npt.NDArray[np.float32],
    queries: npt.NDArray[np.float32],
    k: int,
    repeats: int,
) -> tuple[tuple[tuple[str, ...], ...], list[float]]:
    normalized_vectors = _normalize(vectors)
    normalized_queries = _normalize(queries)
    results: tuple[tuple[str, ...], ...] = ()
    latencies: list[float] = []
    effective_k = min(k, len(item_ids))
    for _ in range(repeats):
        started = perf_counter()
        scores = normalized_queries @ normalized_vectors.T
        indexes = np.argsort(-scores, axis=1)[:, :effective_k]
        latencies.append((perf_counter() - started) * 1000)
        results = tuple(
            tuple(item_ids[int(index)] for index in row)
            for row in indexes
        )
    return results, latencies


def _normalize(values: npt.NDArray[np.float32]) -> npt.NDArray[np.float32]:
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)
    return np.ascontiguousarray(values / norms, dtype=np.float32)


def _retrieval_metrics(
    ranked_results: Sequence[Sequence[str]],
    relevant_sets: Sequence[set[str]],
) -> tuple[float, float]:
    recalls: list[float] = []
    ndcgs: list[float] = []
    for ranked, relevant in zip(ranked_results, relevant_sets, strict=True):
        if not relevant:
            recalls.append(1.0)
            ndcgs.append(1.0)
            continue
        recalls.append(len(set(ranked[:10]) & relevant) / len(relevant))
        gains = [1 if item_id in relevant else 0 for item_id in ranked[:10]]
        ideal = [1] * min(10, len(relevant))
        ndcgs.append(_dcg(gains) / _dcg(ideal))
    return (_mean(recalls), _mean(ndcgs))


def _dcg(gains: Sequence[int]) -> float:
    return float(
        sum(gain / math.log2(index + 2) for index, gain in enumerate(gains))
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _unavailable_result(error: str) -> TurboVecBenchmarkResult:
    return TurboVecBenchmarkResult(
        available=False,
        baseline_recall_at_10=0,
        candidate_recall_at_10=0,
        baseline_ndcg_at_10=0,
        candidate_ndcg_at_10=0,
        baseline_p95_ms=0,
        candidate_p95_ms=0,
        baseline_ram_bytes=0,
        candidate_ram_bytes=0,
        ram_measurement="serialized_proxy",
        incremental_sync_ms=0,
        incremental_sync_consistent=False,
        rebuild_seconds=0,
        rebuild_consistent=False,
        dependency_error=error,
    )
