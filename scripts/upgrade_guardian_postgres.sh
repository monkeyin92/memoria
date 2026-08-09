#!/bin/sh
set -eu

# Idempotent forward-only install for an existing PostgreSQL volume. The data
# Compose release must mount both evolution and guardian schema files first.
: "${POSTGRES_CONTAINER:?set POSTGRES_CONTAINER to the running PostgreSQL container}"
: "${MEMORIA_DB_APP_PASSWORD:?set the current app role password}"
: "${MEMORIA_DB_COMPILER_PASSWORD:?set the current compiler role password}"
: "${MEMORIA_DB_EVOLUTION_PASSWORD:?set the evolution role password}"
: "${MEMORIA_DB_GUARDIAN_PASSWORD:?set the guardian role password}"

docker exec \
  -e MEMORIA_DB_APP_PASSWORD \
  -e MEMORIA_DB_COMPILER_PASSWORD \
  -e MEMORIA_DB_EVOLUTION_PASSWORD \
  -e MEMORIA_DB_GUARDIAN_PASSWORD \
  "$POSTGRES_CONTAINER" \
  sh /docker-entrypoint-initdb.d/001-init-memoria.sh

echo "guardian PostgreSQL role, schema and FORCE RLS are installed"
