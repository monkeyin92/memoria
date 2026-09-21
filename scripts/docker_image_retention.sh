#!/usr/bin/env bash
# Dry-run-first image inventory. Only tagged memoria-* images are candidates.
set -Eeuo pipefail

keep=2
min_age_days=14
apply=false
while (($#)); do
  case "$1" in
    --keep) keep="${2:-}"; shift 2 ;;
    --min-age-days) min_age_days="${2:-}"; shift 2 ;;
    --apply) apply=true; shift ;;
    -h|--help)
      echo "usage: docker_image_retention.sh [--keep N] [--min-age-days N] [--apply]"
      exit 0
      ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$keep" =~ ^[1-9][0-9]*$ && "$min_age_days" =~ ^[0-9]+$ ]] || {
  echo "invalid retention values" >&2
  exit 2
}
command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }

declare -A running_ids=()
while read -r container; do
  [[ -n "$container" ]] || continue
  running_ids["$(docker inspect --format '{{.Image}}' "$container")"]=1
done < <(docker ps -aq)

declare -a candidates=()
declare -A retained=()
now_epoch="$(date +%s)"
while IFS=$'\t' read -r repository tag image_id created_at; do
  [[ "$repository" == memoria-* && "$tag" != "<none>" ]] || continue
  # Rollback images and every runtime-base tag are part of the recovery contract.
  # Runtime-base tags use uv-* names, so matching the repository is intentional.
  if [[ "$repository" == memoria-agent-runtime-base || "$tag" == rollback-* ]]; then
    printf 'KEEP recovery %s:%s\n' "$repository" "$tag"
    continue
  fi
  if [[ -n "${running_ids[$image_id]:-}" ]]; then
    printf 'KEEP running %s:%s\n' "$repository" "$tag"
    continue
  fi
  created="$(docker inspect --format '{{.Created}}' "$image_id" 2>/dev/null || true)"
  created_epoch=""
  if [[ -n "$created" ]]; then
    created_epoch="$(date -u -d "$created" +%s 2>/dev/null || true)"
    if [[ -z "$created_epoch" ]]; then
      created_epoch="$(date -u -j -f '%Y-%m-%dT%H:%M:%S' "${created%%.*}" +%s 2>/dev/null || true)"
    fi
  fi
  if [[ -z "$created_epoch" ]]; then
    printf 'KEEP unparsed-age %s:%s\n' "$repository" "$tag"
    continue
  fi
  age_days=$(( (now_epoch - created_epoch) / 86400 ))
  if ((age_days < min_age_days)); then
    printf 'KEEP recent %s:%s age=%sd\n' "$repository" "$tag" "$age_days"
    continue
  fi
  retained["$repository"]="${retained[$repository]:-0}"
  if ((retained["$repository"] < keep)); then
    retained["$repository"]=$((retained["$repository"] + 1))
    printf 'KEEP retained %s:%s\n' "$repository" "$tag"
  else
    candidates+=("$repository:$tag")
    printf 'REMOVE candidate %s:%s\n' "$repository" "$tag"
  fi
done < <(
  docker image ls 'memoria-*' \
    --format '{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.CreatedAt}}' \
    | sort -k1,1 -k4,4r
)

printf 'retention_mode=%s candidates=%s keep=%s min_age_days=%s\n' \
  "$(if $apply; then echo apply; else echo dry-run; fi)" \
  "${#candidates[@]}" "$keep" "$min_age_days"
if [[ "$apply" != true ]]; then
  exit 0
fi
for image in "${candidates[@]}"; do
  docker rmi "$image"
done
