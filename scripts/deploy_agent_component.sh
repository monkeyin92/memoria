#!/usr/bin/env bash
# Upload only the Agent Python component, build a thin image on the server, and
# optionally cut over Agent + Voice Core Media Bridge with automatic rollback.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: deploy_agent_component.sh \
  --remote SSH_TARGET \
  --release-tag TAG \
  --base-image memoria-agent:TAG \
  [--expected-commit COMMIT] \
  [--remote-root /opt/memoria/component-releases] \
  [--dry-run] [--cutover]

The fast lane is intentionally limited to services/agent/** and the Voice Core
bridge launcher that wires that package. Dependency-lock, shared-service,
packages, or other runtime-script changes fail closed and must use the full
image release path.
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
remote=""
release_tag=""
base_image=""
expected_commit=""
remote_root="/opt/memoria/component-releases"
dry_run=false
cutover=false
allow_scope_drift=false

while (($#)); do
  case "$1" in
    --remote) remote="${2:-}"; shift 2 ;;
    --release-tag) release_tag="${2:-}"; shift 2 ;;
    --base-image) base_image="${2:-}"; shift 2 ;;
    --expected-commit) expected_commit="${2:-}"; shift 2 ;;
    --remote-root) remote_root="${2:-}"; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    --cutover) cutover=true; shift ;;
    --allow-scope-drift) allow_scope_drift=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$remote" && -n "$release_tag" && -n "$base_image" ]] || {
  usage >&2
  exit 2
}
[[ "$remote" =~ ^[A-Za-z0-9._@-]+$ ]] || {
  echo "invalid SSH target" >&2
  exit 2
}
[[ "$release_tag" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
  echo "invalid release tag" >&2
  exit 2
}
[[ "$base_image" =~ ^memoria-agent:[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
  echo "base image must be a versioned memoria-agent tag" >&2
  exit 2
}
[[ "$remote_root" =~ ^/[A-Za-z0-9._/-]+$ && "/$remote_root/" != *"/../"* ]] || {
  echo "invalid remote root" >&2
  exit 2
}

for command_name in git python3 ssh rsync tar; do
  command -v "$command_name" >/dev/null || {
    echo "$command_name is required" >&2
    exit 1
  }
done

if [[ -z "$expected_commit" ]]; then
  expected_commit="$(git -C "$ROOT" rev-parse HEAD)"
fi
[[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]] || {
  echo "expected commit must be a full lowercase Git SHA" >&2
  exit 2
}

python3 "$ROOT/scripts/verify_release_source.py" \
  --root "$ROOT" \
  --expected-commit "$expected_commit" \
  --release-tag "$release_tag"

base_metadata="$(
  ssh "$remote" bash -s -- "$base_image" <<'REMOTE_INSPECT'
set -Eeuo pipefail
docker image inspect "$1" \
  --format '{{.Id}} {{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}}'
REMOTE_INSPECT
)"
read -r base_image_id base_arch base_commit base_version base_role <<<"$base_metadata"
[[ "$base_image_id" =~ ^sha256:[0-9a-f]{64}$ && "$base_arch" == amd64 ]] || {
  echo "remote base image is missing or is not linux/amd64" >&2
  exit 1
}
[[ "$base_commit" =~ ^[0-9a-f]{40}$ && "$base_version" == "${base_image#*:}" ]] || {
  echo "remote base image provenance is invalid" >&2
  exit 1
}
[[ "$base_role" == agent ]] || {
  echo "remote base image role is not agent" >&2
  exit 1
}
git -C "$ROOT" cat-file -e "$base_commit^{commit}"
git -C "$ROOT" merge-base --is-ancestor "$base_commit" "$expected_commit"

dependency_inputs=(
  .dockerignore
  pyproject.toml
  uv.lock
  infra/Dockerfile.agent
)
dependency_changes="$(
  git -C "$ROOT" diff --name-only \
    "$base_commit" "$expected_commit" -- "${dependency_inputs[@]}"
)"
if [[ -n "$dependency_changes" ]]; then
  echo "agent component release rejected; dependency inputs changed:" >&2
  printf '%s\n' "$dependency_changes" >&2
  exit 1
fi

# Overlay replaces only services/agent and run_media_bridge. Reject changes
# that would leave the running Agent process on stale in-image code the overlay
# does not replace (packages, services.common, agent-image scripts/voices).
# Other services, sidecar scripts, and env examples already live in other
# images; they must not block an Agent-only cutover after merging to main.
scope_changes="$(
  git -C "$ROOT" diff --name-only "$base_commit" "$expected_commit" -- \
    services packages scripts infra
)"
scope_rejections=()
while IFS= read -r changed; do
  [[ -z "$changed" ]] && continue
  case "$changed" in
    services/agent/*|services/control_api/tests/test_production_compose.py|infra/Dockerfile.agent-source-overlay|scripts/deploy_agent_component.sh|scripts/run_media_bridge.py)
      ;;
    packages/*|services/common/*|infra/voices/*|infra/kws/*|infra/Dockerfile.agent)
      scope_rejections+=("$changed")
      ;;
    scripts/verify_env.py|scripts/livekit_smoke_test.py|scripts/provider_smoke_test.py|scripts/run_media_slo_reporter.py|scripts/media_runtime_smoke.py)
      scope_rejections+=("$changed")
      ;;
  esac
done <<<"$scope_changes"
if ((${#scope_rejections[@]})) && [[ "$allow_scope_drift" != true ]]; then
  echo "agent component release rejected; runtime changes escape the Agent component:" >&2
  printf '  %s\n' "${scope_rejections[@]}" >&2
  exit 1
fi
if ((${#scope_rejections[@]})) && [[ "$allow_scope_drift" == true ]]; then
  echo "agent component scope drift allowed; continuing overlay release:" >&2
  printf '  %s\n' "${scope_rejections[@]}" >&2
fi

tmp="$(mktemp -d /tmp/memoria-agent-component.XXXXXX)"
trap 'rm -rf "$tmp"' EXIT
artifact="$tmp/agent-source.tar"
dockerfile="$tmp/Dockerfile.agent-source-overlay"
manifest="$tmp/component-manifest.txt"

git -C "$ROOT" archive \
  --format=tar \
  --prefix=memoria/ \
  "$expected_commit" \
  services/agent/__init__.py services/agent/src services/agent/models \
  scripts/run_media_bridge.py \
  >"$artifact"
git -C "$ROOT" show \
  "$expected_commit:infra/Dockerfile.agent-source-overlay" \
  >"$dockerfile"

archive_commit="$(git get-tar-commit-id <"$artifact")"
[[ "$archive_commit" == "$expected_commit" ]] || {
  echo "component archive lost its Git commit identity" >&2
  exit 1
}

if command -v sha256sum >/dev/null 2>&1; then
  source_sha="$(sha256sum "$artifact" | cut -d ' ' -f1)"
  dockerfile_sha="$(sha256sum "$dockerfile" | cut -d ' ' -f1)"
  compose_sha="$(git -C "$ROOT" show "$base_commit:docker-compose.production.yml" | sha256sum | cut -d ' ' -f1)"
else
  source_sha="$(shasum -a 256 "$artifact" | cut -d ' ' -f1)"
  dockerfile_sha="$(shasum -a 256 "$dockerfile" | cut -d ' ' -f1)"
  compose_sha="$(git -C "$ROOT" show "$base_commit:docker-compose.production.yml" | shasum -a 256 | cut -d ' ' -f1)"
fi
if command -v sha256sum >/dev/null 2>&1; then
  lock_sha="$(git -C "$ROOT" show "$base_commit:uv.lock" | sha256sum | cut -c1-16)"
else
  lock_sha="$(git -C "$ROOT" show "$base_commit:uv.lock" | shasum -a 256 | cut -c1-16)"
fi
runtime_base="memoria-agent-runtime-base:uv-${lock_sha}-${base_commit:0:12}"
target_image="memoria-agent:${release_tag}"

cat >"$manifest" <<EOF
schema_version=1
component=agent
release_tag=$release_tag
release_commit=$expected_commit
base_image=$base_image
base_image_id=$base_image_id
base_commit=$base_commit
runtime_base=$runtime_base
target_image=$target_image
source_sha256=$source_sha
dockerfile_sha256=$dockerfile_sha
compose_sha256=$compose_sha
EOF

artifact_bytes="$(wc -c <"$artifact" | tr -d ' ')"
printf 'agent_component_preflight=PASS\n'
printf 'release_tag=%s\nrelease_commit=%s\n' "$release_tag" "$expected_commit"
printf 'base_image=%s\nbase_image_id=%s\n' "$base_image" "$base_image_id"
printf 'source_artifact_bytes=%s\nsource_sha256=%s\n' "$artifact_bytes" "$source_sha"
printf 'target_image=%s\nruntime_base=%s\n' "$target_image" "$runtime_base"

if [[ "$dry_run" == true ]]; then
  printf 'agent_component_dry_run=PASS\n'
  exit 0
fi

remote_dir="$remote_root/$release_tag"
ssh "$remote" sudo -n install -d -o root -g root -m 0700 "$remote_dir"
rsync \
  --archive \
  --no-owner \
  --no-group \
  --checksum \
  --compress \
  --partial \
  --protect-args \
  --chmod=F600 \
  "--rsync-path=sudo -n rsync" \
  "$artifact" "$dockerfile" "$manifest" \
  "$remote:$remote_dir/"

ssh "$remote" sudo -n bash -s -- \
  "$remote_dir" "$source_sha" "$dockerfile_sha" "$expected_commit" \
  "$base_image" "$base_image_id" "$runtime_base" "$target_image" \
  "$release_tag" <<'REMOTE_BUILD'
set -Eeuo pipefail
remote_dir="$1"
source_sha="$2"
dockerfile_sha="$3"
expected_commit="$4"
base_image="$5"
base_image_id="$6"
runtime_base="$7"
target_image="$8"
release_tag="$9"

cd "$remote_dir"
printf '%s  %s\n' "$source_sha" agent-source.tar | sha256sum -c -
printf '%s  %s\n' "$dockerfile_sha" Dockerfile.agent-source-overlay | sha256sum -c -
archive_commit="$(git get-tar-commit-id <agent-source.tar)"
[[ "$archive_commit" == "$expected_commit" ]]
[[ "$(docker image inspect "$base_image" --format '{{.Id}}')" == "$base_image_id" ]]

python3 - agent-source.tar <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

with tarfile.open(sys.argv[1], "r:") as archive:
    members = [member for member in archive.getmembers() if member.type != tarfile.XGLTYPE]
    if not members:
        raise SystemExit("empty component archive")
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or not path.parts
            or path.parts[0] != "memoria"
            or any(part in {"", ".", ".."} for part in path.parts)
            or (not member.isfile() and not member.isdir())
        ):
            raise SystemExit(f"unsafe component archive member: {member.name}")
PY

rm -rf build
install -d -o root -g root -m 0700 build
tar --extract --file agent-source.tar --directory build \
  --no-same-owner --no-same-permissions
install -o root -g root -m 0600 Dockerfile.agent-source-overlay build/Dockerfile

# Preserve one stable dependency base. Every source overlay derives directly
# from this tag, so repeated releases do not accumulate prior source layers.
docker tag "$base_image_id" "$runtime_base"
docker build \
  --pull=false \
  --network=none \
  --build-arg BASE_IMAGE="$runtime_base" \
  --build-arg MEMORIA_RELEASE_COMMIT="$expected_commit" \
  --build-arg MEMORIA_RELEASE_TAG="$release_tag" \
  --tag "$target_image" \
  --file build/Dockerfile \
  build

metadata="$(docker image inspect "$target_image" --format '{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}} {{index .Config.Labels "com.memoria.release.kind"}}')"
[[ "$metadata" == "amd64 $expected_commit $release_tag agent agent-source-overlay" ]]
docker run --rm --network none \
  --entrypoint /app/.venv/bin/python \
  "$target_image" \
  -c 'from services.agent.src.agent import DuplexVoiceAgent; from services.agent.src.providers.open_meteo_weather import OpenMeteoWeather; from services.agent.src.providers.qwen_realtime_search import QwenRealtimeSearch; print("agent_component_import_smoke=PASS")'

{
  printf 'agent_component_build=PASS\n'
  printf 'built_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'target_image=%s\n' "$target_image"
  docker image inspect "$target_image" --format 'target_image_id={{.Id}}'
  docker image inspect "$runtime_base" --format 'runtime_base_image_id={{.Id}}'
} | tee BUILD_RESULT.txt
sha256sum BUILD_RESULT.txt >BUILD_RESULT.txt.sha256
REMOTE_BUILD

printf 'agent_component_upload_build=PASS\n'

if [[ "$cutover" != true ]]; then
  printf 'agent_component_cutover=SKIPPED\n'
  exit 0
fi

ssh "$remote" sudo -n bash -s -- \
  "$remote_dir" "$target_image" "$release_tag" "$compose_sha" \
  "$expected_commit" <<'REMOTE_CUTOVER'
set -Eeuo pipefail
remote_dir="$1"
target_image="$2"
release_tag="$3"
expected_compose_sha="$4"
release_commit="$5"
agent_container="memoria-agent-1"
bridge_container="memoria-voice-core-media-bridge-1"
control_container="memoria-control-api-1"

container_env_value() {
  local container="$1"
  local key="$2"
  docker inspect "$container" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | sed -n "s/^${key}=//p" \
    | head -n 1
}

agent_image_id="$(docker inspect "$agent_container" --format '{{.Image}}')"
bridge_image_id="$(docker inspect "$bridge_container" --format '{{.Image}}')"
agent_image="$(docker inspect "$agent_container" --format '{{.Config.Image}}')"
bridge_image="$(docker inspect "$bridge_container" --format '{{.Config.Image}}')"
agent_release_commit="$(docker inspect "$agent_container" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
bridge_release_commit="$(docker inspect "$bridge_container" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
agent_release_tag="$(docker inspect "$agent_container" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')"
bridge_release_tag="$(docker inspect "$bridge_container" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')"
[[ "$agent_release_commit" =~ ^[0-9a-f]{40}$ \
  && "$agent_release_commit" == "$bridge_release_commit" \
  && -n "$agent_release_tag" \
  && "$agent_release_tag" == "$bridge_release_tag" ]] || {
  echo "Agent and bridge do not share one current release authority" >&2
  exit 1
}
[[ "$agent_image_id" == "$bridge_image_id" \
  && "$agent_image" == "$bridge_image" \
  && "$agent_image" =~ ^memoria-agent:[A-Za-z0-9][A-Za-z0-9._-]*$ \
  && "$(docker image inspect "$agent_image" --format '{{.Id}}')" == "$agent_image_id" ]] || {
  echo "Agent and bridge do not share one current runnable image" >&2
  exit 1
}
agent_stack_release_tag="$(container_env_value "$agent_container" MEMORIA_RELEASE_TAG)"
bridge_stack_release_tag="$(container_env_value "$bridge_container" MEMORIA_RELEASE_TAG)"
control_stack_release_tag="$(container_env_value "$control_container" MEMORIA_RELEASE_TAG)"
control_release_commit="$(docker inspect "$control_container" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
control_release_tag="$(docker inspect "$control_container" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')"
[[ -n "$agent_stack_release_tag" \
  && "$agent_stack_release_tag" == "$bridge_stack_release_tag" \
  && "$agent_stack_release_tag" == "$control_stack_release_tag" \
  && "$agent_stack_release_tag" == "$control_release_tag" \
  && "$control_release_commit" =~ ^[0-9a-f]{40}$ ]] || {
  echo "Agent, bridge, and Control do not share one runtime stack authority" >&2
  exit 1
}
stack_release_tag="$agent_stack_release_tag"
stack_release_commit="$control_release_commit"
config_files="$(docker inspect "$agent_container" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
bridge_config_files="$(docker inspect "$bridge_container" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
working_dir="$(docker inspect "$agent_container" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
project_name="$(docker inspect "$agent_container" --format '{{index .Config.Labels "com.docker.compose.project"}}')"
bridge_project_name="$(docker inspect "$bridge_container" --format '{{index .Config.Labels "com.docker.compose.project"}}')"
[[ -n "$config_files" && -n "$bridge_config_files" ]] || {
  echo "Agent or bridge is missing Compose configuration authority" >&2
  exit 1
}
[[ "$project_name" == memoria && "$bridge_project_name" == "$project_name" ]] || {
  echo "Agent and bridge do not share one Compose project authority" >&2
  exit 1
}

fallback_working_dir="/opt/memoria/releases"
fallback_config="$fallback_working_dir/docker-compose.production.yml"
component_root="${remote_dir%/*}"
IFS=',' read -r -a previous_files <<<"$config_files"
IFS=',' read -r -a bridge_previous_files <<<"$bridge_config_files"
[[ "${#previous_files[@]}" -eq "${#bridge_previous_files[@]}" ]] || {
  echo "Agent and bridge Compose stacks have different depths" >&2
  exit 1
}

# Compose labels retain every historical override path. Component releases only
# write image-only overrides, so preserving that chain makes a healthy service
# undeployable after ordinary artifact pruning. Verify every surviving override
# is one of ours and image-only, tolerate a missing component override only when
# both services reference the same path, then collapse the chain below to the
# production Compose file plus a snapshot of the verified live image.
validate_component_override() {
  local file="$1"
  local parent
  parent="$(dirname "$file")"
  [[ "${parent%/*}" == "$component_root" ]] || return 1
  case "${file##*/}" in
    agent-component.override.yml|agent-component.rollback.override.yml|pre-cutover-live.override.yml)
      ;;
    *) return 1 ;;
  esac
  python3 - "$file" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
