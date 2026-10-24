#!/bin/sh
# Prepare an isolated CI PostgreSQL database on the CI-only cluster.
#
# CI uses memoria-ci-runner-pg on 127.0.0.1:5433, never the shared development
# cluster memoria-ci-pg on 5432. Roles are cluster-wide objects, so concurrent
# test runs against one cluster can fail with "tuple concurrently updated".
#
# The maintenance database (memoria_ci) is probed over TCP first. When it
# answers, no Docker call is made and the target database is dropped and
# recreated through SQL. Only when TCP is unreachable does the script create or
# start the memoria-ci-runner-pg container and probe again.
#
# MEMORIA_CI_POSTGRES_DSN selects the maintenance connection. When it is unset,
# MEMORIA_TEST_POSTGRES_DSN is used with its database replaced by memoria_ci,
# so a job only has to set the DSN it already uses for tests.
#
# The target database must match ^memoria_ci_[a-z0-9_]+$. The memoria_ci
# maintenance database and every other database are never dropped or altered.
set -eu

root=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
container="${MEMORIA_CI_POSTGRES_CONTAINER:-memoria-ci-runner-pg}"
image="${MEMORIA_CI_POSTGRES_IMAGE:-pgvector/pgvector:0.8.1-pg17-bookworm}"
maintenance_db="memoria_ci"
maintenance_dsn="${MEMORIA_CI_POSTGRES_DSN:-postgresql://memoria_ci@127.0.0.1:5433/memoria_ci}"
database=""

usage() {
  echo "usage: $0 --database memoria_ci_<name>" >&2
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --database)
      if [ "$#" -lt 2 ]; then
        usage
        exit 2
      fi
      database="$2"
      shift 2
      ;;
    --database=*)
      database="${1#--database=}"
      shift
      ;;
    --reset-schema)
      echo "--reset-schema is no longer supported; use --database memoria_ci_<name>" >&2
      exit 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [ -z "$database" ]; then
  usage
  exit 2
fi
if [ "$database" = "$maintenance_db" ]; then
  echo "refusing to touch the maintenance database $maintenance_db" >&2
  exit 2
fi
if ! (
  # C collation keeps the a-z range ASCII-only on macOS.
  LC_ALL=C
  case "$database" in
    memoria_ci_|memoria_ci_*[!a-z0-9_]*) exit 1 ;;
    memoria_ci_[a-z0-9_]*) exit 0 ;;
    *) exit 1 ;;
  esac
); then
  echo "invalid database name: $database (must match ^memoria_ci_[a-z0-9_]+$)" >&2
  exit 2
fi

backend=""
python_bin=""
if [ -x "$root/.venv/bin/python" ] &&
  "$root/.venv/bin/python" -c 'import psycopg' >/dev/null 2>&1; then
  backend="python"
  python_bin="$root/.venv/bin/python"
elif command -v psql >/dev/null 2>&1; then
  backend="psql"
elif command -v uv >/dev/null 2>&1; then
  backend="uv"
elif command -v python3 >/dev/null 2>&1 &&
  python3 -c 'import psycopg' >/dev/null 2>&1; then
  backend="python"
  python_bin="python3"
else
  echo "need psql, uv with psycopg, or a python with psycopg to reach PostgreSQL" >&2
  exit 1
fi

run_python() {
  if [ "$backend" = "uv" ]; then
    uv run --quiet --frozen python - "$@"
  else
    "$python_bin" - "$@"
  fi
}

python_probe() {
  run_python probe <<'PY'
import os
import sys

import psycopg
from psycopg import conninfo

dsn = os.environ.get("MEMORIA_CI_POSTGRES_DSN", "").strip()
if not dsn:
    test_dsn = os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip()
    if test_dsn:
        dsn = conninfo.make_conninfo(test_dsn, dbname="memoria_ci")
if not dsn:
    dsn = "postgresql://memoria_ci@127.0.0.1:5433/memoria_ci"

try:
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        connection.execute("select 1")
except Exception:
    sys.exit(1)
PY
}

