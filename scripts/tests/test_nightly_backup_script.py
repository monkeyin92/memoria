"""nightly_backup.sh run against a fake docker, df and container filesystem.

The production host has no pg_restore and no sqlite3 binary, so the fake
docker only accepts a dump that arrives on stdin, as the real container
would. A script that validates on the host fails here the way it would there.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "nightly_backup.sh"

FAKE_DOCKER = r"""#!/usr/bin/env bash
set -u
echo "docker $*" >> "$FAKE_ROOT/calls.log"
box="$FAKE_ROOT/container/tmp"
mkdir -p "$box"
if [[ "$1" == exec ]]; then
  shift
  [[ "$1" == -i ]] && shift
  name="$1"; shift
  case "$name $1" in
    "memoria-data-postgres-1 pg_dump")
      case "${FAKE_PGDUMP:-ok}" in
        fail) echo "pg_dump: connection refused" >&2; exit 1 ;;
        tiny) printf 'PGDMP' ; exit 0 ;;
        *) printf 'PGDMP'; head -c 4096 /dev/zero | tr '\0' 'x'; exit 0 ;;
      esac ;;
    "memoria-data-postgres-1 pg_restore")
      body="$(cat)"
      [[ "$body" == PGDMP* ]] || { echo "pg_restore: no dump on stdin" >&2; exit 1; }
      [[ "${FAKE_PGRESTORE:-ok}" == fail ]] && { echo "pg_restore: bad archive" >&2; exit 1; }
      exit 0 ;;
    "memoria-control-api-1 /app/.venv/bin/python")
      dest="$(printf '%s' "$*" | grep -o "/tmp/memoria-backup-[0-9-]*\.sqlite3")"
      target="$box/$(basename "$dest")"
      if [[ "${FAKE_SQLITE:-ok}" == corrupt ]]; then
        head -c 4096 /dev/urandom > "$target"
      else
        python3 -c "
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.execute('create table t (a text)')
c.executemany('insert into t values (?)', [(str(i) * 40,) for i in range(200)])
c.commit()
" "$target"
      fi
      exit 0 ;;
    "memoria-control-api-1 rm")
      shift; shift
      rm -f "$box/$(basename "${@: -1}")"
      exit 0 ;;
  esac
  echo "fake docker: unexpected exec: $name $*" >&2
  exit 99
fi
if [[ "$1" == cp ]]; then
  src="${2#memoria-control-api-1:}"
  cp "$box/$(basename "$src")" "$3"
  exit 0
fi
echo "fake docker: unexpected: $*" >&2
exit 99
"""

FAKE_DF = """#!/usr/bin/env bash
printf 'Filesystem 1K-blocks Used Available Use%% Mounted on\\n'
printf '/dev/fake 41943040 20971520 %s 50%% /\\n' "${FAKE_AVAIL_KB:-9000000}"
"""


class Sandbox:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "fake"
        self.bin = tmp_path / "bin"
        self.backups = tmp_path / "backups"
        self.root.mkdir()
        self.bin.mkdir()
        self.backups.mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("df", FAKE_DF)):
            path = self.bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        merged = {
            **os.environ,
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "FAKE_ROOT": str(self.root),
            "NIGHTLY_BACKUP_DIR": str(self.backups),
            **env,
        }
        return subprocess.run(
            ["bash", str(SCRIPT)], env=merged, capture_output=True, text=True, timeout=60
        )

    def names(self) -> list[str]:
        return sorted(p.name for p in self.backups.iterdir())

    def calls(self) -> str:
        log = self.root / "calls.log"
        return log.read_text(encoding="utf-8") if log.exists() else ""

    def leftover_container_files(self) -> list[str]:
        box = self.root / "container" / "tmp"
        return sorted(p.name for p in box.iterdir()) if box.exists() else []

    def seed_old(self, *names: str) -> None:
        for name in names:
            (self.backups / name).write_bytes(b"old backup" * 20)


@pytest.fixture()
def box(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def test_script_is_valid_bash() -> None:
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_success_writes_both_backups_and_prunes_old_ones(box: Sandbox) -> None:
    box.seed_old("memoria-20200101-0300.dump", "control-20200101-0300.sqlite3")

    result = box.run()

    assert result.returncode == 0, result.stderr
    names = box.names()
    assert sum(n.startswith("memoria-2") and n.endswith(".dump") for n in names) == 1
    assert sum(n.startswith("control-2") and n.endswith(".sqlite3") for n in names) == 1
    assert not any(n.endswith(".tmp") for n in names)
    assert "memoria-20200101-0300.dump" not in names
    assert "control-20200101-0300.sqlite3" not in names
    assert box.leftover_container_files() == []


def test_dump_is_validated_from_stdin_not_from_a_host_path(box: Sandbox) -> None:
    result = box.run()

    assert result.returncode == 0, result.stderr
    restore = [line for line in box.calls().splitlines() if "pg_restore" in line]
    assert restore == ["docker exec -i memoria-data-postgres-1 pg_restore --list"]


def test_backups_are_private(box: Sandbox) -> None:
    result = box.run()

    assert result.returncode == 0, result.stderr
    for path in box.backups.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


def test_low_disk_skips_the_backup_but_still_prunes_and_fails(box: Sandbox) -> None:
    box.seed_old(
        "memoria-20200101-0300.dump",
        "memoria-20200102-0300.dump",
        "control-20200101-0300.sqlite3",
        "control-20200102-0300.sqlite3",
    )

    result = box.run(FAKE_AVAIL_KB="1000")

    assert result.returncode != 0
    assert "insufficient disk space" in result.stderr
    assert "pg_dump" not in box.calls()
    assert box.names() == ["control-20200102-0300.sqlite3", "memoria-20200102-0300.dump"]


def test_pg_dump_failure_fails_and_leaves_nothing(box: Sandbox) -> None:
    result = box.run(FAKE_PGDUMP="fail")

    assert result.returncode != 0
    assert "pg_dump failed" in result.stderr
    assert box.names() == []


def test_tiny_dump_is_rejected(box: Sandbox) -> None:
    result = box.run(FAKE_PGDUMP="tiny")

    assert result.returncode != 0
    assert box.names() == []


def test_dump_that_pg_restore_cannot_list_is_removed_and_stops_the_run(box: Sandbox) -> None:
    result = box.run(FAKE_PGRESTORE="fail")

    assert result.returncode != 0
    assert "validation failed" in result.stderr
    assert box.names() == []
    assert "/app/.venv/bin/python" not in box.calls()
    assert "docker cp" not in box.calls()


def test_corrupt_sqlite_copy_fails_and_cleans_up(box: Sandbox) -> None:
    result = box.run(FAKE_SQLITE="corrupt")

    assert result.returncode != 0
    assert "SQLite backup validation failed" in result.stderr
    names = box.names()
    assert not any(n.startswith("control-") for n in names)
    assert not any(n.endswith(".tmp") for n in names)
    assert box.leftover_container_files() == []


def test_a_failed_run_never_prunes_existing_backups(box: Sandbox) -> None:
    box.seed_old("memoria-20200101-0300.dump", "memoria-20200102-0300.dump")

    result = box.run(FAKE_PGDUMP="fail")

    assert result.returncode != 0
    assert box.names() == ["memoria-20200101-0300.dump", "memoria-20200102-0300.dump"]
