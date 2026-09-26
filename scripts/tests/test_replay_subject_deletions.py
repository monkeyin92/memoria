"""The post-restore deletion replay runs through the fully wired Control API."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", str(tmp_path)),
        "MEMORIA_DB_PATH": str(tmp_path / "memoria.sqlite3"),
        "OFFLINE_MOCK": "true",
        "MEMORIA_AUTH_SECRET": "test-auth-material-that-is-long-enough",
        "MEMORIA_ARCHIVE_INTERNAL_TOKEN": "test-internal-archive-token",
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


def test_replay_reports_counts_only(tmp_path: Path) -> None:
    result = _run(tmp_path, "--confirm-replay")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"incomplete": 0, "replayed": 0}
