#!/usr/bin/env python3
"""Offline eval: current turn classifiers vs Bailian decision-model-preview.

Default (no live calls):
  - Loads the synthetic fixture set.
  - Scores the deterministic crisis front door on explicit crisis-self fixtures.
  - Scores the known composite FP hole on crisis negatives under forced SELF_CRISIS.
  - Prints a blocked summary when keys / DECISION_MODEL_EVAL_LIVE are absent.

Live dual-run (opt-in):
  export DECISION_MODEL_EVAL_LIVE=1
  export DASHSCOPE_API_KEY=...
  export DECISION_MODEL_URL=https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone
  # Production baseline (preferred when set):
  export DEEPSEEK_API_KEY=...
  # Optional DashScope qwen-flash arm: DECISION_MODEL_EVAL_RUN_QWEN=1 (default on)
  # Auth: DECISION_MODEL_AUTH_STYLE=bearer|raw (default bearer; use raw if 401)
  # Floors: DECISION_MODEL_CONFIDENCE_FLOOR (live, default 0.5)
  #         DECISION_MODEL_CLOSE_CONFIDENCE_FLOOR (close, default 0.9)

Synthetic texts only. No device. No production behavior change.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.agent.evaluation.decision_model_scoring import (  # noqa: E402
    baseline_rule_crisis_rows,
    composite_crisis_routes_to_12356,
    latency_summary,
    rows_for_split,
    score_binary_labeled,
    score_crisis_negative_fp,
    score_must_12356_recall,
)
from services.agent.src.providers.decision_model import (  # noqa: E402
    CrisisChoice,
    DecisionModelClient,
    DecisionModelConfig,
    crisis_choice,
    ends_conversation,
    needs_live_lookup,
)

CASES_PATH = ROOT / "services/agent/evaluation/decision_model_offline_cases.json"


def _env_on(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() not in {"0", "false", "no", "off", ""}


def load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise SystemExit(f"invalid cases file: {path}")
    return cases


def _binary_by_split(
    rows: list[dict[str, Any]],
    *,
    label_key: str,
    pred_key: str,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "all": score_binary_labeled(rows, label_key=label_key, pred_key=pred_key).as_dict()
    }
    for split in ("dev", "heldout", "crisis"):
        subset = list(rows_for_split(rows, split=split))
        if not subset:
            continue
        out[split] = score_binary_labeled(
            subset, label_key=label_key, pred_key=pred_key
        ).as_dict()
    return out


def dry_run_report(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rule_rows = baseline_rule_crisis_rows(cases)
    explicit_rows = [r for r in rule_rows if str(r.get("id", "")).startswith("crisis-self-")]
    explicit_recall = score_must_12356_recall(explicit_rows, routed_key="rule_routes_12356")
    full_recall = score_must_12356_recall(rule_rows, routed_key="rule_routes_12356")
    forced_fp = score_crisis_negative_fp(cases, semantic_choice_for_each="SELF_CRISIS")
    rule_only_fp = score_crisis_negative_fp(cases, semantic_choice_for_each=None)
    return {
        "mode": "dry_run",
        "blocked_reason": (
            "DASHSCOPE_API_KEY and/or DECISION_MODEL_URL missing, or "
            "DECISION_MODEL_EVAL_LIVE not set. No live model numbers fabricated."
        ),
        "case_counts": {
            "total": len(cases),
            "dev": sum(1 for c in cases if c.get("split") == "dev"),
            "heldout": sum(1 for c in cases if c.get("split") == "heldout"),
            "crisis": sum(1 for c in cases if c.get("split") == "crisis"),
            "must_route_12356": full_recall["required_n"],
            "explicit_crisis_self_fixtures": explicit_recall["required_n"],
        },
        "rule_crisis_12356_recall_explicit_fixtures": explicit_recall,
        "rule_crisis_12356_recall_all_labeled": full_recall,
        "crisis_negative_fp_rules_alone": rule_only_fp,
        "crisis_negative_fp_forced_SELF_CRISIS_evidence": forced_fp,
        "limits": (
            "Explicit crisis-self recall 1.0 only proves the fixtures still match the "
            "rules they were copied from. It is not an independent safety proof. "
            "Forced SELF_CRISIS FP on quoted fiction is a known route_crisis hole."
        ),
        "compliance": (
            "Crisis detection keeps companion_safety_decision / route_crisis in front. "
            "Decision-model adapter is additive only and disabled by default."
        ),
        "live_metrics": None,
    }


async def _llm_pair(
    text: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    thinking_mode: str,
) -> dict[str, Any]:
    import httpx
    from services.agent.src.providers.close_intent_semantic_classifier import (
        CloseIntentSemanticClassifier,
        CloseIntentSemanticClassifierConfig,
        CloseIntentSemanticVerdict,
    )
    from services.agent.src.providers.live_lookup_semantic_classifier import (
        LiveLookupSemanticClassifier,
        LiveLookupSemanticClassifierConfig,
        LiveLookupSemanticVerdict,
    )
    from services.common.llm_thinking import ThinkingMode

    mode: ThinkingMode = "deepseek" if thinking_mode == "deepseek" else "dashscope"

    async with httpx.AsyncClient() as client:
        live = LiveLookupSemanticClassifier(
            LiveLookupSemanticClassifierConfig(
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout_s=10.0,
                thinking_mode=mode,
            ),
            client=client,
        )
        close = CloseIntentSemanticClassifier(
            CloseIntentSemanticClassifierConfig(
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout_s=10.0,
                thinking_mode=mode,
            ),
            client=client,
        )
        t0 = time.perf_counter()
        v_live, v_close = await asyncio.gather(
            live.classify(current_text=text),
            close.classify(current_text=text),
        )
        pair_ms = (time.perf_counter() - t0) * 1000.0
    return {
        "pred_live": v_live is LiveLookupSemanticVerdict.NEEDS_LIVE_LOOKUP,
        "pred_close": v_close is CloseIntentSemanticVerdict.END_SESSION,
        "raw_live": str(v_live.value),
        "raw_close": str(v_close.value),
        "pair_ms": pair_ms,
    }


async def live_run(cases: list[dict[str, Any]]) -> dict[str, Any]:
    api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    dm_url = os.environ.get("DECISION_MODEL_URL", "").strip()
    if not api_key or not dm_url:
        raise SystemExit(
            "live mode needs DASHSCOPE_API_KEY and DECISION_MODEL_URL "
            "(Beijing systemone endpoint with WorkspaceId)"
        )
    live_floor = float(os.environ.get("DECISION_MODEL_CONFIDENCE_FLOOR", "0.5"))
    close_floor = float(os.environ.get("DECISION_MODEL_CLOSE_CONFIDENCE_FLOOR", "0.9"))
    auth_style = os.environ.get("DECISION_MODEL_AUTH_STYLE", "bearer").strip().lower()
    qwen_base = os.environ.get(
        "DASHSCOPE_COMPATIBLE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ).strip()
    qwen_model = os.environ.get("LIVE_LOOKUP_SEMANTIC_MODEL", "qwen-flash").strip()
    run_qwen = _env_on("DECISION_MODEL_EVAL_RUN_QWEN", "1")
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    run_deepseek = bool(deepseek_key) and _env_on("DECISION_MODEL_EVAL_RUN_DEEPSEEK", "1")
    deepseek_base = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    deepseek_model = os.environ.get("DEEPSEEK_FAST_MODEL", "deepseek-flash").strip()

    dm = DecisionModelClient(
        DecisionModelConfig(
            api_key=api_key,
            systemone_url=dm_url,
            enabled=True,
            timeout_s=float(os.environ.get("DECISION_MODEL_TIMEOUT_S", "10")),
            confidence_floor=live_floor,
            auth_style=auth_style,
        )
    )
    dm_rows: list[dict[str, Any]] = []
    qwen_rows: list[dict[str, Any]] = []
    deepseek_rows: list[dict[str, Any]] = []
    try:
        for case in cases:
            labels = case.get("labels") or {}
            text = str(case["text"])
            split = case.get("split")
            verdict = await dm.classify_turn_bundle(current_text=text)
            pred_live = needs_live_lookup(verdict.live_lookup, confidence_floor=live_floor)
            pred_close = ends_conversation(
                verdict.close_intent, confidence_floor=close_floor
            )
            grade = crisis_choice(verdict.crisis_grade)
            dm_rows.append(
                {
                    "id": case.get("id"),
                    "split": split,
                    "label_live": labels.get("needs_live_lookup"),
                    "label_close": labels.get("ends_conversation"),
                    "must_route_12356": bool(labels.get("must_route_12356")),
                    "crisis_tier_label": labels.get("crisis_tier"),
                    "pred_live": pred_live,
                    "pred_close": pred_close,
                    "dm_crisis_choice": grade.value,
                    "dm_alone_self_crisis": grade is CrisisChoice.SELF_CRISIS,
                    "composite_routes_12356": composite_crisis_routes_to_12356(
                        text, semantic_choice=grade.value
                    ),
                    "pair_ms": verdict.latency_ms,
                    "server_ms": verdict.server_latency_ms,
                    "input_tokens": verdict.input_tokens,
                }
            )
            if run_qwen:
                q = await _llm_pair(
                    text,
                    api_key=api_key,
                    base_url=qwen_base,
                    model=qwen_model,
                    thinking_mode="dashscope",
                )
                qwen_rows.append(
                    {
                        "id": case.get("id"),
                        "split": split,
                        "label_live": labels.get("needs_live_lookup"),
                        "label_close": labels.get("ends_conversation"),
                        "pred_live": q["pred_live"],
                        "pred_close": q["pred_close"],
                        "pair_ms": q["pair_ms"],
                    }
                )
            if run_deepseek:
                d = await _llm_pair(
                    text,
                    api_key=deepseek_key,
                    base_url=deepseek_base,
                    model=deepseek_model,
                    thinking_mode="deepseek",
                )
                deepseek_rows.append(
                    {
                        "id": case.get("id"),
                        "split": split,
                        "label_live": labels.get("needs_live_lookup"),
                        "label_close": labels.get("ends_conversation"),
                        "pred_live": d["pred_live"],
                        "pred_close": d["pred_close"],
                        "pair_ms": d["pair_ms"],
                    }
                )
    finally:
        await dm.aclose()

    rule_rows = baseline_rule_crisis_rows(cases)
    # Same negative set as score_crisis_negative_fp: crisis-neg-* ids.
    neg_rows = [r for r in dm_rows if str(r.get("id", "")).startswith("crisis-neg-")]
    fp_n = sum(1 for r in neg_rows if r.get("composite_routes_12356"))
    composite_fp = {
        "negative_n": len(neg_rows),
        "false_positives": fp_n,
        "false_positive_rate": None if not neg_rows else fp_n / len(neg_rows),
    }

    report: dict[str, Any] = {
        "mode": "live",
        "confidence_floors": {"live_lookup": live_floor, "close_intent": close_floor},
        "auth_style": auth_style,
        "case_counts": {"total": len(cases)},
        "rule_crisis_12356_recall_explicit_fixtures": score_must_12356_recall(
            [r for r in rule_rows if str(r.get("id", "")).startswith("crisis-self-")],
            routed_key="rule_routes_12356",
        ),
        "crisis_negative_fp_forced_SELF_CRISIS_evidence": score_crisis_negative_fp(
            cases, semantic_choice_for_each="SELF_CRISIS"
        ),
        "decision_model": {
            "live_lookup": _binary_by_split(
                dm_rows, label_key="label_live", pred_key="pred_live"
            ),
            "close_intent": _binary_by_split(
                dm_rows, label_key="label_close", pred_key="pred_close"
            ),
            "crisis_12356_recall_composite": score_must_12356_recall(
                dm_rows, routed_key="composite_routes_12356"
            ),
            "crisis_negative_fp_composite": composite_fp,
            "latency": latency_summary(r["pair_ms"] for r in dm_rows),
        },
        "compliance": (
            "Report composite crisis recall and crisis-negative FP for product decisions. "
            "Model-alone crisis metrics must not gate production."
        ),
    }
    if qwen_rows:
        report["qwen_flash"] = {
            "live_lookup": _binary_by_split(
                qwen_rows, label_key="label_live", pred_key="pred_live"
            ),
            "close_intent": _binary_by_split(
                qwen_rows, label_key="label_close", pred_key="pred_close"
            ),
            "latency": latency_summary(r["pair_ms"] for r in qwen_rows),
        }
    if deepseek_rows:
        report["deepseek_flash"] = {
            "live_lookup": _binary_by_split(
                deepseek_rows, label_key="label_live", pred_key="pred_live"
            ),
            "close_intent": _binary_by_split(
                deepseek_rows, label_key="label_close", pred_key="pred_close"
            ),
            "latency": latency_summary(r["pair_ms"] for r in deepseek_rows),
        }
    elif not deepseek_key:
        report["deepseek_flash"] = {
            "skipped": True,
            "reason": "DEEPSEEK_API_KEY unset; production baseline not measured this run",
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if _env_on("DECISION_MODEL_EVAL_LIVE", "0"):
        report = asyncio.run(live_run(cases))
    else:
        report = dry_run_report(cases)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
