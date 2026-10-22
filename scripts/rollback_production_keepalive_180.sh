#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
base="${root}/docker-compose.production.yml"
self="$(readlink -f "${BASH_SOURCE[0]}")"

if [[ ! -f "$base" ]]; then
  printf 'production compose missing\n' >&2
  exit 1
fi

awk '
  BEGIN { found = 0 }
  /^  voice-core-media-bridge:[[:space:]]*$/ { svc = 1; envb = 0; next }
  svc && /^  [^[:space:]]/ { svc = 0; envb = 0 }
  svc && /^    environment:[[:space:]]*$/ { envb = 1; next }
  svc && envb && /^    [^[:space:]]/ { envb = 0 }
  svc && envb && /^      MEDIA_OWNER_SILENCE_TIMEOUT_S:[[:space:]]*"30"[[:space:]]*$/ {
    found++
    next
  }
  svc && envb && /^      MEDIA_OWNER_SILENCE_TIMEOUT_S:/ { exit 3 }
  END { if (found != 1) exit 2 }
' "$base" || {
  printf 'base keepalive is not 30\n' >&2
  exit 1
}

printf 'keepalive_timeout=30\n'
sha256sum -- "$self" "$base"
printf '%s\n' \
  'A person runs the next command from the release directory. This script does not.' \
  'Run it as root. Set MEMORIA_RELEASE_TAG and MEMORIA_RELEASE_COMMIT in that shell first.' \
  'Leave the keepalive override out of this command.' \
  'docker compose -f docker-compose.production.yml --profile media-runtime up -d --no-deps --no-build --force-recreate voice-core-media-bridge'
