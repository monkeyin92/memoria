#!/usr/bin/env bash
# Server-side full-stack release steps (run as root). Usage:
#   TAG=... COMMIT=... release_ops.sh <step>
#   steps: verify-load | freeze | env | schema | cutover | finish | rollback
#          cleanup-freeze-dumps (expire .cutover freeze dumps older than 3 days)
#          control-store (CONTROL_STORE_MODE=dry-run|apply; separate, authorized
#          SQLite-to-PostgreSQL move of the Control API store, see the runbook)
# Never prints secret values. Every step fails closed.
#
# Installed on the host as /root/memoria-release/release-ops.sh (root 0700).
# The script names no PREV release: freeze derives the stack this release
# replaces from the live containers and writes it to $S/prev-release, and
# cutover, finish and rollback read only that file.
set -Eeuo pipefail
: "${TAG:?}" "${COMMIT:?}"

# The Compose services this script owns, one row each, in cutover order.
#   ship     this release builds memoria-<role> and cutover recreates the service.
#   retired  the candidate compose must not define it. If PREV still runs it,
#            cutover stops it and rollback recreates it from PREV's compose file.
# memoria-agent is the Voice Core media bridge image. media-edge is released
# separately and is not touched here. Delete a retired row only once no
# rollback target runs that service.
STACK='
control-api              control-api    ship
voice-core-media-bridge  agent          ship
speaker-model            -              retired
agent                    -              retired
miniprogram-gateway      -              retired
device-media-gateway     -              retired
'
stack() { awk -v lifecycle="$1" -v column="$2" 'NF == 3 && $3 == lifecycle { print $column }' <<<"$STACK"; }
mapfile -t SHIP < <(stack ship 1)
mapfile -t ROLES < <(stack ship 2)
mapfile -t RETIRED < <(stack retired 1)
is_ship() { [[ " ${SHIP[*]} " == *" $1 "* ]]; }
container_of() { printf 'memoria-%s-1' "$1"; }
ship_images() { local i; for i in "${!SHIP[@]}"; do printf '%s=memoria-%s ' "${SHIP[$i]}" "${ROLES[$i]}"; done; }

release_paths() {
  RELEASES=$1/releases
  CURRENT=$1/current
  U=$1/incoming/$TAG
  R=$RELEASES/$TAG
  S=$R/.cutover
  # PostgreSQL still bind-mounts its schema files from this older tree, so schema
  # upgrades are written there (in place, keeping the inode) -- never into PREV.
  DATA_TREE=$RELEASES/20260827-architecture-split-v1
}
release_paths /opt/memoria

log() { printf '[%s] %s\n' "$(date +%T)" "$*"; }
fail() { log "$*" >&2; exit 1; }

# The host port the Control API publishes: an explicit MEMORIA_CONTROL_API_PORT,
# else the port the live container already publishes, else the repo default
# 8791. 110.42.235.198 shares the host with hr-tracker, which holds 8791, so
# there the Control API sits on 18791; verify-load copies the answer into the
# release dir's .env so Compose publishes the same port after cutover.
# (refresh_readiness.sh carries the same function.)
control_api_port() {
  local port
  port="${MEMORIA_CONTROL_API_PORT:-}"
  if [[ -z "$port" ]]; then
    port="$(docker port memoria-control-api-1 8000/tcp 2>/dev/null \
      | sed -n 's/^127\.0\.0\.1:\([0-9][0-9]*\)$/\1/p' | head -n 1 || true)"
  fi
  printf '%s' "${port:-8791}"
}

# The container facts freeze and cutover decide on. An absent container fails.
inspect_container() {   # <container> chain|version|revision|image|image-id|running
  local format
  case "$2" in
    chain) format='{{index .Config.Labels "com.docker.compose.project.config_files"}}' ;;
    version|revision) format="{{index .Config.Labels \"org.opencontainers.image.$2\"}}" ;;
    image) format='{{.Config.Image}}' ;;
    image-id) format='{{.Image}}' ;;
    running) format='{{.State.Running}}' ;;
    *) fail "inspect_container: unknown field $2" ;;
  esac
  docker inspect "$1" --format "$format"
}

