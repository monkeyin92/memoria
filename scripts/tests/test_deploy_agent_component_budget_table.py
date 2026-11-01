"""An Agent overlay may tighten a module line budget.

The budgets sit in ``pyproject.toml``, which the overlay release treats as a dependency input, while the
budget gate rejects a module that shrank below its budget until the table is tightened. Without this
carve-out a change that shortens a budgeted module could never ship as an overlay.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "deploy_agent_component.sh"

PYPROJECT = """[project]
name = "voice-agent"
dependencies = ["httpx==0.28.1"]

[tool.memoria.module-budgets]
"services/agent/src/duplex_runtime.py" = {budget}

[tool.coverage.report]
fail_under = 85
"""


def _strip(text: str) -> str:
    match = re.search(
        r"^strip_module_budgets\(\) \{\n.*?\n\}\n", SCRIPT.read_text(encoding="utf-8"), re.S | re.M
    )
    assert match, "strip_module_budgets is gone from the deploy script"
    return subprocess.run(
        ["bash", "-c", match.group(0) + "strip_module_budgets"],
        input=text,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_a_budget_change_alone_leaves_the_dependency_inputs_equal() -> None:
    assert _strip(PYPROJECT.format(budget=2780)) == _strip(PYPROJECT.format(budget=2750))


def test_the_rest_of_pyproject_still_counts() -> None:
    stripped = _strip(PYPROJECT.format(budget=2780))

    assert 'dependencies = ["httpx==0.28.1"]' in stripped
    assert "fail_under = 85" in stripped
    assert "duplex_runtime" not in stripped
    assert _strip(PYPROJECT.format(budget=1).replace("0.28.1", "0.28.2")) != stripped
