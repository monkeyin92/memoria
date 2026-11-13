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
if version_info="$(mc --no-color version info "offsite/$MEMORIA_OFFSITE_S3_BUCKET" 2>&1)"; then
  if [ "$version_info" != "offsite/$MEMORIA_OFFSITE_S3_BUCKET versioning is enabled" ]; then
    echo "offsite version info is not the pinned mc enabled output; refusing lifecycle changes" >&2
    exit 2
  fi
else
  version_status=$?
  echo "offsite version query failed; refusing lifecycle changes" >&2
  exit "$version_status"
fi

# Piped mc export uses colorjson's compact non-TTY branch. Exact comparison
# preserves spaces inside strings and rejects extra rules.
retention_policy() {
  printf '{"Rules":[{"Expiration":{"Days":%s},"ID":"memoria-offsite-retention","NoncurrentVersionExpiration":{"NoncurrentDays":%s},"Status":"%s"}]}' "$1" "$2" "$3"
}

export_policy() {
  if actual_ilm="$(mc --no-color ilm rule export "offsite/$MEMORIA_OFFSITE_S3_BUCKET" 2>&1)"; then
    export_status=0
  else
    export_status=$?
  fi
}

# mc's zero-rule and MinIO NoSuchLifecycleConfiguration errors, formatted by
# cmd/error.go and pkg/v3/console. Other backend messages require revalidation.
policy_is_empty() {
  [ "$export_status" -eq 1 ] || return 1
  case "$actual_ilm" in
    'mc: <ERROR> Unable to export lifecycle configuration: lifecycle configuration not set.'|\
    'mc: <ERROR> Unable to get lifecycle configuration. The lifecycle configuration does not exist.') return 0 ;;
    *) return 1 ;;
  esac
}

export_policy
if [ "$export_status" -eq 0 ]; then
  current_days="${actual_ilm#*'"Days":'}"
  current_days="${current_days%%\}*}"
  noncurrent_days="${actual_ilm#*'"NoncurrentDays":'}"
  noncurrent_days="${noncurrent_days%%\}*}"
  case "$current_days:$noncurrent_days" in
    *[!0-9:]*|:*|*:|0:*|*:0) current_days="" ;;
  esac
  owned=false
  if [ -n "$current_days" ]; then
    for status in Enabled Disabled; do
      if [ "$actual_ilm" = "$(retention_policy "$current_days" "$noncurrent_days" "$status")" ]; then
        owned=true
      fi
    done
  fi
  if [ "$owned" != true ]; then
    echo "offsite lifecycle ownership cannot be proven from pinned mc export; refusing import (foreign rules or unknown format)" >&2
    printf 'actual lifecycle export: %s\n' "$actual_ilm" >&2
    exit 2
  fi
elif ! policy_is_empty; then
  echo "offsite lifecycle ownership query failed; refusing import" >&2
  exit "$export_status"
fi

expected_ilm="$(retention_policy "$offsite_retention_days" "$offsite_retention_days" Enabled)"
if printf '%s\n' "$expected_ilm" | mc ilm rule import "offsite/$MEMORIA_OFFSITE_S3_BUCKET"; then
  :
else
  import_status=$?
  echo "offsite lifecycle import failed; stopping sync" >&2
  exit "$import_status"
fi

while :; do
  export_policy
  if [ "$export_status" -ne 0 ]; then
    if policy_is_empty; then
      echo "offsite lifecycle readback is empty (policy drift); stopping sync" >&2
      exit 2
    fi
    echo "offsite lifecycle readback query failed; stopping sync" >&2
    exit "$export_status"
  fi
  if [ "$actual_ilm" != "$expected_ilm" ]; then
    echo "offsite lifecycle readback differs from the required pinned mc output (policy drift or unknown format); stopping sync" >&2
    printf 'actual lifecycle export: %s\n' "$actual_ilm" >&2
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
