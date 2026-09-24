from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
ALLOWED_DOCUMENTS = {
    "README.md",
    "HANDOFF.md",
    "TODOLIST.md",
}
# Permanent references and evidence that HANDOFF/TODOLIST link to live under
# these docs/ subtrees only; anything else is documentation sprawl.
ALLOWED_DOCS_PREFIXES = (
    "docs/acceptance/",
    "docs/compliance/",
    "docs/runbooks/",
    "docs/strategy/",
)
ALLOWED_DOCS_FILES = {
    "docs/HANDOFF-archive-before-0920.md",
}
# Evaluation receipts are data, kept next to the docs that cite them.
ALLOWED_DOCS_DATA_PATTERN = "docs/memory-evaluation-*.json"
DOCUMENT_SUFFIXES = {".md", ".markdown", ".mdown", ".rst", ".adoc", ".asciidoc"}


def test_repository_has_exactly_three_long_lived_documents() -> None:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    repository_files = {
        item.decode("utf-8")
        for item in completed.stdout.split(b"\0")
        if item
    }
    documents = {
        path
        for path in repository_files
        if Path(path).suffix.lower() in DOCUMENT_SUFFIXES
        # The index still lists deletions until they are staged.
        and (ROOT / path).is_file()
    }

    top_level = {path for path in documents if not path.startswith("docs/")}
    assert top_level == ALLOWED_DOCUMENTS
    stray = sorted(
        path
        for path in repository_files
        if path.startswith("docs/")
        and (ROOT / path).is_file()
        and path not in ALLOWED_DOCS_FILES
        and not path.startswith(ALLOWED_DOCS_PREFIXES)
        and not Path(path).match(ALLOWED_DOCS_DATA_PATTERN)
    )
    assert stray == []
