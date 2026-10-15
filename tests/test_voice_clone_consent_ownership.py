"""One module writes and reads voice-clone consent rows."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SQL = re.compile(r"\b(FROM|JOIN|INTO|UPDATE)\s+voice_clone_consents\b")
_OWNERS = {
    "services/voice_profile/clone_authority.py",
    "services/archive/restore_drill.py",
}


@pytest.mark.hermetic
def test_voice_clone_consent_sql_has_one_owner() -> None:
    hits: set[str] = set()
    for path in (_ROOT / "services").rglob("*.py"):
        if "tests" in path.parts:
            continue
        if _SQL.search(path.read_text(encoding="utf-8")):
            hits.add(path.relative_to(_ROOT).as_posix())
    assert hits == _OWNERS
