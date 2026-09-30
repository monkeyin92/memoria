from __future__ import annotations

import json
import os
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from services.archive.memory_distractor_evaluation import (
    DEEP_LIMIT,
    PRODUCTION_LIMIT,
    DistractorPool,
    QueryOutcome,
    build_variant,
    compare_receipts,
    evaluate_variant,
    load_distractor_pool,
    memory_ids,
    receipt_json,
    score_threshold,
    score_variant,
)
from services.archive.memory_domain import content_query_terms, lexical_query_terms
from services.archive.memory_evaluation import (
    CatalogMemoryEvaluationAdapter,
    EvaluationEvidence,
    EvaluationItem,
    EvaluationObservation,
    EvaluationQuery,
    EvaluationQueryResult,
    MemoryEvaluationCase,
)
from services.archive.recall_planner import RecallPlanner

DATASET = Path(__file__).parents[1] / "evaluation" / "memory_eval_zh_v3_distractors.json"


def _shared_terms(query: str, sentence: str) -> list[str]:
    """Content n-grams of the query that the lexical fallback would find in ``sentence``."""

    haystack = unicodedata.normalize("NFKC", sentence).lower()
    return [term for term in content_query_terms(lexical_query_terms(query)) if term in haystack]


@pytest.fixture(scope="module")
def pool() -> DistractorPool:
    return load_distractor_pool(DATASET)


def _target(pool: DistractorPool, query: EvaluationQuery) -> str:
    return next(key for key, grade in query.relevance.items() if grade > 0)


def _sentence(pool: DistractorPool, event_id: str) -> str:
    return next(item.text for item in pool.case.evidence if item.event_id == event_id)


def test_dataset_is_large_and_covers_every_query_kind(pool: DistractorPool) -> None:
    assert len(pool.core_event_ids) >= 100
    assert len(pool.background_order) >= 250
    tags = [tag for tags in pool.tags_by_query.values() for tag in tags]
    assert tags.count("paraphrase") >= 60
    assert tags.count("lexical") >= 15
    assert tags.count("exact_term") >= 25
    assert tags.count("off_topic") >= 8
    assert tags.count("near_miss") >= 8
    texts = [item.text for item in pool.case.evidence]
    assert len(set(texts)) == len(texts)
    query_texts = [query.text for query in pool.case.queries]
    assert len(set(query_texts)) == len(query_texts)


def test_paraphrase_queries_share_no_content_term_with_their_target(pool: DistractorPool) -> None:
    for query in pool.case.queries:
        if "paraphrase" not in pool.tags_by_query[query.query_id]:
            continue
        target = _target(pool, query)
        assert pool.tier_by_event[target] == "core"
        assert _shared_terms(query.text, _sentence(pool, target)) == [], query.query_id


def test_lexical_queries_share_a_content_term_with_their_target(pool: DistractorPool) -> None:
    for query in pool.case.queries:
        if "lexical" not in pool.tags_by_query[query.query_id]:
            continue
        assert _shared_terms(query.text, _sentence(pool, _target(pool, query))), query.query_id


def test_exact_term_queries_carry_a_token_only_their_target_contains(pool: DistractorPool) -> None:
    raw = json.loads(DATASET.read_text(encoding="utf-8"))["cases"][0]["queries"]
    tokens = {record["query_id"]: record["token"] for record in raw if "token" in record}
    exact = [q for q in pool.case.queries if "exact_term" in pool.tags_by_query[q.query_id]]
    assert exact and set(tokens) == {q.query_id for q in exact}
    for query in exact:
        token = tokens[query.query_id]
        holders = [item.event_id for item in pool.case.evidence if token in item.text]
        assert token in query.text
        assert holders == [_target(pool, query)], query.query_id


def test_queries_reach_the_catalog_unchanged_by_the_recall_planner(pool: DistractorPool) -> None:
    """Time windows, alias filters and the closed synonym lists would do the retrieval for it."""

    now = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    for query in pool.case.queries:
        plan = RecallPlanner.plan(query=query.text, now=now, people=())
        assert plan.text == query.text.strip(), query.query_id
        assert plan.occurred_after is None and plan.occurred_before is None, query.query_id
        assert plan.entity_ids == (), query.query_id


def test_background_order_is_a_permutation_of_the_non_core_events(pool: DistractorPool) -> None:
    non_core = {item.event_id for item in pool.case.evidence} - set(pool.core_event_ids)
    assert set(pool.background_order) == non_core
    assert len(pool.background_order) == len(non_core)


