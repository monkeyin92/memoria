from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "docker-compose.production.yml"
OVERRIDE = ROOT / "docker-compose.production.keepalive-180.override.yml"
BRIDGE = "voice-core-media-bridge"
TIMEOUT_KEY = "MEDIA_OWNER_SILENCE_TIMEOUT_S"
BASE_TIMEOUT = "30"
OVERRIDE_TIMEOUT = "180"
_ENV_FILE_ITEM = re.compile(
    r"(?m)^(?P<prefix>\s+-\s+)/etc/memoria-[A-Za-z0-9._-]+\.env\s*$"
)
_ASSIGNMENT = re.compile(r"=")


def _compose_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", "/tmp"),
        "MEMORIA_RELEASE_TAG": "keepalive-check",
        "MEMORIA_RELEASE_COMMIT": "0" * 40,
    }


def _redact(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if _ASSIGNMENT.search(line):
            left, _sep, _right = line.partition("=")
            lines.append(f"{left}=[redacted]")
        else:
            lines.append(line)
    return "\n".join(lines)


@functools.lru_cache(maxsize=1)
def compose_command() -> tuple[str, ...]:
    docker = shutil.which("docker")
    if docker is not None:
        probe = subprocess.run(
            [docker, "compose", "version"],
            check=False,
            capture_output=True,
            text=True,
            env=_compose_env(),
        )
        if probe.returncode == 0:
            return (docker, "compose")
    standalone = shutil.which("docker-compose")
    if standalone is not None:
        return (standalone,)
    raise SystemExit("docker compose is not installed")


def _sandbox_base(text: str, empty_env: Path) -> str:
    if text.count(f'{TIMEOUT_KEY}: "{BASE_TIMEOUT}"') != 1:
        raise SystemExit("base compose does not pin one 30s keepalive")
    if f'{TIMEOUT_KEY}: "{OVERRIDE_TIMEOUT}"' in text:
        raise SystemExit("base compose already pins 180")

    def replace(match: re.Match[str]) -> str:
        return f"{match.group('prefix')}{empty_env}"

    rewritten, count = _ENV_FILE_ITEM.subn(replace, text)
    if count == 0 or _ENV_FILE_ITEM.search(rewritten):
        raise SystemExit("production compose env files were not sandboxed")
    if f'{TIMEOUT_KEY}: "{BASE_TIMEOUT}"' not in rewritten:
        raise SystemExit("sandbox changed the base keepalive")
    return rewritten


def _timeout(document: object) -> str:
    if not isinstance(document, dict):
        raise SystemExit("docker compose config returned an unexpected document")
    services = document.get("services")
    if not isinstance(services, dict):
        raise SystemExit("rendered compose has no services")
    service = services.get(BRIDGE)
    if not isinstance(service, dict):
        raise SystemExit("rendered compose has no voice-core-media-bridge")
    environment = service.get("environment")
    value: object = None
    if isinstance(environment, dict):
        value = environment.get(TIMEOUT_KEY)
    elif isinstance(environment, list):
        prefix = f"{TIMEOUT_KEY}="
        for item in environment:
            if isinstance(item, str) and item.startswith(prefix):
                value = item[len(prefix) :]
                break
    if value not in {BASE_TIMEOUT, OVERRIDE_TIMEOUT}:
        raise SystemExit("rendered keepalive timeout is not 30 or 180")
    return str(value)


def render_timeout(*, with_override: bool) -> str:
    if with_override and not OVERRIDE.is_file():
        raise SystemExit("keepalive override missing")
    base_text = BASE.read_text(encoding="utf-8")
    command = compose_command()
    with tempfile.TemporaryDirectory(prefix="keepalive-check-") as raw:
        tmp = Path(raw)
        empty = tmp / "empty.env"
        empty.write_text("", encoding="utf-8")
        sandbox = tmp / "docker-compose.production.yml"
        sandbox.write_text(_sandbox_base(base_text, empty), encoding="utf-8")
        args = [
            *command,
            "--project-directory",
            str(tmp),
            "--env-file",
            str(empty),
            "-f",
            str(sandbox),
        ]
        if with_override:
            args.extend(["-f", str(OVERRIDE)])
        args.extend(
            ["--profile", "media-runtime", "config", "--format", "json"]
        )
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            env=_compose_env(),
        )
        if completed.returncode != 0:
            detail = _redact(completed.stderr).strip()
            message = "docker compose config failed"
            if detail:
                message = f"{message}\n{detail}"
            raise SystemExit(message)
        try:
            document = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SystemExit("docker compose config did not return JSON") from exc
        return _timeout(document)


def main() -> int:
    applied = render_timeout(with_override=True)
    baseline = render_timeout(with_override=False)
    if applied != OVERRIDE_TIMEOUT or baseline != BASE_TIMEOUT:
        raise SystemExit("rendered keepalive timeout mismatch")
    print("keepalive_timeout=180")
    print("base_keepalive_timeout=30")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
