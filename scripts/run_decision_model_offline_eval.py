#!/usr/bin/env python3
"""Offline eval: current turn classifiers vs Bailian decision-model-preview.

Default (no live calls):
  - Loads the synthetic fixture set.
  - Scores the deterministic crisis front door (must hit 100% on 12356 cases).
  - Prints a blocked summary when DASHSCOPE_API_KEY / DECISION_MODEL_URL are absent.

Live dual-run (opt-in):
  export DECISION_MODEL_EVAL_LIVE=1
  export DASHSCOPE_API_KEY=...
  export DECISION_MODEL_URL=https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1/systemone
  # optional: DEEPSEEK_API_KEY to also run production close/live classifiers

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
    score_binary_labeled,
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


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def load_cases(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases")
    if not isinstance(cases, list):
        raise SystemExit(f"invalid cases file: {path}")
    return cases


def dry_run_report(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rule_rows = baseline_rule_crisis_rows(cases)
    # Explicit crisis-self fixtures must hit 12356 with rules alone.
    explicit_rows = [r for r in rule_rows if str(r.get("id", "")).startswith("crisis-self-")]
    explicit_recall = score_must_12356_recall(explicit_rows, routed_key="rule_routes_12356")
    full_recall = score_must_12356_recall(rule_rows, routed_key="rule_routes_12356")
    live_n = sum(1 for c in cases if (c.get("labels") or {}).get("needs_live_lookup") is not None)
    close_n = sum(1 for c in cases if (c.get("labels") or {}).get("ends_conversation") is not None)
    crisis_n = sum(1 for c in cases if (c.get("labels") or {}).get("crisis_tier") is not None)
    return {
        "mode": "dry_run",
        "blocked_reason": (
            "DASHSCOPE_API_KEY and/or DECISION_MODEL_URL missing, or "
            "DECISION_MODEL_EVAL_LIVE not set. No live model numbers fabricated."
        ),
        "case_counts": {
            "total": len(cases),
            "with_live_label": live_n,
            "with_close_label": close_n,
            "with_crisis_label": crisis_n,
            "must_route_12356": full_recall["required_n"],
            "explicit_crisis_self_fixtures": explicit_recall["required_n"],
        },
        "rule_crisis_12356_recall_explicit_fixtures": explicit_recall,
        "rule_crisis_12356_recall_all_labeled": full_recall,
        "note": (
            "All-labeled rule recall may be <1.0 when semantic-candidate utterances "
            "(crisis-amb-*) need additive evidence under route_crisis. Explicit "
            "crisis-self-* fixtures must stay at recall 1.0."
        ),
        "compliance": (
            "Crisis detection keeps companion_safety_decision / route_crisis in front. "
            "Decision-model adapter is additive only and disabled by default."
        ),
        "live_metrics": None,
    }


async def _qwen_pair(text: str, *, api_key: str, base_url: str, model: str) -> dict[str, Any]:
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

    async with httpx.AsyncClient() as client:
        live = LiveLookupSemanticClassifier(
            LiveLookupSemanticClassifierConfig(
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout_s=10.0,
            ),
            client=client,
        )
        close = CloseIntentSemanticClassifier(
            CloseIntentSemanticClassifierConfig(
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout_s=10.0,
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
    confidence_floor = float(os.environ.get("DECISION_MODEL_CONFIDENCE_FLOOR", "0.5"))
    qwen_base = os.environ.get(
        "DASHSCOPE_COMPATIBLE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ).strip()
    qwen_model = os.environ.get("LIVE_LOOKUP_SEMANTIC_MODEL", "qwen-flash").strip()
    run_qwen = os.environ.get("DECISION_MODEL_EVAL_RUN_QWEN", "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }

    dm = DecisionModelClient(
        DecisionModelConfig(
            api_key=api_key,
            systemone_url=dm_url,
            enabled=True,
            timeout_s=float(os.environ.get("DECISION_MODEL_TIMEOUT_S", "10")),
            confidence_floor=confidence_floor,
        )
    )
    dm_rows: list[dict[str, Any]] = []
    qwen_rows: list[dict[str, Any]] = []
    try:
        for case in cases:
            labels = case.get("labels") or {}
            text = str(case["text"])
            verdict = await dm.classify_turn_bundle(current_text=text)
            pred_live = needs_live_lookup(
                verdict.live_lookup, confidence_floor=confidence_floor
            )
            pred_close = ends_conversation(
                verdict.close_intent, confidence_floor=confidence_floor
            )
            grade = crisis_choice(verdict.crisis_grade)
            dm_only_self = grade is CrisisChoice.SELF_CRISIS
            composite_12356 = composite_crisis_routes_to_12356(
                text, semantic_choice=grade.value
            )
            dm_rows.append(
                {
                    "id": case.get("id"),
                    "label_live": labels.get("needs_live_lookup"),
                    "label_close": labels.get("ends_conversation"),
                    "must_route_12356": bool(labels.get("must_route_12356")),
                    "pred_live": pred_live,
                    "pred_close": pred_close,
                    "dm_crisis_choice": grade.value,
                    "dm_alone_self_crisis": dm_only_self,
                    "composite_routes_12356": composite_12356,
                    "pair_ms": verdict.latency_ms,
                    "server_ms": verdict.server_latency_ms,
                    "input_tokens": verdict.input_tokens,
                    "live_confidence": verdict.live_lookup.confidence,
                    "close_confidence": verdict.close_intent.confidence,
                    "crisis_confidence": verdict.crisis_grade.confidence,
                }
            )
            if run_qwen:
                q = await _qwen_pair(
                    text, api_key=api_key, base_url=qwen_base, model=qwen_model
                )
                qwen_rows.append(
                    {
                        "id": case.get("id"),
                        "label_live": labels.get("needs_live_lookup"),
                        "label_close": labels.get("ends_conversation"),
                        "pred_live": q["pred_live"],
                        "pred_close": q["pred_close"],
                        "pair_ms": q["pair_ms"],
                    }
                )
    finally:
        await dm.aclose()

    rule_rows = baseline_rule_crisis_rows(cases)
    report: dict[str, Any] = {
        "mode": "live",
        "confidence_floor": confidence_floor,
        "case_counts": {"total": len(cases)},
        "rule_crisis_12356_recall": score_must_12356_recall(
            rule_rows, routed_key="rule_routes_12356"
        ),
        "decision_model": {
            "live_lookup": score_binary_labeled(
                dm_rows, label_key="label_live", pred_key="pred_live"
            ).as_dict(),
            "close_intent": score_binary_labeled(
                dm_rows, label_key="label_close", pred_key="pred_close"
            ).as_dict(),
            "crisis_12356_recall_composite": score_must_12356_recall(
                dm_rows, routed_key="composite_routes_12356"
            ),
            "crisis_12356_recall_model_alone_NOT_FOR_PROD": score_must_12356_recall(
                [
                    {
                        "must_route_12356": r["must_route_12356"],
                        "dm_alone": r["dm_alone_self_crisis"],
                    }
                    for r in dm_rows
                ],
                routed_key="dm_alone",
            ),
            "latency": latency_summary(r["pair_ms"] for r in dm_rows),
            "mean_input_tokens": (
                sum(r["input_tokens"] or 0 for r in dm_rows)
                / max(1, sum(1 for r in dm_rows if r["input_tokens"] is not None))
            ),
        },
        "compliance": (
            "Report composite crisis recall for product decisions. "
            "Model-alone crisis recall is diagnostic only and must not gate production."
        ),
    }
    if qwen_rows:
        report["qwen_flash"] = {
            "live_lookup": score_binary_labeled(
                qwen_rows, label_key="label_live", pred_key="pred_live"
            ).as_dict(),
            "close_intent": score_binary_labeled(
                qwen_rows, label_key="label_close", pred_key="pred_close"
            ).as_dict(),
            "latency": latency_summary(r["pair_ms"] for r in qwen_rows),
        }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cases",
        type=Path,
        default=CASES_PATH,
        help="path to decision_model_offline_cases.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="optional JSON report path",
    )
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if _env_truthy("DECISION_MODEL_EVAL_LIVE"):
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
