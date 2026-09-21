#!/usr/bin/env bash
# Read-only audit of release directories and active container references.
set -Eeuo pipefail

root="${1:-/opt/memoria}"
if [[ "$root" == --help || "$root" == -h ]]; then
  echo "usage: production_layout_audit.sh [ABSOLUTE_PRODUCTION_ROOT]"
  exit 0
fi
[[ "$root" == /* && "$root" != *..* ]] || { echo "unsafe root" >&2; exit 2; }
printf 'layout_root=%s\n' "$root"
for name in current releases component-releases incoming sidecars state ops; do
  path="$root/$name"
  if [[ -L "$path" ]]; then
    printf 'path=%s symlink=%s\n' "$path" "$(readlink "$path")"
  elif [[ -d "$path" ]]; then
    printf 'path=%s directory=present size=%s\n' "$path" "$(du -sh "$path" | awk '{print $1}')"
  else
    printf 'path=%s missing\n' "$path"
  fi
done
if command -v docker >/dev/null 2>&1; then
  docker ps --format 'container={{.Names}} image={{.Image}}' 2>/dev/null || true
  for container in $(docker ps -q 2>/dev/null); do
    docker inspect "$container" --format \
      'container={{.Name}} mounts={{range .Mounts}}{{.Source}}=>{{.Destination}} {{end}}' \
      2>/dev/null || true
  done
fi
