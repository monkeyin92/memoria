#!/bin/sh
set -eu

: "${MEMORIA_DB_APP_PASSWORD:?MEMORIA_DB_APP_PASSWORD is required}"
: "${MEMORIA_DB_COMPILER_PASSWORD:?MEMORIA_DB_COMPILER_PASSWORD is required}"
: "${MEMORIA_DB_EVOLUTION_PASSWORD:?MEMORIA_DB_EVOLUTION_PASSWORD is required}"

psql \
  --set=ON_ERROR_STOP=1 \
  --set=app_password="$MEMORIA_DB_APP_PASSWORD" \
  --set=compiler_password="$MEMORIA_DB_COMPILER_PASSWORD" \
  --set=evolution_password="$MEMORIA_DB_EVOLUTION_PASSWORD" \
  --username "$POSTGRES_USER" \
  --dbname postgres <<'SQL'
SELECT format(
    'CREATE ROLE memoria_app LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
    :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_app')
\gexec

SELECT 'CREATE ROLE memoria_archive_compiler NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_archive_compiler')
\gexec

SELECT format(
    'CREATE ROLE memoria_compiler LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
    :'compiler_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_compiler')
\gexec

GRANT memoria_archive_compiler TO memoria_compiler;

SELECT format(
    'CREATE ROLE memoria_evolution LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS',
    :'evolution_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_evolution')
\gexec

-- This file is also used by the idempotent upgrade helper on an existing
-- volume. Keep the controller DSN password in sync when operators rotate it.
SELECT format('ALTER ROLE memoria_evolution PASSWORD %L', :'evolution_password')
WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_evolution')
\gexec

SELECT 'CREATE DATABASE memoria OWNER memoria_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'memoria')
\gexec

REVOKE CONNECT ON DATABASE memoria FROM PUBLIC;
GRANT CONNECT ON DATABASE memoria TO memoria_app, memoria_compiler, memoria_evolution;

\connect memoria
CREATE EXTENSION IF NOT EXISTS vector;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO memoria_app;
GRANT USAGE ON SCHEMA public TO memoria_evolution;
\i /docker-entrypoint-initdb.d/002-evolution-schema.sql
REVOKE CREATE ON SCHEMA public FROM memoria_evolution;
SQL
