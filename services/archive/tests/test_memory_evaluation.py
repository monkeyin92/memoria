from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from services.archive.domain import EvidenceEvent
from services.archive.memory_domain import (
    ExtractedClaim,
    ExtractedTimeline,
    ExtractionUsage,
    MemoryExtraction,
)
from services.archive.memory_evaluation import (
    CatalogMemoryEvaluationAdapter,
    EvaluationItem,
    EvaluationObservation,
    EvaluationQuery,
    EvaluationQueryResult,
    ExpectedMemory,
    MemoryEvaluationAdapter,
    MemoryEvaluationCase,
    MemoryEvaluationDataset,
    _matches,
    _source_account_for_item,
    calculate_memory_metrics,
    covered_scenarios,
    expected_scenarios,
    load_memory_evaluation_dataset,
    report_json,
    run_memory_evaluation,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor

DATASET = Path(__file__).parents[1] / "evaluation" / "memory_eval_zh_v1.json"
UNSEEN_DATASET = Path(__file__).parents[1] / "evaluation" / "memory_eval_zh_v1_unseen.json"
DEMO_DATASET = Path(__file__).parents[1] / "evaluation" / "demo_scenarios_zh_v1.json"

# DEMO-02: the current fixed set is seven cases, not the original six storyboards.
# Six are positive recalls and one is the minor negative case.  The mapping is pinned
# so the set cannot drift away from the scenarios it exists to measure.
DEMO_STORYBOARDS = {
    "demo-student-math-weakness": "学生-学习进度：再次练习时召回分数应用题薄弱点",
    "demo-student-learning-preference": "学生-学习偏好：几天后召回先跟读再自己说",
    "demo-student-reading-preference": "学生-受限日常：显式确认后一周召回阅读偏好",
    "demo-student-unsupported-sensitive": "学生-不支持：情绪、家庭和主动跨会话关怀不进入长期记忆",
    "demo-elder-park-walk": "老年-日常陪伴：次日召回公园散步",
    "demo-elder-factory-story": "老年-人生故事留存：五天后接续纺织厂经历",
    "demo-elder-son-visit": "老年-情感陪伴：三天后召回“儿子好久没来”",
}


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
                    item for key in query.relevance for item in extracted if item.item_id == key
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
    assert len(dataset.cases) == 16
    assert covered_scenarios(dataset) == expected_scenarios()


@pytest.mark.asyncio
async def test_perfect_adapter_produces_stable_golden_metrics() -> None:
    dataset = load_memory_evaluation_dataset(DATASET)

    report = await run_memory_evaluation(dataset, PerfectAdapter())

    assert report.case_count == 16
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
    assert report.metrics.cross_session_recall_at_5 == 1
    assert report.metrics.paraphrase_followup_recall_at_5 == 1
    assert report.metrics.comfort_recall_at_5 == 1
    assert report.metrics.token_cost == 15 * len(dataset.cases)
    assert len(report.case_diagnostics) == len(dataset.cases)
    payload = json.loads(report_json(report))
    assert isinstance(payload["cases"], list)
    assert len(payload["cases"]) == len(dataset.cases)
    for case, diagnostic in zip(dataset.cases, report.case_diagnostics, strict=True):
        assert diagnostic.case_id == case.case_id
        assert diagnostic.scenario == case.scenario
        for expected, detail in zip(case.expected_memories, diagnostic.expected, strict=True):
            assert detail.key == expected.key
            assert detail.matched is True
            assert detail.matched_item_id == expected.key
            if expected.source_event_ids:
                assert detail.source_attribution_passed is True
                assert detail.expected_source_event_ids == expected.source_event_ids
                assert detail.actual_source_event_ids == expected.source_event_ids
            if expected.valid_from is not None or expected.valid_to is not None:
                assert detail.temporal_passed is True
        for query, detail in zip(case.queries, diagnostic.queries, strict=True):
            assert detail.query_id == query.query_id
            assert detail.recall_at_5 == 1
            assert detail.recall_at_10 == 1
            assert set(detail.relevant_keys) == {
                key for key, grade in query.relevance.items() if grade > 0
            }
        assert diagnostic.predicted_count == len(case.expected_memories)
        assert diagnostic.matched_projection_count == len(case.expected_memories)
        for expected, item in zip(case.expected_memories, diagnostic.extracted, strict=True):
            assert item.item_id == expected.key
            assert item.matched_expected_key == expected.key
            assert item.kind == (expected.kind or "claim")
            assert item.memory_kind == (expected.memory_kind or "semantic")
            assert item.status == "confirmed"
            assert item.source_event_ids == expected.source_event_ids


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
    isolation = next(case for case in dataset.cases if case.scenario == "cross_account_isolation")

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


@pytest.mark.asyncio
async def test_catalog_adapter_reports_long_horizon_scenario_scores() -> None:
    dataset = load_memory_evaluation_dataset(DATASET)
    long_horizon = tuple(
        case
        for case in dataset.cases
        if case.scenario
        in {
            "cross_session_followup",
            "paraphrase_followup",
            "comfort_recall",
        }
    )
    adapter = CatalogMemoryEvaluationAdapter()
    observations = tuple([await adapter.observe(case) for case in long_horizon])
    subset = MemoryEvaluationDataset(version=dataset.version, cases=long_horizon)

    metrics = calculate_memory_metrics(subset, observations)

    assert {case.scenario for case in long_horizon} == {
        "cross_session_followup",
        "paraphrase_followup",
        "comfort_recall",
    }
    assert metrics.cross_account_leakage == 0
    assert metrics.candidate_leakage == 0
    assert metrics.cross_session_recall_at_5 == 1
    assert metrics.paraphrase_followup_recall_at_5 == 1
    assert metrics.comfort_recall_at_5 == 1


def test_forbidden_text_rejects_a_persisted_candidate_before_recall() -> None:
    case = MemoryEvaluationCase(
        case_id="sensitive-candidate",
        scenario="cross_session_followup",
        evidence=(),
        expected_memories=(),
        queries=(
            EvaluationQuery(
                query_id="later",
                account_id="minor",
                text="心情好点了吗？",
                mode="context",
                relevance={},
                forbidden_text=("难过",),
            ),
        ),
        subject_category="minor",
    )
    dataset = MemoryEvaluationDataset(version="forbidden-candidate", cases=(case,))
    observation = EvaluationObservation(
        case_id=case.case_id,
        extracted_items=(
            EvaluationItem(
                item_id="candidate-mood",
                account_id="minor",
                kind="claim",
                memory_kind="semantic",
                title="今天状态不好",
                body="需要休息",
                status="candidate",
                source_event_ids=("mood-1",),
                source_texts=("我今天被老师批评了，好难过。",),
            ),
        ),
        query_results=(
            EvaluationQueryResult(
                query_id="later",
                mode="context",
                items=(),
                latency_ms=1,
            ),
        ),
    )

    with pytest.raises(ValueError, match="persisted text outside the subject policy"):
        calculate_memory_metrics(dataset, (observation,))

    supported_case = replace(
        case,
        case_id="supported-study",
        queries=(
            EvaluationQuery(
                query_id="later",
                account_id="minor",
                text="继续练习数学应用题",
                mode="context",
                relevance={},
                forbidden_text=("", "   "),
            ),
        ),
    )
    supported_dataset = MemoryEvaluationDataset(
        version="supported-study",
        cases=(supported_case,),
    )
    supported_observation = EvaluationObservation(
        case_id=supported_case.case_id,
        extracted_items=(
            EvaluationItem(
                item_id="study-progress",
                account_id="minor",
                kind="claim",
                memory_kind="semantic",
                title="分数应用题",
                body="薄弱点",
                status="confirmed",
                source_event_ids=("math-1",),
                source_texts=("我今天练习了数学应用题，分数应用题还是薄弱点。",),
            ),
        ),
        query_results=observation.query_results,
    )

    metrics = calculate_memory_metrics(supported_dataset, (supported_observation,))

    assert metrics.extraction_precision == 0.0


class CanonicalKeyExtractor:
    """The production shape: the extractor states the shared episode key.

    The lexical categoriser files the two statements under different domains;
    only an explicit canonical key (the Qwen path's contract) can join them.
    """

    version = "canonical-key-eval-test-v1"

    async def extract(self, event: EvidenceEvent) -> MemoryExtraction:
        text = str(event.payload["text"])
        domain = "work_experience" if "失败" in text else "daily_life"
        return MemoryExtraction(
            claims=(
                ExtractedClaim(
                    domain_category=domain,
                    subject_key="self",
                    predicate=domain,
                    value=text,
                    confidence=0.8,
                ),
            ),
            timeline=(
                ExtractedTimeline(
                    title=text,
                    domain_category=domain,
                    event_start=event.occurred_at,
                    event_end=None,
                    canonical_key="campus-delivery-startup",
                ),
            ),
            extractor_version=self.version,
        )


@pytest.mark.asyncio
async def test_fixed_dataset_metrics_are_pinned_for_the_rule_extractor() -> None:
    """P1-06: keep the fixed set visible to CI, with its honest ceiling.

    The rule extractor cannot produce canonical episode keys, so the
    cross-session episode case stays at its measured score; pinning the
    numbers makes any change to either the pipeline or the dataset fail here
    instead of silently drifting in a hand-run script.
    """
    dataset = load_memory_evaluation_dataset(DATASET)

    report = await run_memory_evaluation(dataset, CatalogMemoryEvaluationAdapter())

    assert report.metrics.recall_at_5 == pytest.approx(0.9375)
    assert report.metrics.ndcg_at_10 == pytest.approx(0.8590438584406034)
    assert report.metrics.source_attribution_accuracy == pytest.approx(0.95)
    assert report.metrics.candidate_leakage == 0
    assert report.metrics.cross_account_leakage == 0


@pytest.mark.asyncio
async def test_repeated_episode_case_passes_when_extraction_states_the_key() -> None:
    """P1-06 acceptance: one episode carrying both statements, and it is recalled.

    This is the case the rule extractor structurally cannot pass; with the
    extractor that states the shared key the query must reach one episode whose
    sources are exactly the two statements.
    """
    dataset = load_memory_evaluation_dataset(DATASET)
    case = next(
        value for value in dataset.cases if value.case_id == "repeated-episode-campus-startup"
    )

    observation = await CatalogMemoryEvaluationAdapter(CanonicalKeyExtractor()).observe(case)
    metrics = calculate_memory_metrics(
        MemoryEvaluationDataset(version=dataset.version, cases=(case,)),
        (observation,),
    )

    assert metrics.recall_at_5 == 1
    assert metrics.source_attribution_accuracy == 1
    episode_sources = {
        tuple(sorted(item.source_event_ids))
        for query in observation.query_results
        for item in query.items
        if item.kind == "episode"
    }
    assert ("eval-repeat-001", "eval-repeat-002") in episode_sources


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


class MissingRecallAdapter:
    """Returns every expected item except the one named in the constructor.

    ``extra_item`` is appended after the expected projections so diagnostics can
    show an unmatched row in the extraction-precision denominator.
    """

    name = "missing-recall"

    def __init__(
        self,
        missing_key: str,
        extra_item: EvaluationItem | None = None,
    ) -> None:
        self._missing_key = missing_key
        self._extra_item = extra_item

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
            if expected.key != self._missing_key
        )
        if self._extra_item is not None:
            extracted = (*extracted, self._extra_item)
        query_results = tuple(
            EvaluationQueryResult(
                query_id=query.query_id,
                mode=query.mode,
                latency_ms=1,
                items=tuple(
                    item for key in query.relevance for item in extracted if item.item_id == key
                ),
            )
            for query in case.queries
        )
        return EvaluationObservation(
            case_id=case.case_id,
            extracted_items=extracted,
            query_results=query_results,
        )