match = re.fullmatch(
    r'services:\n'
    r'  agent:\n'
    r'    image: "(memoria-agent:[A-Za-z0-9][A-Za-z0-9._-]*)"\n'
    r'  voice-core-media-bridge:\n'
    r'    image: "(memoria-agent:[A-Za-z0-9][A-Za-z0-9._-]*)"\n',
    text,
)
if match is None:
    raise SystemExit("component override is not image-only")
PY
}

base_file=""
for index in "${!previous_files[@]}"; do
  agent_file="${previous_files[$index]}"
  bridge_file="${bridge_previous_files[$index]}"
  agent_name="${agent_file##*/}"
  bridge_name="${bridge_file##*/}"
  if [[ "$agent_name" == docker-compose.production.yml \
    || "$bridge_name" == docker-compose.production.yml ]]; then
    [[ "$agent_name" == docker-compose.production.yml \
      && "$bridge_name" == docker-compose.production.yml \
      && -z "$base_file" ]] || {
      echo "Agent and bridge Compose stacks have an ambiguous base" >&2
      exit 1
    }
    if [[ ! -f "$agent_file" ]]; then
      echo "Agent Compose base snapshot was pruned; using the verified current production file" >&2
      agent_file="$fallback_config"
    fi
    if [[ ! -f "$bridge_file" ]]; then
      echo "Bridge Compose base snapshot was pruned; using the verified current production file" >&2
      bridge_file="$fallback_config"
    fi
    for file in "$agent_file" "$bridge_file"; do
      [[ -f "$file" && ! -L "$file" ]] || {
        echo "required Compose base is unavailable or unsafe: $file" >&2
        exit 1
      }
    done
    [[ "$(sha256sum "$agent_file" | cut -d ' ' -f1)" == "$(sha256sum "$bridge_file" | cut -d ' ' -f1)" ]] || {
      echo "Agent and bridge Compose bases do not describe one current authority" >&2
      exit 1
    }
    base_file="$agent_file"
    continue
  fi

  for file in "$agent_file" "$bridge_file"; do
    parent="$(dirname "$file")"
    [[ "${parent%/*}" == "$component_root" ]] || {
      echo "unrecognized Compose override authority: $file" >&2
      exit 1
    }
    case "${file##*/}" in
      agent-component.override.yml|agent-component.rollback.override.yml|pre-cutover-live.override.yml)
        ;;
      *)
        echo "unrecognized Compose override authority: $file" >&2
        exit 1
        ;;
    esac
  done
  if [[ ! -f "$agent_file" || ! -f "$bridge_file" ]]; then
    [[ "$agent_file" == "$bridge_file" ]] || {
      echo "Agent and bridge are missing different Compose overrides" >&2
      exit 1
    }
    echo "historical component override was pruned; replacing it with a verified live-image snapshot: $agent_file" >&2
    continue
  fi
  for file in "$agent_file" "$bridge_file"; do
    [[ ! -L "$file" ]] && validate_component_override "$file" || {
      echo "component Compose override is unavailable, unsafe, or not image-only: $file" >&2
      exit 1
    }
  done
  [[ "$(sha256sum "$agent_file" | cut -d ' ' -f1)" == "$(sha256sum "$bridge_file" | cut -d ' ' -f1)" ]] || {
    echo "Agent and bridge component overrides do not describe one current authority" >&2
    exit 1
  }
