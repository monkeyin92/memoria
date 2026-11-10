"""Tracked files must not be hidden by ignore rules.

An ignored tracked file still commits and still receives changes, so the only
visible symptom is that new siblings disappear from `git status`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_no_tracked_file_is_ignored() -> None:
    result = subprocess.run(
        ["git", "ls-files", "-ci", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""
