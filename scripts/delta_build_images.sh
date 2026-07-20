#!/usr/bin/env bash
# Fast production image rebuild when uv.lock / pyproject.toml did NOT change.
#
# Problem: the 3.6GB production box reinstalls the full Python venv on every
# `docker compose build` (BuildKit cache is tiny / often cold), so agent builds
# thrash for a long time.
#
# Fix: derive new tags FROM the last known-good amd64 images and only COPY app
# code. When dependencies change, do a full linux/amd64 build on the local build
# machine; do not move the build back to the small production server.
#
# Usage on a build machine that already has the previous healthy images:
#   BASE_TAG=20260718-211231 NEW_TAG=20260719-000731 \
#     bash scripts/delta_build_images.sh
#
# Docker Desktop BuildKit may try to resolve an unqualified local base tag from
# Docker Hub. In that case use the local daemon explicitly:
#   DOCKER_CONTEXT=default DOCKER_BUILDKIT=0 \
#     BASE_TAG=... NEW_TAG=... bash scripts/delta_build_images.sh
#
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
NEW_TAG="${NEW_TAG:-${MEMORIA_RELEASE_TAG:?set NEW_TAG or MEMORIA_RELEASE_TAG}}"
BASE_TAG="${BASE_TAG:?set BASE_TAG to previous healthy image tag}"
TARGET_ARCH="${TARGET_ARCH:-amd64}"

if ! docker image inspect "memoria-agent:${BASE_TAG}" >/dev/null 2>&1; then
  echo "missing base image memoria-agent:${BASE_TAG}" >&2
  exit 1
fi
if ! docker image inspect "memoria-control-api:${BASE_TAG}" >/dev/null 2>&1; then
  echo "missing base image memoria-control-api:${BASE_TAG}" >&2
  exit 1
fi

for image in agent control-api; do
  base_arch="$(docker image inspect "memoria-${image}:${BASE_TAG}" \
    --format '{{.Architecture}}')"
  if [[ "$base_arch" != "$TARGET_ARCH" ]]; then
    echo "base image memoria-${image}:${BASE_TAG} is $base_arch, expected $TARGET_ARCH" >&2
    exit 1
  fi
done

tmp="$(mktemp -d /tmp/memoria-delta-build.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT

cat >"$tmp/Dockerfile.agent" <<EOF
FROM memoria-agent:${BASE_TAG}
USER root
WORKDIR /app
COPY services/agent ./services/agent
COPY services/common ./services/common
COPY packages ./packages
COPY services/__init__.py ./services/__init__.py
COPY scripts/verify_env.py scripts/livekit_smoke_test.py scripts/provider_smoke_test.py ./scripts/
COPY infra/voices/designed_voice_ids.json ./infra/voices/designed_voice_ids.json
USER 65532:65532
EOF

cat >"$tmp/Dockerfile.control-api" <<EOF
FROM memoria-control-api:${BASE_TAG}
USER root
WORKDIR /app
COPY services ./services
COPY packages ./packages
COPY scripts/mark_readiness.py ./scripts/mark_readiness.py
USER 65532:65532
EOF

echo "delta-building memoria-agent:${NEW_TAG} from ${BASE_TAG}"
docker build -f "$tmp/Dockerfile.agent" -t "memoria-agent:${NEW_TAG}" "$ROOT"

echo "delta-building memoria-control-api:${NEW_TAG} from ${BASE_TAG}"
docker build -f "$tmp/Dockerfile.control-api" -t "memoria-control-api:${NEW_TAG}" "$ROOT"

for image in agent control-api; do
  built_arch="$(docker image inspect "memoria-${image}:${NEW_TAG}" \
    --format '{{.Architecture}}')"
  if [[ "$built_arch" != "$TARGET_ARCH" ]]; then
    echo "built image memoria-${image}:${NEW_TAG} is $built_arch, expected $TARGET_ARCH" >&2
    exit 1
  fi
done

docker images --format '{{.Repository}}:{{.Tag}} {{.Size}} {{.CreatedSince}}' \
  | grep -E "memoria-(agent|control-api):${NEW_TAG}" || true
echo "delta_build_ok ${NEW_TAG}"
