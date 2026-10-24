"""The Agent overlay's Docker build context must contain everything it copies.

``deploy_agent_component.sh`` uploads a ``git archive`` of selected paths and
builds ``infra/Dockerfile.agent-source-overlay`` on top of it. The archive is
the whole build context, so a COPY source the archive leaves out fails the
remote build after every gate has passed. That happened for two days while the
overlay copied the approved fixed-safety clips and the archive still listed only
``services`` and ``packages``.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "deploy_agent_component.sh"
OVERLAY = ROOT / "infra" / "Dockerfile.agent-source-overlay"

COPY_SOURCE = re.compile(r"^COPY\s+(.*)$", re.MULTILINE)
ARCHIVE_PREFIX = "  --prefix=memoria/ \\\n"


def _archive_paths() -> list[str]:
    """The paths listed after the release commit in the ``git archive`` call."""

    script = SCRIPT.read_text(encoding="utf-8")
    start = script.index(ARCHIVE_PREFIX) + len(ARCHIVE_PREFIX)
    end = script.index('>"$artifact"', start)
    return [
        path
        for line in script[start:end].splitlines()
        if line.strip() and not line.strip().startswith("#") and '"$expected_commit"' not in line
        for path in line.replace("\\", " ").split()
    ]


def _copy_sources() -> list[str]:
    """Archive-relative paths the overlay copies out of the build context."""

    sources = []
    for operands in COPY_SOURCE.findall(OVERLAY.read_text(encoding="utf-8")):
        tokens = [token for token in operands.split() if not token.startswith("--")]
        assert len(tokens) >= 2, f"unparsed COPY line: {operands}"
        for source in tokens[:-1]:
            if source.startswith("memoria/"):
                sources.append(source[len("memoria/") :])
    return sources


def _covers(archive_paths: list[str], source: str) -> bool:
    return any(source == path or source.startswith(path + "/") for path in archive_paths)


def test_every_overlay_copy_source_is_in_the_uploaded_archive() -> None:
    archive_paths = _archive_paths()
    sources = _copy_sources()

    assert sources, "the overlay stopped copying anything; this test no longer applies"
    missing = [source for source in sources if not _covers(archive_paths, source)]
    assert not missing, (
        f"overlay copies {missing} but the git archive does not carry them; "
        "the remote build would fail before cutover"
    )


def test_every_archive_path_exists_in_the_release_tree() -> None:
    """A path that is not tracked makes `git archive` fail before upload."""

    for path in _archive_paths():
        assert (ROOT / path).exists(), f"archive path is not in the checkout: {path}"
