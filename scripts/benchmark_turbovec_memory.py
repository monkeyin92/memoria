"""Benchmark TurboVec against exact float32 search and apply the hard enablement gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from services.archive.turbovec_experiment import (
    TurboVecGateThresholds,
    VectorDocument,
    VectorQuery,
    benchmark_turbovec,
    decide_turbovec_gate,
    decision_json,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vectors", type=Path, required=True)
    parser.add_argument("--queries", type=Path, required=True)
    parser.add_argument("--item-ids", type=Path, required=True)
    parser.add_argument("--relevance", type=Path, required=True)
    parser.add_argument("--bit-width", type=int, choices=(2, 3, 4), default=4)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    vectors = np.load(args.vectors)
    queries = np.load(args.queries)
    item_ids = json.loads(args.item_ids.read_text(encoding="utf-8"))
    relevance = json.loads(args.relevance.read_text(encoding="utf-8"))
    if (
        not isinstance(item_ids, list)
        or not isinstance(relevance, list)
        or len(item_ids) != len(vectors)
        or len(relevance) != len(queries)
    ):
        raise ValueError("vector ids/relevance must align with their numpy arrays")
    documents = tuple(
        VectorDocument(
            item_id=str(item_id),
            vector=tuple(float(value) for value in vector),
        )
        for item_id, vector in zip(item_ids, vectors, strict=True)
    )
    query_items = tuple(
        VectorQuery(
            query_id=f"query-{index}",
            vector=tuple(float(value) for value in vector),
            relevant_item_ids=tuple(str(value) for value in relevant_ids),
        )
        for index, (vector, relevant_ids) in enumerate(
            zip(queries, relevance, strict=True)
        )
    )
    benchmark = benchmark_turbovec(
        documents=documents,
        queries=query_items,
        bit_width=args.bit_width,
        repeats=args.repeats,
    )
    decision = decide_turbovec_gate(
        benchmark,
        TurboVecGateThresholds(),
    )
    payload = decision_json(decision)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if decision.enabled else 2


if __name__ == "__main__":
    raise SystemExit(main())
