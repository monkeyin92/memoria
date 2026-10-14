from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from scripts.cleanup_freeze_dumps import (
    DEFAULT_RETENTION_DAYS,
    expired_freeze_dumps,
    is_freeze_dump_name,
    list_freeze_dump_candidates,
    main,
)


def _touch(path: Path, *, age_days: float, now: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"dump")
    mtime = now - age_days * 86400
    os.utime(path, (mtime, mtime))
    return path


def test_freeze_dump_name_patterns() -> None:
    assert is_freeze_dump_name("memoria-pre-20261010-n8-endpoint-diag-v1.dump")
    assert is_freeze_dump_name("memoria.pre-20261010-n8-endpoint-diag-v1.sqlite3")
    assert not is_freeze_dump_name("prev-release")
    assert not is_freeze_dump_name("memoria-pre-.dump")
    assert not is_freeze_dump_name("memoria.sqlite3")
    assert not is_freeze_dump_name("memoria-pre-foo.dump.bak")


def test_list_candidates_only_under_release_cutover(tmp_path: Path) -> None:
    now = time.time()
    keep = _touch(
        tmp_path / "releases" / "a" / ".cutover" / "memoria-pre-a.dump",
        age_days=1,
        now=now,
    )
    sqlite = _touch(
        tmp_path / "releases" / "a" / ".cutover" / "memoria.pre-a.sqlite3",
        age_days=1,
        now=now,
    )
    _touch(tmp_path / "releases" / "a" / "memoria-pre-outside.dump", age_days=10, now=now)
    _touch(tmp_path / "incoming" / "memoria-pre-incoming.dump", age_days=10, now=now)
    _touch(tmp_path / "releases" / "a" / ".cutover" / "prev-release", age_days=10, now=now)
    (tmp_path / "releases" / "a" / ".cutover" / "notes.txt").write_text("x", encoding="utf-8")

    found = list_freeze_dump_candidates(tmp_path)
    assert found == [keep, sqlite]


def test_expired_selection_keeps_fresh_newest(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    fresh = tmp_path / "fresh.dump"
    old = tmp_path / "old.dump"
    older = tmp_path / "older.sqlite3"
    mtimes = {
        fresh: now - 1 * 86400,
        old: now - 3 * 86400 - 1,
        older: now - 10 * 86400,
    }
    expired = expired_freeze_dumps(
        [fresh, old, older],
        now=now,
        retention_days=DEFAULT_RETENTION_DAYS,
        mtimes=mtimes,
    )
    assert expired == [old, older]


def test_exactly_three_days_old_is_kept() -> None:
    now = 1_700_000_000.0
    boundary = Path("/virtual/boundary.dump")
    expired = expired_freeze_dumps(
        [boundary],
        now=now,
        retention_days=3,
        mtimes={boundary: now - 3 * 86400},
    )
    assert expired == []


def test_apply_deletes_only_expired_under_releases(tmp_path: Path) -> None:
    now = time.time()
    fresh = _touch(
        tmp_path / "releases" / "new" / ".cutover" / "memoria-pre-new.dump",
        age_days=1,
        now=now,
    )
    stale_dump = _touch(
        tmp_path / "releases" / "old" / ".cutover" / "memoria-pre-old.dump",
        age_days=4,
        now=now,
    )
    stale_sqlite = _touch(
        tmp_path / "releases" / "old" / ".cutover" / "memoria.pre-old.sqlite3",
        age_days=5,
        now=now,
    )
    sibling = _touch(
        tmp_path / "releases" / "old" / ".cutover" / "pre-state.txt",
        age_days=5,
        now=now,
    )

    assert main(["--root", str(tmp_path), "--apply", "--retention-days", "3"]) == 0
    assert fresh.is_file()
    assert not stale_dump.exists()
    assert not stale_sqlite.exists()
    assert sibling.is_file()


def test_dry_run_lists_without_deleting(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    now = time.time()
    stale = _touch(
        tmp_path / "releases" / "old" / ".cutover" / "memoria-pre-old.dump",
        age_days=9,
        now=now,
    )
    assert main(["--root", str(tmp_path), "--retention-days", "3"]) == 0
    assert stale.is_file()
    out = capsys.readouterr().out
    assert str(stale) in out
    assert "dry-run" in out


def test_current_symlink_does_not_double_delete(tmp_path: Path) -> None:
    now = time.time()
    release = tmp_path / "releases" / "20261010-n8-endpoint-diag-v1"
    stale = _touch(
        release / ".cutover" / "memoria-pre-old.dump",
        age_days=5,
        now=now,
    )
    (tmp_path / "releases" / "current").symlink_to(release, target_is_directory=True)

    found = list_freeze_dump_candidates(tmp_path)
    assert len(found) == 1
    assert found[0].resolve() == stale.resolve()

    assert main(["--root", str(tmp_path), "--apply", "--retention-days", "3"]) == 0
    assert not stale.exists()


def test_missing_releases_directory_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty_root = tmp_path / "empty-host"
    empty_root.mkdir()
    assert main(["--root", str(empty_root), "--apply"]) == 0
    out = capsys.readouterr().out
    assert "no releases directory" in out
    assert "nothing to clean" in out
