#!/bin/sh
set -eu

# Run after the data Compose release has mounted the two PostgreSQL init files.
# The script is idempotent and is intended for an existing data volume where
# Docker's automatic /docker-entrypoint-initdb.d execution will not run again.
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

echo "evolution PostgreSQL role, schema and FORCE RLS are installed"