@pytest.mark.asyncio
async def test_case_diagnostics_show_a_missed_recall_and_stay_json_serializable() -> None:
    occurred_at = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    case = MemoryEvaluationCase(
        case_id="missed-recall",
        scenario="exact_fact",
        evidence=(),
        expected_memories=(
            ExpectedMemory(
                key="kept-fact",
                account_id="owner",
                match_all=("公园",),
                source_event_ids=("event-kept",),
                valid_from=occurred_at,
            ),
            ExpectedMemory(
                key="missed-fact",
                account_id="owner",
                match_all=("纺织厂",),
                source_event_ids=("event-missed",),
                valid_from=occurred_at,
            ),
        ),
        queries=(
            EvaluationQuery(
                query_id="later-day",
                account_id="owner",
                text="昨天去了哪里？",
                mode="search",
                relevance={"kept-fact": 2, "missed-fact": 1},
            ),
        ),
    )
    dataset = MemoryEvaluationDataset(version="diagnostics", cases=(case,))

    extra_from = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)
    extra_to = datetime(2026, 8, 2, 8, 0, tzinfo=UTC)
    extra = EvaluationItem(
        item_id="extra-episode",
        account_id="owner",
        kind="episode",
        memory_kind="episodic",
        title="额外包装",
        body="没有对应期望的 wrapper",
        status="candidate",
        source_event_ids=("event-extra",),
        valid_from=extra_from,
        valid_to=extra_to,
    )
    report = await run_memory_evaluation(dataset, MissingRecallAdapter("missed-fact", extra))
    payload = json.loads(report_json(report))

    assert payload["metrics"]["recall_at_5"] == 0.5
    diagnostic = payload["cases"][0]
    assert diagnostic["case_id"] == "missed-recall"
    assert diagnostic["scenario"] == "exact_fact"
    by_key = {item["key"]: item for item in diagnostic["expected"]}
    assert by_key["kept-fact"]["matched"] is True
    assert by_key["kept-fact"]["matched_item_id"] == "kept-fact"
    assert by_key["kept-fact"]["source_attribution_passed"] is True
    assert by_key["kept-fact"]["temporal_passed"] is True
    assert by_key["kept-fact"]["expected_source_event_ids"] == ["event-kept"]
    assert by_key["kept-fact"]["actual_source_event_ids"] == ["event-kept"]
    assert by_key["kept-fact"]["expected_valid_from"] == occurred_at.isoformat()
    assert by_key["kept-fact"]["actual_valid_from"] == occurred_at.isoformat()
    assert by_key["missed-fact"]["matched"] is False
    assert by_key["missed-fact"]["matched_item_id"] is None
    assert by_key["missed-fact"]["source_attribution_passed"] is False
    assert by_key["missed-fact"]["temporal_passed"] is False
    assert by_key["missed-fact"]["actual_source_event_ids"] == []
    query = diagnostic["queries"][0]
    assert query["query_id"] == "later-day"
    assert query["recall_at_5"] == 0.5
    assert query["recall_at_10"] == 0.5
    assert query["relevant_keys"] == ["kept-fact", "missed-fact"]
    assert query["recalled_keys"] == ["kept-fact"]
    assert diagnostic["predicted_count"] == 2
    assert diagnostic["matched_projection_count"] == 1
    extracted = {item["item_id"]: item for item in diagnostic["extracted"]}
    assert extracted["kept-fact"]["matched_expected_key"] == "kept-fact"
    assert extracted["kept-fact"]["kind"] == "claim"
    assert extracted["kept-fact"]["memory_kind"] == "semantic"
    assert extracted["kept-fact"]["status"] == "confirmed"
    assert extracted["kept-fact"]["source_event_ids"] == ["event-kept"]
    assert extracted["kept-fact"]["valid_from"] == occurred_at.isoformat()
    assert extracted["kept-fact"]["valid_to"] is None
    assert extracted["extra-episode"] == {
        "item_id": "extra-episode",
        "account_id": "owner",
        "kind": "episode",
        "memory_kind": "episodic",
        "title": "额外包装",
        "body": "没有对应期望的 wrapper",
        "status": "candidate",
        "source_event_ids": ["event-extra"],
        "valid_from": extra_from.isoformat(),
        "valid_to": extra_to.isoformat(),
        "matched_expected_key": None,
    }