done
[[ -n "$base_file" ]] || {
  echo "Compose stack is missing its production base" >&2
  exit 1
}

live_override="$remote_dir/pre-cutover-live.override.yml"
cat >"$live_override" <<EOF
services:
  agent:
    image: "$agent_image"
  voice-core-media-bridge:
    image: "$bridge_image"
EOF
chmod 0600 "$live_override"
validate_component_override "$live_override"
previous_files=("$base_file" "$live_override")

if [[ ! -d "$working_dir" ]]; then
  echo "Compose working directory was pruned; using the verified current release root" >&2
  working_dir="$fallback_working_dir"
fi
[[ -d "$working_dir" ]]

previous_args=()
for file in "${previous_files[@]}"; do
  if [[ "${file##*/}" == docker-compose.production.yml ]]; then
    [[ "$(sha256sum "$file" | cut -d ' ' -f1)" == "$expected_compose_sha" ]] || {
      echo "production Compose file does not match the Agent overlay base" >&2
      exit 1
    }
  fi
  previous_args+=(--file "$file")
done

rollback_agent="memoria-agent:rollback-${release_tag}-pre-agent"
rollback_bridge="memoria-agent:rollback-${release_tag}-pre-bridge"

freeze_container_image() {
  local container="$1"
  local image_id="$2"
  local rollback_tag="$3"
  local recovery_base="$4"
  if docker image inspect "$image_id" >/dev/null 2>&1; then
    docker tag "$image_id" "$rollback_tag"
  else
    # A running container can outlive a pruned parent image. Docker cannot
    # commit that container because its content digest is gone, so rebuild its
    # exact Agent source tree over the surviving peer image from the same
    # release authority and verify the result byte-for-byte before cutover.
    recovery_commit="$(docker image inspect "$recovery_base" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
    recovery_release="$(docker image inspect "$recovery_base" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')"
    recovery_role="$(docker image inspect "$recovery_base" --format '{{index .Config.Labels "com.memoria.release.role"}}')"
    [[ "$recovery_commit" == "$agent_release_commit" \
      && "$recovery_release" == "$agent_release_tag" \
      && "$recovery_role" == agent ]] || {
      echo "surviving peer image cannot authorize rollback recovery" >&2
      exit 1
    }
    recovery_dir="$(mktemp -d "$remote_dir/rollback-source.XXXXXX")"
    mkdir -p "$recovery_dir/memoria/services"
    docker cp "$container:/app/services/agent" "$recovery_dir/memoria/services/agent"
    cat >"$recovery_dir/Dockerfile" <<'ROLLBACK_DOCKERFILE'
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG MEMORIA_RELEASE_COMMIT
ARG MEMORIA_RELEASE_TAG
LABEL org.opencontainers.image.revision="${MEMORIA_RELEASE_COMMIT}" \
      org.opencontainers.image.version="${MEMORIA_RELEASE_TAG}" \
      com.memoria.release.role="agent" \
      com.memoria.release.kind="agent-running-source-recovery"
USER root
WORKDIR /app
RUN rm -rf /app/services/agent
COPY --chown=65532:65532 memoria/services/agent /app/services/agent
USER 65532:65532
ROLLBACK_DOCKERFILE
    docker build --pull=false --network=none \
      --build-arg "BASE_IMAGE=$recovery_base" \
      --build-arg "MEMORIA_RELEASE_COMMIT=$agent_release_commit" \
      --build-arg "MEMORIA_RELEASE_TAG=$agent_release_tag" \
      --tag "$rollback_tag" \
      --file "$recovery_dir/Dockerfile" \
      "$recovery_dir"
    live_source_digest="$(
      docker exec --user 0 "$container" sh -c \
        'cd /app && find services/agent -type f -not -path "*/__pycache__/*" -print0 | sort -z | xargs -0 sha256sum | sha256sum'
    )"
    rollback_source_digest="$(
      docker run --rm --user 0 --entrypoint sh "$rollback_tag" -c \
        'cd /app && find services/agent -type f -not -path "*/__pycache__/*" -print0 | sort -z | xargs -0 sha256sum | sha256sum'
    )"
    [[ "$live_source_digest" == "$rollback_source_digest" ]] || {
      echo "recovered rollback image does not match the running Agent source" >&2
      exit 1
    }
    rm -rf "$recovery_dir"
  fi
  docker image inspect "$rollback_tag" >/dev/null
}

