"""The CI test shards cover every test file exactly once and stay balanced."""

from __future__ import annotations

import re
from pathlib import Path

from scripts import ci_test_shards as shards

ROOT = Path(__file__).resolve().parents[2]
SHARDS = 4


def test_every_test_file_lands_in_exactly_one_shard() -> None:
    files = shards.discover()
    assert files, "no test files discovered"
    groups = shards.assign(files, shards.load_table(), SHARDS)
    flat = [name for group in groups for name in group]
    assert sorted(flat) == files
    assert len(flat) == len(set(flat))


def test_a_file_missing_from_the_table_is_still_assigned() -> None:
    table = {"a/test_a.py": 30.0, "b/test_b.py": 10.0}
    groups = shards.assign(["a/test_a.py", "b/test_b.py", "c/test_new.py"], table, 2)
    assert sorted(name for group in groups for name in group) == [
        "a/test_a.py",
        "b/test_b.py",
        "c/test_new.py",
    ]


def test_assignment_is_deterministic_and_balanced() -> None:
    files = shards.discover()
    table = shards.load_table()
    first = shards.assign(files, table, SHARDS)
    assert first == shards.assign(list(reversed(files)), table, SHARDS)
    weight = shards.weights(files, table)
    loads = [sum(weight[name] for name in group) for group in first]
    assert max(loads) <= 1.25 * (sum(loads) / SHARDS) + max(weight.values())


def test_durations_table_names_only_existing_files() -> None:
    stale = [name for name in shards.load_table() if not (ROOT / name).exists()]
    assert stale == []


def test_durations_log_is_summed_by_file() -> None:
    log = "\n".join(
        [
            "1.50s call     services/a/tests/test_a.py::test_one",
            "0.25s setup    services/a/tests/test_a.py::test_one",
            "2.00s call     services/b/tests/test_b.py::test_x[param]",
            "not a duration line",
        ]
    )
    assert shards.table_from_log(log) == {
        "services/a/tests/test_a.py": 1.8,
        "services/b/tests/test_b.py": 2.0,
    }


def test_ci_workflow_runs_every_shard_the_script_splits_into() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    matrix = re.search(r"shard: \[([0-9, ]+)\]", workflow)
    assert matrix is not None
    assert [int(item) for item in matrix.group(1).split(",")] == list(range(1, SHARDS + 1))
    assert f"--shards {SHARDS}" in workflow
