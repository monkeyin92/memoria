"""Freeze the cross-package import graph of ``services/``.

Every production (non-test) import from one ``services.<pkg>`` into another is
listed below. A new cross-package dependency fails this test until it is added
here on purpose, so layering changes show up in review. Edges that disappear
must be removed from the baseline too: like the module line budget, the graph
only shrinks.

Known inversions kept only until they are removed (do not add more of these):
``common`` importing ``agent``/``archive``, domain packages importing
``control_api`` (governance, memory_scope, companionship) or ``agent``
(voice_profile, device_media_gateway).
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parents[1]
SERVICES = ROOT / "services"

ALLOWED_EDGES: dict[str, frozenset[str]] = {
    "agent": frozenset(
        {"common", "evolution", "identity", "persona", "policy", "speaker", "tutor"}
    ),
    "archive": frozenset({"common", "evolution", "governance"}),
    "common": frozenset({"agent", "archive"}),
    "companionship": frozenset({"control_api"}),
    "consent": frozenset({"guardian", "identity", "policy"}),
    "control_api": frozenset(
        {
            "agent",
            "archive",
            "common",
            "consent",
            "device_fleet",
            "digital_self",
            "evolution",
            "governance",
            "growth",
            "guardian",
            "identity",
            "legacy",
            "memory_scope",
            "persona",
            "policy",
            "self_model",
            "session_runtime",
            "speaker",
            "tutor",
            "voice_profile",
        }
    ),
    "device_fleet": frozenset({"policy"}),
    "device_media_gateway": frozenset({"agent", "common", "miniprogram_gateway"}),
    "digital_self": frozenset({"archive", "common", "persona", "self_model"}),
    "evolution": frozenset({"archive", "common"}),
    "governance": frozenset(
        {
            "archive",
            "control_api",
            "digital_self",
            "evolution",
            "guardian",
            "identity",
            "legacy",
            "memory_scope",
            "persona",
            "voice_profile",
        }
    ),
    "growth": frozenset({"archive", "common", "persona", "self_model"}),
    "guardian": frozenset({"archive", "governance", "tutor"}),
    "identity": frozenset({"common", "consent"}),
    "legacy": frozenset({"digital_self", "self_model"}),
    "memory_scope": frozenset({"consent", "control_api", "identity", "policy"}),
    "miniprogram_gateway": frozenset({"common"}),
    "persona": frozenset({"archive", "common", "identity"}),
    "policy": frozenset({"consent"}),
    "self_model": frozenset({"archive"}),
    "session_runtime": frozenset({"consent", "policy"}),
    "tutor": frozenset({"archive", "policy"}),
    "voice_profile": frozenset({"agent", "archive"}),
}


def _imported_modules(tree: ast.AST) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
    return modules


def _measured_edges() -> dict[str, set[str]]:
    edges: dict[str, set[str]] = defaultdict(set)
    for path in SERVICES.rglob("*.py"):
        relative = path.relative_to(SERVICES).parts
        if len(relative) < 2 or "tests" in relative or "__pycache__" in relative:
            continue
        source_package = relative[0]
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for module in _imported_modules(tree):
            parts = module.split(".")
            if len(parts) > 1 and parts[0] == "services" and parts[1] != source_package:
                edges[source_package].add(parts[1])
    return edges


def test_no_new_cross_package_imports() -> None:
    measured = _measured_edges()
    added = sorted(
        f"{source} -> {target}"
        for source, targets in measured.items()
        for target in targets - ALLOWED_EDGES.get(source, frozenset())
    )
    assert added == [], (
        "new cross-package imports; depend on a shared port instead, or add the "
        f"edge to ALLOWED_EDGES deliberately: {added}"
    )


def test_removed_cross_package_imports_shrink_the_baseline() -> None:
    measured = _measured_edges()
    stale = sorted(
        f"{source} -> {target}"
        for source, targets in ALLOWED_EDGES.items()
        for target in targets - measured.get(source, set())
    )
    assert stale == [], f"remove these edges from ALLOWED_EDGES: {stale}"
