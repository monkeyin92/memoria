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
offsite_retention_days="${MEMORIA_OFFSITE_RETENTION_DAYS:-7}"
case "$interval:$wal_retention_days:$offsite_retention_days" in
  *[!0-9:]*|:*|*:|0:*|*:0|*:0:*)
    echo "sync interval, WAL retention, and offsite retention must be positive integers" >&2
    exit 2
    ;;
esac

if [ "$offsite_retention_days" -lt 1 ] || [ "$offsite_retention_days" -gt 7 ]; then
  echo "MEMORIA_OFFSITE_RETENTION_DAYS must be between 1 and 7 days (got $offsite_retention_days)" >&2
  exit 2
fi

mc alias set --quiet local http://memoria-minio:9000 \
  "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
mc alias set --quiet offsite "$MEMORIA_OFFSITE_S3_ENDPOINT" \
  "$MEMORIA_OFFSITE_S3_ACCESS_KEY" "$MEMORIA_OFFSITE_S3_SECRET_KEY"
mc stat "offsite/$MEMORIA_OFFSITE_S3_BUCKET" >/dev/null
mc version info "offsite/$MEMORIA_OFFSITE_S3_BUCKET" | grep -q 'Enabled'

# This dedicated bucket's complete policy is owned by this script. mc's pinned
# export serializer omits an empty Filter and emits these fields in this order.
expected_ilm='{"Rules":[{"Expiration":{"Days":'"$offsite_retention_days"'},"ID":"memoria-offsite-retention","NoncurrentVersionExpiration":{"NoncurrentDays":'"$offsite_retention_days"'},"Status":"Enabled"}]}'
printf '%s\n' "$expected_ilm" | mc ilm rule import "offsite/$MEMORIA_OFFSITE_S3_BUCKET"

while :; do
  # Capture the command status separately; a pipeline would hide query failure.
  actual_ilm="$(mc ilm rule export "offsite/$MEMORIA_OFFSITE_S3_BUCKET")"
  actual_ilm="$(printf '%s' "$actual_ilm" | tr -d '[:space:]')"
  if [ "$actual_ilm" != "$expected_ilm" ]; then
    echo "offsite lifecycle readback does not match the required full-bucket expiration policy" >&2
    exit 2
  fi
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
