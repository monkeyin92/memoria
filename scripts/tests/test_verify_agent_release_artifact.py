"""Unit tests for the release artifact verifier."""

from __future__ import annotations

import pytest
from scripts import verify_agent_release_artifact


def test_verify_agent_release_artifact_runs_cleanly() -> None:
    assert verify_agent_release_artifact.main() == 0


def test_verify_agent_release_artifact_fails_when_entrypoint_lacks_privacy() -> None:
    # A faulty code that doesn't apply defaults should fail the check
    faulty_code = """
import os
from livekit.agents.telemetry import gen_ai
assert gen_ai.capture_content_enabled() is False
"""
    with pytest.raises(SystemExit):
        verify_agent_release_artifact._run_subprocess_check(
            faulty_code,
            env_overrides={
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": None,
                "LIVEKIT_TELEMETRY_ALLOW_PII": None,
            },
        )
