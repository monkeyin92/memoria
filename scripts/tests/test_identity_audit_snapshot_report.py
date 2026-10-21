from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from scripts.identity_audit_snapshot_report import (
    PERSON_UPDATE_ACTIONS,
    SnapshotReport,
    build_parser,
    report,
)


class _Connection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self._row = row
        self.queries: list[str] = []

    async def fetchrow(self, query: str, _args: object) -> dict[str, object] | None:
        self.queries.append(query)
        return self._row


def test_report_reads_count_and_time_range() -> None:
    earliest = datetime(2026, 9, 1, tzinfo=UTC)
    latest = datetime(2026, 10, 16, tzinfo=UTC)
    connection = _Connection({"row_count": 7, "earliest": earliest, "latest": latest})

    result = asyncio.run(report(connection))  # type: ignore[arg-type]

    assert result.row_count == 7
    assert result.earliest == earliest
    assert result.latest == latest
    assert result.as_line() == f"7 {earliest.isoformat()} {latest.isoformat()}"
    assert "SELECT" in connection.queries[0]
    assert not any(
        word in connection.queries[0].upper() for word in ("UPDATE ", "DELETE ", "INSERT ")
    )


def test_report_is_zero_without_snapshot_rows() -> None:
    result = asyncio.run(
        report(_Connection({"row_count": 0, "earliest": None, "latest": None}))  # type: ignore[arg-type]
    )

    assert result == SnapshotReport(row_count=0, earliest=None, latest=None)
    assert result.as_line() == "0"


def test_parser_reads_only_the_test_dsn(monkeypatch) -> None:
    monkeypatch.setenv("MEMORIA_TEST_POSTGRES_DSN", "postgresql://test/db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://other/db")

    args = build_parser().parse_args([])

    assert args.dsn == "postgresql://test/db"
    assert not hasattr(args, "apply"), "the report never writes"


def test_actions_cover_every_person_update_audit() -> None:
    assert set(PERSON_UPDATE_ACTIONS) == {
        "person.self_update",
        "person.system_update",
        "person.age_evidence.update",
    }