freeze_container_image "$agent_container" "$agent_image_id" "$rollback_agent" "$bridge_image_id"
freeze_container_image "$bridge_container" "$bridge_image_id" "$rollback_bridge" "$agent_image_id"

override="$remote_dir/agent-component.override.yml"
cat >"$override" <<EOF
services:
  agent:
    image: "$target_image"
  voice-core-media-bridge:
    image: "$target_image"
EOF
chmod 0600 "$override"

rollback_override="$remote_dir/agent-component.rollback.override.yml"
cat >"$rollback_override" <<EOF
services:
  agent:
    image: "$rollback_agent"
  voice-core-media-bridge:
    image: "$rollback_bridge"
EOF
chmod 0600 "$rollback_override"

printf '%s\n' "${previous_files[@]}" >"$remote_dir/PRE_CUTOVER_CONFIG_FILES.txt"
{
  printf 'agent_image_id=%s\n' "$agent_image_id"
  printf 'bridge_image_id=%s\n' "$bridge_image_id"
  printf 'release_commit=%s\n' "$agent_release_commit"
  printf 'release_tag=%s\n' "$agent_release_tag"
  printf 'stack_release_commit=%s\n' "$stack_release_commit"
  printf 'stack_release_tag=%s\n' "$stack_release_tag"
  printf 'rollback_agent=%s\n' "$rollback_agent"
  printf 'rollback_bridge=%s\n' "$rollback_bridge"
} >"$remote_dir/ROLLBACK_POINT.txt"