def test_adapter_protocol_remains_structural() -> None:
    adapter: MemoryEvaluationAdapter = PerfectAdapter()
    assert adapter.name == "perfect"


@pytest.mark.asyncio
async def test_unseen_rewrite_set_reports_its_own_baseline() -> None:
    """P1-06: the unseen paraphrase set is reported separately, ceiling included.

    Two of its five queries miss under the rule extractor and are recorded here
    as the current ceiling rather than tuned away: the avoidance paraphrase
    ("外卖该避开什么") and the comfort paraphrase ("我最近有点撑不住了") do not
    match the planner's existing markers. Any change to either behaviour shows
    up as a metric move in this test.
    """
    dataset = load_memory_evaluation_dataset(UNSEEN_DATASET)

    assert dataset.version == "memory-eval-zh-v1-unseen"
    assert {case.scenario for case in dataset.cases} == {
        "cross_session_followup",
        "paraphrase",
        "comfort_recall",
        "cross_account_isolation",
    }
    report = await run_memory_evaluation(dataset, CatalogMemoryEvaluationAdapter())

    assert report.metrics.recall_at_5 == pytest.approx(0.6)
    assert report.metrics.ndcg_at_10 == pytest.approx(0.6)
    assert report.metrics.extraction_recall == pytest.approx(1.0)
    assert report.metrics.source_attribution_accuracy == pytest.approx(1.0)
    assert report.metrics.candidate_leakage == 0
    assert report.metrics.cross_account_leakage == 0


