"""Guards for the server-side full-stack release script (``scripts/release_ops.sh``).

Two defects surfaced during the 20260925-full-stack-v1 release:

- ``finish`` printed every memoria container's ``.State.Health.Status``. LiveKit
  and the SenseVoice sidecar have no healthcheck, the Go template failed, and
  ``set -e``/``pipefail`` aborted the step before the post-state receipt.
- ``env`` compared env keys but never values, so a stale
  ``MEMORIA_SPEAKER_AUTHORITY_ENABLED=true`` shipped and the first device
  greeting was dropped as ``target_non_owner``.

20260927-unbind-release-v1 folded the device OTA control-api component back
into the full stack, so all targets run from the plain PREV compose file
and freeze and rollback carry no component chain.  The 20260930-vector-keyword-v1,
20261001-wake-mode-v1 control-api components put control-api on a chain for a day
each; the full-stack release that followed folded them back in and the chain support
went again (20261002-stop-diag-v1 is the latest such release).

Since 20260929-livekit-retire-v1 (the LiveKit chain was stopped, then removed
from the host) a release ships speaker-model, control-api and the Voice Core
media bridge, and PREV is those same three roles.
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


def test_env_checks_speaker_authority_for_the_bridge() -> None:
    env_step = _function("step_env")
    guard = _function("assert_speaker_authority_disabled")
    assert "for svc in voice-core-media-bridge; do" in guard
    assert "new_compose run" in guard
    assert "assert_speaker_authority_disabled" in env_step
    # After the env is validated, before the slower provider smoke.
    assert env_step.index("scripts.verify_env") < env_step.index(
        "assert_speaker_authority_disabled"
    )
    assert env_step.index("assert_speaker_authority_disabled") < env_step.index(
        "provider_smoke_test"
    )
    # The retired LiveKit worker service no longer exists in the candidate compose.
    assert "python agent " not in env_step
    assert "voice-core-media-bridge -m scripts.verify_env" in env_step
    assert "voice-core-media-bridge -m scripts.provider_smoke_test" in env_step


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
        "20260925-full-stack-v1", "20260926-persona-subject-v1", "20260926-edge-flush-v1",
        "20260925-device-mascot-sync",
        "20260927-device-ota", "LIVE_CONTROL_RELEASE", "CONTROL_CHAIN",
        "20260927-unbind-release-v1",
        "20260927-child-binding-v1",
        "20260928-child-binding-v2",
        "20260928-session-trust-v1",
        "20260928-review-batches-v1", "20260929-livekit-retire-v1", "20260929-voice-core-refactor-v1",
        "20260929-stop-word-v1", "20260929-stop-playback-v1", "20260929-stop-reconnect-v1",
        "20260929-turn-taking-v1", "20260929-session-limits-v1",
        "20260930-local-stop-v2", "20260930-vector-keyword-v1",
        "20261001-device-archive-v1", "20261001-wake-mode-v1", "20261001-audience-recap-v1", "20261001-speaking-style-v2", "20261001-output-seq-v1", "20261001-turn-budget-v1", "20261001-device-prompt-v1", "20261001-stop-terminal-v1", "20261001-stop-cancel-v1", "20261001-trusted-adult-v1", "20261001-deepseek-flash-v1", "20261002-stop-diag-v1", "20261002-stop-pin-v1", "20261002-device-memory-v1", "20261003-endpoint-latency-v1", "20261003-child-memory-v1", "20261003-echo-merge-v1", "20261004-followup-warm-v1", "20261004-comma-tail-v1", "20261004-vad-warm-v1", "20261004-first-warm-v1", "20261005-subject-candidates-v1", "20261006-speaking-flush-v1", "20261007-n8-diag-v1",
        "RETIRED_TARGETS", "retire_prev_media_chain",
        "/tmp/media-runtime",
    ):
        assert stale not in script, stale
    assert "PREV_TAG=20261009-textless-spare-v1" in script
    assert "PREV_COMMIT=2f70ed2a123f32205a51993d4d8980f3e5d3fd68" in script


def test_freeze_checks_every_target_chain_and_the_current_link() -> None:
    freeze = _function("step_freeze")
    assert 'for c in "${TARGETS[@]}"; do\n    cf="$(live_chain "$c")"' in freeze
    assert '[[ "$cf" == "$PREV/docker-compose.production.yml" ]]' in freeze
    assert 'readlink -f /opt/memoria/current)" == "$PREV"' in freeze


def test_targets_and_rollback_services_are_the_same_three_roles() -> None:
    script = _script()
    targets = re.search(r"^TARGETS=\(([^)]*)\)", script, re.M)
    services = re.search(r"^PREV_STACK_SERVICES=\(([^)]*)\)", script, re.M)
    assert targets and services
    containers = {f"memoria-{name}-1" for name in services.group(1).split()}
    assert containers == set(targets.group(1).split())
    assert services.group(1).split() == ["speaker-model", "control-api", "voice-core-media-bridge"]
    # media-edge is released on its own and never recreated by this script.
    assert "media-edge" not in _code().replace("memoria-media-edge-1", "")


def _array(name: str) -> list[str]:
    match = re.search(rf"^{name}=\(([^)]*)\)", _script(), re.M)
    assert match, name
    return match.group(1).split()


def test_release_ships_only_the_bridge_media_chain() -> None:
    assert _array("ROLES") == ["agent", "control-api", "speaker-model"]
    cutover = _function("step_cutover")
    for retired in ("miniprogram-gateway", "device-media-gateway", "memoria-agent-1"):
        assert retired not in cutover.replace('{"agent", "miniprogram-gateway", "device-media-gateway"}', "")
    assert "force-recreate voice-core-media-bridge" in cutover
    assert 'retired services still defined' in cutover


def test_rollback_recreates_the_prev_services() -> None:
    rollback = _function("step_rollback")
    services = _array("PREV_STACK_SERVICES")
    for retired in ("agent", "miniprogram-gateway", "device-media-gateway"):
        assert retired not in services
    assert '-f "$PREV/docker-compose.production.yml" --profile media-runtime' in rollback
    assert "--force-recreate" in rollback


def test_schema_writes_the_data_tree_and_rollback_returns_to_prev() -> None:
    schema = _function("step_schema")
    rollback = _function("step_rollback")
    assert '"$DATA_TREE/$f"' in schema and "$PREV" not in schema
    assert 'ln -sfn "$PREV" /opt/memoria/current.new' in rollback
    assert 'MEMORIA_RELEASE_TAG="$PREV_TAG" MEMORIA_RELEASE_COMMIT="$PREV_COMMIT"' in rollback
    assert '"${PREV_STACK_SERVICES[@]}"' in rollback
    assert "component-releases" not in rollback
    assert 'for c in "${TARGETS[@]}"; do\n    wait_healthy "$c"' in rollback
    assert "$DATA_TREE" not in rollback


def test_rollback_has_no_persona_guard_once_prev_keys_persona_by_subject() -> None:
    # PREV (20260926-persona-subject-v1 onward) already drops the one-active-version-per-
    # account index, so bound-subject persona versions no longer block a rollback.
    assert "rollback_persona_guard" not in _script()
    assert "ROLLBACK_SUPERSEDE_SUBJECT_PERSONA" not in _script()


# --- Control API host port (110.42.235.198 shares the host with hr-tracker, which holds 8791) ---

REFRESH = ROOT / "scripts" / "refresh_readiness.sh"
SMOKE = ROOT / "scripts" / "smoke_server_deployment.sh"
COMPOSE = ROOT / "docker-compose.production.yml"


def _shell_function(text: str, name: str) -> str:
    start = text.index(f"{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + 3]


def _port_from(script: Path, docker_stub: str, env_port: str | None, tmp_path: Path) -> str:
    """Run the script's own control_api_port against a stub ``docker``."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "docker"
    stub.write_text(f"#!/usr/bin/env bash\n{docker_stub}\n", encoding="utf-8")
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k != "MEMORIA_CONTROL_API_PORT"}
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    if env_port is not None:
        env["MEMORIA_CONTROL_API_PORT"] = env_port
    # The scripts run under set -Eeuo pipefail; so does the function under test.
    program = (
        "set -Eeuo pipefail\ncontrol_container=memoria-control-api-1\n"
        + _shell_function(script.read_text(encoding="utf-8"), "control_api_port")
        + "\ncontrol_api_port\n"
    )
    result = subprocess.run(
        ["bash", "-c", program], env=env, capture_output=True, text=True, check=True
    )
    return result.stdout