rollback() {
  exit_code=$?
  trap - ERR
  set +e
  echo "component cutover failed; restoring previous Compose configuration" >&2
  (
    cd "$working_dir"
    env MEMORIA_RELEASE_TAG="$stack_release_tag" \
      MEMORIA_RELEASE_COMMIT="$stack_release_commit" \
      docker compose --project-name "$project_name" \
      "${previous_args[@]}" --file "$rollback_override" \
      --profile media-runtime up -d --no-deps --no-build \
      agent voice-core-media-bridge
  )
  rollback_status=$?
  if ((rollback_status == 0)); then
    echo "component rollback=PASS" >&2
  else
    echo "component rollback=FAILED status=$rollback_status" >&2
  fi
  exit "$exit_code"
}
trap rollback ERR

cd "$working_dir"
env MEMORIA_RELEASE_TAG="$stack_release_tag" MEMORIA_RELEASE_COMMIT="$stack_release_commit" \
  docker compose --project-name "$project_name" \
  "${previous_args[@]}" --file "$override" \
  --profile media-runtime up -d --no-deps --no-build \
  agent voice-core-media-bridge

for _ in $(seq 1 36); do
  agent_health="$(docker inspect "$agent_container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')"
  bridge_health="$(docker inspect "$bridge_container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')"
  if [[ "$agent_health" == healthy && "$bridge_health" == healthy ]]; then
    break
  fi
  sleep 5
