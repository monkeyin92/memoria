#!/usr/bin/env bash
# Read-only disk patrol; --apply only writes a snapshot and never cleans.
set -Eeuo pipefail

warn_pct=75
crit_pct=85
state_file="${MEMORIA_DISK_PATROL_STATE:-/var/lib/memoria/disk-patrol.json}"
apply=false
json=false
while (($#)); do
  case "$1" in
    --warn-pct) warn_pct="${2:-}"; shift 2 ;;
    --crit-pct) crit_pct="${2:-}"; shift 2 ;;
    --apply) apply=true; shift ;;
    --json) json=true; shift ;;
    -h|--help)
      echo "usage: disk_patrol.sh [--warn-pct N] [--crit-pct N] [--apply] [--json]"
      exit 0
      ;;
    *) echo "unknown argument: $1" >&2; exit 3 ;;
  esac
done
[[ "$warn_pct" =~ ^[0-9]+$ && "$crit_pct" =~ ^[0-9]+$ && "$warn_pct" -lt "$crit_pct" ]] || exit 3

used_pct="$(df -P / | awk 'NR == 2 {gsub(/%/, "", $5); print $5}')"
[[ "$used_pct" =~ ^[0-9]+$ ]] || { echo "unable to read filesystem usage" >&2; exit 3; }
if [[ "$json" == true ]]; then
  printf '{"mount":"/","used_pct":%s,"warn_pct":%s,"crit_pct":%s}\n' \
    "$used_pct" "$warn_pct" "$crit_pct"
else
  printf 'disk_mount=/ used_pct=%s warn_pct=%s crit_pct=%s\n' \
    "$used_pct" "$warn_pct" "$crit_pct"
fi
if [[ "$json" != true ]] && command -v docker >/dev/null 2>&1; then
  docker system df 2>&1 || true
fi

if [[ "$apply" == true ]]; then
  mkdir -p "$(dirname "$state_file")"
  printf '{"observed_at":"%s","used_pct":%s,"warn_pct":%s,"crit_pct":%s}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$used_pct" "$warn_pct" "$crit_pct" >"$state_file"
fi
if ((used_pct >= crit_pct)); then exit 2; fi
if ((used_pct >= warn_pct)); then exit 1; fi
exit 0
