#!/usr/bin/env bash
# Memoria stack bring-up on the shared pocketSparks host (run as root; installed as
# /root/memoria-release/bringup.sh, mode 0700). It mirrors scripts/release_ops.sh (step_schema /
# step_cutover) and the start order of the old host, one explicit service name at a time:
#   net     speaker-model (creates the memoria_default network) + the SenseVoice sidecar
#   data    PostgreSQL + MinIO (+ the one-shot MinIO provisioning)   [project memoria-data]
#   redis   device-state-redis                                       [project memoria]
#   schema  empty-database path only: upgrade + 011 + versioned migrations + verify
#   verify  verify_authoritative_postgres.sh only (restored data path)
#   replay  scripts.replay_subject_deletions (restored data path, before traffic resumes)
#   apps    control-api -> voice-core-media-bridge -> media-edge (component override)
#   checks  control env / bridge env / speaker authority / provider smoke test
#   status  containers, memory, disk, health
#   down    stop and remove the containers of both projects (volumes and data stay)
# Never prints secret values. media-slo-reporter is deliberately not started (it was not running
# on the old host either).
set -Eeuo pipefail
TAG=${TAG:-20261004-first-warm-v1}
COMMIT=${COMMIT:-38dfa5fab88a333df7ef7c70e036350a4369e261}
R=/opt/memoria/releases/$TAG
DATA_TREE=/opt/memoria/releases/20260827-architecture-split-v1
EDGE_OVERRIDE=/opt/memoria/component-releases/20261002-late-progress-v1-media-edge/media-edge-component.override.yml
ASR_IMAGE=memoria-sensevoice-asr:20260901-pin-language
log() { printf '[%s] %s\n' "$(date +%T)" "$*"; }

app_compose() {
  (cd "$R" && env MEMORIA_RELEASE_TAG="$TAG" MEMORIA_RELEASE_COMMIT="$COMMIT" \
    docker compose -p memoria --project-directory "$R" -f "$R/docker-compose.production.yml" \
    --profile media-runtime "$@")
}

edge_compose() {
  (cd "$R" && env MEMORIA_RELEASE_TAG="$TAG" MEMORIA_RELEASE_COMMIT="$COMMIT" \
    docker compose -p memoria --project-directory "$R" -f "$R/docker-compose.production.yml" \
    -f "$EDGE_OVERRIDE" --profile media-runtime "$@")
}

data_compose() {
  (cd "$DATA_TREE/infra" && docker compose -p memoria-data --project-directory "$DATA_TREE/infra" \
    -f "$DATA_TREE/infra/memoria-data.production.yml" "$@")
}

wait_healthy() {
  local status
  for _ in $(seq 1 60); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$1" 2>/dev/null || true)"
    [[ "$status" == healthy ]] && { log "healthy: $1"; return 0; }
    [[ "$status" == no-healthcheck ]] && { log "NO HEALTHCHECK: $1 cannot be waited on"; return 1; }
    sleep 3
  done
  log "UNHEALTHY: $1"; docker logs --tail 40 "$1" 2>&1 | sed 's/^/  | /'; return 1
}

ensure_dirs() {
  # Bind-mount sources must exist with the container user's ownership (uid 65532).
  install -d -o 65532 -g 65532 -m 0750 /var/lib/memoria /var/lib/memoria-agent
  install -d -o 65532 -g 65532 -m 0700 /var/lib/memoria-device-state-redis
}

step_net() {
  ensure_dirs
  log "speaker-model"; app_compose up -d --no-deps --no-build speaker-model
  wait_healthy memoria-speaker-model-1
  docker image inspect "$ASR_IMAGE" >/dev/null
  if docker inspect memoria-sensevoice-asr >/dev/null 2>&1; then
    log "sensevoice-asr exists; starting it"; docker start memoria-sensevoice-asr >/dev/null
  else
    log "sensevoice-asr"
    docker run -d --name memoria-sensevoice-asr --network memoria_default --restart unless-stopped \
      --memory 1g --cpus 2 -v /opt/memoria/sidecars/sensevoice-asr/models:/models/sensevoice:ro "$ASR_IMAGE" >/dev/null
  fi
}

step_data() {
  log "postgres + minio"; data_compose up -d --no-build postgres minio
  wait_healthy memoria-data-postgres-1
  wait_healthy memoria-data-minio-1
  log "minio-provision (one-shot)"; data_compose up --no-build minio-provision 2>&1 | tail -n 8
  docker ps -a --filter name=memoria-data-minio-provision --format '  {{.Names}} {{.Status}}'
}

step_redis() {
  log "device-state-redis"; app_compose up -d --no-deps --no-build device-state-redis
  wait_healthy memoria-device-state-redis-1
}

