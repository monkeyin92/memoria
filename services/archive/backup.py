"""Verified SQLite backup/restore used for local and migration rehearsal."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class BackupManifest:
    manifest_path: Path
    backup_sha256: str
    event_count: int
    outbox_count: int
    created_at: str


@dataclass(frozen=True, slots=True)
class RestoreReport:
    target_path: Path
    integrity_check: str
    event_count: int
    outbox_count: int


def _count(connection: sqlite3.Connection, table: str) -> int:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if exists is None:
        return 0
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _atomic_write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def create_sqlite_backup(source_path: str | Path, backup_path: str | Path) -> BackupManifest:
    source = Path(source_path).expanduser().resolve()
    backup = Path(backup_path).expanduser().resolve()
    if not source.is_file() or source == backup:
        raise ValueError("source must exist and differ from backup path")
    backup.parent.mkdir(parents=True, exist_ok=True)
    temporary = backup.with_name(f".{backup.name}.tmp")
    temporary.unlink(missing_ok=True)
    source_uri = f"file:{source}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=5) as source_connection:
        with sqlite3.connect(temporary, timeout=5) as backup_connection:
            source_connection.backup(backup_connection)
            integrity = str(backup_connection.execute("PRAGMA integrity_check").fetchone()[0])
            if integrity != "ok":
                raise RuntimeError(f"backup integrity check failed: {integrity}")
            event_count = _count(backup_connection, "evidence_events")
            outbox_count = _count(backup_connection, "processing_outbox")
    os.chmod(temporary, 0o600)
    os.replace(temporary, backup)
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    created_at = datetime.now(UTC).isoformat()
    manifest_path = backup.with_suffix(f"{backup.suffix}.manifest.json")
    body: dict[str, Any] = {
        "format_version": 1,
        "backup_file": backup.name,
        "backup_sha256": digest,
        "event_count": event_count,
        "outbox_count": outbox_count,
        "created_at": created_at,
    }
    _atomic_write(
        manifest_path,
        json.dumps(body, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n",
    )
    return BackupManifest(
        manifest_path=manifest_path,
        backup_sha256=digest,
        event_count=event_count,
        outbox_count=outbox_count,
        created_at=created_at,
    )


def restore_sqlite_backup(
    backup_path: str | Path,
    manifest_path: str | Path,
    target_path: str | Path,
) -> RestoreReport:
    backup = Path(backup_path).expanduser().resolve()
    manifest_file = Path(manifest_path).expanduser().resolve()
    target = Path(target_path).expanduser().resolve()
    if not backup.is_file() or not manifest_file.is_file() or target == backup:
        raise ValueError("backup and manifest must exist; target must be separate")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    digest = hashlib.sha256(backup.read_bytes()).hexdigest()
    if digest != manifest.get("backup_sha256"):
        raise ValueError("backup SHA-256 does not match manifest")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.restore.tmp")
    temporary.unlink(missing_ok=True)
    source_uri = f"file:{backup}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=5) as source_connection:
        with sqlite3.connect(temporary, timeout=5) as target_connection:
            source_connection.backup(target_connection)
            integrity = str(target_connection.execute("PRAGMA integrity_check").fetchone()[0])
            event_count = _count(target_connection, "evidence_events")
            outbox_count = _count(target_connection, "processing_outbox")
    if integrity != "ok":
        temporary.unlink(missing_ok=True)
        raise RuntimeError(f"restored database integrity check failed: {integrity}")
    if event_count != int(manifest["event_count"]) or outbox_count != int(
        manifest["outbox_count"]
    ):
        temporary.unlink(missing_ok=True)
        raise ValueError("restored counts do not match backup manifest")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    return RestoreReport(
        target_path=target,
        integrity_check=integrity,
        event_count=event_count,
        outbox_count=outbox_count,
    )
