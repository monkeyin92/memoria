from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "release_ops.sh"
CI = ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(autouse=True, scope="module")
def _no_override_files_in_real_tmp():
    before = set(Path("/tmp").glob("override-*.yml"))
    yield
    new = sorted(set(Path("/tmp").glob("override-*.yml")) - before)
    assert not new, f"tests wrote override files into real /tmp: {new}"


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


def test_freeze_keeps_sqlite_snapshot_in_cutover_and_prunes_old_dumps() -> None:
    freeze = _function("step_freeze")
    assert 'docker cp "memoria-control-api-1:/data/memoria.pre-$TAG.sqlite3" "$S/memoria.pre-$TAG.sqlite3"' in freeze
    assert 'rm -f "/data/memoria.pre-$TAG.sqlite3"' in freeze
    assert "cleanup_freeze_dumps" in freeze
    cleanup = _function("cleanup_freeze_dumps")
    assert "cleanup_freeze_dumps.py" in cleanup
    assert "--retention-days 3" in cleanup
    assert "--apply" in cleanup
    assert "cleanup-freeze-dumps" in _function("main")


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


_LOG_ID = "3f2a1b9c8d7e"
_STAMP = "2026-10-17T10:02:03.123456+08:00"
_STARTUP = f"{_STAMP} control-api/{_LOG_ID} INFO:     Application startup complete.\n"
_MARKER = f"{_STAMP} control-api/{_LOG_ID} control_env_valid=PASS\n"


def _host_log_gate(log_dir: Path, before: int) -> subprocess.CompletedProcess[str]:
    program = "\n".join(
        (
            "set -Eeuo pipefail",
            "export TAG=t COMMIT=c",
            f"source {shlex.quote(str(SCRIPT))}",
            "sleep() { :; }",
            f"HOST_LOG_DIR={shlex.quote(str(log_dir))}",
            # GNU stat is not on every developer host; the gate itself is the subject here.
            'host_log_size() { [[ -f "$HOST_LOG_DIR/$1.log" ]] && wc -c < "$HOST_LOG_DIR/$1.log" || echo 0; }',
            f"assert_host_log_has control-api {before} control_env_valid=PASS",
        )
    )
    return subprocess.run(
        ["bash", "-c", program],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("at_snapshot", "after_env", "passes"),
    [
        (_STARTUP, _STARTUP + _MARKER, True),
        (_STARTUP, _STARTUP + _MARKER.replace("control-api/", "voice-core-media-bridge/"), False),
        (_STARTUP, _STARTUP + f"{_STAMP} control_env_valid=PASS\n", False),
        (_STARTUP + _MARKER, _STARTUP + _MARKER, False),
        (_STARTUP * 50, _MARKER, True),
        (None, None, False),
    ],
    ids=["own-marker", "other-service-tag", "untagged", "earlier-run-only", "rotated", "no-sink"],
)
def test_env_host_log_gate_accepts_only_this_runs_tagged_marker(
    tmp_path: Path, at_snapshot: str | None, after_env: str | None, passes: bool
) -> None:
    log_path = tmp_path / "control-api.log"
    if at_snapshot is None:
        before = 0
    else:
        log_path.write_text(at_snapshot, encoding="utf-8")
        before = log_path.stat().st_size
    if after_env is not None:
        log_path.write_text(after_env, encoding="utf-8")
    result = _host_log_gate(tmp_path, before)
    assert (result.returncode == 0) is passes, result.stdout + result.stderr
    if not passes:
        assert "容器日志落盘" in result.stderr


def test_env_host_log_gate_reads_a_rotated_marker_past_the_snapshot(tmp_path: Path) -> None:
    log_path = tmp_path / "control-api.log"
    log_path.write_text(_STARTUP, encoding="utf-8")
    before = log_path.stat().st_size
    (tmp_path / "control-api.log.1").write_text(_STARTUP + _MARKER, encoding="utf-8")
    log_path.write_text("", encoding="utf-8")
    result = _host_log_gate(tmp_path, before)
    assert result.returncode == 0, result.stdout + result.stderr


def test_env_host_log_gate_ignores_a_marker_already_rotated_before_the_snapshot(
    tmp_path: Path,
) -> None:
    prior = _STARTUP + _MARKER
    log_path = tmp_path / "control-api.log"
    log_path.write_text(prior, encoding="utf-8")
    before = log_path.stat().st_size
    (tmp_path / "control-api.log.1").write_text(prior, encoding="utf-8")
    log_path.write_text("", encoding="utf-8")
    result = _host_log_gate(tmp_path, before)
    assert result.returncode != 0
    assert "容器日志落盘" in result.stderr


