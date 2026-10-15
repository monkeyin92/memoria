from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from services.control_api.app.database import MemoryStore
from services.control_api.app.database.chat_usage import (
    ChatUsageConflictError,
    ChatUsageLedger,
    ChatUsageToday,
    delete_chat_usage,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
FIRST = "a" * 32
SECOND = "b" * 32
THIRD = "c" * 32


@pytest.fixture(params=["sqlite", "postgres"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[MemoryStore]:
    if request.param == "postgres":
        database = request.getfixturevalue("postgres_database")
        store = MemoryStore(
            dsn=database.control_env()["MEMORIA_CONTROL_DATABASE_URL"], initialize_schema=False
        )
    else:
        store = MemoryStore(str(tmp_path / "control.sqlite3"))
    try:
        yield store
    finally:
        store.close()


def _at(store: MemoryStore, moment: str) -> ChatUsageLedger:
    return ChatUsageLedger(store, zone=SHANGHAI, clock=lambda: datetime.fromisoformat(moment))


def test_today_sums_each_admitted_session_of_that_child(store: MemoryStore) -> None:
    ledger = _at(store, "2026-10-15T02:00:00+00:00")
    ledger.record(accrual_id=FIRST, subject_id="child-1", elapsed_seconds=412)
    ledger.record(accrual_id=SECOND, subject_id="child-1", elapsed_seconds=668)
    ledger.record(accrual_id=THIRD, subject_id="child-2", elapsed_seconds=90)

    assert ledger.used_today("child-1") == ChatUsageToday(day=date(2026, 10, 15), seconds=1080)
    assert ledger.used_today("child-1").minutes == 18
    assert ledger.used_today("child-2") == ChatUsageToday(day=date(2026, 10, 15), seconds=90)
    assert ledger.used_today("child-3") == ChatUsageToday(day=date(2026, 10, 15), seconds=0)


def test_a_session_across_shanghai_midnight_credits_only_the_part_after_it(
    store: MemoryStore,
) -> None:
    ledger = _at(store, "2026-10-14T16:10:00+00:00")

    ledger.record(accrual_id=FIRST, subject_id="child-1", elapsed_seconds=1200)

    assert ledger.used_today("child-1") == ChatUsageToday(day=date(2026, 10, 15), seconds=600)


def test_a_repeated_report_counts_once_and_a_different_one_conflicts(store: MemoryStore) -> None:
    ledger = _at(store, "2026-10-15T02:00:00+00:00")
    ledger.record(accrual_id=FIRST, subject_id="child-1", elapsed_seconds=412)
    ledger.record(accrual_id=FIRST, subject_id="child-1", elapsed_seconds=412)

    with pytest.raises(ChatUsageConflictError):
        ledger.record(accrual_id=FIRST, subject_id="child-1", elapsed_seconds=413)
    with pytest.raises(ChatUsageConflictError):
        ledger.record(accrual_id=FIRST, subject_id="child-2", elapsed_seconds=412)

    assert ledger.used_today("child-1").seconds == 412
    assert ledger.used_today("child-2").seconds == 0


def test_a_report_repeated_after_midnight_credits_the_new_day_nothing(store: MemoryStore) -> None:
    _at(store, "2026-10-14T15:59:50+00:00").record(
        accrual_id=FIRST, subject_id="child-1", elapsed_seconds=100
    )
    after_midnight = _at(store, "2026-10-14T16:00:05+00:00")

    after_midnight.record(accrual_id=FIRST, subject_id="child-1", elapsed_seconds=100)

    assert after_midnight.used_today("child-1") == ChatUsageToday(day=date(2026, 10, 15), seconds=0)


def test_a_write_prunes_earlier_days_and_erasure_counts_what_it_deletes(
    store: MemoryStore,
) -> None:
    _at(store, "2026-10-14T13:00:00+00:00").record(
        accrual_id=FIRST, subject_id="child-1", elapsed_seconds=300
    )
    next_day = _at(store, "2026-10-15T00:00:00+00:00")
    next_day.record(accrual_id=SECOND, subject_id="child-1", elapsed_seconds=200)
    next_day.record(accrual_id=THIRD, subject_id="child-2", elapsed_seconds=50)

    with store.connection() as connection:
        deleted = delete_chat_usage(connection, subject_id="child-1")

    assert deleted == 1
    assert next_day.used_today("child-1").seconds == 0
    assert next_day.used_today("child-2").seconds == 50


def test_account_deletion_erases_and_counts_the_accounts_own_usage(store: MemoryStore) -> None:
    ledger = _at(store, "2026-10-15T02:00:00+00:00")
    ledger.record(accrual_id=FIRST, subject_id="owner-1", elapsed_seconds=300)
    deletion = store.begin_account_deletion(
        user_id="owner-1", started_at="2026-10-15T02:01:00+00:00"
    )

    counts = store.finalize_account_deletion(
        user_id="owner-1",
        request_id=deletion["request_id"],
        completed_at="2026-10-15T02:02:00+00:00",
        deleted_counts={},
    )

    assert counts["daily_chat_usage"] == 1
    assert ledger.used_today("owner-1").seconds == 0