done
[[ "$agent_health" == healthy && "$bridge_health" == healthy ]]
[[ "$(docker inspect "$agent_container" --format '{{.Config.Image}}')" == "$target_image" ]]
[[ "$(docker inspect "$bridge_container" --format '{{.Config.Image}}')" == "$target_image" ]]
docker exec "$bridge_container" /app/.venv/bin/python -c \
  'import socket; s=socket.create_connection(("127.0.0.1", 7001), 3); s.close(); print("bridge_grpc_socket=PASS")'

trap - ERR
{
  printf 'agent_component_cutover=PASS\n'
  printf 'cutover_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  docker inspect "$agent_container" --format 'agent_image={{.Config.Image}} agent_image_id={{.Image}} agent_health={{.State.Health.Status}}'
  docker inspect "$bridge_container" --format 'bridge_image={{.Config.Image}} bridge_image_id={{.Image}} bridge_health={{.State.Health.Status}}'
  printf 'runtime_stack_release_tag=%s\n' "$stack_release_tag"
  printf 'rollback_agent=%s\nrollback_bridge=%s\n' "$rollback_agent" "$rollback_bridge"
} | tee "$remote_dir/CUTOVER_RESULT.txt"
(cd "$remote_dir" && sha256sum CUTOVER_RESULT.txt >CUTOVER_RESULT.txt.sha256)
REMOTE_CUTOVER

printf 'agent_component_cutover=PASS\n'
