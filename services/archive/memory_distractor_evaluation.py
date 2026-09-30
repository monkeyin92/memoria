"""Large-corpus distractor evaluation for memory retrieval.

The fixed evaluation sets give an account two to four documents, so any retrieval
path that returns the whole account scores well.  This evaluation puts a few hundred
memories into ONE account -- topic clusters whose members differ in one attribute,
facts about other people, and daily trivia -- and asks questions whose target shares
no content term with the question, plus queries whose one rare token (a year, a name, a
proper noun) occurs in exactly one memory, which show what a retrieval change costs when
the token is all that separates look-alike memories.  A variant keeps the labelled core plus the first
N background memories, so the same queries are asked at growing corpus sizes.

Metrics are memory level.  The claim, episode and knowledge projections of one
utterance are one memory (they share a source event): their relative order is decided
by how the projection text is built, not by the retrieval model under test.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from services.archive.memory_evaluation import (
    EvaluationItem,
    EvaluationObservation,
    EvaluationQuery,
    MemoryEvaluationAdapter,
    MemoryEvaluationCase,
    load_memory_evaluation_dataset,
)

#: The companion context read asks the catalog for this many documents
#: (control_api ``fetch_memory``); a target outside them is never shown to the assistant.
PRODUCTION_LIMIT = 8
#: A deeper read of the same query, for rank metrics beyond the production window.
DEEP_LIMIT = 30

POSITIVE_TAGS = ("paraphrase", "lexical", "exact_term")
NEGATIVE_TAGS = ("off_topic", "near_miss")
_LIMIT_SEPARATOR = "@"
_SELF_PREFIX = "self."
_TIERS = ("core", "extra", "peer", "trivia")


@dataclass(frozen=True, slots=True)
class DistractorPool:
    version: str
    sha256: str
    case: MemoryEvaluationCase
    #: Every non-core event id; a variant keeps a prefix of this order.
    background_order: tuple[str, ...]
    tier_by_event: Mapping[str, str]
    cluster_by_event: Mapping[str, str]
    tags_by_query: Mapping[str, tuple[str, ...]]
    cluster_by_query: Mapping[str, str]

    @property
    def core_event_ids(self) -> frozenset[str]:
        return frozenset(event for event, tier in self.tier_by_event.items() if tier == "core")


@dataclass(frozen=True, slots=True)
class QueryOutcome:
    query_id: str
    tags: tuple[str, ...]
    cluster: str | None
    target: str | None
    #: 1-based memory rank in the deep read; None when the target is absent from it.
    rank: int | None
    #: Whether the target has a document among the first PRODUCTION_LIMIT documents.
    window_hit: bool | None
    window_docs: int
    top_memory: str | None
    top_tier: str | None
    top_in_cluster: bool | None
    #: Ranking score of the first document of the production read.
    top_score: float | None
    #: Best score among the target's documents in the production read.
    target_score: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "tags": list(self.tags),
            "cluster": self.cluster,
            "target": self.target,
            "rank": self.rank,
            "window_hit": self.window_hit,
            "window_docs": self.window_docs,
            "top_memory": self.top_memory,
            "top_tier": self.top_tier,
            "top_in_cluster": self.top_in_cluster,
            "top_score": self.top_score,
            "target_score": self.target_score,
        }


def load_distractor_pool(path: str | Path) -> DistractorPool:
    raw_text = Path(path).read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    dataset = load_memory_evaluation_dataset(path)
    if len(dataset.cases) != 1:
        raise ValueError("a distractor dataset must contain exactly one case")
    case = dataset.cases[0]
    raw_case = raw["cases"][0]
    pool = raw.get("pool")
    if not isinstance(pool, dict) or not isinstance(pool.get("background_order"), list):
        raise ValueError("distractor dataset needs pool.background_order")
    tier_by_event: dict[str, str] = {}
    cluster_by_event: dict[str, str] = {}
    for record in raw_case["evidence"]:
        tier = str(record.get("tier", ""))
        if tier not in _TIERS:
            raise ValueError(f"evidence {record.get('event_id')} has unknown tier {tier!r}")
        tier_by_event[str(record["event_id"])] = tier
        if record.get("cluster"):
            cluster_by_event[str(record["event_id"])] = str(record["cluster"])
    background = tuple(str(value) for value in pool["background_order"])
    non_core = {event for event, tier in tier_by_event.items() if tier != "core"}
    if len(set(background)) != len(background) or set(background) != non_core:
        raise ValueError("pool.background_order must list every non-core event exactly once")
    tags_by_query: dict[str, tuple[str, ...]] = {}
    cluster_by_query: dict[str, str] = {}
    for record in raw_case["queries"]:
        query_id = str(record["query_id"])
        tags = tuple(str(tag) for tag in record.get("tags", ()))
        if not set(tags) & {*POSITIVE_TAGS, *NEGATIVE_TAGS}:
            raise ValueError(f"query {query_id} has no known tag")
        tags_by_query[query_id] = tags
        if record.get("cluster"):
            cluster_by_query[query_id] = str(record["cluster"])
    for query in case.queries:
        positive = bool(set(tags_by_query[query.query_id]) & set(POSITIVE_TAGS))
        relevant = [key for key, grade in query.relevance.items() if grade > 0]
        if positive and (len(relevant) != 1 or relevant[0] not in tier_by_event):
            raise ValueError(f"positive query {query.query_id} needs one relevant event")
        if not positive and relevant:
            raise ValueError(f"negative query {query.query_id} must have no relevant memory")
    return DistractorPool(
        version=str(raw["version"]),
        sha256=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        case=case,
        background_order=background,
        tier_by_event=tier_by_event,
        cluster_by_event=cluster_by_event,
        tags_by_query=tags_by_query,
        cluster_by_query=cluster_by_query,
    )


def build_variant(
    pool: DistractorPool,
    *,
    background: int,
    check_ingestion: bool = False,
) -> MemoryEvaluationCase:
    """The core plus the first ``background`` background memories, each query read twice.

    Every query is asked at the production limit (what the assistant would see) and at
    the deep limit (rank metrics).  ``check_ingestion`` adds one query per memory that
    asks for the memory's own sentence, so a memory the catalog silently failed to store
    shows up as a failure instead of as a retrieval miss.
    """

    if background < 0:
        raise ValueError("background must not be negative")
    keep = set(pool.core_event_ids) | set(pool.background_order[:background])
    evidence = tuple(item for item in pool.case.evidence if item.event_id in keep)
    queries: list[EvaluationQuery] = []
    for query in pool.case.queries:
        for limit in (PRODUCTION_LIMIT, DEEP_LIMIT):
            queries.append(
                replace(query, query_id=f"{query.query_id}{_LIMIT_SEPARATOR}{limit}", limit=limit)
            )
    if check_ingestion:
        queries.extend(
            EvaluationQuery(
                query_id=f"{_SELF_PREFIX}{item.event_id}",
                account_id=item.account_id,
                text=item.text,
                mode="search",
                relevance={item.event_id: 3},
                limit=PRODUCTION_LIMIT,
            )
            for item in evidence
        )
    return replace(
        pool.case,
        case_id=f"{pool.case.case_id}-b{background}",
        evidence=evidence,
        queries=tuple(queries),
    )


def memory_ids(items: Sequence[EvaluationItem]) -> list[str]:
    """One memory id per item; items sharing a source event (chain) share the id."""

    parent: dict[str, str] = {}

    def find(node: str) -> str:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    anchors: list[str] = []
    for item in items:
        events = list(item.source_event_ids) or [item.item_id]
        for event in events:
            parent.setdefault(event, event)
        for event in events[1:]:
            left, right = find(events[0]), find(event)
            if left != right:
                parent[max(left, right)] = min(left, right)
        anchors.append(events[0])
    return [find(anchor) for anchor in anchors]


def _best_score(items: Sequence[EvaluationItem], target: str) -> float | None:
    scores = [
        item.score for item in items if target in item.source_event_ids and item.score is not None
    ]
    return max(scores) if scores else None


def score_query(
    pool: DistractorPool,
    *,
    query_id: str,
    window: Sequence[EvaluationItem],
    deep: Sequence[EvaluationItem],
) -> QueryOutcome:
    query = next(item for item in pool.case.queries if item.query_id == query_id)
    tags = pool.tags_by_query[query_id]
    cluster = pool.cluster_by_query.get(query_id)
    target = next((key for key, grade in query.relevance.items() if grade > 0), None)
    deep_ids = memory_ids(deep)
    order = list(dict.fromkeys(deep_ids))
    rank: int | None = None
    if target is not None:
        target_memory = next(
            (
                memory
                for item, memory in zip(deep, deep_ids, strict=True)
                if target in item.source_event_ids
            ),
            None,
        )
        if target_memory is not None:
            rank = order.index(target_memory) + 1
    window_hit: bool | None = None
    if target is not None:
        window_hit = any(target in item.source_event_ids for item in window)
    top_memory = order[0] if order else None
    return QueryOutcome(
        query_id=query_id,
        tags=tags,
        cluster=cluster,
        target=target,
        rank=rank,
        window_hit=window_hit,
        window_docs=len(window),
        top_memory=top_memory,
        top_tier=pool.tier_by_event.get(top_memory) if top_memory is not None else None,
        top_in_cluster=(
            None
            if top_memory is None or cluster is None
            else pool.cluster_by_event.get(top_memory) == cluster
        ),
        top_score=window[0].score if window else None,
        target_score=None if target is None else _best_score(window, target),
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def summarize_positive(outcomes: Sequence[QueryOutcome]) -> dict[str, Any]:
    ranks = [outcome.rank for outcome in outcomes]
    wrong = [outcome for outcome in outcomes if outcome.rank != 1]
    errors: Counter[str] = Counter()
    for outcome in wrong:
        if outcome.top_memory is None:
            errors["no_result"] += 1
        elif outcome.top_in_cluster:
            errors["same_cluster_sibling"] += 1
        else:
            errors[f"other_{outcome.top_tier}"] += 1
    return {
        "n": len(outcomes),
        "top1": _mean([1.0 if rank == 1 else 0.0 for rank in ranks]),
        "recall_at_3": _mean([1.0 if rank is not None and rank <= 3 else 0.0 for rank in ranks]),
        "recall_at_5": _mean([1.0 if rank is not None and rank <= 5 else 0.0 for rank in ranks]),
        "recall_at_10": _mean([1.0 if rank is not None and rank <= 10 else 0.0 for rank in ranks]),
        "mrr": _mean([0.0 if rank is None else 1.0 / rank for rank in ranks]),
        "ndcg_at_10": _mean(
            [
                1.0 / math.log2(rank + 1) if rank is not None and rank <= 10 else 0.0
                for rank in ranks
            ]
        ),
        "found_in_deep_read": _mean([0.0 if rank is None else 1.0 for rank in ranks]),
        "found_in_production_window": _mean(
            [1.0 if outcome.window_hit else 0.0 for outcome in outcomes]
        ),
        "top1_errors": dict(sorted(errors.items())),
    }


def summarize_negative(outcomes: Sequence[QueryOutcome]) -> dict[str, Any]:
    scores = [outcome.top_score for outcome in outcomes if outcome.top_score is not None]
    named = [outcome for outcome in outcomes if outcome.cluster is not None]
    return {
        "n": len(outcomes),
        "mean_docs_in_window": _mean([float(outcome.window_docs) for outcome in outcomes]),
        "share_with_any_doc": _mean([1.0 if outcome.window_docs else 0.0 for outcome in outcomes]),
        "mean_top_score": _mean(scores) if scores else None,
        "top_in_named_cluster": (
            _mean([1.0 if outcome.top_in_cluster else 0.0 for outcome in named]) if named else None
        ),
    }


def _auc(positive: Sequence[float], negative: Sequence[float]) -> float | None:
    if not positive or not negative:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in positive for n in negative)
    return wins / (len(positive) * len(negative))


def score_threshold(outcomes: Sequence[QueryOutcome]) -> dict[str, Any]:
    """What a minimum-score cut-off would do: keep targets, drop junk.

    A positive query is kept at threshold t when its target is inside the production
    window with a score >= t (a lost target counts as -inf).  A negative query still
    shows junk at t when its best document scores >= t.  ``operating_points`` lists, for
    a few shares of positive queries to keep, the strictest threshold that still keeps
    that share and how much junk survives it (threshold None: the share is unreachable
    because too many targets are already lost).
    """

    positives = [o for o in outcomes if set(o.tags) & set(POSITIVE_TAGS)]
    scores = [
        o.target_score if o.window_hit and o.target_score is not None else -math.inf
        for o in positives
    ]
    finite = sorted(score for score in scores if score != -math.inf)
    result: dict[str, Any] = {}
    for name in NEGATIVE_TAGS:
        negatives = [o for o in outcomes if name in o.tags]
        tops = [o.top_score if o.top_score is not None else -math.inf for o in negatives]
        # A lost target and a query that returned nothing are the same "no score" state.
        auc_positive = [-1.0 if score == -math.inf else score for score in scores]
        auc_negative = [-1.0 if top == -math.inf else top for top in tops]
        result[f"auc_target_vs_{name}"] = _auc(auc_positive, auc_negative)
    points: list[dict[str, Any]] = []
    for share in (0.95, 0.9, 0.75, 0.5):
        need = math.ceil(share * len(positives)) if positives else 0
        threshold: float | None = None
        if need > 0 and len(finite) >= need:
            threshold = finite[len(finite) - need]
        point: dict[str, Any] = {"keep": share, "threshold": threshold}
        if threshold is not None:
            point["targets_kept"] = _mean([1.0 if score >= threshold else 0.0 for score in scores])
            for name in NEGATIVE_TAGS:
                negatives = [o for o in outcomes if name in o.tags]
                point[f"{name}_showing_junk"] = _mean(
                    [
                        1.0 if o.top_score is not None and o.top_score >= threshold else 0.0
                        for o in negatives
                    ]
                )
        points.append(point)
    result["operating_points"] = points
    return result


def score_variant(
    pool: DistractorPool,
    case: MemoryEvaluationCase,
    observation: EvaluationObservation,
    *,
    background: int,
) -> dict[str, Any]:
    by_id = {result.query_id: result for result in observation.query_results}
    outcomes: list[QueryOutcome] = []
    for query in pool.case.queries:
        window = by_id[f"{query.query_id}{_LIMIT_SEPARATOR}{PRODUCTION_LIMIT}"].items
        deep = by_id[f"{query.query_id}{_LIMIT_SEPARATOR}{DEEP_LIMIT}"].items
        outcomes.append(score_query(pool, query_id=query.query_id, window=window, deep=deep))
    by_tag = {
        tag: [outcome for outcome in outcomes if tag in outcome.tags]
        for tag in (*POSITIVE_TAGS, *NEGATIVE_TAGS)
    }
    positives = [outcome for outcome in outcomes if set(outcome.tags) & set(POSITIVE_TAGS)]
    summary: dict[str, Any] = {
        "paraphrase": summarize_positive(by_tag["paraphrase"]),
        "lexical": summarize_positive(by_tag["lexical"]),
        "exact_term": summarize_positive(by_tag["exact_term"]),
        "positive": summarize_positive(positives),
        "off_topic": summarize_negative(by_tag["off_topic"]),
        "near_miss": summarize_negative(by_tag["near_miss"]),
    }
    variant: dict[str, Any] = {
        "background": background,
        "memories": len(case.evidence),
        "summary": summary,
        "threshold": score_threshold(outcomes),
        "queries": [outcome.as_dict() for outcome in outcomes],
    }
    self_results = [
        result for result in observation.query_results if result.query_id.startswith(_SELF_PREFIX)
    ]
    if self_results:
        failures = [
            result.query_id.removeprefix(_SELF_PREFIX)
            for result in self_results
            if not any(
                result.query_id.removeprefix(_SELF_PREFIX) in item.source_event_ids
                for item in result.items
            )
        ]
        variant["ingestion"] = {"checked": len(self_results), "failures": failures}
    return variant


async def evaluate_variant(
    adapter: MemoryEvaluationAdapter,
    pool: DistractorPool,
    *,
    background: int,
    check_ingestion: bool = False,
) -> dict[str, Any]:
    case = build_variant(pool, background=background, check_ingestion=check_ingestion)
    observation = await adapter.observe(case)
    return score_variant(pool, case, observation, background=background)


def build_receipt(
    pool: DistractorPool,
    *,
    adapter_name: str,
    config: Mapping[str, Any],
    variants: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "dataset_version": pool.version,
        "dataset_sha256": pool.sha256,
        "adapter": adapter_name,
        "config": dict(config),
        "production_limit": PRODUCTION_LIMIT,
        "deep_limit": DEEP_LIMIT,
        "variants": list(variants),
    }


def receipt_json(receipt: Mapping[str, Any]) -> str:
    """Indented JSON with one line per query, which keeps receipts a third of the size."""

    slim = json.loads(json.dumps(receipt))
    lines: dict[str, str] = {}
    for v, variant in enumerate(slim["variants"]):
        for q, query in enumerate(variant["queries"]):
            token = f"@@query-{v}-{q}@@"
            lines[json.dumps(token)] = json.dumps(query, ensure_ascii=False, separators=(",", ":"))
            variant["queries"][q] = token
    text = json.dumps(slim, ensure_ascii=False, indent=1)
    for token, line in lines.items():
        text = text.replace(token, line)
    return text + "\n"


def _reciprocal(query: Mapping[str, Any]) -> float:
    rank = query["rank"]
    return 0.0 if rank is None else 1.0 / rank


def _bootstrap(
    differences: Sequence[float], *, seed: int, iterations: int
) -> tuple[float, float, float]:
    rng = random.Random(seed)
    n = len(differences)
    means = sorted(
        sum(differences[rng.randrange(n)] for _ in range(n)) / n for _ in range(iterations)
    )
    low = means[int(0.025 * iterations)]
    high = means[min(iterations - 1, int(0.975 * iterations))]
    return sum(differences) / n, low, high


def compare_receipts(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
    *,
    seed: int = 20260930,
    iterations: int = 2000,
) -> list[dict[str, Any]]:
    """Paired comparison of two receipts (second minus first) per corpus size and group."""

    if first.get("dataset_sha256") != second.get("dataset_sha256"):
        raise ValueError("receipts were produced from different datasets")
    second_by_size = {variant["background"]: variant for variant in second["variants"]}
    rows: list[dict[str, Any]] = []
    for variant in first["variants"]:
        other = second_by_size.get(variant["background"])
        if other is None:
            continue
        left = {q["query_id"]: q for q in variant["queries"]}
        right = {q["query_id"]: q for q in other["queries"]}
        for group, tags in (
            ("paraphrase", ("paraphrase",)),
            ("lexical", ("lexical",)),
            ("exact_term", ("exact_term",)),
            ("positive", POSITIVE_TAGS),
        ):
            ids = [
                query_id
                for query_id, query in left.items()
                if set(query["tags"]) & set(tags) and query_id in right
            ]
            if not ids:
                continue
            rr = [_reciprocal(right[i]) - _reciprocal(left[i]) for i in ids]
            top1 = [
                (1.0 if right[i]["rank"] == 1 else 0.0) - (1.0 if left[i]["rank"] == 1 else 0.0)
                for i in ids
            ]
            mrr_mean, mrr_low, mrr_high = _bootstrap(rr, seed=seed, iterations=iterations)
            top1_mean, top1_low, top1_high = _bootstrap(top1, seed=seed + 1, iterations=iterations)
            rows.append(
                {
                    "background": variant["background"],
                    "group": group,
                    "n": len(ids),
                    "mrr_first": _mean([_reciprocal(left[i]) for i in ids]),
                    "mrr_second": _mean([_reciprocal(right[i]) for i in ids]),
                    "mrr_diff": mrr_mean,
                    "mrr_diff_ci95": [mrr_low, mrr_high],
                    "top1_first": _mean([1.0 if left[i]["rank"] == 1 else 0.0 for i in ids]),
                    "top1_second": _mean([1.0 if right[i]["rank"] == 1 else 0.0 for i in ids]),
                    "top1_diff": top1_mean,
                    "top1_diff_ci95": [top1_low, top1_high],
                    "second_better": sum(1 for d in top1 if d > 0),
                    "first_better": sum(1 for d in top1 if d < 0),
                }
            )
    return rows
