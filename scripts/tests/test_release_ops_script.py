"""Guards for the server-side full-stack release script (``scripts/release_ops.sh``).

Two defects surfaced during the 20260925-full-stack-v1 release:

- ``finish`` printed every memoria container's ``.State.Health.Status``. LiveKit
  and the SenseVoice sidecar have no healthcheck, the Go template failed, and
  ``set -e``/``pipefail`` aborted the step before the post-state receipt.
- ``env`` compared env keys but never values, so a stale
  ``MEMORIA_SPEAKER_AUTHORITY_ENABLED=true`` shipped and the first device
  greeting was dropped as ``target_non_owner``.

After 20260926-persona-subject-v1 every target, control-api included, runs
from the plain release compose file, so the freeze and rollback steps no longer
carry a control-api component chain.
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


def _code() -> str:
    """The script without comment lines, which may cite past releases."""

    return "\n".join(line for line in _script().splitlines() if not line.lstrip().startswith("#"))


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
    uses = [line for line in _code().splitlines() if ".State.Health.Status" in line]
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
    script = _code()
    # Chains retired by earlier full-stack releases must not come back.
    for stale in (
        "20260921-demo02-base",
        "20260921-defect-a-base",
        "20260901-0945",
        "20260828-agent-loss",
        "confirm-bound-subject",
        "$OLD",
        "20260925-full-stack-v1",
        "20260925-device-mascot-sync",
        "LIVE_CONTROL_RELEASE",
        "component-releases",
        "/tmp/media-runtime",
    ):
        assert stale not in script, stale
    assert "PREV_TAG=20260926-persona-subject-v1" in script
    assert "PREV_COMMIT=63cf5f8cf09baace6ae4274844283eebf1d33ff3" in script


def test_freeze_checks_every_target_chain_and_the_current_link() -> None:
    freeze = _function("step_freeze")
    assert 'for c in "${TARGETS[@]}"; do\n    cf="$(live_chain "$c")"' in freeze
    assert '[[ "$cf" == "$PREV/docker-compose.production.yml" ]]' in freeze
    assert 'readlink -f /opt/memoria/current)" == "$PREV"' in freeze


def test_targets_and_rollback_services_are_the_same_six_roles() -> None:
    script = _script()
    targets = re.search(r"^TARGETS=\(([^)]*)\)", script, re.M)
    services = re.search(r"^PREV_STACK_SERVICES=\(([^)]*)\)", script, re.M)
    assert targets and services
    containers = {f"memoria-{name}-1" for name in services.group(1).split()}
    assert containers == set(targets.group(1).split())
    assert "control-api" in services.group(1).split()
    # media-edge is released on its own and never recreated by this script.
    assert "media-edge" not in _code().replace("memoria-media-edge-1", "")


def test_schema_writes_the_data_tree_and_rollback_returns_to_prev() -> None:
    schema = _function("step_schema")
    rollback = _function("step_rollback")
    assert '"$DATA_TREE/$f"' in schema and "$PREV" not in schema
    assert 'ln -sfn "$PREV" /opt/memoria/current.new' in rollback
    assert 'MEMORIA_RELEASE_TAG="$PREV_TAG" MEMORIA_RELEASE_COMMIT="$PREV_COMMIT"' in rollback
    assert '"${PREV_STACK_SERVICES[@]}"' in rollback
    assert 'for c in "${TARGETS[@]}"; do\n    wait_healthy "$c"' in rollback
    assert "$DATA_TREE" not in rollback


def test_rollback_has_no_persona_guard_once_prev_keys_persona_by_subject() -> None:
    # PREV (20260926-persona-subject-v1) already drops the one-active-version-per-
    # account index, so bound-subject persona versions no longer block a rollback.
    assert "rollback_persona_guard" not in _script()
    assert "ROLLBACK_SUPERSEDE_SUBJECT_PERSONA" not in _script()
