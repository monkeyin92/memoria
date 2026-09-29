#!/usr/bin/env python3
"""Split the pytest files into shards of about equal duration for CI.

The python job used to run every test file in one process, and the ``Pytest``
step alone took 16 minutes. Each shard runs on its own runner with its own
PostgreSQL service (the suite's PostgreSQL tests share roles in one cluster and
cannot run in parallel against it), so shards never share state.

Files are assigned largest first to the least loaded shard, using the seconds
recorded in ``scripts/ci_test_durations.json``. A file missing from the table
gets the median weight, so a new test file is never dropped: it just runs in
whichever shard has room. Refresh the table from a serial run with::

    pytest --no-cov --durations=0 --durations-min=0 > durations.log
    python scripts/ci_test_shards.py --from-log durations.log --write

Usage: ``ci_test_shards.py --shards N --index I`` prints shard I's files
(1-based), one per line.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = Path(__file__).with_name("ci_test_durations.json")
#: pytest's ``testpaths``; scripts/tests run in their own CI steps.
TEST_ROOTS = ("services", "tests")
_PATTERNS = ("test_*.py", "*_test.py")
_DURATION_LINE = re.compile(r"^\s*(\d+(?:\.\d+)?)s (?:setup|call|teardown)\s+(\S+?)::")


def discover(root: Path = ROOT) -> list[str]:
    files: set[str] = set()
    for base in TEST_ROOTS:
        for pattern in _PATTERNS:
            files.update(str(path.relative_to(root)) for path in (root / base).rglob(pattern))
    return sorted(files)


def load_table(path: Path = TABLE) -> dict[str, float]:
    if not path.exists():
        return {}
    return {str(k): float(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}


def weights(files: list[str], table: dict[str, float]) -> dict[str, float]:
    known = [table[name] for name in files if name in table]
    default = statistics.median(known) if known else 1.0
    return {name: table.get(name, default) for name in files}


def assign(files: list[str], table: dict[str, float], shards: int) -> list[list[str]]:
    """Greedy longest-processing-time packing; deterministic for equal inputs."""

    weight = weights(files, table)
    loads = [0.0] * shards
    result: list[list[str]] = [[] for _ in range(shards)]
    for name in sorted(files, key=lambda item: (-weight[item], item)):
        target = min(range(shards), key=lambda index: (loads[index], index))
        result[target].append(name)
        loads[target] += weight[name]
    return [sorted(group) for group in result]


def table_from_log(log: str) -> dict[str, float]:
    """Sum the per-test setup/call/teardown seconds of a ``--durations`` log by file."""

    totals: dict[str, float] = {}
    for line in log.splitlines():
        match = _DURATION_LINE.match(line)
        if match:
            name = match.group(2)
            totals[name] = totals.get(name, 0.0) + float(match.group(1))
    return {name: round(seconds, 1) for name, seconds in sorted(totals.items())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shards", type=int, default=4)
    parser.add_argument("--index", type=int, help="1-based shard to print")
    parser.add_argument("--from-log", type=Path, help="pytest --durations log to summarise")
    parser.add_argument("--write", action="store_true", help="with --from-log, replace the table")
    args = parser.parse_args(argv)
    if args.from_log is not None:
        table = table_from_log(args.from_log.read_text(encoding="utf-8"))
        if not args.write:
            json.dump(table, sys.stdout, indent=1)
            return 0
        TABLE.write_text(json.dumps(table, indent=1) + "\n", encoding="utf-8")
        return 0
    if args.index is None or not 1 <= args.index <= args.shards:
        parser.error("--index must be between 1 and --shards")
    groups = assign(discover(), load_table(), args.shards)
    print("\n".join(groups[args.index - 1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
