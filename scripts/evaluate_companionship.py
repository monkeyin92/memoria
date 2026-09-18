"""Run the fixed companionship scenario set against the offline Control API."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from services.companionship.evaluation import (
    OfflineControlAdapter,
    load_companionship_dataset,
    report_json,
    run_companionship_evaluation,
)

DEFAULT_DATASET = (
    Path(__file__).parents[1]
    / "services"
    / "companionship"
    / "evaluation"
    / "companionship_eval_zh_v1.json"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path)
    return parser


async def _run(dataset_path: Path) -> str:
    dataset = load_companionship_dataset(dataset_path)
    report = await run_companionship_evaluation(dataset, OfflineControlAdapter())
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