def test_env_proves_the_host_log_sink_before_the_provider_smoke() -> None:
    env_step = _function("step_env")
    first_run = env_step.index("new_compose run")
    assert env_step.index("host_log_size control-api") < first_run
    assert env_step.index("host_log_size voice-core-media-bridge") < first_run
    assert env_step.index("assert_speaker_authority_disabled") < env_step.index(
        "assert_host_log_has control-api"
    )
    assert env_step.index("assert_host_log_has voice-core-media-bridge") < env_step.index(
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


def test_script_names_no_release_tag_but_the_data_tree() -> None:
    # Whitelisted component overrides are the only other dated names.
    stale = {
        name.removesuffix(".yml")
        for name in re.findall(r"\b20\d{6}-[a-z0-9.-]+", _code())
    }
    assert stale == {
        "20260827-architecture-split-v1",
        "20261021-a94c438-pr345-pr346-pr348-pr352",
        "20261021-a94c438-pr345-pr346-pr348-pr352-pr353-pr355",
        "20261021-stepfun-c272af5",
        "20261021-pr368-dd98c4a",
        "20261021-main-f14be64",
    }
    for retired in (
        "$OLD",
        "LIVE_CONTROL_RELEASE",
        "CONTROL_CHAIN",
        "RETIRED_TARGETS",
        "retire_prev_media_chain",
        "/tmp/media-runtime",
    ):
        assert retired not in _code(), retired
    assert "media-edge" not in _code().replace("memoria-media-edge-1", "")


def test_schema_writes_the_data_tree_never_prev() -> None:
    schema = _function("step_schema")
    assert '"$DATA_TREE/$f"' in schema
    assert "PREV" not in schema


@dataclass(frozen=True)
class SourcedStack:
    ship: list[str]
    roles: list[str]
    retired: list[str]
    ship_images: str


def sourced_stack() -> SourcedStack:
    program = (
        f"export TAG=t COMMIT=c\nsource {shlex.quote(str(SCRIPT))}\n"
        'printf "%s\\n" "${SHIP[*]}" "${ROLES[*]}" "${RETIRED[*]}" "$(ship_images)"'
    )
    lines = subprocess.run(
        ["bash", "-c", program], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    return SourcedStack(lines[0].split(), lines[1].split(), lines[2].split(), lines[3])


def _candidate(tag: str, published: str | None) -> dict[str, dict[str, object]]:
    stack = sourced_stack()
    services: dict[str, dict[str, object]] = {
        service: {"image": f"memoria-{role}:{tag}"}
        for service, role in zip(stack.ship, stack.roles, strict=True)
    }
    services["control-api"]["ports"] = (
        [] if published is None else [{"published": published, "target": 8000}]
    )
    return services


def test_every_release_artifact_list_ships_the_stack_roles() -> None:
    from scripts.verify_release_manifest import _ROLES

    roles = set(sourced_stack().roles)
    assert _ROLES == {role: f"memoria-{role}" for role in roles}

    runbook = (ROOT / "docs" / "runbooks" / "release-rollback.md").read_text(encoding="utf-8")
    save = re.search(r'docker save -o "\$ARTIFACT_DIR/images\.tar" ((?:.*\\\n)*.*)', runbook)
    assert save, "the runbook must show the docker save that builds images.tar"
    assert set(re.findall(r"memoria-([a-z-]+):\$RELEASE_TAG", save.group(1))) == roles

    delta = (ROOT / "scripts" / "delta_build_images.sh").read_text(encoding="utf-8")
    delta_roles = re.search(r"^ROLES=\((.*)\)$", delta, re.M)
    assert delta_roles, "the delta build must name its roles in one ROLES array"
    assert set(delta_roles.group(1).split()) == roles


def _delta_dockerfile(role: str) -> subprocess.CompletedProcess[str]:
    delta = (ROOT / "scripts" / "delta_build_images.sh").read_text(encoding="utf-8")
    program = "\n".join(
        (
            "set -Eeuo pipefail",
            "BASE_TAG=20261013-guardian-summaries-v1",
            _shell_function(delta, "delta_dockerfile"),
            f"delta_dockerfile {shlex.quote(role)}",
        )
    )
    return subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)


def test_delta_build_layers_every_ship_role_and_refuses_any_other() -> None:
    for role in sourced_stack().roles:
        built = _delta_dockerfile(role)
        assert built.returncode == 0, built.stderr
        assert f"FROM memoria-{role}:20261013-guardian-summaries-v1\n" in built.stdout
        assert f'com.memoria.release.role="{role}"' in built.stdout

    unknown = _delta_dockerfile("media-edge")
    assert unknown.returncode == 1
    assert "no delta Dockerfile for role media-edge; run the full image build" in unknown.stderr


GUARDIAN = "20261013-guardian-summaries-v1"
GUARDIAN_COMMIT = "42a095d241a3b33d2bf6f35437d19da3fe937576"
HOST_BUILT = "20261016-readiness-b54189c"
HOST_BUILT_COMMIT = "b54189c5f13d240d24dbb10a6b9e604fb36b96d1"
RELEASE = "20261014-speaker-retire-v1"
NEW_COMMIT = "1" * 40
EDGE_TREE = "20261004-first-warm-v1"
EDGE_OVERRIDE = (
    "/opt/memoria/component-releases/20261002-late-progress-v1-media-edge/"
    "media-edge-component.override.yml"
)

COMPONENT_STACK = "20261020-minor-bypass-a94c438"
COMPONENT_STACK_COMMIT = "a94c43813a8893a07bbe3bf4815cae52745cd88a"
BRIDGE_IMAGE = "memoria-agent:20261021-a94c438-pr345-pr346-pr348-pr352-pr353-pr355"
CONTROL_IMAGE = "memoria-control-api:20261021-a94c438-pr345-pr346-pr348-pr352"
BRIDGE_OVERRIDE = (
    "component-releases/20261021-a94c438-pr345-pr346-pr348-pr352-pr353-pr355/override-pr355.yml"
)
CONTROL_OVERRIDE = (
    "component-releases/20261021-a94c438-pr345-pr346-pr348-pr352/override-control-pr352.yml"
)
BRIDGE_OVERRIDE_SHA = "895b17fefa3401e75ba17e1c3f08aac9cab920f7b25e55aa540ffcc5a7a22da0"
CONTROL_OVERRIDE_SHA = "95a5e03319cafcf7fcf7325f00b2cc753ea444c563bfd5de5472692ce1d67486"
STEPFUN_CONTROL_IMAGE = "memoria-control-api:20261021-stepfun-c272af5"
STEPFUN_BRIDGE_IMAGE = "memoria-agent:20261021-stepfun-c272af5"
STEPFUN_CONTROL_OVERRIDE = (
    "component-releases/20261021-stepfun-c272af5/override-control-stepfun-c272af5.yml"
)
STEPFUN_BRIDGE_OVERRIDE = (
    "component-releases/20261021-stepfun-c272af5/override-bridge-stepfun-c272af5.yml"
)
STEPFUN_CONTROL_OVERRIDE_SHA = (
    "53db50c436f582120ea3a292be1606f11fed46e5e329e30e73f4d55caba97204"
)
STEPFUN_BRIDGE_OVERRIDE_SHA = (
    "a1b51ef44d4f87160649ee2015de815089a27f15d27a88d678196ef507fa5c3c"
)
PR368_CONTROL_IMAGE = "memoria-control-api:20261021-pr368-dd98c4a"
PR368_CONTROL_OVERRIDE = (
    "component-releases/20261021-pr368-dd98c4a/override-control-pr368-dd98c4a.yml"
)
PR368_CONTROL_OVERRIDE_SHA = (
    "bde575b693d379e47af7bbfbcbbcd857487532b78faef2d2957b4facaf1217c0"
)
MAIN_F14BE64_BRIDGE_IMAGE = "memoria-agent:20261021-main-f14be64"
MAIN_F14BE64_BRIDGE_OVERRIDE = (
    "component-releases/20261021-main-f14be64/override-bridge-20261021-main-f14be64.yml"
)
MAIN_F14BE64_BRIDGE_OVERRIDE_SHA = (
    "4cfa1733236927709435c3f068276835a009de8e3603ba2bba08571cc96610cf"
)

_HOST_REPO = {
    "speaker-model": "memoria-speaker-model",
    "control-api": "memoria-control-api",
    "voice-core-media-bridge": "memoria-agent",
    "media-slo-reporter": "memoria-agent",
    "device-state-redis": "redis",
    "media-edge": "memoria-media-edge",
}

_RECORD = """
import json, os, sys
fake = os.environ["FAKE_ROOT"]
argv = sys.argv[1:]
with open(os.path.join(fake, "calls.jsonl"), "a", encoding="utf-8") as calls:
    calls.write(json.dumps({
        "argv": argv,
        "cwd": os.getcwd(),
        "release_tag": os.environ.get("MEMORIA_RELEASE_TAG"),
        "release_commit": os.environ.get("MEMORIA_RELEASE_COMMIT"),
    }) + "\\n")
failing = os.path.join(fake, "fail")
if os.path.exists(failing) and open(failing, encoding="utf-8").read() in " ".join(argv):
    sys.exit(1)
if argv[:2] == ["docker", "compose"] and "config" in argv:
    sys.stdout.write(open(os.path.join(fake, "compose-config.json"), encoding="utf-8").read())
"""

_COMPOSE_PROJECT_FLAGS = {"-p", "--project-directory", "-f", "--profile"}


@dataclass(frozen=True)
class ComposeCall:
    cwd: Path
    release_tag: str | None
    release_commit: str | None
    argv: tuple[str, ...]


@dataclass(frozen=True)
class StepRun:
    returncode: int
    output: str
    docker: list[str]
    actions: list[str]
    copies: list[str]
    compose_calls: list[ComposeCall]


def _step_run(result: subprocess.CompletedProcess[str], calls: list[dict[str, Any]]) -> StepRun:
    docker: list[str] = []
    actions: list[str] = []
    copies: list[str] = []
    compose_calls: list[ComposeCall] = []
    for call in calls:
        tool, *args = call["argv"]
        if tool == "cp":
            copies.append(" ".join(args))
        elif tool != "docker":
            actions.append(" ".join([tool, *args]))
        elif args[0] == "compose":
            docker.append(" ".join(args))
            compose_calls.append(
                ComposeCall(
                    Path(call["cwd"]), call["release_tag"], call["release_commit"], tuple(args)
                )
            )
            command = args[1:]
            while command[0] in _COMPOSE_PROJECT_FLAGS:
                command = command[2:]
            if command[0] in {"up", "run", "stop"}:
                actions.append(" ".join(["compose", *command]))
        else:
            docker.append(" ".join(args))
            if args[0] in {"tag", "stop", "exec"}:
                actions.append(" ".join(args))
    return StepRun(
        result.returncode, result.stdout + result.stderr, docker, actions, copies, compose_calls
    )


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class FakeHost:
    def __init__(self, root: Path, *, current: str) -> None:
        self.root = root
        self.memoria = root / "opt" / "memoria"
        self.tmp = root / "tmp"
        self.fake = root / "fake"
        self.bin = root / "bin"
        (self.fake / "containers").mkdir(parents=True)
        self.tmp.mkdir()
        self.bin.mkdir()
        _executable(self.bin / "record", f"#!{sys.executable}\n{_RECORD}")
        _executable(self.bin / "docker", '#!/usr/bin/env bash\nexec record docker "$@"\n')
        self.set_current(current)

    def release_dir(self, tag: str) -> Path:
        path = self.memoria / "releases" / tag
        if not path.exists():
            (path / "scripts").mkdir(parents=True)
            (path / "docker-compose.production.yml").write_text("services: {}\n", encoding="utf-8")
            _executable(
                path / "scripts" / "refresh_readiness.sh",
                '#!/usr/bin/env bash\nexec record refresh "$MEMORIA_RELEASE_TAG"\n',
            )
        return path

    def set_current(self, tag: str) -> None:
        link = self.memoria / "current"
        link.unlink(missing_ok=True)
        link.symlink_to(self.release_dir(tag))

    def current_target(self) -> Path:
        return Path(os.readlink(self.memoria / "current"))

    def override_file(self, relative: str, service: str, image: str) -> Path:
        assert not Path(relative).is_absolute(), "override path must stay under the fake root"
        path = self.memoria / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"services:\n  {service}:\n    image: {image}\n", encoding="utf-8")
        return path

    def tmp_override(self, name: str, service: str, image: str) -> Path:
        assert not Path(name).is_absolute(), "tmp override must stay under the fake /tmp"
        path = self.tmp / name
        path.write_text(f"services:\n  {service}:\n    image: {image}\n", encoding="utf-8")
        return path

    def container(
        self,
        service: str,
        *,
        release: str,
        commit: str,
        running: bool = True,
        chain: str | None = None,
        version: str | None = None,
        release_tag: str | None = None,
        image: str | None = None,
        image_id: str | None = None,
    ) -> None:
        facts = {
            "chain": chain or str(self.release_dir(release) / "docker-compose.production.yml"),
            "image": image or f"{_HOST_REPO[service]}:{release}",
            "image-id": image_id or f"sha-{service}",
            "running": "true" if running else "false",
            "version": release if version is None else version,
            "revision": commit,
            "release-tag": release_tag or "",
        }
        box = self.fake / "containers" / f"memoria-{service}-1"
        box.mkdir(exist_ok=True)
        for field, value in facts.items():
            (box / field).write_text(value, encoding="utf-8")

    def candidate_compose(self, services: dict[str, dict[str, object]]) -> None:
        (self.fake / "compose-config.json").write_text(
            json.dumps({"services": services}), encoding="utf-8"
        )

    def fail_on(self, docker_args: str) -> None:
        (self.fake / "fail").write_text(docker_args, encoding="utf-8")

    def prev_record(self, tag: str) -> str | None:
        record = self.release_dir(tag) / ".cutover" / "prev-release"
        return record.read_text(encoding="utf-8") if record.exists() else None

    def step(self, name: str, *, tag: str, commit: str = NEW_COMMIT) -> StepRun:
        self.release_dir(tag)
        calls = self.fake / "calls.jsonl"
        calls.write_text("", encoding="utf-8")
        containers = shlex.quote(str(self.fake / "containers"))
        program = "\n".join(
            (
                "set -Eeuo pipefail",
                f"export TAG={tag} COMMIT={commit}",
                f"export RELEASE_OPS_TMP_ROOT={shlex.quote(str(self.tmp))}",
                f"source {shlex.quote(str(SCRIPT))}",
                f"release_paths {shlex.quote(str(self.memoria))}",
                f'inspect_container() {{ cat {containers}/"$1/$2" 2>/dev/null; }}',
                'cp() { record cp "$@"; }',
                'wait_healthy() { record wait "$1"; }',
                # GNU mv -T is not on every developer host; os.replace keeps its replace-not-move-into semantics.
                'mv() { if [[ "$1" == -T ]]; then shift; '
                'python3 -c "import os,sys; os.replace(sys.argv[1], sys.argv[2])" "$1" "$2"; '
                'else command mv "$@"; fi; }',
                f"main {name}",
            )
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("MEMORIA_")}
        env.update(PATH=f"{self.bin}:{env['PATH']}", FAKE_ROOT=str(self.fake))
        result = subprocess.run(
            ["bash", "-c", program],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        lines = calls.read_text(encoding="utf-8").splitlines()
        return _step_run(result, [json.loads(line) for line in lines])


def guardian_host(tmp_path: Path) -> FakeHost:
    host = FakeHost(tmp_path, current=GUARDIAN)
    for service in ("control-api", "voice-core-media-bridge", "speaker-model"):
        host.container(service, release=GUARDIAN, commit=GUARDIAN_COMMIT)
    for sidecar in ("media-slo-reporter", "device-state-redis"):
        host.container(sidecar, release=GUARDIAN, commit=GUARDIAN_COMMIT)
    host.container(
        "media-edge",
        release="20261002-late-progress-v1",
        commit="f3fe8742177e4002db652203b1fadffa549d1098",
        chain=f"{host.release_dir(EDGE_TREE) / 'docker-compose.production.yml'},{EDGE_OVERRIDE}",
    )
    return host


def _up(*services: str) -> str:
    return " ".join(("compose up -d --no-deps --no-build --force-recreate", *services))


def host_built_host(tmp_path: Path) -> FakeHost:
    host = FakeHost(tmp_path, current=HOST_BUILT)
    for service in ("control-api", "voice-core-media-bridge"):
        host.container(
            service,
            release=HOST_BUILT,
            commit=HOST_BUILT_COMMIT,
            version="",
            release_tag=HOST_BUILT,
        )
    return host


def component_host(tmp_path: Path) -> FakeHost:
    """The live stack after two component releases: base compose plus one
    image-only override per ship service, both still chained from /tmp."""

    host = FakeHost(tmp_path, current=COMPONENT_STACK)
    tree = host.release_dir(COMPONENT_STACK)
    (tree / ".env").write_text(
        f"MEMORIA_RELEASE_COMMIT={COMPONENT_STACK_COMMIT}\n", encoding="utf-8"
    )
    control = host.override_file(CONTROL_OVERRIDE, "control-api", CONTROL_IMAGE)
    bridge = host.override_file(BRIDGE_OVERRIDE, "voice-core-media-bridge", BRIDGE_IMAGE)
    control_origin = host.tmp_override("override-control-pr352.yml", "control-api", CONTROL_IMAGE)
    bridge_origin = host.tmp_override("override-pr355.yml", "voice-core-media-bridge", BRIDGE_IMAGE)
    base = tree / "docker-compose.production.yml"
    host.container(
        "control-api",
        release=COMPONENT_STACK,
        commit="b" * 40,
        version=CONTROL_IMAGE.split(":", 1)[1],
        chain=f"{base},{control_origin}",
        image=CONTROL_IMAGE,
        image_id="sha256:control-live",
    )
    host.container(
        "voice-core-media-bridge",
        release=COMPONENT_STACK,
        commit="d" * 40,
        version=BRIDGE_IMAGE.split(":", 1)[1],
        chain=f"{base},{bridge_origin}",
        image=BRIDGE_IMAGE,
        image_id="sha256:bridge-live",
    )
    assert control.read_text(encoding="utf-8") == (
        f"services:\n  control-api:\n    image: {CONTROL_IMAGE}\n"
    )
    assert bridge.read_text(encoding="utf-8") == (
        f"services:\n  voice-core-media-bridge:\n    image: {BRIDGE_IMAGE}\n"
    )
    return host


def _component_record(host: FakeHost) -> str:
    control = host.memoria / CONTROL_OVERRIDE
    bridge = host.memoria / BRIDGE_OVERRIDE
    return (
        f"tag={COMPONENT_STACK}\n"
        f"commit={COMPONENT_STACK_COMMIT}\n"
        "services=control-api voice-core-media-bridge\n"
        f"overrides=control-api {control} {CONTROL_OVERRIDE_SHA} sha256:control-live "
        f"voice-core-media-bridge {bridge} {BRIDGE_OVERRIDE_SHA} sha256:bridge-live\n"
    )


def _finish_this_release(host: FakeHost) -> None:
    host.set_current(RELEASE)
    for service in ("control-api", "voice-core-media-bridge"):
        host.container(service, release=RELEASE, commit=NEW_COMMIT)
    host.container("speaker-model", release=GUARDIAN, commit=GUARDIAN_COMMIT, running=False)


def test_freeze_derives_and_persists_prev_from_the_live_stack(tmp_path: Path) -> None:
    host = guardian_host(tmp_path)

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 0, run.output
    assert host.prev_record(RELEASE) == (
        "tag=20261013-guardian-summaries-v1\n"
        "commit=42a095d241a3b33d2bf6f35437d19da3fe937576\n"
        "services=control-api voice-core-media-bridge speaker-model\n"
    )
    assert [action for action in run.actions if action.startswith("tag ")] == [
        f"tag sha-control-api memoria-control-api:rollback-{RELEASE}-pre",
        f"tag sha-voice-core-media-bridge memoria-agent:rollback-{RELEASE}-pre",
        f"tag sha-speaker-model memoria-speaker-model:rollback-{RELEASE}-pre",
    ]
    assert (
        f"prev={GUARDIAN}@{GUARDIAN_COMMIT} services=control-api voice-core-media-bridge "
        "speaker-model" in run.output
    )


def test_the_override_whitelist_pins_every_persisted_copy() -> None:
    script = _script()
    for path, sha, origin in (
        (
            "/opt/memoria/component-releases/20261021-a94c438-pr345-pr346-pr348-pr352/"
            "override-control-pr352.yml",
            CONTROL_OVERRIDE_SHA,
            "/tmp/override-control-pr352.yml",
        ),
        (
            "/opt/memoria/component-releases/"
            "20261021-a94c438-pr345-pr346-pr348-pr352-pr353-pr355/override-pr355.yml",
            BRIDGE_OVERRIDE_SHA,
            "/tmp/override-pr355.yml",
        ),
        (
            "/opt/memoria/component-releases/20261021-stepfun-c272af5/"
            "override-control-stepfun-c272af5.yml",
            STEPFUN_CONTROL_OVERRIDE_SHA,
            "/tmp/override-control-stepfun-c272af5.yml",
        ),
        (
            "/opt/memoria/component-releases/20261021-stepfun-c272af5/"
            "override-bridge-stepfun-c272af5.yml",
            STEPFUN_BRIDGE_OVERRIDE_SHA,
            "/tmp/override-bridge-stepfun-c272af5.yml",
        ),
        (
            "/opt/memoria/component-releases/20261021-pr368-dd98c4a/"
            "override-control-pr368-dd98c4a.yml",
            PR368_CONTROL_OVERRIDE_SHA,
            "/tmp/override-control-pr368-dd98c4a.yml",
        ),
        (
            "/opt/memoria/component-releases/20261021-main-f14be64/"
            "override-bridge-20261021-main-f14be64.yml",
            MAIN_F14BE64_BRIDGE_OVERRIDE_SHA,
            "/tmp/override-bridge-20261021-main-f14be64.yml",
        ),
    ):
        assert path in script
        assert sha in script
        assert origin in script


def test_freeze_accepts_whitelisted_image_only_overrides(tmp_path: Path) -> None:
    host = component_host(tmp_path)

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 0, run.output
    assert host.prev_record(RELEASE) == _component_record(host)
    assert "PREV override changed" not in run.output


def test_freeze_reruns_a_component_prev_record_unchanged(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    assert host.step("freeze", tag=RELEASE).returncode == 0
    frozen = host.prev_record(RELEASE)

    again = host.step("freeze", tag=RELEASE)

    assert again.returncode == 0, again.output
    assert "prev already frozen (unchanged)" in again.output
    assert host.prev_record(RELEASE) == frozen


def test_freeze_refuses_an_override_path_outside_the_whitelist(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    base = host.release_dir(COMPONENT_STACK) / "docker-compose.production.yml"
    stray = host.tmp_override("override-pr999.yml", "voice-core-media-bridge", BRIDGE_IMAGE)
    host.container(
        "voice-core-media-bridge",
        release=COMPONENT_STACK,
        commit="d" * 40,
        chain=f"{base},{stray}",
        image=BRIDGE_IMAGE,
        image_id="sha256:bridge-live",
    )

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert "is not whitelisted" in run.output
    assert host.prev_record(RELEASE) is None
    assert run.actions == [] and run.copies == []


def test_freeze_refuses_an_override_that_is_not_image_only(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    base = host.release_dir(COMPONENT_STACK) / "docker-compose.production.yml"
    bridge = host.memoria / BRIDGE_OVERRIDE
    bridge.write_text(
        f"services:\n  voice-core-media-bridge:\n    image: {BRIDGE_IMAGE}\n"
        "    environment:\n      ENVIRONMENT: production\n",
        encoding="utf-8",
    )
    host.container(
        "voice-core-media-bridge",
        release=COMPONENT_STACK,
        commit="d" * 40,
        chain=f"{base},{bridge}",
        image=BRIDGE_IMAGE,
        image_id="sha256:bridge-live",
    )

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert "is not exactly services.<service>.image" in run.output
    assert host.prev_record(RELEASE) is None


def test_freeze_refuses_an_override_image_the_container_is_not_running(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    base = host.release_dir(COMPONENT_STACK) / "docker-compose.production.yml"
    control = host.memoria / CONTROL_OVERRIDE
    host.container(
        "control-api",
        release=COMPONENT_STACK,
        commit="b" * 40,
        chain=f"{base},{control}",
        image=f"{CONTROL_IMAGE}-stale",
        image_id="sha256:control-live",
    )

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert "selects memoria-control-api:20261021-a94c438-pr345-pr346-pr348-pr352" in run.output
    assert (
        "but the container runs memoria-control-api:20261021-a94c438-pr345-pr346-pr348-pr352-stale"
        in run.output
    )
    assert host.prev_record(RELEASE) is None


def test_freeze_refuses_a_chain_with_more_than_one_override(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    base = host.release_dir(COMPONENT_STACK) / "docker-compose.production.yml"
    control = host.memoria / CONTROL_OVERRIDE
    bridge = host.memoria / BRIDGE_OVERRIDE
    host.container(
        "control-api",
        release=COMPONENT_STACK,
        commit="b" * 40,
        chain=f"{base},{control},{bridge}",
        image=CONTROL_IMAGE,
        image_id="sha256:control-live",
    )

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert "unexpected live chain for memoria-control-api-1" in run.output
    assert host.prev_record(RELEASE) is None


def test_freeze_refuses_a_tmp_origin_without_the_persisted_copy(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    (host.memoria / CONTROL_OVERRIDE).unlink()

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert "PREV override is missing" in run.output
    assert host.prev_record(RELEASE) is None


def test_freeze_refuses_an_override_for_another_service(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    base = host.release_dir(COMPONENT_STACK) / "docker-compose.production.yml"
    host.container(
        "voice-core-media-bridge",
        release=COMPONENT_STACK,
        commit="d" * 40,
        chain=f"{base},{host.memoria / CONTROL_OVERRIDE}",
        image=BRIDGE_IMAGE,
        image_id="sha256:bridge-live",
    )

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert "covers control-api, not memoria-voice-core-media-bridge-1" in run.output
    assert host.prev_record(RELEASE) is None


def test_freeze_refuses_a_tmp_override_that_differs_from_the_persisted_copy(
    tmp_path: Path,
) -> None:
    host = component_host(tmp_path)
    origin = host.tmp / "override-control-pr352.yml"
    origin.write_text(
        f"services:\n  control-api:\n    image: {CONTROL_IMAGE}\n# drift\n", encoding="utf-8"
    )

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert "does not match the persisted copy" in run.output
    assert host.prev_record(RELEASE) is None


def test_freeze_accepts_a_persisted_override_path(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    base = host.release_dir(COMPONENT_STACK) / "docker-compose.production.yml"
    for service, relative, image, image_id in (
        ("control-api", CONTROL_OVERRIDE, CONTROL_IMAGE, "sha256:control-live"),
        (
            "voice-core-media-bridge",
            BRIDGE_OVERRIDE,
            BRIDGE_IMAGE,
            "sha256:bridge-live",
        ),
    ):
        host.container(
            service,
            release=COMPONENT_STACK,
            commit="e" * 40,
            chain=f"{base},{host.memoria / relative}",
            image=image,
            image_id=image_id,
        )

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 0, run.output
    assert host.prev_record(RELEASE) == _component_record(host)


def test_freeze_accepts_the_stepfun_whitelist_entries(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    base = host.release_dir(COMPONENT_STACK) / "docker-compose.production.yml"
    control = host.override_file(
        STEPFUN_CONTROL_OVERRIDE, "control-api", STEPFUN_CONTROL_IMAGE
    )
    bridge = host.override_file(
        STEPFUN_BRIDGE_OVERRIDE, "voice-core-media-bridge", STEPFUN_BRIDGE_IMAGE
    )
    control_origin = host.tmp_override(
        "override-control-stepfun-c272af5.yml", "control-api", STEPFUN_CONTROL_IMAGE
    )
    bridge_origin = host.tmp_override(
        "override-bridge-stepfun-c272af5.yml",
        "voice-core-media-bridge",
        STEPFUN_BRIDGE_IMAGE,
    )
    for service, image, image_id, origin in (
        ("control-api", STEPFUN_CONTROL_IMAGE, "sha256:control-live", control_origin),
        (
            "voice-core-media-bridge",
            STEPFUN_BRIDGE_IMAGE,
            "sha256:bridge-live",
            bridge_origin,
        ),
    ):
        host.container(
            service,
            release=COMPONENT_STACK,
            commit="e" * 40,
            chain=f"{base},{origin}",
            version="20261021-stepfun-c272af5",
            image=image,
            image_id=image_id,
        )

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 0, run.output
    record = host.prev_record(RELEASE)
    assert record is not None
    assert (
        f"overrides=control-api {control} {STEPFUN_CONTROL_OVERRIDE_SHA} sha256:control-live "
        f"voice-core-media-bridge {bridge} {STEPFUN_BRIDGE_OVERRIDE_SHA} sha256:bridge-live\n"
        in record
    )


@pytest.mark.parametrize(
    ("service", "persisted", "sha", "origin"),
    [
        (
            "control-api",
            "/opt/memoria/component-releases/20261021-pr368-dd98c4a/"
            "override-control-pr368-dd98c4a.yml",
            PR368_CONTROL_OVERRIDE_SHA,
            "/tmp/override-control-pr368-dd98c4a.yml",
        ),
        (
            "voice-core-media-bridge",
            "/opt/memoria/component-releases/20261021-main-f14be64/"
            "override-bridge-20261021-main-f14be64.yml",
            MAIN_F14BE64_BRIDGE_OVERRIDE_SHA,
            "/tmp/override-bridge-20261021-main-f14be64.yml",
        ),
    ],
)
def test_override_spec_recognizes_the_new_whitelist_entries(
    service: str, persisted: str, sha: str, origin: str
) -> None:
    program = "\n".join(
        (
            "set -Eeuo pipefail",
            "export TAG=t COMMIT=c",
            f"source {shlex.quote(str(SCRIPT))}",
            f"override_spec {shlex.quote(persisted)}",
            f"override_spec {shlex.quote(origin)}",
        )
    )

    run = subprocess.run(["bash", "-c", program], capture_output=True, text=True, check=False)

    assert run.returncode == 0, run.stderr
    assert run.stdout == f"{service}|{persisted}|{sha}|{origin}\n" * 2


def test_freeze_accepts_the_pr368_and_main_f14be64_whitelist_entries(
    tmp_path: Path,
) -> None:
    host = component_host(tmp_path)
    base = host.release_dir(COMPONENT_STACK) / "docker-compose.production.yml"
    control = host.override_file(
        PR368_CONTROL_OVERRIDE, "control-api", PR368_CONTROL_IMAGE
    )
    bridge = host.override_file(
        MAIN_F14BE64_BRIDGE_OVERRIDE,
        "voice-core-media-bridge",
        MAIN_F14BE64_BRIDGE_IMAGE,
    )
    control_origin = host.tmp_override(
        "override-control-pr368-dd98c4a.yml", "control-api", PR368_CONTROL_IMAGE
    )
    bridge_origin = host.tmp_override(
        "override-bridge-20261021-main-f14be64.yml",
        "voice-core-media-bridge",
        MAIN_F14BE64_BRIDGE_IMAGE,
    )
    for service, image, image_id, origin, version in (
        (
            "control-api",
            PR368_CONTROL_IMAGE,
            "sha256:control-live",
            control_origin,
            "20261021-pr368-dd98c4a",
        ),
        (
            "voice-core-media-bridge",
            MAIN_F14BE64_BRIDGE_IMAGE,
            "sha256:bridge-live",
            bridge_origin,
            "20261021-main-f14be64",
        ),
    ):
        host.container(
            service,
            release=COMPONENT_STACK,
            commit="e" * 40,
            chain=f"{base},{origin}",
            version=version,
            image=image,
            image_id=image_id,
        )

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 0, run.output
    record = host.prev_record(RELEASE)
    assert record is not None
    assert (
        f"overrides=control-api {control} {PR368_CONTROL_OVERRIDE_SHA} sha256:control-live "
        f"voice-core-media-bridge {bridge} {MAIN_F14BE64_BRIDGE_OVERRIDE_SHA} "
        "sha256:bridge-live\n" in record
    )


def test_freeze_reruns_converge_on_the_same_record(tmp_path: Path) -> None:
    host = guardian_host(tmp_path)
    assert host.step("freeze", tag=RELEASE).returncode == 0
    frozen = host.prev_record(RELEASE)

    again = host.step("freeze", tag=RELEASE)

    assert again.returncode == 0, again.output
    assert "prev already frozen (unchanged)" in again.output
    assert host.prev_record(RELEASE) == frozen


def test_freeze_after_cutover_refuses_before_any_write(tmp_path: Path) -> None:
    host = guardian_host(tmp_path)
    assert host.step("freeze", tag=RELEASE).returncode == 0
    frozen = host.prev_record(RELEASE)
    host.container("control-api", release=RELEASE, commit=NEW_COMMIT)

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert "unexpected live chain for memoria-control-api-1" in run.output
    assert host.prev_record(RELEASE) == frozen
    assert run.docker == [] and run.copies == []


def test_freeze_refuses_once_current_names_this_release(tmp_path: Path) -> None:
    host = guardian_host(tmp_path)
    host.set_current(RELEASE)

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert f"current already points at {RELEASE}; PREV would be this release" in run.output
    assert host.prev_record(RELEASE) is None


def test_freeze_refuses_prev_images_that_disagree_on_commit(tmp_path: Path) -> None:
    host = guardian_host(tmp_path)
    host.container("voice-core-media-bridge", release=GUARDIAN, commit="2" * 40)

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert f"PREV images disagree on commit: {GUARDIAN_COMMIT} vs {'2' * 40}" in run.output
    assert host.prev_record(RELEASE) is None


def test_freeze_accepts_empty_version_when_revision_and_release_tag_match(tmp_path: Path) -> None:
    host = host_built_host(tmp_path)

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 0, run.output
    for service in ("control-api", "voice-core-media-bridge"):
        assert (
            f"accepting empty version label for memoria-{service}-1: "
            "revision b54189c matches tag suffix" in run.output
        )
    assert host.prev_record(RELEASE) == (
        f"tag={HOST_BUILT}\n"
        f"commit={HOST_BUILT_COMMIT}\n"
        "services=control-api voice-core-media-bridge\n"
    )


def test_freeze_refuses_empty_version_when_tag_suffix_does_not_match(tmp_path: Path) -> None:
    host = host_built_host(tmp_path)
    host.container(
        "control-api",
        release=HOST_BUILT,
        commit="a" * 40,
        version="",
        release_tag=HOST_BUILT,
    )

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert f"memoria-control-api-1 runs , not {HOST_BUILT}" in run.output
    assert host.prev_record(RELEASE) is None


def test_freeze_refuses_empty_version_when_container_release_tag_differs(tmp_path: Path) -> None:
    host = host_built_host(tmp_path)
    host.container(
        "control-api",
        release=HOST_BUILT,
        commit=HOST_BUILT_COMMIT,
        version="",
        release_tag="20261015-other-v1",
    )

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert f"memoria-control-api-1 runs , not {HOST_BUILT}" in run.output
    assert host.prev_record(RELEASE) is None


def test_freeze_refuses_a_non_empty_version_that_differs(tmp_path: Path) -> None:
    host = host_built_host(tmp_path)
    host.container(
        "control-api",
        release=HOST_BUILT,
        commit=HOST_BUILT_COMMIT,
        version="20261015-other-v1",
        release_tag=HOST_BUILT,
    )

    run = host.step("freeze", tag=RELEASE)

    assert run.returncode == 1
    assert f"memoria-control-api-1 runs 20261015-other-v1, not {HOST_BUILT}" in run.output
    assert host.prev_record(RELEASE) is None


def test_cutover_recreates_the_ship_rows_then_stops_the_speaker_model(tmp_path: Path) -> None:
    host = guardian_host(tmp_path)
    host.step("freeze", tag=RELEASE)
    host.candidate_compose(_candidate(RELEASE, "8791"))

    run = host.step("cutover", tag=RELEASE)

    assert run.returncode == 0, run.output
    assert run.actions == [
        _up("control-api"),
        "wait memoria-control-api-1",
        _up("voice-core-media-bridge"),
        "wait memoria-voice-core-media-bridge-1",
        "stop memoria-speaker-model-1",
    ]
    assert {(c.cwd, c.release_tag, c.release_commit) for c in run.compose_calls} == {
        (host.release_dir(RELEASE), RELEASE, NEW_COMMIT)
    }


def test_cutover_rerun_leaves_the_stopped_speaker_model_alone(tmp_path: Path) -> None:
    host = guardian_host(tmp_path)
    host.step("freeze", tag=RELEASE)
    host.candidate_compose(_candidate(RELEASE, "8791"))
    host.container("speaker-model", release=GUARDIAN, commit=GUARDIAN_COMMIT, running=False)

    run = host.step("cutover", tag=RELEASE)

    assert run.returncode == 0, run.output
    assert run.actions == [
        _up("control-api"),
        "wait memoria-control-api-1",
        _up("voice-core-media-bridge"),
        "wait memoria-voice-core-media-bridge-1",
    ]


def test_cutover_stops_at_the_first_service_that_fails_to_come_up(tmp_path: Path) -> None:
    host = guardian_host(tmp_path)
    host.step("freeze", tag=RELEASE)
    host.candidate_compose(_candidate(RELEASE, "8791"))
    host.fail_on("up -d --no-deps --no-build --force-recreate control-api")

    run = host.step("cutover", tag=RELEASE)

    assert run.returncode != 0
    assert run.actions == [_up("control-api")]


def test_cutover_refuses_a_candidate_that_still_defines_a_retired_service(tmp_path: Path) -> None:
    host = guardian_host(tmp_path)
    host.step("freeze", tag=RELEASE)
    host.candidate_compose(
        {
            "speaker-model": {"image": f"memoria-speaker-model:{RELEASE}"},
            **_candidate(RELEASE, "8791"),
        }
    )

    run = host.step("cutover", tag=RELEASE)

    assert run.returncode == 1
    assert "retired services still defined: ['speaker-model']" in run.output
    assert run.actions == []


def test_rollback_restores_prev_from_the_frozen_record(tmp_path: Path) -> None:
    host = guardian_host(tmp_path)
    host.step("freeze", tag=RELEASE)
    _finish_this_release(host)
    prev_dir = host.release_dir(GUARDIAN)
    snapshots = host.release_dir(RELEASE) / ".cutover"

    run = host.step("rollback", tag=RELEASE)

    assert run.returncode == 0, run.output
    assert host.current_target() == prev_dir
    assert run.copies == [
        f"-p {snapshots}/memoria-control-api.env /etc/memoria-control-api.env",
        f"-p {snapshots}/memoria-agent.env /etc/memoria-agent.env",
    ]
    assert run.compose_calls == [
        ComposeCall(
            cwd=prev_dir,
            release_tag=GUARDIAN,
            release_commit=GUARDIAN_COMMIT,
            argv=(
                "compose", "-p", "memoria",
                "--project-directory", str(prev_dir),
                "-f", str(prev_dir / "docker-compose.production.yml"),
                "--profile", "media-runtime",
                "up", "-d", "--no-deps", "--no-build", "--force-recreate",
                "control-api", "voice-core-media-bridge", "speaker-model",
            ),
        )
    ]
    assert run.actions == [
        _up("control-api", "voice-core-media-bridge", "speaker-model"),
        "wait memoria-control-api-1",
        "wait memoria-voice-core-media-bridge-1",
        "wait memoria-speaker-model-1",
        f"refresh {GUARDIAN}",
    ]


def _component_cutover_done(host: FakeHost) -> None:
    host.set_current(RELEASE)
    for service, image in (
        ("control-api", CONTROL_IMAGE),
        ("voice-core-media-bridge", BRIDGE_IMAGE),
    ):
        host.container(
            service,
            release=RELEASE,
            commit=NEW_COMMIT,
            image=image,
            image_id="sha256:this-release",
        )


def test_rollback_composes_prev_with_each_persisted_override(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    assert host.step("freeze", tag=RELEASE).returncode == 0
    _component_cutover_done(host)
    prev_dir = host.release_dir(COMPONENT_STACK)
    base = prev_dir / "docker-compose.production.yml"
    control = host.memoria / CONTROL_OVERRIDE
    bridge = host.memoria / BRIDGE_OVERRIDE
    # Compose would recreate the containers on the recorded images.
    for service, image_id in (
        ("control-api", "sha256:control-live"),
        ("voice-core-media-bridge", "sha256:bridge-live"),
    ):
        host.container(
            service,
            release=COMPONENT_STACK,
            commit=COMPONENT_STACK_COMMIT,
            image_id=image_id,
        )

    run = host.step("rollback", tag=RELEASE)

    assert run.returncode == 0, run.output
    assert host.current_target() == prev_dir
    assert run.compose_calls == [
        ComposeCall(
            cwd=prev_dir,
            release_tag=COMPONENT_STACK,
            release_commit=COMPONENT_STACK_COMMIT,
            argv=(
                "compose",
                "-p",
                "memoria",
                "--project-directory",
                str(prev_dir),
                "-f",
                str(base),
                "-f",
                str(control),
                "--profile",
                "media-runtime",
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--force-recreate",
                "control-api",
            ),
        ),
        ComposeCall(
            cwd=prev_dir,
            release_tag=COMPONENT_STACK,
            release_commit=COMPONENT_STACK_COMMIT,
            argv=(
                "compose",
                "-p",
                "memoria",
                "--project-directory",
                str(prev_dir),
                "-f",
                str(base),
                "-f",
                str(bridge),
                "--profile",
                "media-runtime",
                "up",
                "-d",
                "--no-deps",
                "--no-build",
                "--force-recreate",
                "voice-core-media-bridge",
            ),
        ),
    ]
    assert run.actions == [
        _up("control-api"),
        _up("voice-core-media-bridge"),
        "wait memoria-control-api-1",
        "wait memoria-voice-core-media-bridge-1",
        f"refresh {COMPONENT_STACK}",
    ]


def test_rollback_refuses_a_persisted_override_that_changed(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    assert host.step("freeze", tag=RELEASE).returncode == 0
    _component_cutover_done(host)
    (host.memoria / CONTROL_OVERRIDE).write_text(
        f"services:\n  control-api:\n    image: {CONTROL_IMAGE}-changed\n", encoding="utf-8"
    )

    run = host.step("rollback", tag=RELEASE)

    assert run.returncode == 1
    assert "PREV override changed" in run.output
    assert host.current_target() == host.release_dir(RELEASE)
    assert run.copies == [] and run.docker == [] and run.actions == []


def test_rollback_refuses_a_missing_persisted_override(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    assert host.step("freeze", tag=RELEASE).returncode == 0
    _component_cutover_done(host)
    (host.memoria / BRIDGE_OVERRIDE).unlink()

    run = host.step("rollback", tag=RELEASE)

    assert run.returncode == 1
    assert "PREV override is missing" in run.output
    assert host.current_target() == host.release_dir(RELEASE)
    assert run.copies == [] and run.docker == [] and run.actions == []


def test_rollback_refuses_a_container_that_did_not_return_to_the_recorded_image(
    tmp_path: Path,
) -> None:
    host = component_host(tmp_path)
    assert host.step("freeze", tag=RELEASE).returncode == 0
    _component_cutover_done(host)
    for service in ("control-api", "voice-core-media-bridge"):
        host.container(
            service,
            release=COMPONENT_STACK,
            commit=COMPONENT_STACK_COMMIT,
            image_id="sha256:not-the-recorded-image",
        )

    run = host.step("rollback", tag=RELEASE)

    assert run.returncode == 1
    assert "runs image sha256:not-the-recorded-image, recorded sha256:control-live" in run.output
    assert "refresh" not in run.output


def test_rollback_refuses_a_record_that_names_a_tmp_original(tmp_path: Path) -> None:
    host = component_host(tmp_path)
    assert host.step("freeze", tag=RELEASE).returncode == 0
    _component_cutover_done(host)
    snapshots = host.release_dir(RELEASE) / ".cutover"
    (snapshots / "prev-release").write_text(
        _component_record(host).replace(
            str(host.memoria / CONTROL_OVERRIDE),
            str(host.tmp / "override-control-pr352.yml"),
        ),
        encoding="utf-8",
    )

    run = host.step("rollback", tag=RELEASE)

    assert run.returncode == 1
    assert "not the whitelisted copy" in run.output
    assert host.current_target() == host.release_dir(RELEASE)
    assert run.copies == [] and run.docker == [] and run.actions == []


def test_rollback_still_reads_an_old_record_without_overrides(tmp_path: Path) -> None:
    host = guardian_host(tmp_path)
    snapshots = host.release_dir(RELEASE) / ".cutover"
    snapshots.mkdir()
    (snapshots / "prev-release").write_text(
        f"tag={GUARDIAN}\ncommit={GUARDIAN_COMMIT}\n"
        "services=control-api voice-core-media-bridge speaker-model\n",
        encoding="utf-8",
    )
    host.set_current(RELEASE)
    for service in ("control-api", "voice-core-media-bridge", "speaker-model"):
        host.container(service, release=GUARDIAN, commit=GUARDIAN_COMMIT)

    run = host.step("rollback", tag=RELEASE)

    assert run.returncode == 0, run.output
    assert [len(call.argv) for call in run.compose_calls] == [17]
    assert not any("-f" in call.argv[9:] for call in run.compose_calls)


def test_the_next_release_leaves_out_the_stopped_speaker_model(tmp_path: Path) -> None:
    host = guardian_host(tmp_path)
    host.step("freeze", tag=RELEASE)
    _finish_this_release(host)
    following = "20261020-following-v1"

    freeze = host.step("freeze", tag=following)

    assert freeze.returncode == 0, freeze.output
    assert host.prev_record(following) == (
        f"tag={RELEASE}\ncommit={NEW_COMMIT}\nservices=control-api voice-core-media-bridge\n"
    )
    host.candidate_compose(_candidate(following, "8791"))
    cutover = host.step("cutover", tag=following)
    assert cutover.returncode == 0, cutover.output
    assert cutover.actions == [
        _up("control-api"),
        "wait memoria-control-api-1",
        _up("voice-core-media-bridge"),
        "wait memoria-voice-core-media-bridge-1",
    ]


@pytest.mark.parametrize("step", ["cutover", "finish", "rollback"])
def test_steps_that_need_prev_refuse_without_a_frozen_record(tmp_path: Path, step: str) -> None:
    host = guardian_host(tmp_path)

    run = host.step(step, tag=RELEASE)

    assert run.returncode == 1
    assert "run freeze first; the rollback target is unknown" in run.output
    assert run.docker == [] and run.copies == [] and run.actions == []
    assert host.current_target() == host.release_dir(GUARDIAN)


@pytest.mark.parametrize(
    ("record", "complaint"),
    [
        (f"tag={GUARDIAN}\ncommit=42a095d2\nservices=control-api\n", "commit=42a095d2"),
        (f"tag={GUARDIAN}\ncommit={GUARDIAN_COMMIT}\n", "malformed"),
        (
            f"tag={GUARDIAN}\ncommit={GUARDIAN_COMMIT}\nservices=control-api media-edge\n",
            "unknown service media-edge",
        ),
        (f"tag={RELEASE}\ncommit={GUARDIAN_COMMIT}\nservices=control-api\n", f"tag={RELEASE}"),
    ],
    ids=["short-commit", "no-services", "unknown-service", "self-tag"],
)
def test_rollback_refuses_a_malformed_record(tmp_path: Path, record: str, complaint: str) -> None:
    host = guardian_host(tmp_path)
    snapshots = host.release_dir(RELEASE) / ".cutover"
    snapshots.mkdir()
    (snapshots / "prev-release").write_text(record, encoding="utf-8")

    run = host.step("rollback", tag=RELEASE)

    assert run.returncode == 1
    assert "malformed" in run.output and complaint in run.output
    assert run.docker == [] and run.copies == []


def _ci_step(job: str, name: str) -> str:
    steps = yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"][job]["steps"]
    return str(next(step["run"] for step in steps if step.get("name") == name))


def test_ci_python_gates_run_these_tests() -> None:
    gates = _ci_step("python-gates", "Release tool gates (offline)")
    assert f"scripts/tests/{Path(__file__).name}" in gates.split()


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


def _resolve(
    tag: str, services: dict[str, dict[str, object]], control_port: str
) -> subprocess.CompletedProcess[str]:
    stack = sourced_stack()
    return subprocess.run(
        [sys.executable, "-c", _cutover_python()],
        input=json.dumps({"services": services}),
        env={
            **os.environ,
            "TAG": tag,
            "CONTROL_PORT": control_port,
            "SHIP_IMAGES": stack.ship_images,
            "RETIRED": " ".join(stack.retired),
        },
        capture_output=True,
        text=True,
    )


def test_cutover_refuses_a_candidate_that_would_publish_another_control_port() -> None:
    body = _function("step_cutover")
    assert 'CONTROL_PORT="$(control_api_port)"' in body
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
    assert "speaker-model" not in text
    for step in ("net", "data", "redis", "schema", "verify", "replay", "apps", "checks", "status", "down"):
        assert f"  {step}) step_{step} ;;" in text
    # Restored data is replayed through the Control API image, which now carries the module.
    assert "-m scripts.replay_subject_deletions --confirm-replay" in text
    dockerfile = (ROOT / "infra" / "Dockerfile.control-api").read_text(encoding="utf-8")
    assert "scripts/replay_subject_deletions.py" in dockerfile
    # Env files are named by path only; no secret value belongs in a checked-in helper.
    assert not re.search(r"(?im)^\s*(?:export\s+)?[A-Z_]*(?:PASSWORD|SECRET|TOKEN|KEY)[A-Z_]*=\S+", text)
