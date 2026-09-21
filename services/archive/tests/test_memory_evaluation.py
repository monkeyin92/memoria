from __future__ import annotations

from dataclasses import replace
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
    EvaluationQueryResult,
    MemoryEvaluationAdapter,
    MemoryEvaluationCase,
    MemoryEvaluationDataset,
    _matches,
    _source_account_for_item,
    calculate_memory_metrics,
    covered_scenarios,
    expected_scenarios,
    load_memory_evaluation_dataset,
    run_memory_evaluation,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor

DATASET = Path(__file__).parents[1] / "evaluation" / "memory_eval_zh_v1.json"
UNSEEN_DATASET = (
    Path(__file__).parents[1] / "evaluation" / "memory_eval_zh_v1_unseen.json"
)
DEMO_DATASET = Path(__file__).parents[1] / "evaluation" / "demo_scenarios_zh_v1.json"

# DEMO-02: the six fund-raising storyboards, keyed by the case that carries each one.  The
# mapping is pinned so the set cannot drift away from the storyboard it exists to measure.
DEMO_STORYBOARDS = {
    "demo-student-math-weakness": "学生-学习陪伴：听到95分时召回“数学是弱项”",
    "demo-student-mood-recall": "学生-情绪关怀：几天后召回被老师批评的事",
    "demo-student-dinosaur-interest": "学生-兴趣陪伴：一周后由恐龙兴趣接上继续陪伴",
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
        value
        for value in dataset.cases
        if value.case_id == "repeated-episode-campus-startup"
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
    """DEMO-02: the six fixed demo scenarios, each one a memory asked for on a later day.

    The set is a demo storyboard first, so this pins its *shape*: the six storyboard cases
    are present, every expected key is graded in a query (a case can never be unscoreable),
    and each query is asked after the utterance it is supposed to recall.  Quality numbers
    are not pinned here - what the demo can quote is measured by running the set, and the
    offline ceiling is recorded separately.
    """
    dataset = load_memory_evaluation_dataset(DEMO_DATASET)

    assert dataset.version == "demo-scenarios-zh-v1"
    assert [case.case_id for case in dataset.cases] == list(DEMO_STORYBOARDS)
    for case in dataset.cases:
        graded = set().union(*(query.relevance for query in case.queries))
        assert {memory.key for memory in case.expected_memories} <= graded, case.case_id
        for memory in case.expected_memories:
            assert memory.match_all, case.case_id
            assert memory.source_event_ids, case.case_id
        occurred_at = {evidence.event_id: evidence.occurred_at for evidence in case.evidence}
        recalled = {
            event_id
            for memory in case.expected_memories
            for event_id in memory.source_event_ids
        }
        assert recalled <= set(occurred_at), case.case_id
        for query in case.queries:
            assert query.now is not None, case.case_id
            assert all(
                query.now > occurred_at[event_id] for event_id in recalled
            ), case.case_id


@pytest.mark.asyncio
async def test_demo_scenario_dataset_matches_its_measured_offline_state() -> None:
    """All six storyboards are reachable offline, and the expectations describe claims.

    Two things this pins, both learned by measuring the production assembly rather than by
    assuming it (numbers for that path are in HANDOFF; CI runs the offline rule extractor):

    * the expectations follow the *claim* surface, because that is what the memory contract
      stores: a claim carries the value ("七十几分", "难过") while the descriptive sentence
      ("上次数学考试只考了七十几分") lands on the episode item, so `match_all` terms that
      span both would be unsatisfiable for one extractor or the other.  A memory card built
      from the claim alone therefore reads thin - the demo should render the pair.
    * `demo-student-mood-recall` is scoreable again.  It used to carry no review because the
      extractor stored nothing for "我今天被老师批评了，好难过。", and a review whose source
      has no claim aborts the whole run; the prompt now says an explicitly stated feeling is
      a memory (and to keep the speaker's language), which made that case work.
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
            for expected in case.expected_memories:
                if any(_matches(expected, item) for item in result.items):
                    reachable.add(case.case_id)
    assert reachable == set(DEMO_STORYBOARDS)

    report = await run_memory_evaluation(dataset, CatalogMemoryEvaluationAdapter())

    assert report.case_count == len(DEMO_STORYBOARDS)
    assert report.metrics.extraction_recall == pytest.approx(1.0)
    assert report.metrics.recall_at_5 == pytest.approx(1.0)
    assert report.metrics.cross_session_recall_at_5 == pytest.approx(1.0)
    assert report.metrics.comfort_recall_at_5 == pytest.approx(1.0)
    assert report.metrics.cross_account_leakage == 0
