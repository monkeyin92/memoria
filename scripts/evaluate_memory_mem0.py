"""Run the fixed synthetic memory benchmark through an isolated Mem0 shadow store."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from services.archive.mem0_shadow import (
    Mem0ShadowEvaluationAdapter,
    Mem0ShadowUnavailableError,
    ShadowEvaluationStore,
)
from services.archive.memory_evaluation import (
    load_memory_evaluation_dataset,
    report_json,
    run_memory_evaluation,
)

DEFAULT_DATASET = (
    Path(__file__).parents[1]
    / "services"
    / "archive"
    / "evaluation"
    / "memory_eval_zh_v1.json"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument(
        "--store",
        type=Path,
        default=Path("data/memory-shadow-evaluations.sqlite3"),
    )
    parser.add_argument("--output", type=Path)
    return parser


async def _run(args: argparse.Namespace) -> str:
    config_raw = os.getenv("MEMORIA_MEM0_SHADOW_CONFIG_JSON", "").strip()
    if not config_raw:
        raise Mem0ShadowUnavailableError(
            "set MEMORIA_MEM0_SHADOW_CONFIG_JSON to an isolated Mem0 shadow config"
        )
    config = json.loads(config_raw)
    if not isinstance(config, dict):
        raise ValueError("MEMORIA_MEM0_SHADOW_CONFIG_JSON must be a JSON object")
    dataset = load_memory_evaluation_dataset(args.dataset)
    adapter = Mem0ShadowEvaluationAdapter(
        config=config,
        experiment_id=args.experiment_id,
    )
    report = await run_memory_evaluation(dataset, adapter)
    ShadowEvaluationStore(args.store).record(
        experiment_id=args.experiment_id,
        report=report,
    )
    return report_json(report)


def main() -> int:
    args = _parser().parse_args()
    try:
        payload = asyncio.run(_run(args))
    except Mem0ShadowUnavailableError as exc:
        print(json.dumps({"enabled": False, "reason": str(exc)}, ensure_ascii=False))
        return 2
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
