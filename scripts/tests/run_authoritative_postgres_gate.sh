#!/bin/sh
set -eu

ROOT="$(
  CDPATH= cd -- "$(dirname "$0")/../.." >/dev/null 2>&1
  pwd
)"
POSTGRES_IMAGE="${MEMORIA_TEST_POSTGRES_IMAGE:-pgvector/pgvector:0.8.1-pg17-bookworm}"
POSTGRES_CONTAINER="${MEMORIA_TEST_POSTGRES_CONTAINER:-memoria-authority-gate-$$}"
export POSTGRES_CONTAINER

cleanup() {
  docker rm -f "$POSTGRES_CONTAINER" >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM
cleanup

secret() {
  openssl rand -hex 32
}

export POSTGRES_PASSWORD="$(secret)"
export MEMORIA_DB_APP_PASSWORD="$(secret)"
export MEMORIA_DB_COMPILER_PASSWORD="$(secret)"
export MEMORIA_DB_EVOLUTION_PASSWORD="$(secret)"
export MEMORIA_DB_GUARDIAN_PASSWORD="$(secret)"
export MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD="$(secret)"
export MEMORIA_DB_GUARDIAN_WORKER_PASSWORD="$(secret)"
export MEMORIA_DB_IDENTITY_PASSWORD="$(secret)"
export MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD="$(secret)"
export MEMORIA_DB_CONSENT_PASSWORD="$(secret)"
export MEMORIA_DB_DEVICE_ONBOARDING_API_PASSWORD="$(secret)"
export MEMORIA_DB_DEVICE_ONBOARDING_MAINTENANCE_PASSWORD="$(secret)"
export MEMORIA_DB_SESSION_API_PASSWORD="$(secret)"
export MEMORIA_DB_ACTION_EXECUTOR_PASSWORD="$(secret)"
export MEMORIA_DB_SESSION_PROJECTOR_PASSWORD="$(secret)"
export MEMORIA_DB_SESSION_WORKER_PASSWORD="$(secret)"
export MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD="$(secret)"
export MEMORIA_DB_MEMORY_API_PASSWORD="$(secret)"
export MEMORIA_DB_MEMORY_WORKER_PASSWORD="$(secret)"

docker run -d \
  --name "$POSTGRES_CONTAINER" \
  -e POSTGRES_USER=memoria_admin \
  -e POSTGRES_DB=postgres \
  -e POSTGRES_PASSWORD \
  -e MEMORIA_DB_APP_PASSWORD \
  -e MEMORIA_DB_COMPILER_PASSWORD \
  -e MEMORIA_DB_EVOLUTION_PASSWORD \
  -e MEMORIA_DB_GUARDIAN_PASSWORD \
  -e MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD \
  -e MEMORIA_DB_GUARDIAN_WORKER_PASSWORD \
  -e MEMORIA_DB_IDENTITY_PASSWORD \
  -e MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD \
  -e MEMORIA_DB_CONSENT_PASSWORD \
  -e MEMORIA_DB_DEVICE_ONBOARDING_API_PASSWORD \
  -e MEMORIA_DB_DEVICE_ONBOARDING_MAINTENANCE_PASSWORD \
  -e MEMORIA_DB_SESSION_API_PASSWORD \
  -e MEMORIA_DB_ACTION_EXECUTOR_PASSWORD \
  -e MEMORIA_DB_SESSION_PROJECTOR_PASSWORD \
  -e MEMORIA_DB_SESSION_WORKER_PASSWORD \
  -e MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD \
  -e MEMORIA_DB_MEMORY_API_PASSWORD \
  -e MEMORIA_DB_MEMORY_WORKER_PASSWORD \
  -v "$ROOT/infra/postgres/init-memoria.sh:/docker-entrypoint-initdb.d/001-init-memoria.sh:ro" \
  -v "$ROOT/services/identity/postgres_schema.sql:/docker-entrypoint-initdb.d/002-identity-schema.sql:ro" \
  -v "$ROOT/services/consent/postgres_schema.sql:/docker-entrypoint-initdb.d/003-consent-schema.sql:ro" \
  -v "$ROOT/services/policy/postgres_receipt_schema.sql:/docker-entrypoint-initdb.d/004-policy-receipt-schema.sql:ro" \
  -v "$ROOT/services/device_fleet/postgres_schema.sql:/docker-entrypoint-initdb.d/005-device-fleet-schema.sql:ro" \
  -v "$ROOT/services/session_runtime/postgres_schema.sql:/docker-entrypoint-initdb.d/006-session-runtime-schema.sql:ro" \
  -v "$ROOT/services/evolution/postgres_schema.sql:/docker-entrypoint-initdb.d/007-evolution-schema.sql:ro" \
  -v "$ROOT/services/guardian/postgres_schema.sql:/docker-entrypoint-initdb.d/008-guardian-schema.sql:ro" \
  -v "$ROOT/services/memory_scope/postgres_schema.sql:/docker-entrypoint-initdb.d/009-memory-scope-schema.sql:ro" \
  -v "$ROOT/services/device_fleet/bootstrap_postgres_schema.sql:/docker-entrypoint-initdb.d/010-device-onboarding-schema.sql:ro" \
  "$POSTGRES_IMAGE" >/dev/null

ready=false
attempt=0
while [ "$attempt" -lt 60 ]; do
  if [ "$(docker inspect -f '{{.State.Running}}' "$POSTGRES_CONTAINER" 2>/dev/null || true)" = false ]; then
    break
  fi
  if docker exec "$POSTGRES_CONTAINER" \
    sh -c 'test "$(cat /proc/1/comm)" = postgres' >/dev/null 2>&1 &&
    docker exec "$POSTGRES_CONTAINER" \
      pg_isready -U memoria_admin -d memoria >/dev/null 2>&1; then
    ready=true
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done

if [ "$ready" != true ]; then
  python="$ROOT/.venv/bin/python"
  if [ ! -x "$python" ]; then
    python=python3
  fi
  export MEMORIA_REDACT_KEYS="
POSTGRES_PASSWORD
MEMORIA_DB_APP_PASSWORD
MEMORIA_DB_COMPILER_PASSWORD
MEMORIA_DB_EVOLUTION_PASSWORD
MEMORIA_DB_GUARDIAN_PASSWORD
MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD
MEMORIA_DB_GUARDIAN_WORKER_PASSWORD
MEMORIA_DB_IDENTITY_PASSWORD
MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD
MEMORIA_DB_CONSENT_PASSWORD
MEMORIA_DB_DEVICE_ONBOARDING_API_PASSWORD
MEMORIA_DB_DEVICE_ONBOARDING_MAINTENANCE_PASSWORD
MEMORIA_DB_SESSION_API_PASSWORD
MEMORIA_DB_ACTION_EXECUTOR_PASSWORD
MEMORIA_DB_SESSION_PROJECTOR_PASSWORD
MEMORIA_DB_SESSION_WORKER_PASSWORD
MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD
MEMORIA_DB_MEMORY_API_PASSWORD
MEMORIA_DB_MEMORY_WORKER_PASSWORD
"
  docker logs "$POSTGRES_CONTAINER" 2>&1 | "$python" -c '
import os
import sys

text = sys.stdin.read()
for key in os.environ["MEMORIA_REDACT_KEYS"].split():
    value = os.environ.get(key, "")
    if value:
        text = text.replace(value, "<REDACTED>")
print(text, end="")
'
  exit 1
fi

"$ROOT/scripts/verify_authoritative_postgres.sh"
"$ROOT/scripts/upgrade_authoritative_postgres.sh"
"$ROOT/scripts/verify_authoritative_postgres.sh"

echo "ephemeral authoritative PostgreSQL init + repeat-upgrade gate passed"
