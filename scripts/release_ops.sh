#!/usr/bin/env bash
# Server-side full-stack release steps (run as root). Usage:
#   TAG=... COMMIT=... release_ops.sh <step>
#   steps: verify-load | freeze | env | schema | cutover | finish | rollback
# Never prints secret values. Every step fails closed.
#
# Installed on the host as /root/memoria-release/release-ops.sh (root 0700).
# The PREV_* constants describe the chain this release replaces; they were
# read-only checked on production after the 20260926-edge-flush-v1 release
# and must be re-checked before each full-stack release. The freeze step refuses
# to run when the live containers are on any other chain.
set -Eeuo pipefail
: "${TAG:?}" "${COMMIT:?}"
U=/opt/memoria/incoming/$TAG
R=/opt/memoria/releases/$TAG
S=$R/.cutover
# The stack this release replaces: the rollback target and its identity.
PREV_TAG=20260926-edge-flush-v1
PREV_COMMIT=fa8a97d51ca799b53e09e014dd69b78b0f4e18ab
PREV=/opt/memoria/releases/$PREV_TAG
# PostgreSQL still bind-mounts its schema files from this older tree, so schema
# upgrades are written there (in place, keeping the inode) -- never into PREV.
DATA_TREE=/opt/memoria/releases/20260827-architecture-split-v1
# Every target runs from the plain PREV compose file and returns to it on
# rollback. media-edge is released separately and is not touched here.
PREV_STACK_SERVICES=(speaker-model control-api agent voice-core-media-bridge miniprogram-gateway device-media-gateway)
ROLES=(agent control-api device-media-gateway miniprogram-gateway speaker-model)
TARGETS=(memoria-speaker-model-1 memoria-control-api-1 memoria-agent-1 memoria-voice-core-media-bridge-1 memoria-miniprogram-gateway-1 memoria-device-media-gateway-1)
log() { printf '[%s] %s\n' "$(date +%T)" "$*"; }

live_chain() {
  docker inspect "$1" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
}

# One status line per container. Some memoria containers (LiveKit, the
# SenseVoice sidecar) define no healthcheck, and a bare .State.Health.Status
# template aborts the whole step under set -e/pipefail.
container_state() {
  docker inspect "$1" --format '{{.Name}} {{.Config.Image}} {{.Image}} {{.State.StartedAt}} {{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}} restarts={{.RestartCount}}'
}

memoria_container_states() {
  local c
  for c in $(docker ps --format '{{.Names}}' | grep '^memoria' | sort); do
    container_state "$c"
  done
}

# P1-11: a device serves one bound subject and voiceprint authority is off.
# 20260925-full-stack-v1 shipped with a stale MEMORIA_SPEAKER_AUTHORITY_ENABLED=true
# in /etc/memoria-agent.env and the first device greeting was dropped as
# target_non_owner. Check the value the candidate agent and bridge actually see,
# accepting only values that every reader treats as off.
assert_speaker_authority_disabled() {
  local svc
  for svc in agent voice-core-media-bridge; do
    new_compose run --rm --no-deps -T --entrypoint /app/.venv/bin/python "$svc" -c '
import os, sys
raw = os.environ.get("MEMORIA_SPEAKER_AUTHORITY_ENABLED", "")
if raw.strip().lower() not in {"", "0", "false", "no", "off", "f", "n"}:
    print("INVALID MEMORIA_SPEAKER_AUTHORITY_ENABLED must be false (P1-11); got a value that enables it")
    sys.exit(1)
print("speaker_authority_disabled=PASS")' || { log "speaker authority must be disabled for $svc"; exit 1; }
  done
}

wait_healthy() {
  local status
  for _ in $(seq 1 60); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$1" 2>/dev/null || true)"
    [[ "$status" == healthy ]] && return 0
    [[ "$status" == no-healthcheck ]] && { log "NO HEALTHCHECK: $1 cannot be waited on"; return 1; }
    sleep 3
  done
  log "UNHEALTHY: $1"; docker logs --tail 40 "$1" 2>&1 | sed 's/^/  | /'; return 1
}

