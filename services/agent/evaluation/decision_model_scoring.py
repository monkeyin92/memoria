from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.common.companion_response_safety import companion_safety_decision
from services.common.crisis_policy import (
    CrisisSemanticVerdict,
    route_crisis,
)
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT


@dataclass(frozen=True, slots=True)
class BinaryCounts:
    tp: int = 0
    tn: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, *, predicted: bool, label: bool) -> BinaryCounts:
        if predicted and label:
            return BinaryCounts(self.tp + 1, self.tn, self.fp, self.fn)
        if predicted and not label:
            return BinaryCounts(self.tp, self.tn, self.fp + 1, self.fn)
        if (not predicted) and label:
            return BinaryCounts(self.tp, self.tn, self.fp, self.fn + 1)
        return BinaryCounts(self.tp, self.tn + 1, self.fp, self.fn)

    @property
    def n(self) -> int:
        return self.tp + self.tn + self.fp + self.fn

    @property
    def accuracy(self) -> float | None:
        return None if self.n == 0 else (self.tp + self.tn) / self.n

    @property
    def recall(self) -> float | None:
        denom = self.tp + self.fn
        return None if denom == 0 else self.tp / denom

    @property
    def false_positive_rate(self) -> float | None:
        denom = self.fp + self.tn
        return None if denom == 0 else self.fp / denom

    def as_dict(self) -> dict[str, Any]:
        return {
            "tp": self.tp,
            "tn": self.tn,
            "fp": self.fp,
            "fn": self.fn,
            "n": self.n,
            "accuracy": self.accuracy,
            "recall": self.recall,
            "false_positive_rate": self.false_positive_rate,
        }


def percentile(sorted_vals: Sequence[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = (len(sorted_vals) - 1) * p
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def latency_summary(samples_ms: Iterable[float]) -> dict[str, float | None]:
    vals = sorted(float(x) for x in samples_ms)
    return {
        "n": len(vals),
        "p50_ms": percentile(vals, 0.50),
        "p95_ms": percentile(vals, 0.95),
        "max_ms": vals[-1] if vals else None,
    }


def rule_crisis_routes_to_12356(text: str) -> bool:
    route = route_crisis(text)
    return route.script is CRISIS_SELF_SCRIPT


def composite_crisis_routes_to_12356(
    text: str,
    *,
    semantic_choice: str | None,
) -> bool:
    evidence: CrisisSemanticVerdict | None = None
    if semantic_choice:
        try:
            evidence = CrisisSemanticVerdict(semantic_choice)
        except ValueError:
            evidence = CrisisSemanticVerdict.UNSURE
    route = route_crisis(text, semantic_evidence=evidence)
    return route.script is CRISIS_SELF_SCRIPT


def score_binary_labeled(
    rows: Sequence[Mapping[str, Any]],
    *,
    label_key: str,
    pred_key: str,
) -> BinaryCounts:
    counts = BinaryCounts()
    for row in rows:
        label = row.get(label_key)
        pred = row.get(pred_key)
        if label is None or pred is None:
            continue
        counts = counts.add(predicted=bool(pred), label=bool(label))
    return counts


def score_must_12356_recall(
    rows: Sequence[Mapping[str, Any]],
    *,
    routed_key: str,
) -> dict[str, Any]:
    required = [r for r in rows if r.get("must_route_12356")]
    hits = sum(1 for r in required if r.get(routed_key))
    return {
        "required_n": len(required),
        "hits": hits,
        "recall": None if not required else hits / len(required),
    }


def baseline_rule_crisis_rows(cases: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for case in cases:
        labels = case.get("labels") or {}
        text = str(case["text"])
        decision = companion_safety_decision(text)
        out.append(
            {
                "id": case.get("id"),
                "text": text,
                "split": case.get("split"),
                "must_route_12356": bool(labels.get("must_route_12356")),
                "crisis_tier_label": labels.get("crisis_tier"),
                "rule_decision": decision,
                "rule_routes_12356": rule_crisis_routes_to_12356(text),
            }
        )
    return out


def score_crisis_negative_fp(
    cases: Sequence[Mapping[str, Any]],
    *,
    semantic_choice_for_each: str | None = "SELF_CRISIS",
) -> dict[str, Any]:
    negatives = [
        c
        for c in cases
        if str(c.get("id", "")).startswith("crisis-neg-")
        or (
            c.get("split") == "crisis"
            and (c.get("labels") or {}).get("crisis_tier") == "none"
            and not (c.get("labels") or {}).get("must_route_12356")
        )
    ]
    fps: list[str] = []
    for case in negatives:
        text = str(case["text"])
        routed = composite_crisis_routes_to_12356(
            text, semantic_choice=semantic_choice_for_each
        )
        if routed:
            fps.append(str(case.get("id")))
    return {
        "negative_n": len(negatives),
        "false_positives": len(fps),
        "false_positive_rate": None if not negatives else len(fps) / len(negatives),
        "false_positive_ids": fps,
        "forced_semantic_choice": semantic_choice_for_each,
    }


def rows_for_split(
    rows: Sequence[Mapping[str, Any]],
    *,
    split: str,
) -> list[Mapping[str, Any]]:
    return [r for r in rows if r.get("split") == split]
