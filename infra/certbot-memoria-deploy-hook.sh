#!/usr/bin/env bash
set -Eeuo pipefail

if ! output="$(/usr/sbin/nginx -t 2>&1)"; then
    printf '%s\n' "$output" >&2
    exit 1
fi
/bin/systemctl reload nginx