def test_variant_keeps_the_core_and_a_background_prefix_and_reads_each_query_twice(
    pool: DistractorPool,
) -> None:
    case = build_variant(pool, background=7)
    kept = {item.event_id for item in case.evidence}
    assert kept == set(pool.core_event_ids) | set(pool.background_order[:7])
    limits: dict[str, set[int]] = {}
    for query in case.queries:
        base, _, limit = query.query_id.rpartition("@")
        limits.setdefault(base, set()).add(int(limit))
    assert set(limits) == {query.query_id for query in pool.case.queries}
    assert all(value == {PRODUCTION_LIMIT, DEEP_LIMIT} for value in limits.values())
    with_self = build_variant(pool, background=7, check_ingestion=True)
    assert len(with_self.queries) == len(case.queries) + len(kept)


def _item(
    item_id: str,
    events: tuple[str, ...],
    score: float | None = None,
    kind: str = "claim",
) -> EvaluationItem:
    return EvaluationItem(
        item_id=item_id,
        account_id="a",
        kind=kind,
        memory_kind="semantic",
        title=item_id,
        body=item_id,
        status="candidate",
        source_event_ids=events,
        score=score,
    )


def test_projections_of_one_source_event_are_one_memory() -> None:
    items = [
        _item("claim-1", ("e1",)),
        _item("episode-1", ("e1",), kind="episode"),
        _item("claim-2", ("e2",)),
        _item("merged", ("e2", "e3"), kind="episode"),
        _item("claim-3", ("e3",)),
    ]
    ids = memory_ids(items)
    assert ids[0] == ids[1] == "e1"
    assert ids[2] == ids[3] == ids[4] == "e2"


def _tiny_pool() -> DistractorPool:
    when = datetime(2026, 9, 1, tzinfo=UTC)
    evidence = tuple(
        EvaluationEvidence(event_id=event, account_id="a", text=text, occurred_at=when)
        for event, text in (
            ("t.one", "第一句"),
            ("t.two", "第二句"),
            ("t.three", "第三句"),
            ("bg.one", "背景句"),
        )
    )
    queries = (
        EvaluationQuery("q.para", "a", "问一", "search", {"t.one": 3}),
        EvaluationQuery("q.lex", "a", "问二", "search", {"t.two": 3}),
        EvaluationQuery("q.off", "a", "无关", "search", {}),
    )
    case = MemoryEvaluationCase(
        case_id="tiny",
        scenario="paraphrase",
        evidence=evidence,
        expected_memories=(),
        queries=queries,
    )
    return DistractorPool(
        version="tiny",
        sha256="0",
        case=case,
        background_order=("bg.one",),
        tier_by_event={"t.one": "core", "t.two": "core", "t.three": "core", "bg.one": "trivia"},
        cluster_by_event={"t.one": "c", "t.two": "c", "t.three": "c"},
        tags_by_query={
            "q.para": ("paraphrase",),
            "q.lex": ("lexical",),
            "q.off": ("negative", "off_topic"),
        },
        cluster_by_query={"q.para": "c", "q.lex": "c"},
    )


def _result(query_id: str, items: list[EvaluationItem]) -> EvaluationQueryResult:
    return EvaluationQueryResult(
        query_id=query_id, mode="search", items=tuple(items), latency_ms=1.0
    )


def test_score_variant_ranks_memories_and_flags_window_misses_and_junk() -> None:
    pool = _tiny_pool()
    case = build_variant(pool, background=1)
    p, d = f"@{PRODUCTION_LIMIT}", f"@{DEEP_LIMIT}"
    # q.para: a sibling memory first, the target second (two projections each).
    para = [
        _item("s-claim", ("t.three",), 0.9),
        _item("s-episode", ("t.three",), 0.8, kind="episode"),
        _item("t-claim", ("t.one",), 0.7),
        _item("t-episode", ("t.one",), 0.6, kind="episode"),
    ]
    # q.lex: target first, but only found beyond the production window in the deep read.
    lex_window = [_item(f"bg-{i}", ("bg.one",), 0.5) for i in range(PRODUCTION_LIMIT)]
    lex_deep = [*lex_window, _item("t2-claim", ("t.two",), 0.4)]
    observation = EvaluationObservation(
        case_id=case.case_id,
        extracted_items=(),
        query_results=(
            _result("q.para" + p, para),
            _result("q.para" + d, para),
            _result("q.lex" + p, lex_window),
            _result("q.lex" + d, lex_deep),
            _result("q.off" + p, [_item("junk", ("bg.one",), 0.3)]),
            _result("q.off" + d, [_item("junk", ("bg.one",), 0.3)]),
        ),
    )
    variant = score_variant(pool, case, observation, background=1)
    by_id = {query["query_id"]: query for query in variant["queries"]}
    assert by_id["q.para"]["rank"] == 2 and by_id["q.para"]["top_in_cluster"] is True
    assert by_id["q.para"]["window_hit"] is True and by_id["q.para"]["target_score"] == 0.7
    assert by_id["q.lex"]["rank"] == 2 and by_id["q.lex"]["window_hit"] is False
    assert by_id["q.lex"]["top_tier"] == "trivia"
    summary = variant["summary"]
    assert summary["paraphrase"]["top1"] == 0.0 and summary["paraphrase"]["mrr"] == 0.5
    assert summary["paraphrase"]["top1_errors"] == {"same_cluster_sibling": 1}
    assert summary["lexical"]["found_in_production_window"] == 0.0
    assert summary["lexical"]["top1_errors"] == {"other_trivia": 1}
    assert summary["off_topic"]["mean_docs_in_window"] == 1.0
    assert summary["positive"]["n"] == 2 and summary["positive"]["recall_at_3"] == 1.0


