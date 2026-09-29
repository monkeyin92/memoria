"""Read-only, deterministic migration from the legacy SQLite message projection.

The legacy source is a SQLite file; the target is the PostgreSQL archive.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from services.archive.domain import EvidenceEvent
from services.archive.postgres_archive import PostgresLifeArchive


@dataclass(frozen=True, slots=True)
class MigrationReport:
    source_sha256: str
    event_set_sha256: str
    eligible_messages: int
    legacy_projection_count: int
    inserted: int
    duplicates: int
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _read_source(source: Path) -> tuple[list[EvidenceEvent], int]:
    uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "messages" not in tables:
            raise ValueError("legacy source has no messages table")
        rows = connection.execute(
            """
            SELECT id, user_id, role, text, emotion, local_date, created_at
            FROM messages ORDER BY id
            """
        ).fetchall()
        projection_count = (
            int(connection.execute("SELECT COUNT(*) FROM daily_summaries").fetchone()[0])
            if "daily_summaries" in tables
            else 0
        )

    events: list[EvidenceEvent] = []
    for row in rows:
        role = str(row["role"])
        if role not in {"user", "assistant"}:
            raise ValueError(f"unsupported legacy message role: {role}")
        identity = (
            f"memoria:legacy-message:{row['user_id']}:{row['id']}:"
            f"{row['created_at']}:{role}"
        )
        payload: dict[str, Any] = {
            "text": str(row["text"]),
            "legacy_message_id": int(row["id"]),
            "local_date": str(row["local_date"]),
        }
        if row["emotion"] is not None:
            payload["emotion"] = str(row["emotion"])
        if role == "assistant":
            payload["actual_heard"] = "unverified_legacy"
        events.append(
            EvidenceEvent(
                event_id=str(uuid.uuid5(uuid.NAMESPACE_URL, identity)),
                account_id=str(row["user_id"]),
                event_type="legacy.message_imported",
                occurred_at=_parse_timestamp(str(row["created_at"])),
                speaker_class="assistant" if role == "assistant" else "uncertain",
                source="legacy.sqlite.messages",
                payload=payload,
            )
        )
    return events, projection_count


def _event_set_sha256(events: list[EvidenceEvent]) -> str:
    rows = "\n".join(
        f"{event.event_id}:{event.content_sha256}" for event in sorted(events, key=lambda e: e.event_id)
    )
    return hashlib.sha256(rows.encode("utf-8")).hexdigest()


async def migrate_legacy_sqlite_to_postgres(
    source_path: str | Path,
    archive: PostgresLifeArchive,
    *,
    dry_run: bool = False,
) -> MigrationReport:
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise ValueError("legacy source must be an existing SQLite file")
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    events, projection_count = _read_source(source)
    event_hash = _event_set_sha256(events)
    if dry_run:
        return MigrationReport(
            source_sha256=source_hash,
            event_set_sha256=event_hash,
            eligible_messages=len(events),
            legacy_projection_count=projection_count,
            inserted=0,
            duplicates=0,
            dry_run=True,
        )
    results = await archive.import_events_atomic(events)
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise RuntimeError("legacy source changed during migration")
    return MigrationReport(
        source_sha256=source_hash,
        event_set_sha256=event_hash,
        eligible_messages=len(events),
        legacy_projection_count=projection_count,
        inserted=sum(not result.duplicate for result in results),
        duplicates=sum(result.duplicate for result in results),
        dry_run=False,
    )
