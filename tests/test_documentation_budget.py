from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
ALLOWED_DOCUMENTS = {"README.md", "PROJECT_RULES.md", "HANDOFF.md", "RESEARCH.md"}
DOCUMENT_SUFFIXES = {".md", ".markdown", ".mdown", ".rst", ".adoc", ".asciidoc"}


def test_repository_has_exactly_four_long_lived_documents() -> None:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    tracked = {
        item.decode("utf-8")
        for item in completed.stdout.split(b"\0")
        if item
    }
    documents = {
        path
        for path in tracked
        if Path(path).suffix.lower() in DOCUMENT_SUFFIXES
    }

    assert documents == ALLOWED_DOCUMENTS
    assert not any(path.startswith("docs/") for path in tracked)
