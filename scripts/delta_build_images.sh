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

base_commit=""
for image in agent control-api speaker-model miniprogram-gateway; do
  if ! docker image inspect "memoria-${image}:${BASE_TAG}" >/dev/null 2>&1; then
    echo "missing base image memoria-${image}:${BASE_TAG}; run the full image build" >&2
    exit 1
  fi
  base_arch="$(docker image inspect "memoria-${image}:${BASE_TAG}" \
    --format '{{.Architecture}}')"
  if [[ "$base_arch" != "$TARGET_ARCH" ]]; then
    echo "base image memoria-${image}:${BASE_TAG} is $base_arch, expected $TARGET_ARCH" >&2
    exit 1
  fi
  read -r revision version role < <(
    docker image inspect "memoria-${image}:${BASE_TAG}" \
      --format '{{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}}'
  )
  if [[ "$version" != "$BASE_TAG" || "$role" != "$image" || ! "$revision" =~ ^[0-9a-f]{40}$ ]]; then
    echo "base image memoria-${image}:${BASE_TAG} provenance does not match" >&2
    exit 1
  fi
  if [[ -z "$base_commit" ]]; then
    base_commit="$revision"
  elif [[ "$revision" != "$base_commit" ]]; then
    echo "base images do not share one release commit" >&2
    exit 1
  fi
done

git -C "$ROOT" cat-file -e "$base_commit^{commit}"
tagged_base_commit="$(git -C "$ROOT" rev-parse --verify "refs/tags/$BASE_TAG^{}")"
[[ "$tagged_base_commit" == "$base_commit" ]] || {
  echo "base image commit does not match Git tag $BASE_TAG" >&2
  exit 1
}
git -C "$ROOT" merge-base --is-ancestor "$base_commit" "$MEMORIA_RELEASE_COMMIT"
dependency_inputs=(
  .dockerignore
  pyproject.toml
  uv.lock
  infra/Dockerfile.agent
  infra/Dockerfile.control-api
  infra/Dockerfile.miniprogram-gateway
  infra/Dockerfile.speaker-model
  infra/requirements-speaker-model.txt
  infra/patches/3d-speaker-campplus-average-pool.patch
  scripts/export_campplus_onnx.py
)
dependency_changes=()
dependency_diff="$(
  git -C "$ROOT" diff --name-only \
    "$base_commit" "$MEMORIA_RELEASE_COMMIT" -- "${dependency_inputs[@]}"
)"
while IFS= read -r changed; do
  [[ -z "$changed" ]] || dependency_changes+=("$changed")
done <<<"$dependency_diff"
if ((${#dependency_changes[@]})); then
  printf 'delta build rejected; dependency inputs changed:\n' >&2
  printf '  %s\n' "${dependency_changes[@]}" >&2
  exit 1
fi

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
COPY infra/kws/keywords.txt ./infra/kws/keywords.txt
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
COPY scripts/mark_readiness.py scripts/rebuild_memory_projections.py ./scripts/
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
USER root
WORKDIR /app
COPY services/speaker_model /app/services/speaker_model
USER 65532:65532
EOF

cat >"$tmp/Dockerfile.miniprogram-gateway" <<EOF
FROM memoria-miniprogram-gateway:${BASE_TAG}
ARG MEMORIA_RELEASE_COMMIT
ARG MEMORIA_RELEASE_TAG
LABEL org.opencontainers.image.revision="\${MEMORIA_RELEASE_COMMIT}" \\
      org.opencontainers.image.version="\${MEMORIA_RELEASE_TAG}" \\
      com.memoria.release.role="miniprogram-gateway"
USER root
WORKDIR /app
COPY services ./services
COPY packages ./packages
USER 65532:65532
EOF

echo "delta-building memoria-agent:${NEW_TAG} from ${BASE_TAG}"
docker build \
  --pull=false \
  --network=none \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$NEW_TAG" \
  -f "$tmp/Dockerfile.agent" -t "memoria-agent:${NEW_TAG}" "$ROOT"

echo "delta-building memoria-control-api:${NEW_TAG} from ${BASE_TAG}"
docker build \
  --pull=false \
  --network=none \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$NEW_TAG" \
  -f "$tmp/Dockerfile.control-api" -t "memoria-control-api:${NEW_TAG}" "$ROOT"

echo "delta-relabeling memoria-speaker-model:${NEW_TAG} from ${BASE_TAG}"
docker build \
  --pull=false \
  --network=none \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$NEW_TAG" \
  -f "$tmp/Dockerfile.speaker-model" -t "memoria-speaker-model:${NEW_TAG}" "$ROOT"

echo "delta-building memoria-miniprogram-gateway:${NEW_TAG} from ${BASE_TAG}"
docker build \
  --pull=false \
  --network=none \
  --build-arg MEMORIA_RELEASE_COMMIT="$MEMORIA_RELEASE_COMMIT" \
  --build-arg MEMORIA_RELEASE_TAG="$NEW_TAG" \
  -f "$tmp/Dockerfile.miniprogram-gateway" \
  -t "memoria-miniprogram-gateway:${NEW_TAG}" "$ROOT"

for image in agent control-api speaker-model miniprogram-gateway; do
  built_arch="$(docker image inspect "memoria-${image}:${NEW_TAG}" \
    --format '{{.Architecture}}')"
  if [[ "$built_arch" != "$TARGET_ARCH" ]]; then
    echo "built image memoria-${image}:${NEW_TAG} is $built_arch, expected $TARGET_ARCH" >&2
    exit 1
  fi
done

for image in agent control-api speaker-model miniprogram-gateway; do
  labels="$(docker image inspect "memoria-${image}:${NEW_TAG}" \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}}')"
  if [[ "$labels" != "$MEMORIA_RELEASE_COMMIT $NEW_TAG $image" ]]; then
    echo "built image memoria-${image}:${NEW_TAG} provenance labels do not match release" >&2
    exit 1
  fi
done

docker images --format '{{.Repository}}:{{.Tag}} {{.Size}} {{.CreatedSince}}' \
  | grep -E "memoria-(agent|control-api|speaker-model|miniprogram-gateway):${NEW_TAG}" || true
echo "delta_build_ok ${NEW_TAG}"
