"""Unit tests for the release artifact verifier."""

from __future__ import annotations

import asyncio

import pytest
from scripts import verify_agent_release_artifact


def test_verify_agent_release_artifact_runs_cleanly() -> None:
    # The SDK compatibility check constructs a real AgentSession, which asks
    # asyncio for the current loop. Give it one so the gate behaves the same
    # whether it runs alone or after other tests that closed the default loop.
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        assert verify_agent_release_artifact.main() == 0
    finally:
        asyncio.set_event_loop(None)
        loop.close()


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