def test_demo_scenario_dataset_pairs_every_storyboard_with_a_later_recall() -> None:
    """DEMO-02: fixed demo scenarios, each one checked on a later day.

    Student cases declare the minor subject and stay inside the current long-term
    allowlist.  The unsupported case is intentionally empty: emotion, family, and
    proactive cross-session comfort are measured as failures, not as memories.
    """
    dataset = load_memory_evaluation_dataset(DEMO_DATASET)

    assert dataset.version == "demo-scenarios-zh-v1"
    assert [case.case_id for case in dataset.cases] == list(DEMO_STORYBOARDS)
    student_cases = [case for case in dataset.cases if case.case_id.startswith("demo-student-")]
    assert {case.subject_category for case in student_cases} == {"minor"}
    assert all(case.subject_category is None for case in dataset.cases if case not in student_cases)
    unsupported = next(
        case for case in dataset.cases if case.case_id == "demo-student-unsupported-sensitive"
    )
    assert unsupported.expected_memories == ()
    assert unsupported.queries
    assert all(any(term.strip() for term in query.forbidden_text) for query in unsupported.queries)
    for case in dataset.cases:
        graded = set().union(*(query.relevance for query in case.queries))
        assert {memory.key for memory in case.expected_memories} <= graded, case.case_id
        for memory in case.expected_memories:
            assert memory.match_all, case.case_id
            assert memory.source_event_ids, case.case_id
        occurred_at = {evidence.event_id: evidence.occurred_at for evidence in case.evidence}
        recalled = {
            event_id for memory in case.expected_memories for event_id in memory.source_event_ids
        }
        assert recalled <= set(occurred_at), case.case_id
        for query in case.queries:
            assert query.now is not None, case.case_id
            assert all(query.now > occurred_at[event_id] for event_id in recalled), case.case_id


