#!/bin/sh
set -eu

umask 077

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"

interval="${MEMORIA_BASE_BACKUP_INTERVAL_S:-86400}"
retention_days="${MEMORIA_BASE_BACKUP_LOCAL_RETENTION_DAYS:-3}"
case "$interval:$retention_days" in
  *[!0-9:]*|:*|*:|0:*|*:0)
    echo "backup interval and retention must be positive integers" >&2
    exit 2
    ;;
esac

mkdir -p /backup-staging

while :; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  temporary="/backup-staging/.${stamp}.partial"
  destination="/backup-staging/${stamp}"
  rm -rf "$temporary"
  mkdir "$temporary"
  PGPASSWORD="$POSTGRES_PASSWORD" pg_basebackup \
    --host=memoria-postgres \
    --username=memoria_admin \
    --dbname=postgres \
    --pgdata="$temporary" \
    --format=plain \
    --wal-method=stream \
    --checkpoint=fast \
    --manifest-checksums=SHA256 \
    --no-password
  mv "$temporary" "$destination"
  printf '%s\n' "$stamp" >"/backup-staging/.LATEST.tmp"
  mv "/backup-staging/.LATEST.tmp" "/backup-staging/LATEST"
  find /backup-staging -mindepth 1 -maxdepth 1 -type d \
    -mtime "+$retention_days" -exec rm -rf {} +
  sleep "$interval"
done
