#!/bin/sh
set -eu

# Idempotent, forward-only installation for an existing PostgreSQL volume.
# The candidate data Compose release must mount every authoritative schema
# before this command runs.
: "${POSTGRES_CONTAINER:?set POSTGRES_CONTAINER to the running PostgreSQL container}"
: "${MEMORIA_DB_APP_PASSWORD:?set the current app role password}"
: "${MEMORIA_DB_COMPILER_PASSWORD:?set the current compiler role password}"
: "${MEMORIA_DB_EVOLUTION_PASSWORD:?set the evolution role password}"
: "${MEMORIA_DB_GUARDIAN_PASSWORD:?set the guardian API role password}"
: "${MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD:?set the guardian maintenance role password}"
: "${MEMORIA_DB_GUARDIAN_WORKER_PASSWORD:?set the guardian worker role password}"
: "${MEMORIA_DB_IDENTITY_PASSWORD:?set the identity API role password}"
: "${MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD:?set the identity registration role password}"
: "${MEMORIA_DB_CONSENT_PASSWORD:?set the consent API role password}"
: "${MEMORIA_DB_DEVICE_ONBOARDING_API_PASSWORD:?set the device onboarding API role password}"
: "${MEMORIA_DB_DEVICE_ONBOARDING_MAINTENANCE_PASSWORD:?set the device onboarding maintenance role password}"
: "${MEMORIA_DB_SESSION_API_PASSWORD:?set the Session Runtime API role password}"
: "${MEMORIA_DB_ACTION_EXECUTOR_PASSWORD:?set the action executor role password}"
: "${MEMORIA_DB_SESSION_PROJECTOR_PASSWORD:?set the Session Runtime projector role password}"
: "${MEMORIA_DB_SESSION_WORKER_PASSWORD:?set the Session Runtime worker role password}"
: "${MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD:?set the Session Runtime maintenance role password}"
: "${MEMORIA_DB_MEMORY_API_PASSWORD:?set the MemoryScope API role password}"
: "${MEMORIA_DB_MEMORY_WORKER_PASSWORD:?set the MemoryScope worker role password}"

docker exec \
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
  "$POSTGRES_CONTAINER" \
  sh /docker-entrypoint-initdb.d/001-init-memoria.sh

echo "authoritative PostgreSQL roles, schemas and RLS are installed"
