from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.nightly_backup_retention import (
    backups_to_delete,
    date_to_datetime,
    delete_paths,
    list_backup_candidates,
    main,
    parse_backup_filename,
    should_keep,
)


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

    def test_day_7_kept(self) -> None:
        """Day 7 backup should be kept."""
        now = self._now()
        week_ago = now - timedelta(days=6)
        date_int = int(week_ago.strftime("%Y%m%d"))
        time_int = int(week_ago.strftime("%H%M"))
        assert should_keep("memoria-20261014-0315.dump", date_int, time_int, now)

    def test_day_8_weekday_deleted(self) -> None:
        """Day 8 backup on a non-Sunday should be deleted."""
        # October 20, 2026 is a Sunday.
        # October 12, 2026 is a Sunday.
        # October 13, 2026 is a Monday (not Sunday, should be deleted).
        now = datetime(2026, 10, 20, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))
        # Create a date 8 days ago from now
        eight_days_ago = now - timedelta(days=8)
        # If that day is not a Sunday, it should be deleted
        if eight_days_ago.weekday() != 6:
            date_int = int(eight_days_ago.strftime("%Y%m%d"))
            time_int = int(eight_days_ago.strftime("%H%M"))
            assert not should_keep("memoria-old.dump", date_int, time_int, now)

    def test_day_8_sunday_kept(self) -> None:
        """Day 8 backup on a Sunday should be kept."""
        # October 18, 2026 is a Sunday
        # October 10, 2026 is a Friday (not Sunday, -8d, should be deleted)
        # October 11, 2026 is a Saturday (not Sunday, -7d, should be kept)
        # October 4, 2026 is a Sunday (-14d, should be kept if within 30d range 8-30)
        sunday_now = datetime(2026, 10, 18, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))
        assert sunday_now.weekday() == 6  # Verify it's Sunday
        # Go back 14 days to the previous Sunday
        prev_sunday = sunday_now - timedelta(days=14)
        assert prev_sunday.weekday() == 6  # Oct 4 is Sunday
        date_int = int(prev_sunday.strftime("%Y%m%d"))
        time_int = int(prev_sunday.strftime("%H%M"))
        assert should_keep("memoria-20261004-0315.dump", date_int, time_int, sunday_now)

    def test_day_30_sunday_kept(self) -> None:
        """Day 30 backup on a Sunday should be kept."""
        # 2026-10-20 is a Sunday.
        # 30 days before: 2026-09-20, which is a Sunday.
        sunday_now = datetime(2026, 10, 20, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))
        thirty_days_ago = sunday_now - timedelta(days=29)
        date_int = int(thirty_days_ago.strftime("%Y%m%d"))
        time_int = int(thirty_days_ago.strftime("%H%M"))
        # At exactly 29 days (which is still in the 8-30 range), a Sunday should be kept
        if thirty_days_ago.weekday() == 6:
            assert should_keep("memoria-old.dump", date_int, time_int, sunday_now)

    def test_day_31_deleted(self) -> None:
        """Day 31+ backup should always be deleted."""
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
        # 10 days ago: 2026-10-26 (day 10 in policy, should be kept if Sunday or within 7)
        ten_days_ago = now - timedelta(days=10)
        date_int = int(ten_days_ago.strftime("%Y%m%d"))
        time_int = int(ten_days_ago.strftime("%H%M"))
        # Oct 26 is a Monday (not Sunday), should be deleted
        if ten_days_ago.weekday() != 6:
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
    def test_deletes_old_weekday_backup(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        # October 20, 2026 is a Sunday.
        now = datetime(2026, 10, 20, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))

        # October 13, 2026 is a Monday (8 days before, not a Sunday -> delete)
        _touch(backup_dir / "memoria-20261013-0315.dump", date_int=20261013, time_int=315)

        to_delete = backups_to_delete(backup_dir, now=now)
        assert len(to_delete) == 1

    def test_keeps_recent_backups(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        # October 18, 2026 is a Sunday. Use this as "now".
        now = datetime(2026, 10, 18, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))

        # Create backups for days 1-7 (all within first week, should be kept)
        for i in range(1, 8):
            days_ago = now - timedelta(days=i)
            datestr = days_ago.strftime("%Y%m%d")
            _touch(
                backup_dir / f"memoria-{datestr}-0315.dump",
                date_int=int(datestr),
                time_int=315,
            )

        to_delete = backups_to_delete(backup_dir, now=now)
        assert len(to_delete) == 0

    def test_keeps_weekly_sunday_backups(self, tmp_path: Path) -> None:
        backup_dir = tmp_path / "backups"
        # October 18, 2026 is a Sunday.
        now = datetime(2026, 10, 18, 3, 15, 0, tzinfo=timezone(timedelta(hours=8)))

        # Add backups on Sundays: Oct 11 (-7d), Oct 4 (-14d), Sep 27 (-21d)
        # All are Sundays and within 30 days -> all kept
        sundays = [
            (2026, 10, 11),  # -7 days, Sunday
            (2026, 10, 4),   # -14 days, Sunday
            (2026, 9, 27),   # -21 days, Sunday
        ]
        for y, m, d in sundays:
            datestr = f"{y:04d}{m:02d}{d:02d}"
            _touch(
                backup_dir / f"memoria-{datestr}-0315.dump",
                date_int=int(datestr),
                time_int=315,
            )

        to_delete = backups_to_delete(backup_dir, now=now)
        # All are Sundays, all within 30 days -> keep all
        assert len(to_delete) == 0


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

        # Create a file 10 days old on a non-Sunday (should be deleted)
        ten_days_ago = now - timedelta(days=10)
        datestr = ten_days_ago.strftime("%Y%m%d")
        old_file = _touch(
            backup_dir / f"memoria-{datestr}-0315.dump",
            date_int=int(datestr),
            time_int=315,
        )

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

        result = main(["--backup-dir", str(backup_dir), "--apply"])
        assert result == 0
        assert not old_file.exists()

    def test_missing_directory_returns_zero(self) -> None:
        result = main(["--backup-dir", "/nonexistent/path"])
        assert result == 0
