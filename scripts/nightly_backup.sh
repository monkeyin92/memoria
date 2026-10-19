#!/usr/bin/env bash
# Nightly database backup for PostgreSQL and SQLite control store.
#
# Backups both memoria PostgreSQL database and control-api SQLite store
# to /var/lib/memoria/backups/nightly/ with a 7-day retention policy (delete
# everything aged 7 days or more; the newest dump and control copy always
# stay). Intended to run via systemd timer.
#
# Exit codes:
#   0  backup succeeded
#   1  backup or cleanup failed
#
set -Eeuo pipefail
umask 077

BACKUP_DIR="${NIGHTLY_BACKUP_DIR:-/var/lib/memoria/backups/nightly}"
RETENTION_SCRIPT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/nightly_backup_retention.py"
NOW_ISO=$(date +%Y%m%d-%H%M)
# Not /tmp: that is a tmpfs in the container and docker cp cannot read it.
CONTAINER_SQLITE="/data/memoria.nightly-${NOW_ISO}.sqlite3"

# Cleanup handler: remove temporary files on any error or exit
cleanup() {
  local exit_code=$?
  # Remove PG temp file if it exists
  if [[ -n "${DUMP_TEMP:-}" ]] && [[ -f "$DUMP_TEMP" ]]; then
    rm -f "$DUMP_TEMP"
  fi
  # Remove SQLite temp file from container
  if [[ -n "${NOW_ISO:-}" ]]; then
    docker exec memoria-control-api-1 rm -f "$CONTAINER_SQLITE" 2>/dev/null || true
  fi
  return $exit_code
}
trap cleanup EXIT

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
  log "WARNING: available space $(python3 -c "print(f'{$available_bytes/1024/1024:.1f} MB')" 2>/dev/null || echo "$available_bytes bytes") < 1 GB"
  log "skipping backup but running retention cleanup"
  # Run cleanup anyway and then exit with error
  python3 "$RETENTION_SCRIPT" --backup-dir "$BACKUP_DIR" --apply || true
  fail "backup skipped: insufficient disk space"
fi

log "backing up PostgreSQL database..."

DUMP_TEMP="$BACKUP_DIR/memoria-$NOW_ISO.dump.tmp"
DUMP_FINAL="$BACKUP_DIR/memoria-$NOW_ISO.dump"

# Execute pg_dump inside the container, write to temp file on host
if ! docker exec memoria-data-postgres-1 pg_dump -U memoria_admin -d memoria -Fc > "$DUMP_TEMP"; then
  rm -f "$DUMP_TEMP"
  fail "pg_dump failed"
fi

# Verify dump is non-empty.
dump_size=$(stat -c %s "$DUMP_TEMP" 2>/dev/null || stat -f %z "$DUMP_TEMP" 2>/dev/null || echo 0)
if [[ $dump_size -lt 100 ]]; then
  rm -f "$DUMP_TEMP"
  fail "pg_dump produced empty or tiny dump ($dump_size bytes)"
fi

# Verify dump is valid (pg_restore --list must work)
if ! docker exec -i memoria-data-postgres-1 pg_restore --list < "$DUMP_TEMP" >/dev/null; then
  rm -f "$DUMP_TEMP"
  fail "pg_dump validation failed (pg_restore --list error)"
fi

# Atomic rename.
mv "$DUMP_TEMP" "$DUMP_FINAL" || fail "cannot rename dump to final location"
log "PostgreSQL backup: $DUMP_FINAL ($dump_size bytes)"

# Backup SQLite control store.
log "backing up SQLite control store..."

SQLITE_TEMP="$BACKUP_DIR/control-$NOW_ISO.sqlite3.tmp"
SQLITE_FINAL="$BACKUP_DIR/control-$NOW_ISO.sqlite3"

# Create backup via Python in the container, then copy out
if ! docker exec memoria-control-api-1 /app/.venv/bin/python -c \
  "import sqlite3; s=sqlite3.connect('/data/memoria.sqlite3'); d=sqlite3.connect('$CONTAINER_SQLITE'); s.backup(d); d.close()"; then
  fail "SQLite backup creation failed"
fi

if ! docker cp "memoria-control-api-1:$CONTAINER_SQLITE" "$SQLITE_TEMP"; then
  fail "cannot copy SQLite backup from container"
fi
chmod 0600 "$SQLITE_TEMP"  # docker cp keeps the container file mode and ignores umask

# Verify SQLite dump is non-empty and valid (use Python, as sqlite3 CLI is not available on host)
sqlite_size=$(stat -c %s "$SQLITE_TEMP" 2>/dev/null || stat -f %z "$SQLITE_TEMP" 2>/dev/null || echo 0)
if [[ $sqlite_size -lt 100 ]]; then
  rm -f "$SQLITE_TEMP"
  fail "SQLite backup is empty or tiny ($sqlite_size bytes)"
fi

# Verify SQLite file is valid using Python's sqlite3 library
if ! python3 -c "
import sqlite3, sys
try:
    conn = sqlite3.connect('file:$SQLITE_TEMP?mode=ro', uri=True)
    result = conn.execute('PRAGMA integrity_check').fetchone()
    if result and result[0] == 'ok':
        exit(0)
    else:
        print('integrity_check failed:', result, file=sys.stderr)
        exit(1)
except Exception as e:
    print('Error:', e, file=sys.stderr)
    exit(1)
"; then
  rm -f "$SQLITE_TEMP"
  fail "SQLite backup validation failed"
fi

# Atomic rename.
mv "$SQLITE_TEMP" "$SQLITE_FINAL" || fail "cannot rename SQLite backup to final location"
log "SQLite backup: $SQLITE_FINAL ($sqlite_size bytes)"

# Apply retention policy (runs even if backup was skipped earlier).
log "applying retention policy..."
if ! python3 "$RETENTION_SCRIPT" --backup-dir "$BACKUP_DIR" --apply; then
  fail "retention policy cleanup failed"
fi

log "nightly backup complete"
exit 0

