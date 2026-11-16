from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
ALLOWED_DOCUMENTS = {
    "README.md",
    "HANDOFF.md",
    "TODOLIST.md",
    "AGENTS.md",
}
# Agent guidance files sit beside the code they describe, at any depth.
AGENT_GUIDE_NAME = "AGENTS.md"


def _is_agent_guide(path: str) -> bool:
    return Path(path).name == AGENT_GUIDE_NAME


# Each document type has one canonical aggregate. Evidence files stay beside
# the aggregates and are covered by the data pattern below.
ALLOWED_DOCS_PREFIXES = ()
ALLOWED_DOCS_FILES = {
    "docs/AGENTS.md",
    "docs/acceptance.md",
    "docs/compliance.md",
    "docs/design.md",
    "docs/eval.md",
    "docs/handoff-archive.md",
    "docs/product.md",
    "docs/ops/runtime-inventory.md",
    "docs/runbooks.md",
    "docs/strategy.md",
}
# Evaluation receipts are data, kept next to the docs that cite them.
ALLOWED_DOCS_DATA_PATTERN = "docs/memory-evaluation-*.json"
DOCUMENT_SUFFIXES = {".md", ".markdown", ".mdown", ".rst", ".adoc", ".asciidoc"}
# Vendored agent tooling (.cursor/) and files a tool or the mini program reads
# at runtime are markdown by format, not project docs.
ALLOWED_TOOLING_PREFIX = ".cursor/"
ALLOWED_TOOLING_FILES = {
    ".github/pull_request_template.md",
    "apps/miniprogram/legal/user-agreement.md",
}


def test_repository_has_exactly_three_long_lived_documents() -> None:
    completed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    repository_files = {item.decode("utf-8") for item in completed.stdout.split(b"\0") if item}
    documents = {
        path
        for path in repository_files
        if Path(path).suffix.lower() in DOCUMENT_SUFFIXES
        # The index still lists deletions until they are staged.
        and (ROOT / path).is_file()
    }

    top_level = {
        path
        for path in documents
        if not path.startswith(("docs/", ALLOWED_TOOLING_PREFIX))
        and path not in ALLOWED_TOOLING_FILES
        and not _is_agent_guide(path)
    }
    # AGENTS.md guides sit beside the code they describe at any depth and are
    # not counted; the name stays allowed so a root guide is permitted.
    assert top_level == ALLOWED_DOCUMENTS - {AGENT_GUIDE_NAME}
    stray = sorted(
        path
        for path in repository_files
        if path.startswith("docs/")
        and (ROOT / path).is_file()
        and Path(path).suffix.lower() in DOCUMENT_SUFFIXES
        and path not in ALLOWED_DOCS_FILES
    )
    assert stray == []
