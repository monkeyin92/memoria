#!/bin/sh
set -eu

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
echo "upgrade_guardian_postgres.sh is a compatibility wrapper; running the authoritative upgrade"
exec "$script_dir/upgrade_authoritative_postgres.sh" "$@"