# One status line per container. Some memoria containers (the SenseVoice
# sidecar, a LiveKit server left on the host) define no healthcheck, and a bare .State.Health.Status
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
# target_non_owner. Check the value the candidate bridge actually sees (the
# LiveKit worker that also read it is retired), accepting only values that every
# reader treats as off.
assert_speaker_authority_disabled() {
  local svc
  for svc in voice-core-media-bridge; do
    new_compose run --rm --no-deps -T --entrypoint /app/.venv/bin/python "$svc" -c '
import os, sys
raw = os.environ.get("MEMORIA_SPEAKER_AUTHORITY_ENABLED", "")
if raw.strip().lower() not in {"", "0", "false", "no", "off", "f", "n"}:
    print("INVALID MEMORIA_SPEAKER_AUTHORITY_ENABLED must be false (P1-11); got a value that enables it")
    sys.exit(1)
print("speaker_authority_disabled=PASS")' || { log "speaker authority must be disabled for $svc"; exit 1; }
  done
}

HOST_LOG_DIR=/var/log/memoria

host_log_size() {
  local service="$1"
  stat -c %s "$HOST_LOG_DIR/$service.log" 2>/dev/null || echo 0
}

assert_host_log_has() {
  local service="$1" before="$2" marker="$3"
  local attempt size skip current older
  for attempt in 1 2 3 4 5; do
    size="$(host_log_size "$service")"
    older=""
    if [[ "$size" -lt "$before" ]]; then
      skip=0
      # The active file was replaced. Bytes past the snapshot now live in .1.
      older="$(tail -c +$((before + 1)) "$HOST_LOG_DIR/$service.log.1" 2>/dev/null || true)"
    else
      skip=$before
    fi
    current="$(tail -c +$((skip + 1)) "$HOST_LOG_DIR/$service.log" 2>/dev/null || true)"
    if printf '%s\n%s\n' "$older" "$current" | grep -Eq "^[^ ]+ ${service}/[0-9a-f]{12} .*${marker}"; then
      return 0
    fi
    [[ "$attempt" == 5 ]] || sleep 1
  done
  fail "no $service line with $marker reached $HOST_LOG_DIR/$service.log; install the host log sink (docs/runbooks/release-rollback.md, 容器日志落盘) and rerun env"
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

release_compose() {   # <release dir> <tag> <commit> <compose args...>
  local dir=$1 tag=$2 commit=$3
  shift 3
  (cd "$dir" && env MEMORIA_RELEASE_TAG="$tag" MEMORIA_RELEASE_COMMIT="$commit" \
    docker compose -p memoria --project-directory "$dir" -f "$dir/docker-compose.production.yml" \
    --profile media-runtime "$@")
}
new_compose() { release_compose "$R" "$TAG" "$COMMIT" "$@"; }
prev_compose() { release_compose "$PREV_DIR" "$PREV_TAG" "$PREV_COMMIT" "$@"; }

# Prints the PREV record for the live stack: the release `current` names, its
# commit, and the services it runs. Every ship row must run on PREV's compose
# file; a retired row counts only if its container is on that file too.
derive_prev() {
  local dir tag compose s c chain version revision commit="" services=()
  dir="$(readlink -f "$CURRENT")"
  [[ "$(dirname "$dir")" == "$(readlink -f "$RELEASES")" ]] || fail "current is not a release dir: $dir"
  tag="$(basename "$dir")"
  [[ "$tag" != "$TAG" ]] || fail "current already points at $TAG; PREV would be this release"
  # Compose labels the chain with the -f path as release_compose passes it.
  compose=$RELEASES/$tag/docker-compose.production.yml
  for s in "${SHIP[@]}" "${RETIRED[@]}"; do
    c="$(container_of "$s")"
    chain="$(inspect_container "$c" chain 2>/dev/null)" || chain=""
    if [[ "$chain" != "$compose" ]]; then
      is_ship "$s" && fail "unexpected live chain for $c: ${chain:-absent}"
      continue
    fi
    version="$(inspect_container "$c" version)" || fail "cannot read the image version of $c"
    revision="$(inspect_container "$c" revision)" || fail "cannot read the image revision of $c"
    [[ "$version" == "$tag" ]] || fail "$c runs $version, not $tag"
    [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || fail "$c image carries no commit label"
    [[ -z "$commit" || "$revision" == "$commit" ]] || fail "PREV images disagree on commit: $commit vs $revision"
    commit=$revision
    services+=("$s")
  done
  printf 'tag=%s\ncommit=%s\nservices=%s\n' "$tag" "$commit" "${services[*]}"
}

# Writes $S/prev-release once. A re-run must derive the same record: after
# cutover the live stack is this release, and PREV must not follow it.
freeze_prev() {
  local record note
  record="$(derive_prev)" || exit 1
  if [[ -e "$S/prev-release" ]]; then
    if [[ "$(<"$S/prev-release")" != "$record" ]]; then
      diff "$S/prev-release" <(printf '%s\n' "$record") >&2 || true
      fail "live stack no longer matches $S/prev-release; refusing to rewrite it"
    fi
    note="prev already frozen (unchanged)"
  else
    printf '%s\n' "$record" > "$S/prev-release.tmp"
    mv -f "$S/prev-release.tmp" "$S/prev-release"
    note="prev frozen"
  fi
  load_prev
  log "prev=$PREV_TAG@$PREV_COMMIT services=${PREV_SERVICES[*]}"
  log "$note: $S/prev-release"
}

# Sets PREV_TAG, PREV_COMMIT, PREV_SERVICES and PREV_DIR from $S/prev-release.
load_prev() {
  local key tag commit services s
  [[ -f "$S/prev-release" ]] || fail "no $S/prev-release: run freeze first; the rollback target is unknown"
  { IFS='=' read -r key tag && [[ "$key" == tag ]] \
    && IFS='=' read -r key commit && [[ "$key" == commit ]] \
    && IFS='=' read -r key services && [[ "$key" == services ]]; } < "$S/prev-release" \
    || fail "malformed $S/prev-release"
  [[ "$tag" =~ ^[0-9]{8}-[a-z0-9.-]+$ && "$tag" != "$TAG" ]] || fail "malformed $S/prev-release: tag=$tag"
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail "malformed $S/prev-release: commit=$commit"
  read -ra PREV_SERVICES <<<"$services"
  for s in "${PREV_SERVICES[@]}"; do
    [[ " ${SHIP[*]} ${RETIRED[*]} " == *" $s "* ]] || fail "malformed $S/prev-release: unknown service $s"
  done
  PREV_TAG=$tag
  PREV_COMMIT=$commit
  PREV_DIR=$RELEASES/$tag
  [[ -f "$PREV_DIR/docker-compose.production.yml" ]] || fail "PREV release tree is missing: $PREV_DIR"
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
  printf 'MEMORIA_RELEASE_COMMIT=%s\nMEMORIA_RELEASE_TAG=%s\nMEMORIA_CONTROL_API_PORT=%s\n' \
    "$COMMIT" "$TAG" "$(control_api_port)" > "$R/.env"
  chmod 0644 "$R/.env"
  bash "$R/scripts/smoke_server_deployment.sh" "$TAG"
  log "verify_load=PASS"
}

step_freeze() {
  [[ -d "$R" ]]
  install -d -m 0700 "$S"
  freeze_prev
  local s c img
  for s in "${PREV_SERVICES[@]}"; do
    c="$(container_of "$s")"
    img="$(inspect_container "$c" image)"
    docker tag "$(inspect_container "$c" image-id)" "${img%%:*}:rollback-$TAG-pre"
  done
  for c in $(docker ps --format '{{.Names}}' | grep '^memoria' | sort); do
    docker inspect "$c" --format '{{.Name}} {{.Config.Image}} {{.Image}} {{.State.StartedAt}} {{index .Config.Labels "com.docker.compose.project.config_files"}}'
    docker inspect "$c" --format '{{json .HostConfig.PortBindings}} {{json .Mounts}}' | sha256sum | sed "s|^|$c ports+mounts |"
    docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' | cut -d= -f1 | sort | sha256sum | sed "s|^|$c envkeys |"
  done > "$S/pre-state.txt"
  cp -p /etc/memoria-postgres.env /etc/memoria-control-api.env /etc/memoria-agent.env "$S/"
  docker exec memoria-data-postgres-1 pg_dump -U memoria_admin -d memoria -Fc > "$S/memoria-pre-$TAG.dump"
  # Control-store snapshot stays inside this release's .cutover (not /var/lib/memoria),
  # so freeze-dump retention can expire it with the pg_dump and never leave the releases tree.
  docker exec memoria-control-api-1 /app/.venv/bin/python -c "import sqlite3; s=sqlite3.connect('/data/memoria.sqlite3'); d=sqlite3.connect('/data/memoria.pre-$TAG.sqlite3'); s.backup(d); d.close()"
  docker cp "memoria-control-api-1:/data/memoria.pre-$TAG.sqlite3" "$S/memoria.pre-$TAG.sqlite3"
  docker exec memoria-control-api-1 rm -f "/data/memoria.pre-$TAG.sqlite3"
  ls -la "$S" | sed 's/^/  /'
  cleanup_freeze_dumps
  log "freeze=PASS"
}

# Expire freeze dumps under /opt/memoria/releases/*/.cutover only (3-day window).
cleanup_freeze_dumps() {
  local script root
  root="$(dirname "$RELEASES")"
  script="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/cleanup_freeze_dumps.py"
  [[ -f "$script" ]] || script="$R/scripts/cleanup_freeze_dumps.py"
  [[ -f "$script" ]] || fail "cleanup_freeze_dumps.py missing"
  python3 "$script" --root "$root" --retention-days 3 --apply
}

step_cleanup_freeze_dumps() {
  cleanup_freeze_dumps
  log "cleanup-freeze-dumps=PASS"
}

step_env() {
  [[ -f "$S/memoria-control-api.env" && -f "$S/memoria-postgres.env" ]] || { log "run freeze first"; exit 1; }
  local control_log_before bridge_log_before
  control_log_before="$(host_log_size control-api)"
  bridge_log_before="$(host_log_size voice-core-media-bridge)"
  if ! grep -q '^MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD=' /etc/memoria-postgres.env; then
    local pw; pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf 'MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD=%s\n' "$pw" >> /etc/memoria-postgres.env
    grep -q '^MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL=' /etc/memoria-control-api.env \
      || printf 'MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL=postgresql://memoria_memory_maintenance:%s@memoria-postgres:5432/memoria\n' "$pw" >> /etc/memoria-control-api.env
    unset pw
  fi
  # The control store role exists before its data moves; its DSN reaches the
  # control-api env only in the separate, authorized SQLite-to-PostgreSQL cutover.
  if ! grep -q '^MEMORIA_DB_CONTROL_PASSWORD=' /etc/memoria-postgres.env; then
    local control_pw; control_pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf 'MEMORIA_DB_CONTROL_PASSWORD=%s\n' "$control_pw" >> /etc/memoria-postgres.env
    unset control_pw
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
  new_compose run --rm --no-deps -T --entrypoint /app/.venv/bin/python voice-core-media-bridge -m scripts.verify_env
  assert_speaker_authority_disabled
  assert_host_log_has control-api "$control_log_before" control_env_valid=PASS
  assert_host_log_has voice-core-media-bridge "$bridge_log_before" speaker_authority_disabled=PASS
  new_compose run --rm --no-deps -T -e MEMORIA_PROVIDER_SMOKE_REQUIRED=true \
    --entrypoint /app/.venv/bin/python voice-core-media-bridge -m scripts.provider_smoke_test
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
    2>&1 | { grep -vE '^(psql:.*NOTICE|NOTICE|SET|CREATE|ALTER|GRANT|REVOKE|DO|COMMENT|DROP|INSERT|BEGIN|COMMIT|RESET)' || true; } | tail -5
  # The running container may predate the 011-control-schema.sql mount; the
  # file is idempotent, so apply it over stdin every release. A clean apply
  # prints only NOTICEs: grep then matches nothing and exits 1, which pipefail
  # would turn into a failed step (first seen on 20260928-review-batches-v1).
  docker exec -i memoria-data-postgres-1 psql -U memoria_admin -d memoria -v ON_ERROR_STOP=1 -q \
    < "$R/services/control_api/app/database/postgres_schema.sql" \
    2>&1 | { grep -vE '^(psql:.*NOTICE|NOTICE)' || true; } | tail -5
  # Versioned control migrations (database/migrations.py): each pending file
  # runs once, with its ledger row, in one transaction (psql -1).
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

step_cutover() {
  load_prev
  # The recreated Control API must publish the port the live one holds: a
  # candidate that falls back to 8791 on a shared host cannot bind, and by then
  # the old container is already gone.
  new_compose config --format json \
    | CONTROL_PORT="$(control_api_port)" SHIP_IMAGES="$(ship_images)" RETIRED="${RETIRED[*]}" python3 -c '
import json, sys, os
t = os.environ["TAG"]; s = json.load(sys.stdin)["services"]
want = dict(pair.split("=", 1) for pair in os.environ["SHIP_IMAGES"].split())
bad = [k for k, v in want.items() if s[k]["image"] != f"{v}:{t}"]
assert not bad, bad
published = sorted({str(p.get("published")) for p in s["control-api"].get("ports", [])})
assert published == [os.environ["CONTROL_PORT"]], (published, os.environ["CONTROL_PORT"])
retired = sorted(set(os.environ["RETIRED"].split()) & set(s))
assert not retired, f"retired services still defined: {retired}"
print("resolve=PASS")'
  local s
  for s in "${SHIP[@]}"; do
    log "$s"
    # Two statements: set -e ignores a failure on the left of &&.
    new_compose up -d --no-deps --no-build --force-recreate "$s"
    wait_healthy "$(container_of "$s")"
  done
  stop_orphans
  log "cutover=PASS"
}

# Stops (never removes) what PREV runs and this release no longer ships, once
# the ship rows are healthy; rollback recreates it from PREV's compose file.
stop_orphans() {
  local s c
  for s in "${PREV_SERVICES[@]}"; do
    is_ship "$s" && continue
    c="$(container_of "$s")"
    [[ "$(inspect_container "$c" running 2>/dev/null || true)" == true ]] || continue
    docker stop "$c" >/dev/null
    log "orphan stopped (rollback recreates it from PREV): $c"
  done
}

step_finish() {
  load_prev
  ln -sfn "$R" "$CURRENT.new" && mv -T "$CURRENT.new" "$CURRENT"
  log "current -> $(readlink -f "$CURRENT")"
  "$CURRENT/scripts/refresh_readiness.sh"
  systemctl reset-failed memoria-readiness-refresh.service || true
  systemctl start memoria-readiness-refresh.service
  systemctl show -p Result,ExecMainStatus memoria-readiness-refresh.service
  curl -fsS "http://127.0.0.1:$(control_api_port)/health/ready" | python3 -c 'import json,sys; b=json.load(sys.stdin); print("ready:", b["status"], b["release_tag"], b["checks"]["agent"]["status"])'
  curl -fsS -o /dev/null https://aginice.cn:8443/memoria-api/health/ready && echo external_ready=200
  memoria_container_states | tee "$S/post-state.txt"
  if grep -E ' (unhealthy|starting) restarts=' "$S/post-state.txt"; then
    log "containers not healthy after finish (see above)"; exit 1
  fi
  docker logs --since 10m memoria-media-edge-1 2>&1 | grep -iE 'voice.?core|grpc|connect' | tail -5 || true
  log "rollback target: $PREV_TAG@$PREV_COMMIT"
  log "finish=PASS"
}

# Rollback returns to PREV as a whole: the services and identity freeze wrote
# to $S/prev-release, PREV's compose file, its images (freeze tagged each
# rollback-$TAG-pre) and the env files freeze snapshotted.
step_rollback() {
  load_prev
  log "ROLLBACK to $PREV_TAG@$PREV_COMMIT: restoring env files"
  cp -p "$S/memoria-control-api.env" /etc/memoria-control-api.env
  cp -p "$S/memoria-agent.env" /etc/memoria-agent.env
  ln -sfn "$PREV_DIR" "$CURRENT.new" && mv -T "$CURRENT.new" "$CURRENT"
  prev_compose up -d --no-deps --no-build --force-recreate "${PREV_SERVICES[@]}"
  local s
  for s in "${PREV_SERVICES[@]}"; do
    wait_healthy "$(container_of "$s")"
  done
  MEMORIA_RELEASE_TAG="$PREV_TAG" "$CURRENT/scripts/refresh_readiness.sh" || true
  memoria_container_states
  log "rollback done; compare image ids with $S/pre-state.txt"
}

# The Control API store moves from /data/memoria.sqlite3 into PostgreSQL once,
# after a release whose schema step installed the memoria_control role and
# tables. dry-run copies a snapshot of the live file into PostgreSQL, verifies
# every table and rolls back; apply stops control-api, backs up both stores,
# migrates, then points control-api at PostgreSQL. Any failure after the stop
# restores the env file and starts control-api on SQLite again.
control_store_dsn() {
  python3 - <<'PY'
import urllib.parse
values = {}
for path in ("/etc/memoria-postgres.env", "/etc/memoria-control-api.env"):
    for line in open(path, encoding="utf-8"):
        key, sep, value = line.rstrip("\n").partition("=")
        if sep:
            values[key] = value
password = values.get("MEMORIA_DB_CONTROL_PASSWORD", "")
other = values.get("MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL", "")
host = urllib.parse.urlsplit(other).netloc.rpartition("@")[2]
if not password or not host:
    raise SystemExit("missing MEMORIA_DB_CONTROL_PASSWORD or the PostgreSQL host")
print(f"postgresql://memoria_control:{urllib.parse.quote(password, safe='')}@{host}/memoria")
PY
}

step_control_store() {
  local mode="${CONTROL_STORE_MODE:-}" env=/etc/memoria-control-api.env
  [[ "$mode" == dry-run || "$mode" == apply ]] || { log "set CONTROL_STORE_MODE=dry-run or apply"; exit 1; }
  if grep -q '^MEMORIA_CONTROL_DATABASE_URL=.' "$env"; then log "control store is already on PostgreSQL"; exit 1; fi
  [[ "$(readlink -f /opt/memoria/current)" == "$R" ]] || { log "run on the live release ($R is not current)"; exit 1; }
  POSTGRES_CONTAINER=memoria-data-postgres-1 sh "$R/scripts/verify_authoritative_postgres.sh"
  local out; out="$R/.control-store-$(date -u +%Y%m%dT%H%M%SZ)"
  install -d -m 0700 "$out"
  MEMORIA_CONTROL_DATABASE_URL="$(control_store_dsn)"; export MEMORIA_CONTROL_DATABASE_URL
  if [[ "$mode" == dry-run ]]; then
    local snap=/data/memoria.control-store-dry-run.sqlite3
    docker exec memoria-control-api-1 /app/.venv/bin/python -c "import sqlite3; s=sqlite3.connect('file:/data/memoria.sqlite3?mode=ro', uri=True); d=sqlite3.connect('$snap'); s.backup(d); d.close()"
    docker exec -e MEMORIA_CONTROL_DATABASE_URL memoria-control-api-1 /app/.venv/bin/python \
      scripts/migrate_control_sqlite_to_postgres.py --sqlite "$snap" --dry-run > "$out/dry-run-receipt.json"
    docker exec memoria-control-api-1 rm -f "$snap"
    unset MEMORIA_CONTROL_DATABASE_URL
    log "receipt: $out/dry-run-receipt.json"
    log "control-store dry-run=PASS"
    return
  fi
  local data; data="$(docker inspect memoria-control-api-1 --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}')"
  [[ -n "$data" && -f "$data/memoria.sqlite3" ]] || { log "control SQLite file not found on the host"; exit 1; }
  cp -p "$env" "$out/memoria-control-api.env"
  restore_sqlite_control() {
    log "control-store FAILED: restoring the env file and starting control-api on SQLite"
    cp -p "$out/memoria-control-api.env" "$env"
    new_compose up -d --no-deps --no-build --force-recreate control-api || true
    wait_healthy memoria-control-api-1 || true
  }
  trap restore_sqlite_control ERR
  new_compose stop control-api
  local f
  for f in memoria.sqlite3 memoria.sqlite3-wal memoria.sqlite3-shm; do
    [[ -e "$data/$f" ]] && cp -p "$data/$f" "$out/$f"
  done
  docker exec memoria-data-postgres-1 pg_dump -U memoria_admin -d memoria -Fc > "$out/memoria-pre-control-store.dump"
  new_compose run --rm --no-deps -T -e MEMORIA_CONTROL_DATABASE_URL --entrypoint /app/.venv/bin/python \
    control-api scripts/migrate_control_sqlite_to_postgres.py --sqlite /data/memoria.sqlite3 --apply \
    > "$out/apply-receipt.json"
  printf 'MEMORIA_CONTROL_DATABASE_URL=%s\n' "$MEMORIA_CONTROL_DATABASE_URL" >> "$env"
  unset MEMORIA_CONTROL_DATABASE_URL
  new_compose up -d --no-deps --no-build --force-recreate control-api
  wait_healthy memoria-control-api-1
  trap - ERR
  log "receipt: $out/apply-receipt.json; the SQLite file stays untouched for rollback"
  log "control-store apply=PASS"
}

main() {
  case "${1:-}" in
    verify-load) step_verify_load ;;
    freeze) step_freeze ;;
    env) step_env ;;
    schema) step_schema ;;
    cutover) step_cutover ;;
    finish) step_finish ;;
    rollback) step_rollback ;;
    cleanup-freeze-dumps) step_cleanup_freeze_dumps ;;
    control-store) step_control_store ;;
    *) echo "usage: $0 verify-load|freeze|env|schema|cutover|finish|rollback|cleanup-freeze-dumps|control-store" >&2; exit 2 ;;
  esac
}
# Sourcing only defines (the tests run single steps against a fake host).
[[ "${BASH_SOURCE[0]}" != "$0" ]] || main "$@"
