#!/bin/sh
set -eu

umask 077

env_file="${MEMORIA_OFFSITE_BACKUP_ENV_FILE:-/etc/memoria-offsite-backup.env}"
restore_root="${MEMORIA_OFFSITE_RESTORE_ROOT:?MEMORIA_OFFSITE_RESTORE_ROOT is required}"
report="${MEMORIA_OFFSITE_RESTORE_REPORT:?MEMORIA_OFFSITE_RESTORE_REPORT is required}"
postgres_image="${MEMORIA_RESTORE_POSTGRES_IMAGE:-pgvector/pgvector:0.8.1-pg17-bookworm}"
mc_image="${MEMORIA_RESTORE_MC_IMAGE:-minio/mc:RELEASE.2025-04-16T18-13-26Z}"
container="memoria-offsite-restore-$$"

test -f "$env_file"
test ! -e "$restore_root"
test ! -e "$report"
mkdir -m 700 -p "$restore_root" "$(dirname "$report")"
started_epoch="$(date +%s)"

cleanup() {
  docker rm -f "$container" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

docker run --rm \
  --env-file "$env_file" \
  --volume "$restore_root:/restore" \
  --entrypoint /bin/sh \
  "$mc_image" -c '
    set -eu
    : "${MEMORIA_OFFSITE_S3_ENDPOINT:?}"
    : "${MEMORIA_OFFSITE_S3_ACCESS_KEY:?}"
    : "${MEMORIA_OFFSITE_S3_SECRET_KEY:?}"
    : "${MEMORIA_OFFSITE_S3_BUCKET:?}"
    case "$MEMORIA_OFFSITE_S3_ENDPOINT" in
      *localhost*|*127.0.0.1*|*memoria-minio*|*host.docker.internal*) exit 2 ;;
    esac
    mc alias set --quiet offsite "$MEMORIA_OFFSITE_S3_ENDPOINT" \
      "$MEMORIA_OFFSITE_S3_ACCESS_KEY" "$MEMORIA_OFFSITE_S3_SECRET_KEY"
    mc cp --quiet \
      "offsite/$MEMORIA_OFFSITE_S3_BUCKET/postgres/base/LATEST" /restore/LATEST
    latest="$(tr -d "\r\n" </restore/LATEST)"
    case "$latest" in
      ????[0-1][0-9][0-3][0-9]T[0-2][0-9][0-5][0-9][0-5][0-9]Z) ;;
      *) exit 2 ;;
    esac
    mkdir -p /restore/postgres /restore/objects/archive /restore/objects/voice
    mc mirror --quiet \
      "offsite/$MEMORIA_OFFSITE_S3_BUCKET/postgres/base/$latest" /restore/postgres
    mc mirror --quiet \
      "offsite/$MEMORIA_OFFSITE_S3_BUCKET/minio/memoria-archive" \
      /restore/objects/archive
    mc mirror --quiet \
      "offsite/$MEMORIA_OFFSITE_S3_BUCKET/minio/memoria-voice" \
      /restore/objects/voice
  '

test -s "$restore_root/LATEST"
test -s "$restore_root/postgres/backup_manifest"
docker run --rm \
  --volume "$restore_root/postgres:/backup:ro" \
  --entrypoint pg_verifybackup \
  "$postgres_image" /backup
docker run --rm \
  --volume "$restore_root/postgres:/data" \
  --entrypoint /bin/sh \
  "$postgres_image" -c 'chown -R postgres:postgres /data && chmod 700 /data'
docker run --detach \
  --name "$container" \
  --network none \
  --volume "$restore_root/postgres:/var/lib/postgresql/data" \
  "$postgres_image" postgres -c listen_addresses= >/dev/null

ready=false
attempt=0
while test "$attempt" -lt 30; do
  if docker exec "$container" pg_isready -U memoria_admin -d postgres >/dev/null 2>&1; then
    ready=true
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done
test "$ready" = true

docker exec "$container" psql -v ON_ERROR_STOP=1 -U memoria_admin -d memoria -Atc \
  "SELECT to_regclass('public.archive_evidence_events') IS NOT NULL
       AND to_regclass('public.guardian_links') IS NOT NULL
       AND to_regclass('public.tutor_practice_sessions') IS NOT NULL" \
  | grep -qx t

archive_keys="$restore_root/archive-object-keys.txt"
voice_keys="$restore_root/voice-object-keys.txt"
docker exec "$container" psql -v ON_ERROR_STOP=1 -U memoria_admin -d memoria -Atc \
  "SELECT object_key FROM archive_evidence_blobs ORDER BY object_key" >"$archive_keys"
docker exec "$container" psql -v ON_ERROR_STOP=1 -U memoria_admin -d memoria -Atc \
  "SELECT object_key FROM voice_samples WHERE deleted_at IS NULL ORDER BY object_key" \
  >"$voice_keys"

archive_count=0
while IFS= read -r key; do
  test -z "$key" && continue
  test -f "$restore_root/objects/archive/$key"
  archive_count=$((archive_count + 1))
done <"$archive_keys"
voice_count=0
while IFS= read -r key; do
  test -z "$key" && continue
  test -f "$restore_root/objects/voice/$key"
  voice_count=$((voice_count + 1))
done <"$voice_keys"

finished_epoch="$(date +%s)"
rto_seconds=$((finished_epoch - started_epoch))
label="$(tr -d '\r\n' <"$restore_root/LATEST")"
temporary_report="${report}.tmp.$$"
printf '{"passed":true,"backup_label":"%s","rto_seconds":%s,"archive_objects":%s,"voice_objects":%s}\n' \
  "$label" "$rto_seconds" "$archive_count" "$voice_count" >"$temporary_report"
chmod 600 "$temporary_report"
mv "$temporary_report" "$report"
printf '{"passed":true,"report":"%s"}\n' "$report"