new_compose() {
  (cd "$R" && env MEMORIA_RELEASE_TAG="$TAG" MEMORIA_RELEASE_COMMIT="$COMMIT" \
    docker compose -p memoria --project-directory "$R" -f "$R/docker-compose.production.yml" \
    --profile media-runtime "$@")
}

step_verify_load() {
  : "${VERIFIER_SHA:?}" "${MANIFEST_SHA:?}"
  printf '%s  %s\n' "$VERIFIER_SHA" "$U/release-verifier.pyz" | sha256sum -c -
  printf '%s  %s\n' "$MANIFEST_SHA" "$U/release-manifest.json" | sha256sum -c -
  python3 "$U/release-verifier.pyz" --manifest "$U/release-manifest.json" --artifact-dir "$U" \
    --expected-tag "$TAG" --expected-commit "$COMMIT"
  for r in "${ROLES[@]}"; do
    if docker image inspect "memoria-$r:$TAG" >/dev/null 2>&1; then log "image already present: memoria-$r:$TAG"; exit 1; fi
  done
  docker load -i "$U/images.tar"
  python3 "$U/release-verifier.pyz" --manifest "$U/release-manifest.json" --artifact-dir "$U" \
    --expected-tag "$TAG" --expected-commit "$COMMIT" --verify-imported-images
  [[ ! -e "$R" ]] || { log "release dir exists: $R"; exit 1; }
  install -d -o root -g root -m 0755 "$R"
  tar --extract --file "$U/source.tar" --directory "$R" --strip-components=1 --no-same-owner
  printf 'MEMORIA_RELEASE_COMMIT=%s\nMEMORIA_RELEASE_TAG=%s\n' "$COMMIT" "$TAG" > "$R/.env"
  chmod 0644 "$R/.env"
  bash "$R/scripts/smoke_server_deployment.sh" "$TAG"
  log "verify_load=PASS"
}

step_freeze() {
  [[ -d "$R" ]]
  install -d -m 0700 "$S"
  # Every target must be on the chain the rollback rebuilds.
  local cf c
  for c in "${TARGETS[@]}"; do
    cf="$(live_chain "$c")"
    [[ "$cf" == "$PREV/docker-compose.production.yml" ]] || { log "unexpected live chain for $c: $cf"; exit 1; }
  done
  [[ "$(readlink -f /opt/memoria/current)" == "$PREV" ]] || { log "/opt/memoria/current is not $PREV"; exit 1; }
  for c in "${TARGETS[@]}"; do
    local img; img="$(docker inspect "$c" --format '{{.Config.Image}}')"
    local repo="${img%%:*}"
    docker tag "$(docker inspect "$c" --format '{{.Image}}')" "$repo:rollback-$TAG-pre"
  done
  for c in $(docker ps --format '{{.Names}}' | grep '^memoria' | sort); do
    docker inspect "$c" --format '{{.Name}} {{.Config.Image}} {{.Image}} {{.State.StartedAt}} {{index .Config.Labels "com.docker.compose.project.config_files"}}'
    docker inspect "$c" --format '{{json .HostConfig.PortBindings}} {{json .Mounts}}' | sha256sum | sed "s|^|$c ports+mounts |"
    docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' | cut -d= -f1 | sort | sha256sum | sed "s|^|$c envkeys |"
  done > "$S/pre-state.txt"
  cp -p /etc/memoria-postgres.env /etc/memoria-control-api.env /etc/memoria-agent.env "$S/"
  docker exec memoria-data-postgres-1 pg_dump -U memoria_admin -d memoria -Fc > "$S/memoria-pre-$TAG.dump"
  docker exec memoria-control-api-1 /app/.venv/bin/python -c "import sqlite3; s=sqlite3.connect('/data/memoria.sqlite3'); d=sqlite3.connect('/data/memoria.pre-$TAG.sqlite3'); s.backup(d); d.close()"
  ls -la "$S" | sed 's/^/  /'
  log "freeze=PASS"
}

