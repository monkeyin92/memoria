#!/bin/sh
set -eu

: "${MEMORIA_DB_APP_PASSWORD:?MEMORIA_DB_APP_PASSWORD is required}"
: "${MEMORIA_DB_COMPILER_PASSWORD:?MEMORIA_DB_COMPILER_PASSWORD is required}"
: "${MEMORIA_DB_EVOLUTION_PASSWORD:?MEMORIA_DB_EVOLUTION_PASSWORD is required}"
: "${MEMORIA_DB_GUARDIAN_PASSWORD:?MEMORIA_DB_GUARDIAN_PASSWORD is required}"
: "${MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD:?MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD is required}"
: "${MEMORIA_DB_GUARDIAN_WORKER_PASSWORD:?MEMORIA_DB_GUARDIAN_WORKER_PASSWORD is required}"
: "${MEMORIA_DB_IDENTITY_PASSWORD:?MEMORIA_DB_IDENTITY_PASSWORD is required}"
: "${MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD:?MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD is required}"
: "${MEMORIA_DB_CONSENT_PASSWORD:?MEMORIA_DB_CONSENT_PASSWORD is required}"
: "${MEMORIA_DB_SESSION_API_PASSWORD:?MEMORIA_DB_SESSION_API_PASSWORD is required}"
: "${MEMORIA_DB_ACTION_EXECUTOR_PASSWORD:?MEMORIA_DB_ACTION_EXECUTOR_PASSWORD is required}"
: "${MEMORIA_DB_SESSION_PROJECTOR_PASSWORD:?MEMORIA_DB_SESSION_PROJECTOR_PASSWORD is required}"
: "${MEMORIA_DB_SESSION_WORKER_PASSWORD:?MEMORIA_DB_SESSION_WORKER_PASSWORD is required}"
: "${MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD:?MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD is required}"
: "${MEMORIA_DB_MEMORY_API_PASSWORD:?MEMORIA_DB_MEMORY_API_PASSWORD is required}"
: "${MEMORIA_DB_MEMORY_WORKER_PASSWORD:?MEMORIA_DB_MEMORY_WORKER_PASSWORD is required}"

psql \
  --set=ON_ERROR_STOP=1 \
  --set=app_password="$MEMORIA_DB_APP_PASSWORD" \
  --set=compiler_password="$MEMORIA_DB_COMPILER_PASSWORD" \
  --set=evolution_password="$MEMORIA_DB_EVOLUTION_PASSWORD" \
  --set=guardian_password="$MEMORIA_DB_GUARDIAN_PASSWORD" \
  --set=guardian_maintenance_password="$MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD" \
  --set=guardian_worker_password="$MEMORIA_DB_GUARDIAN_WORKER_PASSWORD" \
  --set=identity_password="$MEMORIA_DB_IDENTITY_PASSWORD" \
  --set=identity_registration_password="$MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD" \
  --set=consent_password="$MEMORIA_DB_CONSENT_PASSWORD" \
  --set=session_api_password="$MEMORIA_DB_SESSION_API_PASSWORD" \
  --set=action_executor_password="$MEMORIA_DB_ACTION_EXECUTOR_PASSWORD" \
  --set=session_projector_password="$MEMORIA_DB_SESSION_PROJECTOR_PASSWORD" \
  --set=session_worker_password="$MEMORIA_DB_SESSION_WORKER_PASSWORD" \
  --set=session_maintenance_password="$MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD" \
  --set=memory_api_password="$MEMORIA_DB_MEMORY_API_PASSWORD" \
  --set=memory_worker_password="$MEMORIA_DB_MEMORY_WORKER_PASSWORD" \
  --username "$POSTGRES_USER" \
  --dbname postgres <<'SQL'
SELECT format(
    'CREATE ROLE memoria_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'app_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_app')
\gexec
SELECT format(
    'ALTER ROLE memoria_app WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'app_password'
)
\gexec

SELECT 'CREATE ROLE memoria_archive_compiler NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_archive_compiler')
\gexec
ALTER ROLE memoria_archive_compiler
    WITH NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;

