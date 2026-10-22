#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
override="${root}/docker-compose.production.keepalive-180.override.yml"
self="$(readlink -f "${BASH_SOURCE[0]}")"

if [[ ! -f "$override" ]]; then
  printf 'keepalive override missing\n' >&2
  exit 1
fi

if ! cmp -s "$override" <(
  printf '%s\n' \
    'services:' \
    '  voice-core-media-bridge:' \
    '    environment:' \
    '      MEDIA_OWNER_SILENCE_TIMEOUT_S: "180"'
); then
  printf 'keepalive override rejected\n' >&2
  exit 1
fi

printf 'keepalive_timeout=180\n'
sha256sum -- "$override" "$self"
printf '%s\n' \
  'A person runs the next command from the release directory. This script does not.' \
  'Run it as root. Set MEMORIA_RELEASE_TAG and MEMORIA_RELEASE_COMMIT in that shell first.' \
  'This override changes one environment key. release_ops accepts image-only overrides.' \
  'docker compose -f docker-compose.production.yml -f docker-compose.production.keepalive-180.override.yml --profile media-runtime up -d --no-deps --no-build --force-recreate voice-core-media-bridge'