python_setup() {
  run_python "$database" <<'PY'
import os
import sys

import psycopg
from psycopg import conninfo, sql

database = sys.argv[1]
dsn = os.environ.get("MEMORIA_CI_POSTGRES_DSN", "").strip()
if not dsn:
    test_dsn = os.environ.get("MEMORIA_TEST_POSTGRES_DSN", "").strip()
    if test_dsn:
        dsn = conninfo.make_conninfo(test_dsn, dbname="memoria_ci")
if not dsn:
    dsn = "postgresql://memoria_ci@127.0.0.1:5433/memoria_ci"

try:
    admin = psycopg.connect(dsn, autocommit=True, connect_timeout=5)
    with admin:
        current = admin.execute("select current_database()").fetchone()[0]
        if current != "memoria_ci":
            print(
                "the CI PostgreSQL maintenance DSN must connect to the memoria_ci database",
                file=sys.stderr,
            )
            raise SystemExit(1)
        admin.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(database)
            )
        )
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(database), sql.Identifier("memoria_ci")
            )
        )

    target_dsn = conninfo.make_conninfo(dsn, dbname=database)
    with psycopg.connect(target_dsn, autocommit=True, connect_timeout=5) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
        row = connection.execute(
            "select extversion from pg_extension where extname = 'vector'"
        ).fetchone()
except SystemExit:
    raise
except Exception as exc:
    detail = getattr(getattr(exc, "diag", None), "message_primary", None)
    print(
        f"failed to initialize database {database}: {type(exc).__name__}"
        + (f": {detail}" if detail else ""),
        file=sys.stderr,
    )
    raise SystemExit(1)

if not row or not row[0]:
    print(f"the vector extension is missing from database {database}", file=sys.stderr)
    raise SystemExit(1)
print(f"database {database} is ready; vector {row[0]}")
PY
}

psql_probe() {
  psql "$maintenance_dsn" --no-psqlrc --tuples-only --no-align \
    -c 'select 1' >/dev/null 2>&1
}

psql_setup() {
  maintenance_current=$(psql "$maintenance_dsn" --no-psqlrc --tuples-only --no-align \
    -v ON_ERROR_STOP=1 --command 'select current_database()')
  if [ "$maintenance_current" != "$maintenance_db" ]; then
    echo "the CI PostgreSQL maintenance DSN must connect to the $maintenance_db database" >&2
    exit 1
  fi

  vector_version=$(psql "$maintenance_dsn" --no-psqlrc --quiet \
    --tuples-only --no-align -v ON_ERROR_STOP=1 <<SQL
DROP DATABASE IF EXISTS "$database" WITH (FORCE);
CREATE DATABASE "$database" OWNER memoria_ci;
\connect "$database"
CREATE EXTENSION IF NOT EXISTS vector;
SELECT extversion FROM pg_extension WHERE extname = 'vector';
SQL
  )
  vector_version=$(printf '%s\n' "$vector_version" | tail -n 1)
  if [ -z "$vector_version" ]; then
    echo "the vector extension is missing from database $database" >&2
    exit 1
  fi
  echo "database $database is ready; vector $vector_version"
}

probe_tcp() {
  if [ "$backend" = "psql" ]; then
    psql_probe
  else
    python_probe >/dev/null 2>&1
  fi
}

setup_database() {
  if [ "$backend" = "psql" ]; then
    psql_setup
  else
    python_setup
  fi
}

if probe_tcp; then
  echo "using the CI PostgreSQL over TCP"
else
  echo "CI PostgreSQL is unreachable over TCP; falling back to docker container $container"
  if ! command -v docker >/dev/null 2>&1; then
    echo "docker is not installed; start $container or set MEMORIA_CI_POSTGRES_DSN" >&2
    exit 1
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "the docker daemon is not accessible (check docker socket permissions); start $container or set MEMORIA_CI_POSTGRES_DSN" >&2
    exit 1
  fi

  if docker container inspect "$container" >/dev/null 2>&1; then
    if [ "$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true)" = true ]; then
      echo "reusing running container $container"
    else
      echo "starting existing container $container"
      docker start "$container" >/dev/null
    fi
  else
    echo "creating container $container from $image"
    docker run -d \
      --name "$container" \
      --restart unless-stopped \
      --env POSTGRES_USER=memoria_ci \
      --env POSTGRES_DB="$maintenance_db" \
      --env POSTGRES_HOST_AUTH_METHOD=trust \
      --publish 127.0.0.1:5433:5432 \
      "$image" >/dev/null
  fi

  attempt=0
  while [ "$attempt" -lt 60 ]; do
    if probe_tcp; then
      break
    fi
    attempt=$((attempt + 1))
    sleep 1
  done
  if ! probe_tcp; then
    echo "container $container is running but the CI PostgreSQL is still unreachable over TCP" >&2
    exit 1
  fi
fi

setup_database
