"""Run the fixed Chinese long-term-memory evaluation dataset."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from services.archive.memory_evaluation import (
    CatalogMemoryEvaluationAdapter,
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
    parser.add_argument("--output", type=Path)
    return parser


async def _run(dataset_path: Path) -> str:
    dataset = load_memory_evaluation_dataset(dataset_path)
    report = await run_memory_evaluation(dataset, CatalogMemoryEvaluationAdapter())
    return report_json(report)


def main() -> int:
    args = _parser().parse_args()
    payload = asyncio.run(_run(args.dataset))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