step_env() {
  [[ -f "$S/memoria-control-api.env" && -f "$S/memoria-postgres.env" ]] || { log "run freeze first"; exit 1; }
  if ! grep -q '^MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD=' /etc/memoria-postgres.env; then
    local pw; pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf 'MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD=%s\n' "$pw" >> /etc/memoria-postgres.env
    grep -q '^MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL=' /etc/memoria-control-api.env \
      || printf 'MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL=postgresql://memoria_memory_maintenance:%s@memoria-postgres:5432/memoria\n' "$pw" >> /etc/memoria-control-api.env
    unset pw
  fi
  # The DSN host must match the one the other maintenance DSNs use.
  local host_other host_new
  host_other="$(sed -n 's/^MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL=postgresql:\/\/[^@]*@\([^/]*\)\/.*/\1/p' /etc/memoria-control-api.env)"
  host_new="$(sed -n 's/^MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL=postgresql:\/\/[^@]*@\([^/]*\)\/.*/\1/p' /etc/memoria-control-api.env)"
  [[ -n "$host_other" && "$host_other" == "$host_new" ]] || { log "maintenance DSN host mismatch: '$host_new' vs '$host_other'"; exit 1; }
  sed -i -E "s/^MEMORIA_RELEASE_COMMIT=.*/MEMORIA_RELEASE_COMMIT=$COMMIT/; s/^MEMORIA_RELEASE_TAG=.*/MEMORIA_RELEASE_TAG=$TAG/" /etc/memoria-control-api.env
  for f in postgres control-api agent; do
    echo "key diff $f:"; diff <(cut -d= -f1 "$S/memoria-$f.env" | sort) <(cut -d= -f1 "/etc/memoria-$f.env" | sort) | sed 's/^/  /' || true
  done
  new_compose run --rm --no-deps -T --entrypoint /app/.venv/bin/python control-api -c '
from pydantic import ValidationError
from services.control_api.app.config import ControlSettings
try:
    ControlSettings().validate_production()
except ValidationError as e:
    print("INVALID", [(".".join(map(str, x["loc"])), x["msg"]) for x in e.errors()]); raise SystemExit(1)
except ValueError as e:
    print("INVALID", e); raise SystemExit(1)
print("control_env_valid=PASS")'
  new_compose run --rm --no-deps -T --entrypoint /app/.venv/bin/python agent -m scripts.verify_env
  assert_speaker_authority_disabled
  new_compose run --rm --no-deps -T -e MEMORIA_PROVIDER_SMOKE_REQUIRED=true \
    --entrypoint /app/.venv/bin/python agent -m scripts.provider_smoke_test
  log "env=PASS"
}

step_schema() {
  local BK="$DATA_TREE/.pre-$TAG-schema-backup"
  local files=(infra/postgres/init-memoria.sh services/identity/postgres_schema.sql services/consent/postgres_schema.sql
    services/policy/postgres_receipt_schema.sql services/device_fleet/postgres_schema.sql
    services/session_runtime/postgres_schema.sql services/evolution/postgres_schema.sql
    services/guardian/postgres_schema.sql services/memory_scope/postgres_schema.sql
    services/device_fleet/bootstrap_postgres_schema.sql)
  install -d -m 0700 "$BK"
  for f in "${files[@]}"; do
    if ! cmp -s "$DATA_TREE/$f" "$R/$f"; then
      [[ -e "$BK/$f" ]] || install -D -m 0644 "$DATA_TREE/$f" "$BK/$f"
      cp "$R/$f" "$DATA_TREE/$f"   # cp keeps the inode the running container bind-mounts
      log "schema file updated: $f"
    fi
  done
  (set -a; . /etc/memoria-postgres.env; set +a
    POSTGRES_CONTAINER=memoria-data-postgres-1 sh "$R/scripts/upgrade_authoritative_postgres.sh") \
    2>&1 | grep -vE '^(psql:.*NOTICE|NOTICE|SET|CREATE|ALTER|GRANT|REVOKE|DO|COMMENT|DROP|INSERT|BEGIN|COMMIT|RESET)' | tail -5
  POSTGRES_CONTAINER=memoria-data-postgres-1 sh "$R/scripts/verify_authoritative_postgres.sh"
  log "schema=PASS"
}