@pytest.mark.asyncio
async def test_demo_scenario_dataset_matches_its_measured_offline_state() -> None:
    """Supported storyboards are reachable; the minor sensitive case stores nothing.

    Student cases run through the same subject-category filter as catalog compilation.
    Their supported memories are study progress, an explicit learning preference, and
    one closed daily preference confirmed by the existing write policy.  Emotion,
    family conflict, and a later proactive comfort question must not produce or recall
    a long-term memory.  Elder cases stay on the historical unspecified path.
    """
    dataset = load_memory_evaluation_dataset(DEMO_DATASET)
    adapter = CatalogMemoryEvaluationAdapter()

    reachable = set()
    for case in dataset.cases:
        observation = await adapter.observe(case)
        for query in case.queries:
            result = next(
                item for item in observation.query_results if item.query_id == query.query_id
            )
            if query.forbidden_text:
                assert observation.extracted_items == ()
                assert not any(
                    term in f"{item.title} {item.body}"
                    for item in (*observation.extracted_items, *result.items)
                    for term in query.forbidden_text
                )
            for expected in case.expected_memories:
                if any(_matches(expected, item) for item in result.items):
                    reachable.add(case.case_id)
    assert reachable == {
        case_id for case_id in DEMO_STORYBOARDS if case_id != "demo-student-unsupported-sensitive"
    }

    report = await run_memory_evaluation(dataset, CatalogMemoryEvaluationAdapter())

    assert report.case_count == len(DEMO_STORYBOARDS)
    assert report.metrics.extraction_recall == pytest.approx(1.0)
    assert report.metrics.recall_at_5 == pytest.approx(1.0)
    assert report.metrics.cross_session_recall_at_5 == pytest.approx(1.0)
    assert report.metrics.comfort_recall_at_5 == pytest.approx(1.0)
    assert report.metrics.cross_account_leakage == 0
