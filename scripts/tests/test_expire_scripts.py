from __future__ import annotations

from datetime import UTC, datetime, timedelta

import asyncpg
import pytest
from scripts import expire_idle_child_archive

NOW = datetime(2026, 10, 15, tzinfo=UTC)


class _Connection:
    def __init__(self, rows: list[dict[str, object]] | None) -> None:
        self._rows = rows
        self.closed = False

    async def fetch(self, _query: str, _subjects: list[str]) -> list[dict[str, object]]:
        if self._rows is None:
            raise asyncpg.UndefinedTableError('relation "guardian_subject_activity" does not exist')
        return self._rows

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_idle_filter_keeps_old_and_missing_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = _Connection(
        [
            {"subject_person_id": "recent", "last_active_at": NOW - timedelta(days=1)},
            {"subject_person_id": "old", "last_active_at": NOW - timedelta(days=200)},
        ]
    )

    async def connect(_dsn: str) -> _Connection:
        return connection

    monkeypatch.setattr(expire_idle_child_archive.asyncpg, "connect", connect)

    idle = await expire_idle_child_archive.idle_filter(
        "postgresql://guardian", ["recent", "old", "missing"], now=NOW
    )

    assert idle == ["old", "missing"]
    assert connection.closed is True


@pytest.mark.asyncio
async def test_idle_filter_fails_closed_without_the_activity_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(None)

    async def connect(_dsn: str) -> _Connection:
        return connection

    monkeypatch.setattr(expire_idle_child_archive.asyncpg, "connect", connect)

    with pytest.raises(asyncpg.UndefinedTableError):
        await expire_idle_child_archive.idle_filter("postgresql://guardian", ["a", "b"], now=NOW)
    assert connection.closed is True
