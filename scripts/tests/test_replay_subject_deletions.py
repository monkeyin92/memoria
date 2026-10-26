"""The post-restore deletion replay runs through the fully wired Control API."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(tmp_path: Path, *args: str, database_url: str = "") -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "MEMORIA_DB_PATH": str(tmp_path / "memoria.sqlite3"),
        "OFFLINE_MOCK": "true",
        "MEMORIA_AUTH_SECRET": "test-auth-material-that-is-long-enough",
        "MEMORIA_ARCHIVE_INTERNAL_TOKEN": "test-internal-archive-token",
        "MEMORIA_GUARDIAN_DATABASE_URL": database_url,
        "MEMORIA_ARCHIVE_DATABASE_URL": database_url,
    }
    return subprocess.run(
        [sys.executable, "-m", "scripts.replay_subject_deletions", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_replay_requires_explicit_confirmation(tmp_path: Path) -> None:
    result = _run(tmp_path)
    assert result.returncode == 2
    assert "--confirm-replay is required" in result.stderr


def test_replay_reports_counts_only(tmp_path: Path, dev_database_url: str) -> None:
    result = _run(tmp_path, "--confirm-replay", database_url=dev_database_url)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "account_incomplete": 0,
        "account_replayed": 0,
        "incomplete": 0,
        "replayed": 0,
    }


def test_export_ledger_writes_a_root_only_snapshot_and_prints_counts(
    tmp_path: Path, dev_database_url: str
) -> None:
    snapshot = tmp_path / "ledger.json"
    result = _run(tmp_path, "--export-ledger", str(snapshot), database_url=dev_database_url)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"exported": 0}
    assert snapshot.stat().st_mode & 0o777 == 0o600
    assert json.loads(snapshot.read_text(encoding="utf-8")) == {
        "account_rows": [],
        "rows": [],
        "version": 1,
    }


def test_ledger_snapshot_requires_confirmation(tmp_path: Path) -> None:
    result = _run(tmp_path, "--ledger-snapshot", str(tmp_path / "ledger.json"))
    assert result.returncode == 2
    assert "--confirm-replay is required" in result.stderr


def test_export_cannot_be_combined_with_replay(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "--export-ledger",
        str(tmp_path / "ledger.json"),
        "--confirm-replay",
    )
    assert result.returncode == 2
    assert "cannot be combined" in result.stderr
