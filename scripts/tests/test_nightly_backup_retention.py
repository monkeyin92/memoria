from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from scripts.nightly_backup_retention import (
    backups_to_delete,
    date_to_datetime,
    delete_paths,
    list_backup_candidates,
    main,
    parse_backup_filename,
    should_keep,
    stale_backups,
)

ROOT = Path(__file__).resolve().parents[2]
CST = timezone(timedelta(hours=8))


def _backup_name(kind: str, when: datetime) -> str:
    stamp = when.strftime("%Y%m%d-%H%M")
    if kind == "dump":
        return f"memoria-{stamp}.dump"
    return f"control-{stamp}.sqlite3"


def _write_backup(backup_dir: Path, kind: str, when: datetime) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / _backup_name(kind, when)
    path.write_bytes(b"backup")
    return path


def _touch(path: Path, *, date_int: int, time_int: int) -> Path:
    """Create a backup file with specific date/time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"backup")
    return path


class TestParseBackupFilename:
    def test_postgresql_dump(self) -> None:
        parsed = parse_backup_filename("memoria-20261010-1515.dump")
        assert parsed == ("memoria-20261010-1515.dump", 20261010, 1515)

    def test_sqlite_backup(self) -> None:
        parsed = parse_backup_filename("control-20261010-1515.sqlite3")
        assert parsed == ("control-20261010-1515.sqlite3", 20261010, 1515)

    def test_invalid_name(self) -> None:
        assert parse_backup_filename("memoria-20261010.dump") is None
        assert parse_backup_filename("memoria.sqlite3") is None
        assert parse_backup_filename("random-file.txt") is None


class TestDateConversion:
    def test_date_to_datetime(self) -> None:
        dt = date_to_datetime(20261010, 1515)
        assert dt.year == 2026
        assert dt.month == 10
        assert dt.day == 10
        assert dt.hour == 15
        assert dt.minute == 15
        assert dt.tzinfo == timezone(timedelta(hours=8))  # CST

    def test_midnight(self) -> None:
        dt = date_to_datetime(20261010, 0)
        assert dt.hour == 0
        assert dt.minute == 0


class TestRetentionPolicy:
    def _now(self, offset_days: int = 0) -> datetime:
        """Create a reference 'now' time (3:15 AM CST)."""
        base = datetime(2026, 10, 20, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))
        return base + timedelta(days=offset_days)

    def test_day_1_kept(self) -> None:
        """Day 1 backup should be kept."""
        now = self._now()
        yesterday = now - timedelta(days=1)
        date_int = int(yesterday.strftime("%Y%m%d"))
        time_int = int(yesterday.strftime("%H%M"))
        assert should_keep("memoria-20261019-0315.dump", date_int, time_int, now)

    def test_age_6_days_kept(self) -> None:
        """The last kept backup is the one less than 7 full days old."""
        now = self._now()
        six_days_ago = now - timedelta(days=6)
        date_int = int(six_days_ago.strftime("%Y%m%d"))
        time_int = int(six_days_ago.strftime("%H%M"))
        assert should_keep("memoria-20261014-0315.dump", date_int, time_int, now)

    def test_age_7_days_deleted(self) -> None:
        """Exactly 7 full days old is deleted, whatever the weekday."""
        now = self._now()
        seven_days_ago = now - timedelta(days=7)
        date_int = int(seven_days_ago.strftime("%Y%m%d"))
        time_int = int(seven_days_ago.strftime("%H%M"))
        assert not should_keep("memoria-20261013-0315.dump", date_int, time_int, now)

    def test_sunday_age_10_days_deleted(self) -> None:
        """Sundays get no extension: 10 days old is deleted."""
        # 2026-10-21 is a Wednesday; 10 days before is Sunday 2026-10-11.
        now = datetime(2026, 10, 21, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))
        ten_days_ago = now - timedelta(days=10)
        assert ten_days_ago.weekday() == 6
        date_int = int(ten_days_ago.strftime("%Y%m%d"))
        time_int = int(ten_days_ago.strftime("%H%M"))
        assert not should_keep("memoria-20261011-0315.dump", date_int, time_int, now)

    def test_day_31_deleted(self) -> None:
        """Old backups are deleted regardless of weekday."""
        now = self._now()
        month_ago = now - timedelta(days=31)
        date_int = int(month_ago.strftime("%Y%m%d"))
        time_int = int(month_ago.strftime("%H%M"))
        assert not should_keep("memoria-old.dump", date_int, time_int, now)

    def test_cross_year_boundary(self) -> None:
        """Test retention policy across year boundary."""
        now = datetime(2027, 1, 5, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))
        # 35 days ago would be 2026-12-01
        long_ago = now - timedelta(days=35)
        date_int = int(long_ago.strftime("%Y%m%d"))
        time_int = int(long_ago.strftime("%H%M"))
        assert not should_keep("memoria-old.dump", date_int, time_int, now)

    def test_cross_month_boundary(self) -> None:
        """Test retention policy across month boundary."""
        now = datetime(2026, 11, 5, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))
        # 10 days ago: 2026-10-26, past the 7-day window, so it is deleted.
        ten_days_ago = now - timedelta(days=10)
        date_int = int(ten_days_ago.strftime("%Y%m%d"))
        time_int = int(ten_days_ago.strftime("%H%M"))
        assert not should_keep("memoria-old.dump", date_int, time_int, now)


class TestListCandidates:
    def test_lists_both_dump_types(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        _touch(backup_dir / "memoria-20261010-1515.dump", date_int=20261010, time_int=1515)
        _touch(backup_dir / "control-20261010-1515.sqlite3", date_int=20261010, time_int=1515)
        _touch(backup_dir / "other-file.txt", date_int=0, time_int=0)

        candidates = list_backup_candidates(backup_dir)
        assert len(candidates) == 2
        filenames = [c[1] for c in candidates]
        assert "memoria-20261010-1515.dump" in filenames
        assert "control-20261010-1515.sqlite3" in filenames

    def test_ignores_non_backup_files(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        _touch(backup_dir / "memoria-20261010-1515.dump", date_int=20261010, time_int=1515)
        _touch(backup_dir / "random-file.txt", date_int=0, time_int=0)
        _touch(backup_dir / "prev-release", date_int=0, time_int=0)

        candidates = list_backup_candidates(backup_dir)
        assert len(candidates) == 1
        assert candidates[0][1] == "memoria-20261010-1515.dump"

    def test_empty_directory(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()
        candidates = list_backup_candidates(backup_dir)
        assert candidates == []


class TestBackupsToDelete:
    def test_deletes_old_backup(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        now = datetime(2026, 10, 20, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))

        # October 13 is exactly 7 full days old, so it goes.
        _touch(backup_dir / "memoria-20261013-0315.dump", date_int=20261013, time_int=315)
        # The newest copy of a kind is never deleted, so keep a newer one beside it.
        _touch(backup_dir / "memoria-20261020-0315.dump", date_int=20261020, time_int=315)

        to_delete = backups_to_delete(backup_dir, now=now)
        assert [p.name for p in to_delete] == ["memoria-20261013-0315.dump"]

    def test_keeps_recent_backups(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        now = datetime(2026, 10, 18, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))

        # Ages 0-6 days are the whole retained window.
        for i in range(0, 7):
            days_ago = now - timedelta(days=i)
            datestr = days_ago.strftime("%Y%m%d")
            _touch(
                backup_dir / f"memoria-{datestr}-0315.dump",
                date_int=int(datestr),
                time_int=315,
            )

        to_delete = backups_to_delete(backup_dir, now=now)
        assert len(to_delete) == 0

    def test_deletes_old_sunday_backups(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        now = datetime(2026, 10, 18, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))

        # Sundays Oct 11 (-7d), Oct 4 (-14d), Sep 27 (-21d): no weekly exception.
        sundays = [
            (2026, 10, 11),
            (2026, 10, 4),
            (2026, 9, 27),
        ]
        for y, m, d in sundays:
            datestr = f"{y:04d}{m:02d}{d:02d}"
            _touch(
                backup_dir / f"memoria-{datestr}-0315.dump",
                date_int=int(datestr),
                time_int=315,
            )
        # A current dump keeps the newest-copy exception from masking the test.
        _touch(backup_dir / "memoria-20261018-0315.dump", date_int=20261018, time_int=315)

        to_delete = backups_to_delete(backup_dir, now=now)
        assert sorted(p.name for p in to_delete) == [
            "memoria-20260927-0315.dump",
            "memoria-20261004-0315.dump",
            "memoria-20261011-0315.dump",
        ]


class TestDeletePaths:
    def test_deletes_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "file1.txt"
        f2 = tmp_path / "file2.txt"
        f1.write_text("x")
        f2.write_text("y")

        deleted = delete_paths([f1, f2])
        assert len(deleted) == 2
        assert not f1.exists()
        assert not f2.exists()

    def test_handles_missing_files(self, tmp_path: Path) -> None:
        f1 = tmp_path / "file1.txt"
        f2 = tmp_path / "file2.txt"
        f1.write_text("x")

        deleted = delete_paths([f1, f2])
        assert len(deleted) == 1
        assert deleted[0] == f1


class TestMain:
    def test_dry_run_lists_without_deleting(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        now = datetime(2026, 10, 20, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))

        # Create a file 10 days old (should be deleted)
        ten_days_ago = now - timedelta(days=10)
        datestr = ten_days_ago.strftime("%Y%m%d")
        old_file = _touch(
            backup_dir / f"memoria-{datestr}-0315.dump",
            date_int=int(datestr),
            time_int=315,
        )
        # A newer dump keeps the newest-copy exception from masking the test.
        _touch(backup_dir / "memoria-20261020-0315.dump", date_int=20261020, time_int=315)

        # Run in dry-run mode (no --apply)
        result = main(["--backup-dir", str(backup_dir)])
        assert result == 0
        assert old_file.exists()  # File should still exist

    def test_apply_deletes_old_backups(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        now = datetime(2026, 10, 20, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))

        # Create a file 31 days old (should always be deleted)
        very_old = now - timedelta(days=31)
        datestr = very_old.strftime("%Y%m%d")
        old_file = _touch(
            backup_dir / f"memoria-{datestr}-0315.dump",
            date_int=int(datestr),
            time_int=315,
        )

        # The newest copy of a kind is never deleted, so keep a current one beside it.
        today = datetime.now(tz=timezone(timedelta(hours=8))).strftime("%Y%m%d")
        _touch(
            backup_dir / f"memoria-{today}-0315.dump",
            date_int=int(today),
            time_int=315,
        )

        result = main(["--backup-dir", str(backup_dir), "--apply"])
        assert result == 0
        assert not old_file.exists()

    def test_missing_directory_returns_zero(self) -> None:
        result = main(["--backup-dir", "/nonexistent/path"])
        assert result == 0


def test_the_newest_copy_of_each_kind_is_kept_even_when_past_seven_days(tmp_path: Path) -> None:
    now = datetime(2026, 12, 1, 4, 0, tzinfo=timezone(timedelta(hours=8)))
    for name in (
        "memoria-20260101-0315.dump",
        "memoria-20260102-0315.dump",
        "control-20260101-0315.sqlite3",
        "control-20260102-0315.sqlite3",
    ):
        (tmp_path / name).write_bytes(b"x" * 200)

    gone = {p.name for p in backups_to_delete(tmp_path, now=now)}

    assert gone == {"memoria-20260101-0315.dump", "control-20260101-0315.sqlite3"}


def test_a_lone_ancient_backup_is_not_deleted(tmp_path: Path) -> None:
    now = datetime(2027, 1, 1, 4, 0, tzinfo=timezone(timedelta(hours=8)))
    (tmp_path / "memoria-20260101-0315.dump").write_bytes(b"x" * 200)

    assert backups_to_delete(tmp_path, now=now) == []


class TestStaleBackups:
    NOW = datetime(2026, 10, 20, 3, 15, 0, tzinfo=CST)

    def test_both_kinds_fresh_returns_empty(self, tmp_path: Path) -> None:
        _write_backup(tmp_path, "dump", self.NOW - timedelta(hours=1))
        _write_backup(tmp_path, "control", self.NOW - timedelta(hours=2))

        assert stale_backups(tmp_path, now=self.NOW) == []

    def test_dump_fifty_hours_old_is_stale(self, tmp_path: Path) -> None:
        dump = _write_backup(tmp_path, "dump", self.NOW - timedelta(hours=50))
        _write_backup(tmp_path, "control", self.NOW - timedelta(hours=1))

        assert stale_backups(tmp_path, now=self.NOW) == [
            f"dump stale: newest={dump.name} age_hours=50.0 max_hours=48"
        ]

    def test_control_missing(self, tmp_path: Path) -> None:
        _write_backup(tmp_path, "dump", self.NOW - timedelta(hours=1))

        assert stale_backups(tmp_path, now=self.NOW) == [
            f"control missing: no control-*.sqlite3 in {tmp_path}"
        ]

    def test_exactly_forty_eight_hours_is_fresh(self, tmp_path: Path) -> None:
        _write_backup(tmp_path, "dump", self.NOW - timedelta(hours=48))
        _write_backup(tmp_path, "control", self.NOW - timedelta(hours=1))

        assert stale_backups(tmp_path, now=self.NOW) == []

    def test_forty_eight_hours_and_one_minute_is_stale(self, tmp_path: Path) -> None:
        _write_backup(
            tmp_path,
            "dump",
            self.NOW - timedelta(hours=48, minutes=1),
        )
        _write_backup(tmp_path, "control", self.NOW - timedelta(hours=1))

        problems = stale_backups(tmp_path, now=self.NOW)
        assert len(problems) == 1
        assert problems[0].startswith("dump stale: newest=memoria-20261018-0314.dump")

    def test_missing_directory_reports_both_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "missing"

        assert stale_backups(missing, now=self.NOW) == [
            f"dump missing: no memoria-*.dump in {missing}",
            f"control missing: no control-*.sqlite3 in {missing}",
        ]


class TestFreshnessCli:
    def _fresh_dir(self, tmp_path: Path) -> tuple[Path, str, str]:
        now = datetime.now(tz=CST)
        dump = _write_backup(tmp_path, "dump", now - timedelta(hours=1))
        control = _write_backup(tmp_path, "control", now - timedelta(hours=1))
        return tmp_path, dump.name, control.name

    def test_fresh_exits_zero(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        backup_dir, dump_name, control_name = self._fresh_dir(tmp_path)

        result = main(["--backup-dir", str(backup_dir), "--check-freshness"])

        captured = capsys.readouterr()
        assert result == 0
        assert captured.out.strip() == f"fresh dump={dump_name} control={control_name}"
        assert captured.err == ""

    def test_stale_dump_exits_two(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        now = datetime.now(tz=CST)
        _write_backup(tmp_path, "dump", now - timedelta(hours=50))
        _write_backup(tmp_path, "control", now - timedelta(hours=1))

        result = main(["--backup-dir", str(tmp_path), "--check-freshness"])

        captured = capsys.readouterr()
        assert result == 2
        assert "ALERT backup freshness: dump stale: newest=memoria-" in captured.err
        assert "max_hours=48" in captured.err

    def test_missing_control_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        now = datetime.now(tz=CST)
        _write_backup(tmp_path, "dump", now - timedelta(hours=1))

        result = main(["--backup-dir", str(tmp_path), "--check-freshness"])

        captured = capsys.readouterr()
        assert result == 2
        assert "ALERT backup freshness: control missing: no control-*.sqlite3" in captured.err

    def test_missing_directory_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        result = main(["--backup-dir", str(tmp_path / "missing"), "--check-freshness"])

        captured = capsys.readouterr()
        assert result == 2
        assert "ALERT backup freshness: dump missing:" in captured.err
        assert "ALERT backup freshness: control missing:" in captured.err

    def test_check_freshness_deletes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        now = datetime.now(tz=CST)
        old_dump = _write_backup(tmp_path, "dump", now - timedelta(days=10))
        _write_backup(tmp_path, "dump", now - timedelta(hours=1))
        _write_backup(tmp_path, "control", now - timedelta(hours=1))

        result = main(["--backup-dir", str(tmp_path), "--check-freshness"])

        capsys.readouterr()
        assert result == 0
        assert old_dump.exists()

    def test_apply_and_check_freshness_are_mutually_exclusive(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main(
                [
                    "--backup-dir",
                    str(tmp_path),
                    "--check-freshness",
                    "--apply",
                ]
            )

        assert excinfo.value.code != 0


def test_freshness_units_wire_the_check() -> None:
    service = (ROOT / "infra" / "memoria-backup-freshness.service").read_text()
    timer = (ROOT / "infra" / "memoria-backup-freshness.timer").read_text()

    assert (
        "/usr/bin/python3 "
        "/opt/memoria/ops-tools/nightly_backup_retention.py "
        "--backup-dir /var/lib/memoria/backups/nightly "
        "--check-freshness --max-age-hours 48"
    ) in service
    assert "Type=oneshot" in service
    assert "User=root" in service
    assert "OnUnitActiveSec=6h" in timer
    assert "OnBootSec=20min" in timer
    assert "RandomizedDelaySec=10min" in timer
    assert "Persistent=true" in timer
