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
#     MEMORIA_RELEASE_COMMIT=<tagged-clean-commit> \
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
MEMORIA_RELEASE_COMMIT="${MEMORIA_RELEASE_COMMIT:?set MEMORIA_RELEASE_COMMIT to the tagged clean commit}"

python3 "$ROOT/scripts/verify_release_source.py" \
  --root "$ROOT" \
  --expected-commit "$MEMORIA_RELEASE_COMMIT" \
  --release-tag "$NEW_TAG"

if ! docker image inspect "memoria-agent:${BASE_TAG}" >/dev/null 2>&1; then
  echo "missing base image memoria-agent:${BASE_TAG}" >&2
  exit 1
fi
if ! docker image inspect "memoria-control-api:${BASE_TAG}" >/dev/null 2>&1; then
  echo "missing base image memoria-control-api:${BASE_TAG}" >&2
  exit 1
fi
if ! docker image inspect "memoria-speaker-model:${BASE_TAG}" >/dev/null 2>&1; then
  echo "missing base image memoria-speaker-model:${BASE_TAG}" >&2
  exit 1
fi

for image in agent control-api speaker-model; do
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
ARG MEMORIA_RELEASE_COMMIT
ARG MEMORIA_RELEASE_TAG
LABEL org.opencontainers.image.revision="\${MEMORIA_RELEASE_COMMIT}" \\
      org.opencontainers.image.version="\${MEMORIA_RELEASE_TAG}" \\
      com.memoria.release.role="agent"
USER root
WORKDIR /app
COPY services ./services
COPY packages ./packages
COPY scripts/verify_env.py scripts/livekit_smoke_test.py scripts/provider_smoke_test.py ./scripts/
COPY infra/voices/designed_voice_ids.json ./infra/voices/designed_voice_ids.json
COPY infra/voices/doubao_voice_ids.json ./infra/voices/doubao_voice_ids.json
USER 65532:65532
EOF

cat >"$tmp/Dockerfile.control-api" <<EOF
FROM memoria-control-api:${BASE_TAG}
ARG MEMORIA_RELEASE_COMMIT
ARG MEMORIA_RELEASE_TAG
LABEL org.opencontainers.image.revision="\${MEMORIA_RELEASE_COMMIT}" \\
      org.opencontainers.image.version="\${MEMORIA_RELEASE_TAG}" \\
      com.memoria.release.role="control-api"
USER root
WORKDIR /app
COPY services ./services
COPY packages ./packages
COPY scripts/mark_readiness.py ./scripts/mark_readiness.py
COPY infra/voices/designed_voice_ids.json ./infra/voices/designed_voice_ids.json
USER 65532:65532
EOF

cat >"$tmp/Dockerfile.speaker-model" <<EOF
FROM memoria-speaker-model:${BASE_TAG}
ARG MEMORIA_RELEASE_COMMIT
ARG MEMORIA_RELEASE_TAG
LABEL org.opencontainers.image.revision="\${MEMORIA_RELEASE_COMMIT}" \\
      org.opencontainers.image.version="\${MEMORIA_RELEASE_TAG}" \\
      com.memoria.release.role="speaker-model"
EOF

echo "delta-building memoria-agent:${NEW_TAG} from ${BASE_TAG}"
docker build \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$NEW_TAG" \
  -f "$tmp/Dockerfile.agent" -t "memoria-agent:${NEW_TAG}" "$ROOT"

echo "delta-building memoria-control-api:${NEW_TAG} from ${BASE_TAG}"
docker build \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$NEW_TAG" \
  -f "$tmp/Dockerfile.control-api" -t "memoria-control-api:${NEW_TAG}" "$ROOT"

echo "delta-relabeling memoria-speaker-model:${NEW_TAG} from ${BASE_TAG}"
docker build \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$NEW_TAG" \
  -f "$tmp/Dockerfile.speaker-model" -t "memoria-speaker-model:${NEW_TAG}" "$ROOT"

for image in agent control-api speaker-model; do
  built_arch="$(docker image inspect "memoria-${image}:${NEW_TAG}" \
    --format '{{.Architecture}}')"
  if [[ "$built_arch" != "$TARGET_ARCH" ]]; then
    echo "built image memoria-${image}:${NEW_TAG} is $built_arch, expected $TARGET_ARCH" >&2
    exit 1
  fi
done

for image in agent control-api speaker-model; do
  labels="$(docker image inspect "memoria-${image}:${NEW_TAG}" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}}')"
  if [[ "$labels" != "$MEMORIA_RELEASE_COMMIT $NEW_TAG $image" ]]; then
    echo "built image memoria-${image}:${NEW_TAG} provenance labels do not match release" >&2
    exit 1
  fi
done

docker images --format '{{.Repository}}:{{.Tag}} {{.Size}} {{.CreatedSince}}' \
  | grep -E "memoria-(agent|control-api|speaker-model):${NEW_TAG}" || true
echo "delta_build_ok ${NEW_TAG}"
