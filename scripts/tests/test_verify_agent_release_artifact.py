"""Unit tests for the release artifact verifier."""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts import verify_agent_release_artifact


def test_verify_agent_release_artifact_runs_cleanly() -> None:
    assert verify_agent_release_artifact.main() == 0


def test_verifier_fails_when_a_livekit_module_is_imported() -> None:
    leak = "import sys, types\nsys.modules['livekit.agents'] = types.ModuleType('livekit.agents')\n"
    with pytest.raises(SystemExit):
        verify_agent_release_artifact._run_subprocess_check(
            leak + verify_agent_release_artifact.NO_LIVEKIT_IMPORTED
        )
    verify_agent_release_artifact._run_subprocess_check(
        verify_agent_release_artifact.NO_LIVEKIT_IMPORTED
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
