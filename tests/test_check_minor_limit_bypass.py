"""The bypass check tells an operator yes or no and never prints the subject id."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_minor_limit_bypass.py"
SUBJECT = "subject-test-only"
OTHER = "subject-someone-else"


def _run(subject: str, whitelist: str | None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("MINOR_LIMIT_BYPASS_TEST_SUBJECTS", None)
    if whitelist is not None:
        env["MINOR_LIMIT_BYPASS_TEST_SUBJECTS"] = whitelist
    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=subject,
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


def _hash8(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:8]


def test_a_matching_subject_is_a_bypass_and_the_id_stays_off_the_page() -> None:
    result = _run(SUBJECT, SUBJECT)
    assert result.returncode == 0
    assert result.stdout == (
        f"bypass=yes\nenabled=yes\nsubject_sha256_8={_hash8(SUBJECT)}\n"
    )
    assert SUBJECT not in result.stdout
    assert SUBJECT not in result.stderr


def test_a_different_subject_is_not_a_bypass() -> None:
    result = _run(OTHER, SUBJECT)
    assert result.returncode == 0
    assert result.stdout.startswith("bypass=no\nenabled=yes\n")
    assert OTHER not in result.stdout + result.stderr
    assert SUBJECT not in result.stdout + result.stderr


def test_an_empty_whitelist_is_off() -> None:
    result = _run(SUBJECT, "")
    assert result.returncode == 0
    assert result.stdout.startswith("bypass=no\nenabled=no\n")


def test_two_ids_disable_the_whitelist() -> None:
    result = _run(SUBJECT, f"{SUBJECT},{OTHER}")
    assert result.returncode == 0
    assert result.stdout.startswith("bypass=no\nenabled=no\n")
    assert SUBJECT not in result.stdout + result.stderr


def test_blank_stdin_is_refused() -> None:
    result = _run("  ", None)
    assert result.returncode == 2
    assert result.stdout == ""
