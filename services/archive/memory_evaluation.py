"""Versioned, provider-neutral evaluation for long-term memory extraction and retrieval."""

from __future__ import annotations

import json
import math
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal, Protocol, cast

from services.archive.domain import EvidenceEvent, SpeakerClass
from services.archive.life_archive import LifeArchive
from services.archive.memory_catalog import MemoryCatalog
from services.archive.memory_domain import (
    ConflictState,
    MemoryClaimReview,
    MemoryExtraction,
    MemoryExtractor,
    MemoryKind,
    MemorySearchQuery,
    MemorySensitivity,
    MemoryStatus,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor

EvaluationScenario = Literal[
    "exact_fact",
    "paraphrase",
    "person_alias",
    "temporal_question",
    "current_vs_historical",
    "episodic_recall",
    "repeated_episode",
    "conflicting_fact",
    "user_correction",
    "retracted_memory",
    "procedural_knowledge",
    "relationship_boundary",
    "cross_account_isolation",
]
EvaluationQueryMode = Literal["search", "context"]
EvaluationReviewAction = Literal["confirm", "dispute", "retract", "correct"]
_EVALUATION_SCENARIOS: tuple[EvaluationScenario, ...] = (
    "exact_fact",
    "paraphrase",
    "person_alias",
    "temporal_question",
    "current_vs_historical",
    "episodic_recall",
    "repeated_episode",
    "conflicting_fact",
    "user_correction",
    "retracted_memory",
    "procedural_knowledge",
    "relationship_boundary",
    "cross_account_isolation",
)
_EVALUATION_QUERY_MODES: tuple[EvaluationQueryMode, ...] = ("search", "context")
_EVALUATION_REVIEW_ACTIONS: tuple[EvaluationReviewAction, ...] = (
    "confirm",
    "dispute",
    "retract",
    "correct",
)
_SPEAKER_CLASSES: tuple[SpeakerClass, ...] = (
    "owner",
    "guest",
    "uncertain",
    "assistant",
    "system",
)
_MEMORY_KINDS: tuple[MemoryKind, ...] = (
    "semantic",
    "episodic",
    "procedural",
    "relationship",
)


@dataclass(frozen=True, slots=True)
class EvaluationEvidence:
    event_id: str
    account_id: str
    text: str
    occurred_at: datetime
    speaker_class: SpeakerClass = "owner"
    session_id: str | None = None


@dataclass(frozen=True, slots=True)
class EvaluationReview:
    source_event_id: str
    action: EvaluationReviewAction
    corrected_value: str | None = None
    value_contains: str = ""


@dataclass(frozen=True, slots=True)
class ExpectedMemory:
    key: str
    account_id: str
    match_all: tuple[str, ...]
    kind: str = ""
    memory_kind: MemoryKind | None = None
    source_event_ids: tuple[str, ...] = ()
    valid_from: datetime | None = None
    valid_to: datetime | None = None


@dataclass(frozen=True, slots=True)
class EvaluationQuery:
    query_id: str
    account_id: str
    text: str
    mode: EvaluationQueryMode
    relevance: Mapping[str, int]
    limit: int = 10
    valid_at: datetime | None = None
    entity_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryEvaluationCase:
    case_id: str
    scenario: EvaluationScenario
    evidence: tuple[EvaluationEvidence, ...]
    expected_memories: tuple[ExpectedMemory, ...]
    queries: tuple[EvaluationQuery, ...]
    reviews: tuple[EvaluationReview, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryEvaluationDataset:
    version: str
    cases: tuple[MemoryEvaluationCase, ...]


@dataclass(frozen=True, slots=True)
class EvaluationItem:
    item_id: str
    account_id: str
    kind: str
    memory_kind: MemoryKind
    title: str
    body: str
    status: MemoryStatus
    source_event_ids: tuple[str, ...]
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    sensitivity: MemorySensitivity = "personal"
    conflict_state: ConflictState = "none"


@dataclass(frozen=True, slots=True)
class EvaluationQueryResult:
    query_id: str
    mode: EvaluationQueryMode
    items: tuple[EvaluationItem, ...]
    latency_ms: float


@dataclass(frozen=True, slots=True)
class EvaluationObservation:
    case_id: str
    extracted_items: tuple[EvaluationItem, ...]
    query_results: tuple[EvaluationQueryResult, ...]
    input_tokens: int = 0
    output_tokens: int = 0
    error_code: str | None = None


class MemoryEvaluationAdapter(Protocol):
    name: str

    async def observe(self, case: MemoryEvaluationCase) -> EvaluationObservation: ...


@dataclass(frozen=True, slots=True)
class MemoryEvaluationMetrics:
    extraction_precision: float
    extraction_recall: float
    recall_at_5: float
    recall_at_10: float
    ndcg_at_10: float
    temporal_accuracy: float
    source_attribution_accuracy: float
    contradiction_rate: float
    cross_account_leakage: float
    candidate_leakage: float
    latency_p50_ms: float
    latency_p95_ms: float
    input_tokens: int
    output_tokens: int
    token_cost: int

    def as_dict(self) -> dict[str, float | int]:
        return {
            "extraction_precision": self.extraction_precision,
            "extraction_recall": self.extraction_recall,
            "recall_at_5": self.recall_at_5,
            "recall_at_10": self.recall_at_10,
            "ndcg_at_10": self.ndcg_at_10,
            "temporal_accuracy": self.temporal_accuracy,
            "source_attribution_accuracy": self.source_attribution_accuracy,
            "contradiction_rate": self.contradiction_rate,
            "cross_account_leakage": self.cross_account_leakage,
            "candidate_leakage": self.candidate_leakage,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "token_cost": self.token_cost,
        }


@dataclass(frozen=True, slots=True)
class MemoryEvaluationReport:
    dataset_version: str
    adapter: str
    case_count: int
    scenario_coverage: tuple[EvaluationScenario, ...]
    failed_cases: tuple[str, ...]
    metrics: MemoryEvaluationMetrics

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset_version": self.dataset_version,
            "adapter": self.adapter,
            "case_count": self.case_count,
            "scenario_coverage": list(self.scenario_coverage),
            "failed_cases": list(self.failed_cases),
            "metrics": self.metrics.as_dict(),
        }


async def run_memory_evaluation(
    dataset: MemoryEvaluationDataset,
    adapter: MemoryEvaluationAdapter,
) -> MemoryEvaluationReport:
    observations = tuple([await adapter.observe(case) for case in dataset.cases])
    return MemoryEvaluationReport(
        dataset_version=dataset.version,
        adapter=adapter.name,
        case_count=len(dataset.cases),
        scenario_coverage=tuple(dict.fromkeys(case.scenario for case in dataset.cases)),
        failed_cases=tuple(
            observation.case_id
            for observation in observations
            if observation.error_code is not None
        ),
        metrics=calculate_memory_metrics(dataset, observations),
    )


def calculate_memory_metrics(
    dataset: MemoryEvaluationDataset,
    observations: Sequence[EvaluationObservation],
) -> MemoryEvaluationMetrics:
    observed_by_case = {observation.case_id: observation for observation in observations}
    if set(observed_by_case) != {case.case_id for case in dataset.cases}:
        raise ValueError("evaluation observations must cover every dataset case exactly once")

    matched_predictions = 0
    predicted_count = 0
    expected_count = 0
    temporal_checks: list[bool] = []
    source_checks: list[bool] = []
    recall_5: list[float] = []
    recall_10: list[float] = []
    ndcg_10: list[float] = []
    contradiction_items = 0
    guarded_items = 0
    cross_account_items = 0
    retrieved_items = 0
    candidate_items = 0
    confirmed_only_items = 0
    latencies: list[float] = []
    input_tokens = 0
    output_tokens = 0

    for case in dataset.cases:
        observation = observed_by_case[case.case_id]
        expected_count += len(case.expected_memories)
        predicted_count += len(observation.extracted_items)
        pairs, matched_indexes = _match_items(case.expected_memories, observation.extracted_items)
        matched_predictions += len(matched_indexes)
        for expected in case.expected_memories:
            predicted = pairs.get(expected.key)
            if predicted is None:
                if expected.valid_from is not None or expected.valid_to is not None:
                    temporal_checks.append(False)
                if expected.source_event_ids:
                    source_checks.append(False)
                continue
            if expected.valid_from is not None or expected.valid_to is not None:
                temporal_checks.append(
                    _same_time(expected.valid_from, predicted.valid_from)
                    and _same_time(expected.valid_to, predicted.valid_to)
                )
            if expected.source_event_ids:
                source_checks.append(
                    set(expected.source_event_ids) == set(predicted.source_event_ids)
                )

        query_by_id = {query.query_id: query for query in case.queries}
        if set(query_by_id) != {result.query_id for result in observation.query_results}:
            raise ValueError(f"query observations do not match dataset case {case.case_id}")
        for result in observation.query_results:
            query = query_by_id[result.query_id]
            latencies.append(max(0.0, result.latency_ms))
            ranked_keys = _ranked_expected_keys(case.expected_memories, result.items)
            relevant = {key for key, grade in query.relevance.items() if grade > 0}
            recall_5.append(_recall_at(ranked_keys, relevant, 5))
            recall_10.append(_recall_at(ranked_keys, relevant, 10))
            grades = [query.relevance.get(key, 0) for key in ranked_keys[:10]]
            ideal = sorted(query.relevance.values(), reverse=True)[:10]
            ndcg_10.append(_ndcg(grades, ideal))
            for item in result.items:
                retrieved_items += 1
                if item.account_id != query.account_id:
                    cross_account_items += 1
                if query.mode == "context":
                    guarded_items += 1
                    confirmed_only_items += 1
                    if item.conflict_state == "active":
                        contradiction_items += 1
                    if item.status == "candidate":
                        candidate_items += 1

        input_tokens += observation.input_tokens
        output_tokens += observation.output_tokens

    return MemoryEvaluationMetrics(
        extraction_precision=_ratio(matched_predictions, predicted_count),
        extraction_recall=_ratio(len(_matched_expected(dataset, observed_by_case)), expected_count),
        recall_at_5=_mean(recall_5),
        recall_at_10=_mean(recall_10),
        ndcg_at_10=_mean(ndcg_10),
        temporal_accuracy=_boolean_mean(temporal_checks),
        source_attribution_accuracy=_boolean_mean(source_checks),
        contradiction_rate=_ratio(contradiction_items, guarded_items),
        cross_account_leakage=_ratio(cross_account_items, retrieved_items),
        candidate_leakage=_ratio(candidate_items, confirmed_only_items),
        latency_p50_ms=_percentile(latencies, 0.50),
        latency_p95_ms=_percentile(latencies, 0.95),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        token_cost=input_tokens + output_tokens,
    )


def load_memory_evaluation_dataset(path: str | Path) -> MemoryEvaluationDataset:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise ValueError("memory evaluation dataset must contain a cases array")
    version = _required_text(raw, "version")
    cases = tuple(_parse_case(value) for value in cast(list[object], raw["cases"]))
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("memory evaluation case ids must be unique")
    return MemoryEvaluationDataset(version=version, cases=cases)


class CatalogMemoryEvaluationAdapter:
    """Run the fixed dataset against the reconstructable SQLite production contract."""

    name = "memoria-sqlite-rules"

    def __init__(self, extractor: MemoryExtractor | None = None) -> None:
        self._extractor = extractor or RuleBasedMemoryExtractor()

    async def observe(self, case: MemoryEvaluationCase) -> EvaluationObservation:
        with tempfile.TemporaryDirectory(prefix="memoria-memory-eval-") as directory:
            path = Path(directory) / "archive.sqlite3"
            archive = LifeArchive.sqlite(path)
            extractor = _UsageTrackingExtractor(self._extractor)
            catalog = MemoryCatalog.sqlite(path, extractor=extractor)
            source_accounts = {
                evidence.event_id: evidence.account_id for evidence in case.evidence
            }
            for evidence in case.evidence:
                await archive.record(
                    EvidenceEvent(
                        event_id=evidence.event_id,
                        account_id=evidence.account_id,
                        session_id=evidence.session_id or f"eval-{case.case_id}",
                        event_type="speech.utterance_finalized",
                        occurred_at=evidence.occurred_at,
                        speaker_class=evidence.speaker_class,
                        source="memory.evaluation",
                        payload={
                            "text": evidence.text,
                            "interaction_mode": "companion",
                            "prompt_kind": "spontaneous",
                            "owner_projection_eligible": evidence.speaker_class == "owner",
                        },
                    )
                )
            await catalog.compile_pending(limit=1000)
            await self._apply_reviews(catalog, case)
            accounts = tuple(
                dict.fromkeys(
                    [
                        *(evidence.account_id for evidence in case.evidence),
                        *(query.account_id for query in case.queries),
                    ]
                )
            )
            extracted: list[EvaluationItem] = []
            for account_id in accounts:
                search = await catalog.search(
                    MemorySearchQuery(
                        account_id=account_id,
                        speaker_class="owner",
                        include_candidates=True,
                        limit=100,
                    )
                )
                extracted.extend(
                    EvaluationItem(
                        item_id=item.item_id,
                        account_id=_source_account_for_item(
                            item.source_event_ids,
                            source_accounts,
                            fallback=account_id,
                        ),
                        kind=item.kind,
                        memory_kind=item.memory_kind,
                        title=item.title,
                        body=item.snippet,
                        status=item.status,
                        source_event_ids=item.source_event_ids,
                        valid_from=item.valid_from,
                        valid_to=item.valid_to,
                        sensitivity=item.sensitivity,
                        conflict_state=item.conflict_state,
                    )
                    for item in search.items
                )
                extracted.extend(
                    EvaluationItem(
                        item_id=person.person_id,
                        account_id=_source_account_for_item(
                            (person.source_event_id,),
                            source_accounts,
                            fallback=account_id,
                        ),
                        kind="person",
                        memory_kind="relationship",
                        title=person.display_name,
                        body=" ".join(
                            (
                                person.relationship_to_owner,
                                *person.aliases,
                            )
                        ),
                        status=person.status,
                        source_event_ids=(person.source_event_id,),
                    )
                    for person in await catalog.people(account_id=account_id)
                )

            results: list[EvaluationQueryResult] = []
            for query in case.queries:
                started = perf_counter()
                request = MemorySearchQuery(
                    account_id=query.account_id,
                    speaker_class="owner",
                    text=query.text,
                    valid_at=query.valid_at,
                    entity_ids=query.entity_ids,
                    limit=query.limit,
                )
                response = (
                    await catalog.context(request)
                    if query.mode == "context"
                    else await catalog.search(request)
                )
                latency_ms = (perf_counter() - started) * 1000
                results.append(
                    EvaluationQueryResult(
                        query_id=query.query_id,
                        mode=query.mode,
                        latency_ms=latency_ms,
                        items=tuple(
                            EvaluationItem(
                                item_id=item.item_id,
                                account_id=_source_account_for_item(
                                    item.source_event_ids,
                                    source_accounts,
                                    fallback=query.account_id,
                                ),
                                kind=item.kind,
                                memory_kind=item.memory_kind,
                                title=item.title,
                                body=item.snippet,
                                status=item.status,
                                source_event_ids=item.source_event_ids,
                                valid_from=item.valid_from,
                                valid_to=item.valid_to,
                                sensitivity=item.sensitivity,
                                conflict_state=item.conflict_state,
                            )
                            for item in response.items
                        ),
                    )
                )
            return EvaluationObservation(
                case_id=case.case_id,
                extracted_items=tuple(extracted),
                query_results=tuple(results),
                input_tokens=extractor.input_tokens,
                output_tokens=extractor.output_tokens,
            )

    @staticmethod
    async def _apply_reviews(
        catalog: MemoryCatalog,
        case: MemoryEvaluationCase,
    ) -> None:
        for review in case.reviews:
            queue = await catalog.review_queue(
                account_id=_source_account(case, review.source_event_id)
            )
            targets = [
                item
                for item in queue
                if item.source_event_id == review.source_event_id
                and (
                    not review.value_contains
                    or _normalized(review.value_contains) in _normalized(item.value)
                )
            ]
            if not targets:
                raise ValueError(
                    f"review source {review.source_event_id} did not match a memory claim"
                )
            for target in targets:
                await catalog.review(
                    MemoryClaimReview(
                        account_id=_source_account(case, review.source_event_id),
                        claim_id=target.item_id,
                        action=review.action,
                        corrected_value=review.corrected_value,
                    )
                )


class _UsageTrackingExtractor:
    def __init__(self, delegate: MemoryExtractor) -> None:
        self._delegate = delegate
        self.version = delegate.version
        self.input_tokens = 0
        self.output_tokens = 0

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        extraction = await self._delegate.extract(event)
        self.input_tokens += extraction.usage.input_tokens
        self.output_tokens += extraction.usage.output_tokens
        return extraction


def _parse_case(value: object) -> MemoryEvaluationCase:
    if not isinstance(value, dict):
        raise ValueError("memory evaluation cases must be objects")
    raw = cast(dict[str, object], value)
    scenario = _required_text(raw, "scenario")
    if scenario not in _EVALUATION_SCENARIOS:
        raise ValueError(f"unknown memory evaluation scenario: {scenario}")
    evidence = tuple(
        _parse_evidence(item) for item in _required_list(raw, "evidence")
    )
    expected = tuple(
        _parse_expected(item) for item in _required_list(raw, "expected_memories")
    )
    queries = tuple(_parse_query(item) for item in _required_list(raw, "queries"))
    reviews = tuple(_parse_review(item) for item in _optional_list(raw, "reviews"))
    return MemoryEvaluationCase(
        case_id=_required_text(raw, "case_id"),
        scenario=scenario,
        evidence=evidence,
        expected_memories=expected,
        queries=queries,
        reviews=reviews,
    )


def _parse_evidence(value: object) -> EvaluationEvidence:
    raw = _object(value, "evidence")
    speaker = str(raw.get("speaker_class", "owner"))
    if speaker not in _SPEAKER_CLASSES:
        raise ValueError(f"invalid speaker_class: {speaker}")
    return EvaluationEvidence(
        event_id=_required_text(raw, "event_id"),
        account_id=_required_text(raw, "account_id"),
        text=_required_text(raw, "text"),
        occurred_at=_timestamp(raw.get("occurred_at")),
        speaker_class=speaker,
        session_id=_optional_text(raw.get("session_id")),
    )


def _parse_review(value: object) -> EvaluationReview:
    raw = _object(value, "review")
    action = _required_text(raw, "action")
    if action not in _EVALUATION_REVIEW_ACTIONS:
        raise ValueError(f"invalid evaluation review action: {action}")
    return EvaluationReview(
        source_event_id=_required_text(raw, "source_event_id"),
        action=action,
        corrected_value=_optional_text(raw.get("corrected_value")),
        value_contains=str(raw.get("value_contains", "")),
    )


def _parse_expected(value: object) -> ExpectedMemory:
    raw = _object(value, "expected memory")
    memory_kind_value = raw.get("memory_kind")
    memory_kind: MemoryKind | None = None
    if memory_kind_value is not None:
        parsed_kind = str(memory_kind_value)
        if parsed_kind not in _MEMORY_KINDS:
            raise ValueError(f"invalid expected memory_kind: {parsed_kind}")
        memory_kind = parsed_kind
    return ExpectedMemory(
        key=_required_text(raw, "key"),
        account_id=_required_text(raw, "account_id"),
        match_all=tuple(_text_list(raw, "match_all")),
        kind=str(raw.get("kind", "")),
        memory_kind=memory_kind,
        source_event_ids=tuple(_text_list(raw, "source_event_ids", required=False)),
        valid_from=_optional_timestamp(raw.get("valid_from")),
        valid_to=_optional_timestamp(raw.get("valid_to")),
    )


def _parse_query(value: object) -> EvaluationQuery:
    raw = _object(value, "query")
    mode = str(raw.get("mode", "search"))
    if mode not in _EVALUATION_QUERY_MODES:
        raise ValueError(f"invalid evaluation query mode: {mode}")
    relevance_raw = raw.get("relevance")
    if not isinstance(relevance_raw, dict):
        raise ValueError("evaluation query relevance must be an object")
    relevance = {
        str(key): int(str(grade))
        for key, grade in cast(dict[object, object], relevance_raw).items()
    }
    return EvaluationQuery(
        query_id=_required_text(raw, "query_id"),
        account_id=_required_text(raw, "account_id"),
        text=str(raw.get("text", "")),
        mode=mode,
        relevance=relevance,
        limit=int(str(raw.get("limit", 10))),
        valid_at=_optional_timestamp(raw.get("valid_at")),
        entity_ids=tuple(_text_list(raw, "entity_ids", required=False)),
    )


def _match_items(
    expected_items: Sequence[ExpectedMemory],
    predicted_items: Sequence[EvaluationItem],
) -> tuple[dict[str, EvaluationItem], set[int]]:
    pairs: dict[str, EvaluationItem] = {}
    matched_indexes: set[int] = set()
    for expected in expected_items:
        for index, predicted in enumerate(predicted_items):
            if index in matched_indexes or not _matches(expected, predicted):
                continue
            pairs[expected.key] = predicted
            matched_indexes.add(index)
            break
    return pairs, matched_indexes


def _matches(expected: ExpectedMemory, predicted: EvaluationItem) -> bool:
    if expected.account_id != predicted.account_id:
        return False
    if expected.kind and expected.kind != predicted.kind:
        return False
    if expected.memory_kind is not None and expected.memory_kind != predicted.memory_kind:
        return False
    content = _normalized(f"{predicted.title} {predicted.body}")
    return all(_normalized(term) in content for term in expected.match_all)


def _ranked_expected_keys(
    expected_items: Sequence[ExpectedMemory],
    predicted_items: Sequence[EvaluationItem],
) -> list[str]:
    result: list[str] = []
    for predicted in predicted_items:
        key = next(
            (
                expected.key
                for expected in expected_items
                if expected.key not in result and _matches(expected, predicted)
            ),
            f"unmatched:{predicted.item_id}",
        )
        result.append(key)
    return result


def _matched_expected(
    dataset: MemoryEvaluationDataset,
    observed_by_case: Mapping[str, EvaluationObservation],
) -> set[tuple[str, str]]:
    matched: set[tuple[str, str]] = set()
    for case in dataset.cases:
        pairs, _ = _match_items(
            case.expected_memories,
            observed_by_case[case.case_id].extracted_items,
        )
        matched.update((case.case_id, key) for key in pairs)
    return matched


def _source_account(case: MemoryEvaluationCase, source_event_id: str) -> str:
    for evidence in case.evidence:
        if evidence.event_id == source_event_id:
            return evidence.account_id
    raise ValueError(f"unknown review source event: {source_event_id}")


def _source_account_for_item(
    source_event_ids: Sequence[str],
    source_accounts: Mapping[str, str],
    *,
    fallback: str,
) -> str:
    accounts = {
        source_accounts[source_event_id]
        for source_event_id in source_event_ids
        if source_event_id in source_accounts
    }
    if len(accounts) == 1:
        return next(iter(accounts))
    if len(accounts) > 1:
        return "__mixed_source_accounts__"
    return fallback


def _same_time(expected: datetime | None, actual: datetime | None) -> bool:
    if expected is None or actual is None:
        return expected is actual
    return expected.astimezone(UTC) == actual.astimezone(UTC)


def _recall_at(ranked: Sequence[str], relevant: set[str], limit: int) -> float:
    if not relevant:
        return 1.0
    return len(set(ranked[:limit]) & relevant) / len(relevant)


def _ndcg(grades: Sequence[int], ideal: Sequence[int]) -> float:
    ideal_score = _dcg(ideal)
    return _dcg(grades) / ideal_score if ideal_score else 1.0


def _dcg(grades: Sequence[int]) -> float:
    return float(
        sum(
            (2**grade - 1) / math.log2(index + 2)
            for index, grade in enumerate(grades)
        )
    )


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _boolean_mean(values: Sequence[bool]) -> float:
    return _mean([1.0 if value else 0.0 for value in values])


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _normalized(value: str) -> str:
    return "".join(character.casefold() for character in value if character.isalnum())


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"memory evaluation {label} must be an object")
    return cast(dict[str, object], value)


