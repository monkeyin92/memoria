#!/bin/sh
# Reuse the shared CI PostgreSQL container when it is already running, or
# create/start it. Only the container named memoria-ci-pg and the database
# memoria_ci are touched.
#
# --reset-schema (or MEMORIA_CI_POSTGRES_RESET_SCHEMA=1) additionally drops and
# recreates only the public schema of the memoria_ci database and reinstalls
# the vector extension there.
set -eu

container="${MEMORIA_CI_POSTGRES_CONTAINER:-memoria-ci-pg}"
image="${MEMORIA_CI_POSTGRES_IMAGE:-pgvector/pgvector:0.8.1-pg17-bookworm}"
db="memoria_ci"
user="memoria_ci"

do_reset="${MEMORIA_CI_POSTGRES_RESET_SCHEMA:-0}"
case "$do_reset" in
  0|false|no|"") do_reset=0 ;;
  1|true|yes) do_reset=1 ;;
  *)
    echo "invalid MEMORIA_CI_POSTGRES_RESET_SCHEMA: $do_reset" >&2
    exit 2
    ;;
esac

for arg in "$@"; do
  case "$arg" in
    --reset-schema) do_reset=1 ;;
    *)
      echo "unknown argument: $arg" >&2
      exit 2
      ;;
  esac
done

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
    --env POSTGRES_USER="$user" \
    --env POSTGRES_DB="$db" \
    --env POSTGRES_HOST_AUTH_METHOD=trust \
    --publish 127.0.0.1:5432:5432 \
    "$image" >/dev/null
fi

attempt=0
while [ "$attempt" -lt 60 ]; do
  if docker exec "$container" pg_isready -U "$user" -d "$db" >/dev/null 2>&1; then
    break
  fi
  attempt=$((attempt + 1))
  sleep 1
done

if ! docker exec "$container" pg_isready -U "$user" -d "$db" >/dev/null 2>&1; then
  echo "container $container did not become ready" >&2
  exit 1
fi

if ! docker exec "$container" psql \
  -U "$user" -d "$db" --no-psqlrc --tuples-only --no-align \
  --command 'select 1' >/dev/null 2>&1; then
  echo "container $container does not accept user $user on database $db" >&2
  exit 1
fi

if [ "$do_reset" = 1 ]; then
  echo "resetting schema public in database $db on $container"
  if ! docker exec -i "$container" psql \
    -U "$user" -d "$db" --no-psqlrc --quiet \
    -v ON_ERROR_STOP=1 --single-transaction <<'SQL'
DROP SCHEMA public CASCADE;
CREATE SCHEMA public;
GRANT ALL ON SCHEMA public TO memoria_ci;
CREATE EXTENSION IF NOT EXISTS vector;
SQL
  then
    echo "failed to reset schema public in database $db on $container" >&2
    exit 1
  fi

  if ! vector_version="$(docker exec "$container" psql \
    -U "$user" -d "$db" --no-psqlrc --tuples-only --no-align \
    -v ON_ERROR_STOP=1 \
    --command "select extversion from pg_extension where extname = 'vector'")"; then
    echo "failed to verify the vector extension in database $db on $container" >&2
    exit 1
  fi
  if [ -z "$vector_version" ]; then
    echo "the vector extension is not installed in database $db on $container" >&2
    exit 1
  fi
  echo "reset schema public in database $db; vector $vector_version is ready"
fi

echo "container $container is ready"
