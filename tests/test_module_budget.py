from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check_module_budget.py"


def _workspace(tmp_path: Path, *, budget: int, actual: int) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    module = root / "services" / "example.py"
    module.parent.mkdir(parents=True)
    module.write_text("".join(f"line {index}\n" for index in range(actual)), encoding="utf-8")
    status = root / "architecture-status.yaml"
    status.write_text(
        "# test architecture status\n"
        "student_track: p0_in_progress\n"
        "module_budgets:\n"
        f"  services/example.py: {budget}  # downward only\n"
        "gates:\n"
        "  ci: pending\n",
        encoding="utf-8",
    )
    return root, status


def _run(root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), mode, "--root", str(root)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_check_accepts_an_exact_budget_without_modifying_status(tmp_path: Path) -> None:
    root, status = _workspace(tmp_path, budget=3, actual=3)
    before = status.read_bytes()

    completed = _run(root, "check")

    assert completed.returncode == 0, completed.stderr
    assert "module_budget_check=PASS count=1" in completed.stdout
    assert "services/example.py lines=3 budget=3" in completed.stdout
    assert status.read_bytes() == before


def test_check_rejects_growth_without_modifying_status(tmp_path: Path) -> None:
    root, status = _workspace(tmp_path, budget=2, actual=3)
    before = status.read_bytes()

    completed = _run(root, "check")

    assert completed.returncode == 1
    assert "3 lines exceeds budget 2" in completed.stderr
    assert status.read_bytes() == before


def test_check_requires_a_reduction_to_tighten_the_recorded_budget(tmp_path: Path) -> None:
    root, status = _workspace(tmp_path, budget=4, actual=2)
    before = status.read_bytes()

    completed = _run(root, "check")

    assert completed.returncode == 1
    assert "2 lines is below budget 4" in completed.stderr
    assert "check_module_budget.py update" in completed.stderr
    assert status.read_bytes() == before


def test_update_only_tightens_the_budget_and_preserves_yaml_context(tmp_path: Path) -> None:
    root, status = _workspace(tmp_path, budget=4, actual=2)

    updated = _run(root, "update")
    checked = _run(root, "check")

    assert updated.returncode == 0, updated.stderr
    assert checked.returncode == 0, checked.stderr
    contents = status.read_text(encoding="utf-8")
    assert "services/example.py: 2  # downward only" in contents
    assert "gates:\n  ci: pending\n" in contents


def test_update_refuses_to_turn_growth_into_the_new_baseline(tmp_path: Path) -> None:
    root, status = _workspace(tmp_path, budget=2, actual=3)
    before = status.read_bytes()

    completed = _run(root, "update")

    assert completed.returncode == 1
    assert "update mode never raises a budget" in completed.stderr
    assert status.read_bytes() == before


def test_check_rejects_a_budget_path_outside_the_repository(tmp_path: Path) -> None:
    root, status = _workspace(tmp_path, budget=1, actual=1)
    outside = tmp_path / "outside.py"
    outside.write_text("line\n", encoding="utf-8")
    status.write_text(
        "module_budgets:\n  ../outside.py: 1\n",
        encoding="utf-8",
    )

    completed = _run(root, "check")

    assert completed.returncode == 1
    assert "must stay inside the repository" in completed.stderr
