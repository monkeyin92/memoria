"""Unit tests for the release artifact verifier."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from scripts import verify_agent_release_artifact


def test_verify_agent_release_artifact_runs_cleanly() -> None:
    # Give the gate a current loop so it behaves the same whether it runs alone
    # or after other tests that closed the default loop.
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


def test_verifier_targets_the_media_bridge_not_the_retired_worker() -> None:
    source = Path(verify_agent_release_artifact.__file__).read_text(encoding="utf-8")
    # The LiveKit worker entrypoint and its session wiring were deleted.
    assert "services.agent.src.main" not in source
    assert "session_entrypoint" not in source
    assert "import scripts.run_media_bridge" in source
    compose = (Path(__file__).resolve().parents[2] / "docker-compose.production.yml").read_text(
        encoding="utf-8"
    )
    factory = verify_agent_release_artifact.PRODUCTION_SESSION_FACTORY
    assert f'MEDIA_BRIDGE_SESSION_FACTORY: "{factory}"' in compose
    assert verify_agent_release_artifact.EXPECTED_VERSIONS["livekit-agents"] == "1.8.1"
