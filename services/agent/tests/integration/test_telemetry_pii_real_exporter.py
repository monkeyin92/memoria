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

import pytest
from services.agent.tests.integration.telemetry_pii_probe import (
    CASES,
    ProbeCase,
    run_case,
)


@pytest.mark.parametrize("case", CASES, ids=[case.name for case in CASES])
def test_privacy_gate_holds_for_the_real_otlp_exporter(case: ProbeCase) -> None:
    result = run_case(case)

    assert result.ok, result.detail