SELECT format(
    'CREATE ROLE memoria_compiler LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'compiler_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_compiler')
\gexec
SELECT format(
    'ALTER ROLE memoria_compiler WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'compiler_password'
)
\gexec
GRANT memoria_archive_compiler TO memoria_compiler;

SELECT format(
    'CREATE ROLE memoria_evolution LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'evolution_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_evolution')
\gexec
SELECT format(
    'ALTER ROLE memoria_evolution WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'evolution_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_guardian LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'guardian_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_guardian')
\gexec
SELECT format(
    'ALTER ROLE memoria_guardian WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'guardian_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_guardian_maintenance LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'guardian_maintenance_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'memoria_guardian_maintenance'
)
\gexec
SELECT format(
    'ALTER ROLE memoria_guardian_maintenance WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'guardian_maintenance_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_guardian_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'guardian_worker_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'memoria_guardian_worker'
)
\gexec
SELECT format(
    'ALTER ROLE memoria_guardian_worker WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'guardian_worker_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_identity LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'identity_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_identity')
\gexec
SELECT format(
    'ALTER ROLE memoria_identity WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'identity_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_identity_registration LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'identity_registration_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'memoria_identity_registration'
)
\gexec
SELECT format(
    'ALTER ROLE memoria_identity_registration WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'identity_registration_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_consent LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'consent_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_consent')
\gexec
SELECT format(
    'ALTER ROLE memoria_consent WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'consent_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_session_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'session_api_password'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'memoria_session_api')
\gexec
SELECT format(
    'ALTER ROLE memoria_session_api WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'session_api_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_action_executor LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'action_executor_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'memoria_action_executor'
)
\gexec
SELECT format(
    'ALTER ROLE memoria_action_executor WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'action_executor_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_session_projector LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'session_projector_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'memoria_session_projector'
)
\gexec
SELECT format(
    'ALTER ROLE memoria_session_projector WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'session_projector_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_session_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'session_worker_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'memoria_session_worker'
)
\gexec
SELECT format(
    'ALTER ROLE memoria_session_worker WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'session_worker_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_session_maintenance LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'session_maintenance_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'memoria_session_maintenance'
)
\gexec
SELECT format(
    'ALTER ROLE memoria_session_maintenance WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'session_maintenance_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_memory_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'memory_api_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'memoria_memory_api'
)
\gexec
SELECT format(
    'ALTER ROLE memoria_memory_api WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'memory_api_password'
)
\gexec

SELECT format(
    'CREATE ROLE memoria_memory_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'memory_worker_password'
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_roles WHERE rolname = 'memoria_memory_worker'
)
\gexec
SELECT format(
    'ALTER ROLE memoria_memory_worker WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS PASSWORD %L',
    :'memory_worker_password'
)
\gexec

SELECT 'CREATE DATABASE memoria OWNER memoria_app'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'memoria')
\gexec

REVOKE CONNECT ON DATABASE memoria FROM PUBLIC;
GRANT CONNECT ON DATABASE memoria TO
    memoria_app,
    memoria_compiler,
    memoria_evolution,
    memoria_guardian,
    memoria_guardian_maintenance,
    memoria_guardian_worker,
    memoria_identity,
    memoria_identity_registration,
    memoria_consent,
    memoria_session_api,
    memoria_action_executor,
    memoria_session_projector,
    memoria_session_worker,
    memoria_session_maintenance,
    memoria_memory_api,
    memoria_memory_worker;

\connect memoria
CREATE EXTENSION IF NOT EXISTS vector;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO memoria_app;
\i /docker-entrypoint-initdb.d/002-identity-schema.sql
RESET ROLE;
\i /docker-entrypoint-initdb.d/003-consent-schema.sql
RESET ROLE;
\i /docker-entrypoint-initdb.d/004-policy-receipt-schema.sql
RESET ROLE;
\i /docker-entrypoint-initdb.d/005-device-fleet-schema.sql
RESET ROLE;
\i /docker-entrypoint-initdb.d/006-session-runtime-schema.sql
RESET ROLE;
\i /docker-entrypoint-initdb.d/007-evolution-schema.sql
RESET ROLE;
\i /docker-entrypoint-initdb.d/008-guardian-schema.sql
RESET ROLE;
\i /docker-entrypoint-initdb.d/009-memory-scope-schema.sql
RESET ROLE;

-- Runtime credentials are never DDL principals.  Schema owner roles created
-- by the SQL files are NOLOGIN and remain the only domain object owners.
GRANT USAGE ON SCHEMA public TO
    memoria_compiler,
    memoria_evolution,
    memoria_guardian,
    memoria_guardian_maintenance,
    memoria_guardian_worker,
    memoria_identity,
    memoria_identity_registration,
    memoria_consent,
    memoria_session_api,
    memoria_action_executor,
    memoria_session_projector,
    memoria_session_worker,
    memoria_session_maintenance,
    memoria_memory_api,
    memoria_memory_worker;
REVOKE CREATE ON SCHEMA public FROM
    memoria_compiler,
    memoria_evolution,
    memoria_guardian,
    memoria_guardian_maintenance,
    memoria_guardian_worker,
    memoria_identity,
    memoria_identity_registration,
    memoria_consent,
    memoria_session_api,
    memoria_action_executor,
    memoria_session_projector,
    memoria_session_worker,
    memoria_session_maintenance,
    memoria_memory_api,
    memoria_memory_worker;
SQL