@pytest.mark.parametrize("script", [SCRIPT, REFRESH], ids=["release_ops", "refresh_readiness"])
@pytest.mark.parametrize(
    ("docker_stub", "env_port", "expected"),
    [
        # The live container publishes 18791 (a neighbour holds 8791).
        ('echo "127.0.0.1:18791"', None, "18791"),
        # An explicit override wins over what is live.
        ('echo "127.0.0.1:18791"', "28791", "28791"),
        # Default host: 8791.
        ('echo "127.0.0.1:8791"', None, "8791"),
        # Only the loopback binding counts (IPv4 and IPv6 wildcard lines are ignored).
        ('printf "0.0.0.0:9999\\n[::]:9999\\n127.0.0.1:18791\\n"', None, "18791"),
        ('printf "0.0.0.0:9999\\n"', None, "8791"),
        # No such container, a failing docker, or no output: the repo default, and never an abort.
        ('echo "Error: No public port 8000/tcp published" >&2; exit 1', None, "8791"),
        ("exit 1", None, "8791"),
        ("exit 0", None, "8791"),
    ],
)
def test_control_api_port_prefers_override_then_live_container_then_default(
    script: Path, docker_stub: str, env_port: str | None, expected: str, tmp_path: Path
) -> None:
    assert _port_from(script, docker_stub, env_port, tmp_path) == expected


