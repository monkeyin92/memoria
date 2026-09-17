"""P1-01: prove the privacy gate holds for a REAL OTLP exporter.

``scripts/verify_agent_release_artifact.py`` (and the canary it drives) uses
``InMemorySpanExporter``: that proves the SDK's gating logic but never exercises an
exporter, a wire payload or a receiver.  The probe in
``services/agent/tests/integration/telemetry_pii_probe.py`` runs the shipped
bootstrap in a fresh interpreter, installs the real OTLP/HTTP exporter against a
loopback collector and asserts on the bytes that actually arrived; this module
runs its cases one by one so a failure names the case.

The probe module is pytest-free on purpose, so the same code can run inside the
built candidate image:

    /app/.venv/bin/python -m services.agent.tests.integration.telemetry_pii_probe

Boundary: this is the local/CI gate.  Running it inside the candidate image on the
target host, the production exporter, the cutover and the rollback drill still
need the release window.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

import pytest
from services.agent.tests.integration import telemetry_pii_probe as probe
from services.agent.tests.integration.telemetry_pii_probe import (
    CASES,
    ProbeCase,
    run_case,
)


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_privacy_gate_holds_for_the_real_otlp_exporter(case: ProbeCase) -> None:
    result = run_case(case)

    assert result.ok, result.detail


def test_probe_repo_root_points_at_project_root_with_branch_coverage() -> None:
    """Catch a wrong ``parents[N]`` before it breaks the image gate and coverage."""
    root = probe._REPO_ROOT
    assert (root / "pyproject.toml").is_file()
    assert (root / "services" / "__init__.py").is_file()
    assert (
        root / "services" / "agent" / "tests" / "integration" / "telemetry_pii_emitter.py"
    ).is_file()
    content = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert "branch = true" in content


def test_probe_case_uses_repo_root_and_preserves_coverage_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The emitter subprocess must inherit cwd/PYTHONPATH and COV_CORE_* together."""
    from services.agent.tests.integration.telemetry_pii_emitter import CANARY_MODEL

    captured: dict[str, Any] = {}

    class _FakeCollector:
        raw_bodies: list[bytes] = []

        def __enter__(self) -> _FakeCollector:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        @property
        def endpoint(self) -> str:
            return "http://127.0.0.1:1/v1/traces"

        def exported_attributes(self) -> str:
            return f"gen_ai.request.model={CANARY_MODEL}"

    def _fake_run(cmd: object, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(probe, "OtlpCollector", _FakeCollector)
    monkeypatch.setattr(probe.subprocess, "run", _fake_run)
    monkeypatch.setenv("COV_CORE_CONFIG", "/tmp/fake-coveragerc")
    monkeypatch.setenv("COV_CORE_DATAFILE", "/tmp/fake-coverage-data")

    case = next(c for c in CASES if not c.expect_canary)
    result = run_case(case)
    assert result.ok, result.detail
    env = captured["env"]
    assert isinstance(env, dict)
    assert captured["cwd"] == probe._REPO_ROOT
    pythonpath = str(env.get("PYTHONPATH", ""))
    assert pythonpath.split(os.pathsep)[0] == str(probe._REPO_ROOT)
    cmd = captured["cmd"]
    assert isinstance(cmd, list) and probe.EMITTER_MODULE in cmd
    assert env.get("COV_CORE_CONFIG") == "/tmp/fake-coveragerc"
    assert env.get("COV_CORE_DATAFILE") == "/tmp/fake-coverage-data"
