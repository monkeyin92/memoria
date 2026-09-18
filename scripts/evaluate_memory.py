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
from services.archive.memory_extractor import RuleBasedMemoryExtractor

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
    parser.add_argument(
        "--extractor",
        choices=("rules", "configured"),
        default="rules",
        help=(
            "rules: offline rule extractor (structural ceiling: it never emits "
            "a canonical episode key). configured: the production assembly -- "
            "Qwen with rule fallback -- which needs DASHSCOPE_API_KEY and "
            "OFFLINE_MOCK=false; it is the only path that can produce the "
            "cross-session canonical keys some cases expect."
        ),
    )
    return parser


def _build_adapter(kind: str) -> CatalogMemoryEvaluationAdapter:
    if kind == "rules":
        return CatalogMemoryEvaluationAdapter()
    # Production parity: the same assembly the Control API uses, so an offline
    # or unkeyed environment fails loudly instead of quietly scoring rules.
    from services.control_api.app.config import ControlSettings
    from services.control_api.app.memory_components import build_memory_extractor

    extractor = build_memory_extractor(ControlSettings())
    if isinstance(extractor, RuleBasedMemoryExtractor):
        raise SystemExit(
            "--extractor configured needs DASHSCOPE_API_KEY set and "
            "OFFLINE_MOCK=false; without them the production assembly is the "
            "rule extractor, which cannot produce canonical episode keys."
        )
    adapter = CatalogMemoryEvaluationAdapter(extractor)
    adapter.name = f"memoria-sqlite-{kind}"
    return adapter


async def _run(dataset_path: Path, extractor: str) -> str:
    dataset = load_memory_evaluation_dataset(dataset_path)
    report = await run_memory_evaluation(dataset, _build_adapter(extractor))
    return report_json(report)


def main() -> int:
    args = _parser().parse_args()
    payload = asyncio.run(_run(args.dataset, args.extractor))
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