def _outcome(
    tags: tuple[str, ...], *, hit: bool, target: float | None, top: float | None
) -> QueryOutcome:
    return QueryOutcome(
        query_id="q",
        tags=tags,
        cluster=None,
        target="t" if "negative" not in tags else None,
        rank=1 if hit else None,
        window_hit=hit if "negative" not in tags else None,
        window_docs=1 if top is not None else 0,
        top_memory=None,
        top_tier=None,
        top_in_cluster=None,
        top_score=top,
        target_score=target,
    )


def test_score_threshold_separates_targets_from_junk() -> None:
    positives = [
        _outcome(("paraphrase",), hit=True, target=0.6 + 0.01 * i, top=0.6 + 0.01 * i)
        for i in range(20)
    ]
    negatives = [
        _outcome(("negative", "off_topic"), hit=False, target=None, top=0.2 + 0.01 * i)
        for i in range(5)
    ]
    result = score_threshold([*positives, *negatives])
    assert result["auc_target_vs_off_topic"] == 1.0
    points = {point["keep"]: point for point in result["operating_points"]}
    assert points[0.95]["threshold"] == pytest.approx(0.61)
    assert points[0.95]["off_topic_showing_junk"] == 0.0
    assert points[0.5]["targets_kept"] >= 0.5
    lossy = [*positives[:10], *[_outcome(("paraphrase",), hit=False, target=None, top=0.1)] * 10]
    lossy_points = {point["keep"]: point for point in score_threshold(lossy)["operating_points"]}
    assert lossy_points[0.95]["threshold"] is None
    assert lossy_points[0.5]["threshold"] is not None


def _receipt(sha: str, ranks: list[int | None]) -> dict[str, Any]:
    queries = [
        {"query_id": f"q{i}", "tags": ["paraphrase"], "rank": rank} for i, rank in enumerate(ranks)
    ]
    return {
        "dataset_sha256": sha,
        "adapter": "x",
        "variants": [{"background": 0, "queries": queries}],
    }


def test_compare_receipts_pairs_queries_and_refuses_different_datasets() -> None:
    first = _receipt("s", [1, 2, None, 1])
    second = _receipt("s", [1, 1, 1, 3])
    row = next(
        r for r in compare_receipts(first, second, iterations=200) if r["group"] == "paraphrase"
    )
    assert row["n"] == 4 and row["second_better"] == 2 and row["first_better"] == 1
    assert row["mrr_diff"] == pytest.approx(((1 - 1) + (1 - 0.5) + (1 - 0) + (1 / 3 - 1)) / 4)
    assert row["mrr_diff_ci95"][0] <= row["mrr_diff"] <= row["mrr_diff_ci95"][1]
    with pytest.raises(ValueError, match="different datasets"):
        compare_receipts(first, _receipt("other", [1]))


@pytest.mark.skipif(
    not os.getenv("MEMORIA_TEST_POSTGRES_DSN"),
    reason="set MEMORIA_TEST_POSTGRES_DSN to a pgvector PostgreSQL to run the harness end to end",
)
async def test_lexical_fallback_ingests_every_memory_and_cannot_reach_paraphrases(
    pool: DistractorPool,
) -> None:
    adapter = CatalogMemoryEvaluationAdapter(os.environ["MEMORIA_TEST_POSTGRES_DSN"])
    variant = await evaluate_variant(adapter, pool, background=0, check_ingestion=True)
    assert variant["ingestion"] == {"checked": len(pool.core_event_ids), "failures": []}
    summary = variant["summary"]
    # By construction no paraphrase shares a content term with its target.
    assert summary["paraphrase"]["found_in_deep_read"] == 0.0
    assert summary["lexical"]["top1"] >= 0.9


def test_receipt_json_round_trips_and_keeps_one_line_per_query() -> None:
    receipt = _receipt("s", [1, 2, None])
    text = receipt_json(receipt)

    assert json.loads(text) == receipt
    assert sum(1 for line in text.splitlines() if line.strip().startswith('{"query_id"')) == 3
