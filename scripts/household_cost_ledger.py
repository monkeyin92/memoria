#!/usr/bin/env python3
"""Reconcile a per-household cost ledger sample against vendor bills.

Reads a JSON sample (see packages/contracts/schemas/cost-ledger/), prints a
JSON report with intensity distributions and 未验 gaps. Never invents prices.

  python3 scripts/household_cost_ledger.py tests/fixtures/cost_ledger/sample-matched.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.cost_ledger.parse import LedgerParseError, parse_ledger_sample
from scripts.cost_ledger.reconcile import reconcile_sample
from scripts.cost_ledger.report import report_to_dict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "sample",
        type=Path,
        help="path to a household cost ledger JSON sample",
    )
    parser.add_argument(
        "--fail-on-gaps",
        action="store_true",
        help="exit 2 when the report contains any gap (including honest 未验)",
    )
    args = parser.parse_args(argv)

    try:
        payload = json.loads(args.sample.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"household_cost_ledger_error={exc}", file=sys.stderr)
        return 1

    try:
        sample = parse_ledger_sample(payload)
    except LedgerParseError as exc:
        print(f"household_cost_ledger_parse_error={exc}", file=sys.stderr)
        return 1

    report = reconcile_sample(sample)
    if report.invented_prices:
        print("household_cost_ledger_error=invented_prices", file=sys.stderr)
        return 1

    print(json.dumps(report_to_dict(report), ensure_ascii=False, indent=2))
    gap_count = len(report.gaps)
    print(
        f"household_cost_ledger=OK invented_prices=false gaps={gap_count} "
        f"households={len(report.households)}",
        file=sys.stderr,
    )
    if args.fail_on_gaps and gap_count:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
