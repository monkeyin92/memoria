"""Isolation restore + deletion-ledger replay drill (A15 lab proof)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(ROOT)),
        "PYTHONPATH": str(ROOT),
    }
    return subprocess.run(
        [sys.executable, "-m", "scripts.run_isolation_restore_deletion_drill", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_isolation_restore_deletion_drill_passes_and_writes_evidence(tmp_path: Path) -> None:
    report_path = tmp_path / "report.json"
    work_dir = tmp_path / "lab"
    result = _run(
        "--work-dir",
        str(work_dir),
        "--report",
        str(report_path),
        "--allow-missing-inventory",
    )
    assert result.returncode == 0, result.stderr + result.stdout
    summary = json.loads(result.stdout.strip().splitlines()[-1])
    assert summary["passed"] is True
    assert summary["report"] == str(report_path)
    assert isinstance(summary["rto_seconds"], (int, float))

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["passed"] is True
    assert report["drill"] == "a15-isolation-restore-deletion-ledger-replay"
    assert report["child_audio"] == "none"
    assert report["holds"] == ["#514", "#448"]
    assert report["ledger"]["replay"] == {"replayed": 1, "incomplete": 0}
    assert report["assertions"]["deleted_child_recall_empty"] is True
    assert report["assertions"]["deleted_parent_ui_empty"] is True
    assert report["assertions"]["keep_subject_still_visible"] is True
    assert report["surfaces_after_replay"]["child_recall"] == {
        "owner_events": [],
        "subject_events": [],
        "vectors": 0,
    }
    assert report["surfaces_after_replay"]["keep_subject"]["owner_events"] == ["keep-e1"]
    assert any(gap["status"] == "未验" for gap in report["a14_gaps"])
    assert report_path.stat().st_mode & 0o777 == 0o600
    assert "phase_log" in report
    assert [entry["phase"] for entry in report["phase_log"]][-1] == "write_report"


def test_missing_inventory_without_allow_flag_exits_2(tmp_path: Path) -> None:
    result = _run(
        "--work-dir",
        str(tmp_path / "lab"),
        "--report",
        str(tmp_path / "report.json"),
        "--inventory",
        str(tmp_path / "missing-inventory.json"),
    )
    assert result.returncode == 2
    assert "inventory_missing" in result.stderr


def test_inventory_roles_are_cited_when_present(tmp_path: Path) -> None:
    inventory = tmp_path / "inventory.json"
    inventory.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "evidence_class": "lab_fixture",
                "stack": {"current_tag": "lab", "current_commit": "0" * 40},
                "components": [
                    {
                        "role": "control-api",
                        "image_ref": "memoria-control-api:lab",
                        "image_revision": "0" * 40,
                        "stack_commit": "0" * 40,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    report_path = tmp_path / "report.json"
    result = _run(
        "--work-dir",
        str(tmp_path / "lab"),
        "--report",
        str(report_path),
        "--inventory",
        str(inventory),
    )
    assert result.returncode == 0, result.stderr + result.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["inventory"]["present"] is True
    assert report["inventory"]["components"][0]["role"] == "control-api"
    assert report["inventory"]["sha256"]
