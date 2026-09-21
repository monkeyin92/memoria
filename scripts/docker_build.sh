#!/usr/bin/env bash
# Common Docker build entry point. BuildKit is opt-out, not silently forced.
set -Eeuo pipefail

if (($# == 0)); then
  echo "usage: docker_build.sh [docker build arguments...]" >&2
  exit 2
fi
if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  echo "usage: docker_build.sh [docker build arguments...]"
  echo "environment: MEMORIA_DOCKER_BUILDKIT=0|1 MEMORIA_DOCKER_BUILDER=docker|buildx"
  exit 0
fi

buildkit="${MEMORIA_DOCKER_BUILDKIT:-1}"
builder="${MEMORIA_DOCKER_BUILDER:-docker}"
case "$buildkit" in
  0|1) ;;
  *) echo "MEMORIA_DOCKER_BUILDKIT must be 0 or 1" >&2; exit 2 ;;
esac
export DOCKER_BUILDKIT="$buildkit"

case "$builder" in
  docker)
    exec docker build "$@"
    ;;
  buildx)
    docker buildx version >/dev/null 2>&1 || {
      echo "MEMORIA_DOCKER_BUILDER=buildx requested but docker buildx is unavailable" >&2
      exit 1
    }
    exec docker buildx build --load --provenance=false --sbom=false "$@"
    ;;
  *)
    echo "MEMORIA_DOCKER_BUILDER must be docker or buildx" >&2
    exit 2
    ;;
esac
