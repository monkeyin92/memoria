"""Paired comparison of two distractor-evaluation receipts (second minus first).

    uv run python scripts/compare_memory_distractor_receipts.py first.json second.json

Prints, per corpus size and query group, the paired MRR / top-1 difference with a
bootstrap 95% interval over queries and the number of queries each side wins.  Both
receipts must come from the same dataset file.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.archive.memory_distractor_evaluation import compare_receipts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("first", type=Path)
    parser.add_argument("second", type=Path)
    parser.add_argument("--iterations", type=int, default=2000)
    args = parser.parse_args()
    first = json.loads(args.first.read_text(encoding="utf-8"))
    second = json.loads(args.second.read_text(encoding="utf-8"))
    print(f"first : {first['adapter']}\nsecond: {second['adapter']}")
    print(
        f"{'background':>10s} {'group':>10s} {'n':>4s} {'MRR 1st':>8s} {'MRR 2nd':>8s} {'diff [95% CI]':>24s} {'top1 diff [95% CI]':>26s} {'2nd/1st better':>15s}"
    )
    for row in compare_receipts(first, second, iterations=args.iterations):
        mrr_low, mrr_high = row["mrr_diff_ci95"]
        top_low, top_high = row["top1_diff_ci95"]
        print(
            f"{row['background']:>10d} {row['group']:>10s} {row['n']:>4d} "
            f"{row['mrr_first']:>8.3f} {row['mrr_second']:>8.3f} "
            f"{row['mrr_diff']:>+8.3f} [{mrr_low:+.3f},{mrr_high:+.3f}] "
            f"{row['top1_diff']:>+8.3f} [{top_low:+.3f},{top_high:+.3f}] "
            f"{row['second_better']:>7d}/{row['first_better']:<7d}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
