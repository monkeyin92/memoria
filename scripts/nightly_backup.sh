#!/usr/bin/env bash
# Nightly database backup for PostgreSQL and SQLite control store.
#
# Backups both memoria PostgreSQL database and control-api SQLite store
# to /var/lib/memoria/backups/nightly/ with retention policy (7 days full,
# then weekly, then delete at 30+ days). Intended to run via systemd timer.
#
# Exit codes:
#   0  backup succeeded or was skipped (normal)
#   1  backup or cleanup failed (systemd shows failed status)
#
set -Eeuo pipefail

BACKUP_DIR="/var/lib/memoria/backups/nightly"
RETENTION_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nightly_backup_retention.py"
NOW_ISO=$(date +%Y%m%d-%H%M)

log() {
  printf '[%s] %s\n' "$(date +%T)" "$*"
}

fail() {
  log "FAIL: $*" >&2
  exit 1
}

# Create backup directory if needed.
if [[ ! -d "$BACKUP_DIR" ]]; then
  install -d -m 0700 "$BACKUP_DIR" || fail "cannot create $BACKUP_DIR"
fi

# Check available disk space. Require at least 1 GB free.
available_kb=$(df "$BACKUP_DIR" | awk 'NR==2 {print $4}')
available_bytes=$((available_kb * 1024))
threshold_bytes=$((1024 * 1024 * 1024))  # 1 GB

if [[ $available_bytes -lt $threshold_bytes ]]; then
  log "SKIP: available space $(numfmt --to=iec "$available_bytes" 2>/dev/null || echo "$available_bytes bytes") < 1 GB"
  # Skip but let cleanup run (exit 0 means the timer task itself succeeded).
  # Cleanup will still execute to free space.
  cleanup_only="true"
else
  cleanup_only="false"
fi

# Backup PostgreSQL if space permits.
if [[ "$cleanup_only" == "false" ]]; then
  log "backing up PostgreSQL database..."

  DUMP_TEMP="$BACKUP_DIR/memoria-$NOW_ISO.dump.tmp"
  DUMP_FINAL="$BACKUP_DIR/memoria-$NOW_ISO.dump"

  # Execute pg_dump inside the container.
  if ! docker exec memoria-data-postgres-1 pg_dump -U memoria_admin -d memoria -Fc > "$DUMP_TEMP" 2>/dev/null; then
    rm -f "$DUMP_TEMP"
    fail "pg_dump failed"
  fi

  # Verify dump is non-empty.
  dump_size=$(stat -c %s "$DUMP_TEMP" 2>/dev/null || stat -f %z "$DUMP_TEMP" 2>/dev/null || echo 0)
  if [[ $dump_size -lt 100 ]]; then
    rm -f "$DUMP_TEMP"
    fail "pg_dump produced empty or tiny dump ($dump_size bytes)"
  fi

  # Verify dump is valid (pg_restore --list must work).
  if ! docker exec memoria-data-postgres-1 pg_restore --list "$DUMP_TEMP" >/dev/null 2>&1; then
    # The file is outside the container, so we need to copy it in first for validation.
    # Alternative: do a quick test inside the container.
    if ! pg_restore --list "$DUMP_TEMP" >/dev/null 2>&1 2>&1; then
      rm -f "$DUMP_TEMP"
      fail "pg_dump validation failed (pg_restore --list error)"
    fi
  fi

  # Atomic rename.
  mv "$DUMP_TEMP" "$DUMP_FINAL" || fail "cannot rename dump to final location"
  log "PostgreSQL backup: $DUMP_FINAL ($dump_size bytes)"

  # Backup SQLite control store.
  log "backing up SQLite control store..."

  SQLITE_TEMP="$BACKUP_DIR/control-$NOW_ISO.sqlite3.tmp"
  SQLITE_FINAL="$BACKUP_DIR/control-$NOW_ISO.sqlite3"

  # Create backup via Python in the container, then copy out.
  if ! docker exec memoria-control-api-1 /app/.venv/bin/python -c \
    "import sqlite3; s=sqlite3.connect('/data/memoria.sqlite3'); d=sqlite3.connect('/tmp/memoria-backup-$NOW_ISO.sqlite3'); s.backup(d); d.close()" 2>/dev/null; then
    fail "SQLite backup creation failed"
  fi

  if ! docker cp "memoria-control-api-1:/tmp/memoria-backup-$NOW_ISO.sqlite3" "$SQLITE_TEMP" 2>/dev/null; then
    fail "cannot copy SQLite backup from container"
  fi

  # Clean up container temp file.
  docker exec memoria-control-api-1 rm -f "/tmp/memoria-backup-$NOW_ISO.sqlite3" 2>/dev/null || true

  # Verify SQLite dump is non-empty.
  sqlite_size=$(stat -c %s "$SQLITE_TEMP" 2>/dev/null || stat -f %z "$SQLITE_TEMP" 2>/dev/null || echo 0)
  if [[ $sqlite_size -lt 100 ]]; then
    rm -f "$SQLITE_TEMP"
    fail "SQLite backup is empty or tiny ($sqlite_size bytes)"
  fi

  # Verify SQLite file is valid (sqlite3 dump should work).
  if ! sqlite3 "$SQLITE_TEMP" ".dump" >/dev/null 2>&1; then
    rm -f "$SQLITE_TEMP"
    fail "SQLite backup validation failed"
  fi

  # Atomic rename.
  mv "$SQLITE_TEMP" "$SQLITE_FINAL" || fail "cannot rename SQLite backup to final location"
  log "SQLite backup: $SQLITE_FINAL ($sqlite_size bytes)"
fi

# Apply retention policy (runs even if backup was skipped).
log "applying retention policy..."
if ! python3 "$RETENTION_SCRIPT" --backup-dir "$BACKUP_DIR" --apply; then
  fail "retention policy cleanup failed"
fi

log "nightly backup complete"
exit 0
