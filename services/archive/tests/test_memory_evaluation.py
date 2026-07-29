from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import ExtractionUsage, MemoryExtraction
from services.archive.memory_evaluation import (
    CatalogMemoryEvaluationAdapter,
    EvaluationItem,
    EvaluationObservation,
    EvaluationQueryResult,
    MemoryEvaluationAdapter,
    MemoryEvaluationCase,
    _source_account_for_item,
    calculate_memory_metrics,
    covered_scenarios,
    expected_scenarios,
    load_memory_evaluation_dataset,
    run_memory_evaluation,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor

DATASET = (
    Path(__file__).parents[1] / "evaluation" / "memory_eval_zh_v1.json"
)


class PerfectAdapter:
    name = "perfect"

    async def observe(self, case: MemoryEvaluationCase) -> EvaluationObservation:
        extracted = tuple(
            EvaluationItem(
                item_id=expected.key,
                account_id=expected.account_id,
                kind=expected.kind or "claim",
                memory_kind=expected.memory_kind or "semantic",
                title=" ".join(expected.match_all),
                body=" ".join(expected.match_all),
                status="confirmed",
                source_event_ids=expected.source_event_ids,
                valid_from=expected.valid_from,
                valid_to=expected.valid_to,
            )
            for expected in case.expected_memories
        )
        query_results = tuple(
            EvaluationQueryResult(
                query_id=query.query_id,
                mode=query.mode,
                latency_ms=float(index + 1),
                items=tuple(
                    item
                    for key in query.relevance
                    for item in extracted
                    if item.item_id == key
                ),
            )
            for index, query in enumerate(case.queries)
        )
        return EvaluationObservation(
            case_id=case.case_id,
            extracted_items=extracted,
            query_results=query_results,
            input_tokens=10,
            output_tokens=5,
        )


class UnsafeAdapter:
    name = "unsafe"

    async def observe(self, case: MemoryEvaluationCase) -> EvaluationObservation:
        results = tuple(
            EvaluationQueryResult(
                query_id=query.query_id,
                mode=query.mode,
                latency_ms=10,
                items=(
                    EvaluationItem(
                        item_id=f"unsafe-{query.query_id}",
                        account_id="another-account",
                        kind="claim",
                        memory_kind="semantic",
                        title="错误候选",
                        body="错误候选",
                        status="candidate",
                        source_event_ids=("wrong-source",),
                        conflict_state="active",
                    ),
                ),
            )
            for query in case.queries
        )
        return EvaluationObservation(
            case_id=case.case_id,
            extracted_items=(),
            query_results=results,
        )


class UsageExtractor:
    version = "usage-eval-v1"

    def __init__(self) -> None:
        self._delegate = RuleBasedMemoryExtractor()

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        extraction = await self._delegate.extract(event)
        return replace(
            extraction,
            usage=ExtractionUsage(input_tokens=7, output_tokens=2),
        )


def test_versioned_dataset_covers_every_required_memory_scenario() -> None:
    dataset = load_memory_evaluation_dataset(DATASET)

    assert dataset.version == "memory-eval-zh-v1"
    assert len(dataset.cases) == 13
    assert covered_scenarios(dataset) == expected_scenarios()


@pytest.mark.asyncio
async def test_perfect_adapter_produces_stable_golden_metrics() -> None:
    dataset = load_memory_evaluation_dataset(DATASET)

    report = await run_memory_evaluation(dataset, PerfectAdapter())

    assert report.case_count == 13
    assert report.metrics.extraction_precision == 1
    assert report.metrics.extraction_recall == 1
    assert report.metrics.recall_at_5 == 1
    assert report.metrics.recall_at_10 == 1
    assert report.metrics.ndcg_at_10 == 1
    assert report.metrics.temporal_accuracy == 1
    assert report.metrics.source_attribution_accuracy == 1
    assert report.metrics.contradiction_rate == 0
    assert report.metrics.cross_account_leakage == 0
    assert report.metrics.candidate_leakage == 0
    assert report.metrics.token_cost == 15 * len(dataset.cases)


@pytest.mark.asyncio
async def test_safety_metrics_detect_conflict_candidate_and_cross_account_leakage() -> None:
    dataset = load_memory_evaluation_dataset(DATASET)
    observations = tuple([await UnsafeAdapter().observe(case) for case in dataset.cases])

    metrics = calculate_memory_metrics(dataset, observations)

    assert metrics.extraction_precision == 0
    assert metrics.extraction_recall == 0
    assert metrics.contradiction_rate == 1
    assert metrics.cross_account_leakage == 1
    assert metrics.candidate_leakage == 1
    assert metrics.latency_p50_ms == 10
    assert metrics.latency_p95_ms == 10


@pytest.mark.asyncio
async def test_current_catalog_adapter_runs_offline_and_preserves_account_isolation() -> None:
    dataset = load_memory_evaluation_dataset(DATASET)
    isolation = next(
        case for case in dataset.cases if case.scenario == "cross_account_isolation"
    )

    observation = await CatalogMemoryEvaluationAdapter().observe(isolation)

    query = observation.query_results[0]
    assert all(item.account_id == "eval-owner-a" for item in query.items)
    assert all("拉萨" not in f"{item.title}{item.body}" for item in query.items)


@pytest.mark.asyncio
async def test_catalog_adapter_reports_extractor_token_usage() -> None:
    dataset = load_memory_evaluation_dataset(DATASET)
    case = next(value for value in dataset.cases if value.scenario == "exact_fact")

    observation = await CatalogMemoryEvaluationAdapter(UsageExtractor()).observe(case)

    assert (observation.input_tokens, observation.output_tokens) == (7, 2)


def test_source_account_attribution_exposes_cross_account_and_mixed_results() -> None:
    source_accounts = {"source-a": "account-a", "source-b": "account-b"}

    assert (
        _source_account_for_item(
            ("source-b",),
            source_accounts,
            fallback="account-a",
        )
        == "account-b"
    )
    assert (
        _source_account_for_item(
            ("source-a", "source-b"),
            source_accounts,
            fallback="account-a",
        )
        == "__mixed_source_accounts__"
    )


def test_adapter_protocol_remains_structural() -> None:
    adapter: MemoryEvaluationAdapter = PerfectAdapter()
    assert adapter.name == "perfect"
