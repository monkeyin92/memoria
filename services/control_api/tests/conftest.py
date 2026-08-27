"""Hermetic environment for control API tests.

``ControlSettings`` declares ``env_file=".env"``, which pydantic-settings
resolves against the current working directory.  Running pytest from the
repository root would otherwise let the developer's local ``.env`` inject
unmanaged settings (for example a speaker-embedding endpoint) and flip
readiness/session tests that CI passes without such a file.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _hermetic_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