step_schema() {
  (set -a; . /etc/memoria-postgres.env; set +a
    POSTGRES_CONTAINER=memoria-data-postgres-1 sh "$R/scripts/upgrade_authoritative_postgres.sh") \
    2>&1 | { grep -vE '^(psql:.*NOTICE|NOTICE|SET|CREATE|ALTER|GRANT|REVOKE|DO|COMMENT|DROP|INSERT|BEGIN|COMMIT|RESET)' || true; } | tail -5
  docker exec -i memoria-data-postgres-1 psql -U memoria_admin -d memoria -v ON_ERROR_STOP=1 -q \
    < "$R/services/control_api/app/database/postgres_schema.sql" \
    2>&1 | { grep -vE '^(psql:.*NOTICE|NOTICE)' || true; } | tail -5
  local mig v name
  for mig in "$R"/services/control_api/app/database/migrations/[0-9][0-9][0-9][0-9]_*.sql; do
    [[ -e "$mig" ]] || continue
    name="$(basename "$mig" .sql)"; v=$((10#${name:0:4}))
    if [[ "$(docker exec memoria-data-postgres-1 psql -U memoria_admin -d memoria -tAc \
        "SELECT count(*) FROM control_schema_migrations WHERE version >= $v")" != 0 ]]; then
      continue
    fi
    { cat "$mig"; printf "\nINSERT INTO control_schema_migrations (version, name, applied_at) VALUES (%d, '%s', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"'));\n" "$v" "$name"; } \
      | docker exec -i memoria-data-postgres-1 psql -U memoria_admin -d memoria -1 -v ON_ERROR_STOP=1 -q
    log "control migration applied: $name"
  done
  POSTGRES_CONTAINER=memoria-data-postgres-1 sh "$R/scripts/verify_authoritative_postgres.sh"
  log "schema=PASS"
}

step_verify() {
  POSTGRES_CONTAINER=memoria-data-postgres-1 sh "$R/scripts/verify_authoritative_postgres.sh"
  log "verify=PASS"
}

step_replay() {
  # runbook "数据层、备份与恢复": after restoring data, re-apply completed subject deletions before traffic resumes
  app_compose run --rm --no-deps -T --entrypoint /app/.venv/bin/python control-api \
    -m scripts.replay_subject_deletions --confirm-replay
}

step_apps() {
  log "control-api"; app_compose up -d --no-deps --no-build control-api && wait_healthy memoria-control-api-1
  log "bridge"; app_compose up -d --no-deps --no-build voice-core-media-bridge && wait_healthy memoria-voice-core-media-bridge-1
  log "media-edge"; edge_compose up -d --no-deps --no-build media-edge && wait_healthy memoria-media-edge-1
}

step_checks() {
  app_compose run --rm --no-deps -T --entrypoint /app/.venv/bin/python control-api -c '
from pydantic import ValidationError
from services.control_api.app.config import ControlSettings
try:
    ControlSettings().validate_production()
except ValidationError as e:
    print("INVALID", [(".".join(map(str, x["loc"])), x["msg"]) for x in e.errors()]); raise SystemExit(1)
except ValueError as e:
    print("INVALID", e); raise SystemExit(1)
print("control_env_valid=PASS")'
  app_compose run --rm --no-deps -T --entrypoint /app/.venv/bin/python voice-core-media-bridge -m scripts.verify_env
  app_compose run --rm --no-deps -T --entrypoint /app/.venv/bin/python voice-core-media-bridge -c '
import os, sys
raw = os.environ.get("MEMORIA_SPEAKER_AUTHORITY_ENABLED", "")
if raw.strip().lower() not in {"", "0", "false", "no", "off", "f", "n"}:
    print("INVALID MEMORIA_SPEAKER_AUTHORITY_ENABLED must be false (P1-11)"); sys.exit(1)
print("speaker_authority_disabled=PASS")'
  app_compose run --rm --no-deps -T -e MEMORIA_PROVIDER_SMOKE_REQUIRED=true \
    --entrypoint /app/.venv/bin/python voice-core-media-bridge -m scripts.provider_smoke_test
  log "checks=PASS"
}

step_status() {
  docker ps --format '{{.Names}} {{.Image}} {{.Status}}' | sort | sed 's/^/  /'
  echo "-- memory"; docker stats --no-stream --format '  {{.Name}} mem={{.MemUsage}} cpu={{.CPUPerc}}' 2>/dev/null | sort
  free -m | sed 's/^/  /'
  df -h / | sed 's/^/  /'
  echo "-- health"
  curl -fsS -m 5 http://127.0.0.1:18791/health/ready | python3 -c 'import json,sys; b=json.load(sys.stdin); print("  control-api ready:", b.get("status"), b.get("release_tag"), b.get("checks",{}).get("agent",{}).get("status"))' || echo "  control-api ready: FAILED"
}

step_down() {
  log "stopping media-edge, bridge, control-api, redis"
  edge_compose rm -sf media-edge 2>&1 | tail -n 2 || true
  app_compose rm -sf voice-core-media-bridge control-api device-state-redis 2>&1 | tail -n 4 || true
  log "stopping postgres, minio"
  data_compose down 2>&1 | tail -n 6 || true
  docker rm -f memoria-sensevoice-asr 2>/dev/null | sed 's/^/  removed /' || true
  app_compose down 2>&1 | tail -n 4 || true
  docker ps -a --format '  {{.Names}} {{.Status}}' | grep '^  memoria' || echo "  no memoria containers left"
}

case "${1:-}" in
  net) step_net ;;
  data) step_data ;;
  redis) step_redis ;;
  schema) step_schema ;;
  verify) step_verify ;;
  replay) step_replay ;;
  apps) step_apps ;;
  checks) step_checks ;;
  status) step_status ;;
  down) step_down ;;
  *) echo "usage: $0 net|data|redis|schema|verify|replay|apps|checks|status|down" >&2; exit 2 ;;
esac
