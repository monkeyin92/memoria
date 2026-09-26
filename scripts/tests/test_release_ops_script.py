"""Guards for the server-side full-stack release script (``scripts/release_ops.sh``).

Two defects surfaced during the 20260925-full-stack-v1 release:

- ``finish`` printed every memoria container's ``.State.Health.Status``. LiveKit
  and the SenseVoice sidecar have no healthcheck, the Go template failed, and
  ``set -e``/``pipefail`` aborted the step before the post-state receipt.
- ``env`` compared env keys but never values, so a stale
  ``MEMORIA_SPEAKER_AUTHORITY_ENABLED=true`` shipped and the first device
  greeting was dropped as ``target_non_owner``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release_ops.sh"


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def _function(name: str) -> str:
    script = _script()
    start = script.index(f"{name}() {{")
    end = script.index("\n}\n", start)
    return script[start : end + 3]


def _speaker_guard_python() -> str:
    body = _function("assert_speaker_authority_disabled")
    match = re.search(r"-c '\n(.*?)' \|\|", body, re.S)
    assert match, "speaker authority guard must run an inline Python check"
    return match.group(1)


def test_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_no_unguarded_health_status_template() -> None:
    code_lines = [line for line in _script().splitlines() if not line.lstrip().startswith("#")]
    uses = [line for line in code_lines if ".State.Health.Status" in line]
    assert uses, "the state listing must still report health where it exists"
    for line in uses:
        assert "{{if .State.Health}}{{.State.Health.Status}}" in line, (
            "containers without a healthcheck break a bare .State.Health.Status template"
        )


def test_finish_and_rollback_use_the_guarded_state_listing() -> None:
    assert "memoria_container_states | tee" in _function("step_finish")
    assert "memoria_container_states" in _function("step_rollback")
    assert "{{else}}no-healthcheck{{end}}" in _function("container_state")


@pytest.mark.parametrize(
    ("line", "blocks"),
    [
        ("/memoria-agent-1 img sha t healthy restarts=0", False),
        ("/memoria-livekit-livekit-1 img sha t no-healthcheck restarts=0", False),
        ("/memoria-sensevoice-asr img sha t no-healthcheck restarts=0", False),
        ("/memoria-control-api-1 img sha t unhealthy restarts=2", True),
        ("/memoria-control-api-1 img sha t starting restarts=0", True),
    ],
)
def test_finish_fails_only_on_unhealthy_or_starting(line: str, blocks: bool) -> None:
    body = _function("step_finish")
    match = re.search(r"grep -E '([^']+)' \"\$S/post-state.txt\"", body)
    assert match, "finish must fail closed on unhealthy containers"
    found = re.search(match.group(1), line) is not None
    assert found is blocks


def test_env_checks_speaker_authority_for_agent_and_bridge() -> None:
    env_step = _function("step_env")
    guard = _function("assert_speaker_authority_disabled")
    assert "for svc in agent voice-core-media-bridge" in guard
    assert "new_compose run" in guard
    assert "assert_speaker_authority_disabled" in env_step
    # After the env is validated, before the slower provider smoke.
    assert env_step.index("scripts.verify_env") < env_step.index(
        "assert_speaker_authority_disabled"
    )
    assert env_step.index("assert_speaker_authority_disabled") < env_step.index(
        "provider_smoke_test"
    )


@pytest.mark.parametrize(
    ("value", "passes"),
    [
        (None, True),
        ("", True),
        ("false", True),
        ("False", True),
        ("0", True),
        ("no", True),
        ("off", True),
        ("true", False),
        ("TRUE", False),
        ("1", False),
        ("yes", False),
        ("on", False),
        (" true ", False),
        ("enabled", False),
    ],
)
def test_speaker_guard_accepts_only_values_every_reader_treats_as_off(
    value: str | None, passes: bool
) -> None:
    env = {k: v for k, v in os.environ.items() if k != "MEMORIA_SPEAKER_AUTHORITY_ENABLED"}
    if value is not None:
        env["MEMORIA_SPEAKER_AUTHORITY_ENABLED"] = value
    result = subprocess.run(
        [sys.executable, "-c", _speaker_guard_python()],
        env=env,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) is passes, result.stdout + result.stderr
    if passes:
        assert "speaker_authority_disabled=PASS" in result.stdout


def test_live_chain_constants_have_no_stale_release_trees() -> None:
    script = _script()
    # Chains retired by the 20260925 full-stack release must not come back.
    for stale in ("20260921-demo02-base", "20260921-defect-a-base", "20260901-0945",
                  "20260828-agent-loss", "confirm-bound-subject", "$OLD"):
        assert stale not in script, stale
    assert "PREV_TAG=20260925-full-stack-v1" in script
    assert "LIVE_CONTROL_RELEASE=20260925-device-mascot-sync" in script


def test_freeze_checks_every_target_chain_and_the_current_link() -> None:
    freeze = _function("step_freeze")
    assert '"$PREV/docker-compose.production.yml,$CR/$LIVE_CONTROL_RELEASE/' in freeze
    for container in ("memoria-speaker-model-1", "memoria-agent-1",
                      "memoria-voice-core-media-bridge-1", "memoria-miniprogram-gateway-1",
                      "memoria-device-media-gateway-1"):
        assert container in freeze
    assert '[[ "$cf" == "$PREV/docker-compose.production.yml" ]]' in freeze
    assert 'readlink -f /opt/memoria/current)" == "$PREV"' in freeze
    # media-edge's host-side override is optional now that no rebuilt role uses it.
    assert "missing /tmp/media-runtime.override.yml" not in freeze


def test_schema_writes_the_data_tree_and_rollback_returns_to_prev() -> None:
    schema = _function("step_schema")
    rollback = _function("step_rollback")
    assert '"$DATA_TREE/$f"' in schema and "$PREV" not in schema
    assert 'ln -sfn "$PREV" /opt/memoria/current.new' in rollback
    assert 'MEMORIA_RELEASE_TAG="$PREV_TAG" MEMORIA_RELEASE_COMMIT="$PREV_COMMIT"' in rollback
    assert '"${PREV_STACK_SERVICES[@]}"' in rollback
    assert "$DATA_TREE" not in rollback


def test_rollback_refuses_when_bound_subjects_hold_active_persona_versions() -> None:
    guard = _function("rollback_persona_guard")
    rollback = _function("step_rollback")
    # PREV's control-api rebuilds the one-active-version-per-account index at start.
    assert "subject_id IS DISTINCT FROM account_id" in guard
    assert "ROLLBACK_SUPERSEDE_SUBJECT_PERSONA" in guard
    assert "exit 1" in guard
    assert "SET status = 'superseded'" in guard
    # It runs before anything is recreated or any env file is restored.
    assert rollback.index("rollback_persona_guard") < rollback.index("cp -p")
    assert rollback.index("rollback_persona_guard") < rollback.index("docker compose")
