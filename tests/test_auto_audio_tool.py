"""The audio-acceptance tool's own self-checks, run as one pytest entry.

The tool lives in `scripts/auto_audio/`: `auto_audio_session.py` drives a bounded
speaker-to-microphone acceptance run (self-test, wake, one question per turn, cleanup) and
`auto_audio_analyze.py` judges the recorded take offline.  Each self-check builds its own
fixtures and asserts the positive wording *and* the fail-closed wording of that judgment, so
a change to the tool cannot land unverified - which it could not be while the tool lived under
the ignored `outputs/` tree.

The two `main()`-style suites are run through their own entry point; the third is a unittest
module with no `main()`.  No microphone, speaker, serial port, device, network or system
setting is touched: the fixtures synthesize their own WAV files and log lines, and the
volume-restore suite mocks `osascript` instead of changing a system value.
"""

from __future__ import annotations

import unittest
from collections.abc import Callable
from typing import Any

from scripts.auto_audio import (
    selfcheck_analyzer,
    selfcheck_session_evidence,
    selfcheck_volume_restore,
)


def _run_script_suite(module: Any) -> tuple[bool, str]:
    entry: Callable[[], int] = module.main
    return entry() == 0, str(module.__name__)


def _run_unittest_suite(module: Any) -> tuple[bool, str]:
    suite = unittest.defaultTestLoader.loadTestsFromModule(module)
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    return result.wasSuccessful(), str(module.__name__)


def test_audio_acceptance_tool_self_checks_pass() -> None:
    outcomes = [
        _run_script_suite(selfcheck_analyzer),
        _run_script_suite(selfcheck_session_evidence),
        _run_unittest_suite(selfcheck_volume_restore),
    ]
    failed = [name for passed, name in outcomes if not passed]
    assert failed == [], f"audio-acceptance self-checks failed: {failed}"