def test_compose_publishes_the_control_api_on_a_configurable_loopback_port() -> None:
    compose = COMPOSE.read_text(encoding="utf-8")
    assert '- "127.0.0.1:${MEMORIA_CONTROL_API_PORT:-8791}:8000"' in compose
    assert '"127.0.0.1:8791:8000"' not in compose


def test_verify_load_hands_the_live_port_to_the_new_release_dir() -> None:
    body = _function("step_verify_load")
    assert "MEMORIA_CONTROL_API_PORT=%s" in body
    assert '"$(control_api_port)" > "$R/.env"' in body
    # Compose reads it from the release dir's .env; the preflight runs after it is written.
    assert body.index("control_api_port") < body.index("smoke_server_deployment.sh")


def _cutover_python() -> str:
    body = _function("step_cutover")
    match = re.search(r"python3 -c '\n(.*?)print\(\"resolve=PASS\"\)'", body, re.S)
    assert match, "cutover must resolve the candidate compose before touching containers"
    return match.group(1) + 'print("resolve=PASS")'


def _resolve(tag: str, services: dict, control_port: str) -> subprocess.CompletedProcess[str]:
    import json

    return subprocess.run(
        [sys.executable, "-c", _cutover_python()],
        input=json.dumps({"services": services}),
        env={**os.environ, "TAG": tag, "CONTROL_PORT": control_port},
        capture_output=True,
        text=True,
    )


def _candidate(tag: str, published: str | None) -> dict:
    services = {
        "speaker-model": {"image": f"memoria-speaker-model:{tag}"},
        "control-api": {
            "image": f"memoria-control-api:{tag}",
            "ports": [] if published is None else [{"published": published, "target": 8000}],
        },
        "voice-core-media-bridge": {"image": f"memoria-agent:{tag}"},
    }
    return services


def test_cutover_refuses_a_candidate_that_would_publish_another_control_port() -> None:
    body = _function("step_cutover")
    assert 'CONTROL_PORT="$(control_api_port)" python3 -c' in body
    ok = _resolve("t1", _candidate("t1", "18791"), "18791")
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "resolve=PASS" in ok.stdout
    # On a shared host an unconfigured candidate falls back to 8791, which hr-tracker holds.
    assert _resolve("t1", _candidate("t1", "8791"), "18791").returncode != 0
    assert _resolve("t1", _candidate("t1", None), "18791").returncode != 0
    # The default host keeps working unchanged.
    assert _resolve("t1", _candidate("t1", "8791"), "8791").returncode == 0


def test_finish_probes_readiness_on_the_live_port() -> None:
    finish = _function("step_finish")
    assert 'curl -fsS "http://127.0.0.1:$(control_api_port)/health/ready"' in finish
    # 8791 survives only as the helper's default, never as a hard-coded probe.
    assert _code().count("8791") == 1
    assert 'printf \'%s\' "${port:-8791}"' in _function("control_api_port")


def test_preflight_ports_never_collide_with_the_production_control_api() -> None:
    smoke = SMOKE.read_text(encoding="utf-8")
    assert 'api_port="${MEMORIA_PREFLIGHT_API_PORT:-28791}"' in smoke
    assert 'nginx_port="${MEMORIA_PREFLIGHT_NGINX_PORT:-28891}"' in smoke
    # 18791 is where the Control API sits beside hr-tracker; the free-port check would refuse every release.
    assert "api_port=18791" not in smoke
    subprocess.run(["bash", "-n", str(SMOKE)], check=True)
    subprocess.run(["bash", "-n", str(REFRESH)], check=True)


BRINGUP = ROOT / "scripts" / "bringup_shared_host.sh"


def test_shared_host_bringup_script_is_checked_in_and_keeps_its_step_contract() -> None:
    text = BRINGUP.read_text(encoding="utf-8")
    assert os.access(BRINGUP, os.X_OK)
    subprocess.run(["bash", "-n", str(BRINGUP)], check=True)
    for step in ("net", "data", "redis", "schema", "verify", "replay", "apps", "checks", "status", "down"):
        assert f"  {step}) step_{step} ;;" in text
    # Restored data is replayed through the Control API image, which now carries the module.
    assert "-m scripts.replay_subject_deletions --confirm-replay" in text
    dockerfile = (ROOT / "infra" / "Dockerfile.control-api").read_text(encoding="utf-8")
    assert "scripts/replay_subject_deletions.py" in dockerfile
    # Env files are named by path only; no secret value belongs in a checked-in helper.
    assert not re.search(r"(?im)^\s*(?:export\s+)?[A-Z_]*(?:PASSWORD|SECRET|TOKEN|KEY)[A-Z_]*=\S+", text)