step_cutover() {
  new_compose config --format json | python3 -c '
import json, sys, os
t = os.environ["TAG"]; s = json.load(sys.stdin)["services"]
want = {"speaker-model": "memoria-speaker-model", "control-api": "memoria-control-api", "agent": "memoria-agent",
        "voice-core-media-bridge": "memoria-agent", "miniprogram-gateway": "memoria-miniprogram-gateway",
        "device-media-gateway": "memoria-device-media-gateway"}
bad = [k for k, v in want.items() if s[k]["image"] != f"{v}:{t}"]
assert not bad, bad
print("resolve=PASS")'
  log "speaker-model";  new_compose up -d --no-deps --no-build --force-recreate speaker-model && wait_healthy memoria-speaker-model-1
  log "control-api";    new_compose up -d --no-deps --no-build --force-recreate control-api && wait_healthy memoria-control-api-1
  log "agent+bridge";   new_compose up -d --no-deps --no-build --force-recreate agent voice-core-media-bridge \
    && wait_healthy memoria-agent-1 && wait_healthy memoria-voice-core-media-bridge-1
  log "gateways";       new_compose up -d --no-deps --no-build --force-recreate miniprogram-gateway device-media-gateway \
    && wait_healthy memoria-miniprogram-gateway-1 && wait_healthy memoria-device-media-gateway-1
  log "cutover=PASS"
}

step_finish() {
  ln -sfn "$R" /opt/memoria/current.new && mv -T /opt/memoria/current.new /opt/memoria/current
  log "current -> $(readlink -f /opt/memoria/current)"
  /opt/memoria/current/scripts/refresh_readiness.sh
  systemctl reset-failed memoria-readiness-refresh.service || true
  systemctl start memoria-readiness-refresh.service
  systemctl show -p Result,ExecMainStatus memoria-readiness-refresh.service
  curl -fsS http://127.0.0.1:8791/health/ready | python3 -c 'import json,sys; b=json.load(sys.stdin); print("ready:", b["status"], b["release_tag"], b["checks"]["agent"]["status"])'
  curl -fsS -o /dev/null https://aigcnice.com:8443/memoria-api/health/ready && echo external_ready=200
  memoria_container_states | tee "$S/post-state.txt"
  if grep -E ' (unhealthy|starting) restarts=' "$S/post-state.txt"; then
    log "containers not healthy after finish (see above)"; exit 1
  fi
  docker logs --since 10m memoria-media-edge-1 2>&1 | grep -iE 'voice.?core|grpc|connect' | tail -5 || true
  log "finish=PASS"
}

step_rollback() {
  log "ROLLBACK: restoring env files"
  cp -p "$S/memoria-control-api.env" /etc/memoria-control-api.env
  cp -p "$S/memoria-agent.env" /etc/memoria-agent.env
  ln -sfn "$PREV" /opt/memoria/current.new && mv -T /opt/memoria/current.new /opt/memoria/current
  local ID=(MEMORIA_RELEASE_TAG="$PREV_TAG" MEMORIA_RELEASE_COMMIT="$PREV_COMMIT")
  (cd "$PREV" && env "${ID[@]}" docker compose -p memoria --project-directory "$PREV" \
    -f "$PREV/docker-compose.production.yml" --profile media-runtime \
    up -d --no-deps --no-build --force-recreate "${PREV_STACK_SERVICES[@]}")
  local c
  for c in "${TARGETS[@]}"; do
    wait_healthy "$c"
  done
  MEMORIA_RELEASE_TAG="$PREV_TAG" /opt/memoria/current/scripts/refresh_readiness.sh || true
  memoria_container_states
  log "rollback done; compare image ids with $S/pre-state.txt"
}

case "${1:-}" in
  verify-load) step_verify_load ;;
  freeze) step_freeze ;;
  env) step_env ;;
  schema) step_schema ;;
  cutover) step_cutover ;;
  finish) step_finish ;;
  rollback) step_rollback ;;
  *) echo "usage: $0 verify-load|freeze|env|schema|cutover|finish|rollback" >&2; exit 2 ;;
esac