def _required_text(raw: Mapping[str, object], key: str) -> str:
    value = str(raw.get(key, "")).strip()
    if not value:
        raise ValueError(f"memory evaluation field {key} must not be blank")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _required_list(raw: Mapping[str, object], key: str) -> list[object]:
    value = raw.get(key)
    if not isinstance(value, list):
        raise ValueError(f"memory evaluation field {key} must be an array")
    return cast(list[object], value)


def _optional_list(raw: Mapping[str, object], key: str) -> list[object]:
    value = raw.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"memory evaluation field {key} must be an array")
    return cast(list[object], value)


def _text_list(
    raw: Mapping[str, object],
    key: str,
    *,
    required: bool = True,
) -> list[str]:
    values = _required_list(raw, key) if required else _optional_list(raw, key)
    result = [str(value).strip() for value in values if str(value).strip()]
    if required and not result:
        raise ValueError(f"memory evaluation field {key} must not be empty")
    return result


def _timestamp(value: object) -> datetime:
    timestamp = _optional_timestamp(value)
    if timestamp is None:
        raise ValueError("memory evaluation timestamp is required")
    return timestamp


def _optional_timestamp(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("memory evaluation timestamps must include timezone")
    return parsed


def report_json(report: MemoryEvaluationReport) -> str:
    return json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)


def covered_scenarios(dataset: MemoryEvaluationDataset) -> frozenset[EvaluationScenario]:
    return frozenset(case.scenario for case in dataset.cases)


def expected_scenarios() -> frozenset[EvaluationScenario]:
    return frozenset(_EVALUATION_SCENARIOS)
