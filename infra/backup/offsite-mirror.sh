#!/bin/sh
set -eu

umask 077

: "${MINIO_ROOT_USER:?MINIO_ROOT_USER is required}"
: "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD is required}"
: "${MEMORIA_OFFSITE_S3_ENDPOINT:?MEMORIA_OFFSITE_S3_ENDPOINT is required}"
: "${MEMORIA_OFFSITE_S3_ACCESS_KEY:?MEMORIA_OFFSITE_S3_ACCESS_KEY is required}"
: "${MEMORIA_OFFSITE_S3_SECRET_KEY:?MEMORIA_OFFSITE_S3_SECRET_KEY is required}"
: "${MEMORIA_OFFSITE_S3_BUCKET:?MEMORIA_OFFSITE_S3_BUCKET is required}"

case "$MEMORIA_OFFSITE_S3_ENDPOINT" in
  *localhost*|*127.0.0.1*|*memoria-minio*|*host.docker.internal*)
    echo "offsite endpoint must not resolve to the local Memoria host" >&2
    exit 2
    ;;
esac

interval="${MEMORIA_OFFSITE_SYNC_INTERVAL_S:-60}"
wal_retention_days="${MEMORIA_WAL_LOCAL_RETENTION_DAYS:-7}"
case "$interval:$wal_retention_days" in
  *[!0-9:]*|:*|*:|0:*|*:0)
    echo "sync interval and WAL retention must be positive integers" >&2
    exit 2
    ;;
esac

mc alias set --quiet local http://memoria-minio:9000 \
  "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc alias set --quiet offsite "$MEMORIA_OFFSITE_S3_ENDPOINT" \
  "$MEMORIA_OFFSITE_S3_ACCESS_KEY" "$MEMORIA_OFFSITE_S3_SECRET_KEY"
mc stat "offsite/$MEMORIA_OFFSITE_S3_BUCKET" >/dev/null
mc version info "offsite/$MEMORIA_OFFSITE_S3_BUCKET" | grep -q 'Enabled'

while :; do
  test -s /backup-staging/LATEST
  latest="$(tr -d '\r\n' </backup-staging/LATEST)"
  case "$latest" in
    ????[0-1][0-9][0-3][0-9]T[0-2][0-9][0-5][0-9][0-5][0-9]Z) ;;
    *) echo "invalid completed base-backup label" >&2; exit 2 ;;
  esac
  mc mirror --quiet --overwrite --preserve \
    "/backup-staging/$latest" \
    "offsite/$MEMORIA_OFFSITE_S3_BUCKET/postgres/base/$latest"
  mc cp --quiet /backup-staging/LATEST \
    "offsite/$MEMORIA_OFFSITE_S3_BUCKET/postgres/base/LATEST"
  mc mirror --quiet --overwrite --preserve \
    /wal-archive "offsite/$MEMORIA_OFFSITE_S3_BUCKET/postgres/wal"
  mc mirror --quiet --overwrite --preserve \
    local/memoria-archive \
    "offsite/$MEMORIA_OFFSITE_S3_BUCKET/minio/memoria-archive"
  mc mirror --quiet --overwrite --preserve \
    local/memoria-voice \
    "offsite/$MEMORIA_OFFSITE_S3_BUCKET/minio/memoria-voice"
  date -u +%Y-%m-%dT%H:%M:%SZ >/tmp/offsite-mirror.last-success
  find /wal-archive -mindepth 1 -maxdepth 1 -type f \
    -mtime "+$wal_retention_days" -delete
  sleep "$interval"
done
