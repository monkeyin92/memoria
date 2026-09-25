#!/usr/bin/env bash
# Build a commit-bound Control API source overlay and, only with --cutover,
# replace the single production control-api service with automatic rollback.
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: deploy_control_component.sh \
  --remote SSH_TARGET \
  --release-tag TAG \
  --base-image memoria-control-api:TAG \
  --expected-commit COMMIT \
  [--remote-root /opt/memoria/component-releases] \
  [--dry-run] [--cutover]

Default behavior is dry-run. It verifies a clean tagged source tree and performs
only a read-only SSH base-image provenance check, then verifies dependency inputs,
the Control/API archive scope, an exact Git archive, and the component manifest
without uploading, building, or touching Compose.

--cutover is the only mode allowed to upload/build or replace control-api. The
remote preflight explicitly verifies the authoritative PostgreSQL schema/RLS
contract before Compose is touched; this tool never upgrades schema or reads,
prints, or copies production secrets.
EOF
}

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
remote=""
release_tag=""
base_image=""
expected_commit=""
remote_root="/opt/memoria/component-releases"
cutover=false
dry_run=true

while (($#)); do
  case "$1" in
    --remote) remote="${2:-}"; shift 2 ;;
    --release-tag) release_tag="${2:-}"; shift 2 ;;
    --base-image) base_image="${2:-}"; shift 2 ;;
    --expected-commit) expected_commit="${2:-}"; shift 2 ;;
    --remote-root) remote_root="${2:-}"; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    --cutover) cutover=true; dry_run=false; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$remote" && -n "$release_tag" && -n "$base_image" && -n "$expected_commit" ]] || {
  usage >&2
  exit 2
}
[[ "$remote" =~ ^[A-Za-z0-9._@-]+$ ]] || { echo "invalid SSH target" >&2; exit 2; }
[[ "$release_tag" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
  echo "invalid release tag" >&2
  exit 2
}
[[ "$base_image" =~ ^memoria-control-api:[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
  echo "base image must be a versioned memoria-control-api tag" >&2
  exit 2
}
[[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]] || {
  echo "expected commit must be a full lowercase Git SHA" >&2
  exit 2
}
[[ "$remote_root" =~ ^/[A-Za-z0-9._/-]+$ && "/$remote_root/" != *"/../"* ]] || {
  echo "invalid remote root" >&2
  exit 2
}

for command_name in git python3 ssh tar; do
  command -v "$command_name" >/dev/null || {
    echo "$command_name is required" >&2
    exit 1
  }
done
if [[ "$cutover" == true ]]; then
  command -v rsync >/dev/null || { echo "rsync is required for --cutover" >&2; exit 1; }
fi

# Dry-run still requires the real tag. A local untagged worktree is not a
# release candidate and must never produce a misleading PASS receipt.
python3 "$ROOT/scripts/verify_release_source.py" \
  --root "$ROOT" \
  --expected-commit "$expected_commit" \
  --release-tag "$release_tag"

base_metadata="$({
  ssh "$remote" bash -s -- "$base_image" <<'REMOTE_INSPECT'
set -Eeuo pipefail
docker image inspect "$1" --format '{{.Id}} {{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}}'
REMOTE_INSPECT
} 2>/dev/null)" || {
  echo "remote base image is missing or unreadable" >&2
  exit 1
}
read -r base_image_id base_arch base_commit base_version base_role <<<"$base_metadata"
[[ "$base_image_id" =~ ^sha256:[0-9a-f]{64}$ && "$base_arch" == amd64 ]] || {
  echo "remote base image is not a linux/amd64 image" >&2
  exit 1
}
[[ "$base_commit" =~ ^[0-9a-f]{40}$ \
  && "$base_version" == "${base_image#*:}" \
  && "$base_role" == control-api ]] || {
  echo "remote base image provenance is invalid" >&2
  exit 1
}
git -C "$ROOT" cat-file -e "$base_commit^{commit}"
git -C "$ROOT" merge-base --is-ancestor "$base_commit" "$expected_commit"

dependency_inputs=(
  .dockerignore
  docker-compose.production.yml
  pyproject.toml
  uv.lock
  infra/Dockerfile.control-api
)
dependency_changes="$(
  git -C "$ROOT" diff --name-only "$base_commit" "$expected_commit" -- \
    "${dependency_inputs[@]}"
)"
if [[ -n "$dependency_changes" ]]; then
  echo "control component release rejected; dependency inputs changed:" >&2
  printf '  %s\n' $dependency_changes >&2
  exit 1
fi

# The overlay replaces all services/packages because Control imports shared
# authorities. Files outside this explicit release-tool/runtime boundary are a
# separate component and fail closed rather than being silently omitted.
# Session Runtime code runs only inside the Control API process (no other image
# imports it), so it ships with Control; its PostgreSQL schema does not, because
# this tool never upgrades schema.
scope_changes="$(
  git -C "$ROOT" diff --name-only "$base_commit" "$expected_commit" -- \
    services packages scripts infra
)"
scope_rejections=()
while IFS= read -r changed; do
  [[ -z "$changed" ]] && continue
  case "$changed" in
    services/session_runtime/*.sql)
      scope_rejections+=("$changed")
      ;;
    services/control_api/*|services/archive/*|services/session_runtime/*)
      ;;
    scripts/deploy_control_component.sh|scripts/verify_control_release_artifact.py|scripts/resolve_target_images.py|scripts/mark_readiness.py|scripts/rebuild_memory_projections.py|scripts/verify_authoritative_postgres.sh|scripts/tests/test_control_release_contract.py|scripts/tests/test_verify_control_release_artifact.py|scripts/tests/test_resolve_target_images.py)
      ;;
    infra/Dockerfile.control-api-source-overlay|infra/Dockerfile.control-api)
      ;;
    *)
      scope_rejections+=("$changed")
      ;;
  esac
done <<<"$scope_changes"
if ((${#scope_rejections[@]})); then
  echo "control component release rejected; changes escape the Control/archive scope:" >&2
  printf '  %s\n' "${scope_rejections[@]}" >&2
  exit 1
fi

tmp="$(mktemp -d "${TMPDIR:-/tmp}/memoria-control-component.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT
artifact="$tmp/control-source.tar"
dockerfile="$tmp/Dockerfile.control-api-source-overlay"
verifier="$tmp/verify_control_release_artifact.py"
resolver="$tmp/resolve_target_images.py"
schema_verifier="$tmp/verify_authoritative_postgres.sh"
compose_snapshot="$tmp/docker-compose.production.snapshot.yml"
manifest="$tmp/component-manifest.txt"

git -C "$ROOT" archive --format=tar --prefix=memoria/ "$expected_commit" \
  services packages \
  scripts/mark_readiness.py \
  scripts/rebuild_memory_projections.py \
  scripts/verify_control_release_artifact.py \
  >"$artifact"
git -C "$ROOT" show "$expected_commit:infra/Dockerfile.control-api-source-overlay" >"$dockerfile"
git -C "$ROOT" show "$expected_commit:scripts/verify_control_release_artifact.py" >"$verifier"
git -C "$ROOT" show "$expected_commit:scripts/resolve_target_images.py" >"$resolver"
git -C "$ROOT" show "$expected_commit:scripts/verify_authoritative_postgres.sh" >"$schema_verifier"
git -C "$ROOT" show "$base_commit:docker-compose.production.yml" >"$compose_snapshot"

archive_commit="$(git get-tar-commit-id <"$artifact")"
[[ "$archive_commit" == "$expected_commit" ]] || {
  echo "component archive lost its Git commit identity" >&2
  exit 1
}

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d ' ' -f1
  else
    shasum -a 256 "$1" | cut -d ' ' -f1
  fi
}
source_sha="$(sha256_file "$artifact")"
dockerfile_sha="$(sha256_file "$dockerfile")"
verifier_sha="$(sha256_file "$verifier")"
resolver_sha="$(sha256_file "$resolver")"
schema_verifier_sha="$(sha256_file "$schema_verifier")"
compose_sha="$(sha256_file "$compose_snapshot")"
target_image="memoria-control-api:${release_tag}"

cat >"$manifest" <<EOF
schema_version=1
component=control-api
release_tag=$release_tag
release_commit=$expected_commit
base_image=$base_image
base_image_id=$base_image_id
base_commit=$base_commit
target_image=$target_image
source_sha256=$source_sha
dockerfile_sha256=$dockerfile_sha
verifier_sha256=$verifier_sha
resolver_sha256=$resolver_sha
schema_verifier_sha256=$schema_verifier_sha
compose_sha256=$compose_sha
EOF

# Parse the manifest as data before upload. Do not source it.
python3 - "$manifest" <<'PY'
import re
import sys
from pathlib import Path

expected = (
    "schema_version", "component", "release_tag", "release_commit", "base_image",
    "base_image_id", "base_commit", "target_image", "source_sha256",
    "dockerfile_sha256", "verifier_sha256", "resolver_sha256",
    "schema_verifier_sha256", "compose_sha256",
)
values = {}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if line.count("=") != 1:
        raise SystemExit("component manifest line is invalid")
    key, value = line.split("=", 1)
    if key in values or not value:
        raise SystemExit("component manifest has duplicate or empty data")
    values[key] = value
if tuple(values) != expected:
    raise SystemExit("component manifest keys are invalid")
if values["schema_version"] != "1" or values["component"] != "control-api":
    raise SystemExit("component manifest authority is invalid")
for key in ("release_commit", "base_commit"):
    if not re.fullmatch(r"[0-9a-f]{40}", values[key]):
        raise SystemExit("component manifest commit is invalid")
for key in (
    "source_sha256", "dockerfile_sha256", "verifier_sha256",
    "resolver_sha256", "schema_verifier_sha256", "compose_sha256",
):
    if not re.fullmatch(r"[0-9a-f]{64}", values[key]):
        raise SystemExit("component manifest digest is invalid")
print("control_component_manifest=PASS")
PY
manifest_sha="$(sha256_file "$manifest")"

printf 'control_component_source=PASS\n'
printf 'release_tag=%s\nrelease_commit=%s\n' "$release_tag" "$expected_commit"
printf 'base_image=%s\nbase_image_id=%s\n' "$base_image" "$base_image_id"
printf 'target_image=%s\nsource_sha256=%s\n' "$target_image" "$source_sha"
printf 'manifest_sha256=%s\n' "$manifest_sha"

if [[ "$dry_run" == true ]]; then
  printf 'control_component_dry_run=PASS\n'
  exit 0
fi

remote_dir="$remote_root/$release_tag"
ssh "$remote" sudo -n test ! -e "$remote_dir" || {
  echo "remote release directory already exists" >&2
  exit 1
}
ssh "$remote" sudo -n install -d -o root -g root -m 0700 "$remote_dir"
rsync --archive --no-owner --no-group --checksum --compress --partial --protect-args \
  --chmod=F600 "--rsync-path=sudo -n rsync" \
  "$artifact" "$dockerfile" "$verifier" "$resolver" "$schema_verifier" \
  "$compose_snapshot" "$manifest" \
  "$remote:$remote_dir/"

ssh "$remote" sudo -n bash -s -- \
  "$remote_dir" "$source_sha" "$dockerfile_sha" "$verifier_sha" "$resolver_sha" \
  "$schema_verifier_sha" "$manifest_sha" "$expected_commit" "$release_tag" "$base_image" \
  "$base_image_id" "$base_commit" "$target_image" "$compose_sha" <<'REMOTE_BUILD'
set -Eeuo pipefail
remote_dir="$1"
source_sha="$2"
dockerfile_sha="$3"
verifier_sha="$4"
resolver_sha="$5"
schema_verifier_sha="$6"
manifest_sha="$7"
expected_commit="$8"
release_tag="$9"
base_image="${10}"
base_image_id="${11}"
base_commit="${12}"
target_image="${13}"
compose_sha="${14}"
cd "$remote_dir"
printf '%s  %s\n' "$source_sha" control-source.tar | sha256sum -c -
printf '%s  %s\n' "$dockerfile_sha" Dockerfile.control-api-source-overlay | sha256sum -c -
printf '%s  %s\n' "$verifier_sha" verify_control_release_artifact.py | sha256sum -c -
printf '%s  %s\n' "$resolver_sha" resolve_target_images.py | sha256sum -c -
printf '%s  %s\n' "$schema_verifier_sha" verify_authoritative_postgres.sh | sha256sum -c -
printf '%s  %s\n' "$compose_sha" docker-compose.production.snapshot.yml | sha256sum -c -
printf '%s  %s\n' "$manifest_sha" component-manifest.txt | sha256sum -c -
[[ "$(git get-tar-commit-id <control-source.tar)" == "$expected_commit" ]]
[[ "$(docker image inspect "$base_image" --format '{{.Id}}')" == "$base_image_id" ]]
if docker image inspect "$target_image" >/dev/null 2>&1; then
  echo "target image tag already exists" >&2
  exit 1
fi

python3 - control-source.tar <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

with tarfile.open(sys.argv[1], "r:") as archive:
    members = [item for item in archive.getmembers() if item.type != tarfile.XGLTYPE]
    if not members:
        raise SystemExit("empty component archive")
    for item in members:
        path = PurePosixPath(item.name)
        if (
            path.is_absolute() or not path.parts or path.parts[0] != "memoria"
            or any(part in {"", ".", ".."} for part in path.parts)
            or (not item.isfile() and not item.isdir())
        ):
            raise SystemExit("unsafe component archive member")
PY

rm -rf build
install -d -o root -g root -m 0700 build
tar --extract --file control-source.tar --directory build --no-same-owner --no-same-permissions
install -o root -g root -m 0600 Dockerfile.control-api-source-overlay build/Dockerfile
DOCKER_BUILDKIT="${MEMORIA_DOCKER_BUILDKIT:-0}" docker build \
  --pull=false --network=none \
  --build-arg "BASE_IMAGE=$base_image_id" \
  --build-arg "MEMORIA_RELEASE_COMMIT=$expected_commit" \
  --build-arg "MEMORIA_RELEASE_TAG=$release_tag" \
  --tag "$target_image" --file build/Dockerfile build
metadata="$(docker image inspect "$target_image" --format '{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}} {{index .Config.Labels "com.memoria.release.kind"}}')"
[[ "$metadata" == "amd64 $expected_commit $release_tag control-api control-api-source-overlay" ]]
[[ "$(docker image inspect "$target_image" --format '{{.Id}}')" != "$base_image_id" ]]
docker run --rm --network none \
  -e ENVIRONMENT=development -e OFFLINE_MOCK=true \
  -e "MEMORIA_RELEASE_COMMIT=$expected_commit" \
  -e "MEMORIA_RELEASE_TAG=$release_tag" \
  -e MEMORIA_RELEASE_ROLE=control-api \
  -e MEMORIA_RELEASE_KIND=control-api-source-overlay \
  --entrypoint /app/.venv/bin/python "$target_image" \
  -m scripts.verify_control_release_artifact \
  --expected-commit "$expected_commit" \
  --expected-tag "$release_tag" --require-metadata
printf 'control_component_build=PASS\n' | tee BUILD_RESULT.txt
sha256sum BUILD_RESULT.txt >BUILD_RESULT.txt.sha256
REMOTE_BUILD

ssh "$remote" sudo -n bash -s -- \
  "$remote_dir" "$target_image" "$release_tag" "$expected_commit" \
  "$manifest_sha" "$compose_sha" <<'REMOTE_CUTOVER'
set -Eeuo pipefail
remote_dir="$1"
target_image="$2"
release_tag="$3"
release_commit="$4"
expected_manifest_sha="$5"
expected_compose_sha="$6"
control_container="memoria-control-api-1"
fallback_base="/opt/memoria/releases/docker-compose.production.yml"

cd "$remote_dir"
printf '%s  %s\n' "$expected_manifest_sha" component-manifest.txt | sha256sum -c -
printf '%s  %s\n' "$expected_compose_sha" docker-compose.production.snapshot.yml | sha256sum -c -

python3 - component-manifest.txt "$target_image" "$release_tag" "$release_commit" \
  "$expected_compose_sha" <<'PY'
import re
import sys
from pathlib import Path

expected_keys = (
    "schema_version", "component", "release_tag", "release_commit", "base_image",
    "base_image_id", "base_commit", "target_image", "source_sha256",
    "dockerfile_sha256", "verifier_sha256", "resolver_sha256",
    "schema_verifier_sha256", "compose_sha256",
)
values = {}
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if line.count("=") != 1:
        raise SystemExit("component manifest line is invalid")
    key, value = line.split("=", 1)
    if key in values or not value:
        raise SystemExit("component manifest has duplicate or empty data")
    values[key] = value
if tuple(values) != expected_keys:
    raise SystemExit("component manifest keys are invalid")
if values["schema_version"] != "1" or values["component"] != "control-api":
    raise SystemExit("component manifest authority is invalid")
if values["target_image"] != sys.argv[2] or values["release_tag"] != sys.argv[3]:
    raise SystemExit("component manifest target identity is invalid")
if values["release_commit"] != sys.argv[4] or values["compose_sha256"] != sys.argv[5]:
    raise SystemExit("component manifest source authority is invalid")
if not re.fullmatch(r"memoria-control-api:[A-Za-z0-9][A-Za-z0-9._-]*", values["base_image"]):
    raise SystemExit("component manifest base image is invalid")
if not re.fullmatch(r"sha256:[0-9a-f]{64}", values["base_image_id"]):
    raise SystemExit("component manifest base image id is invalid")
if not re.fullmatch(r"[0-9a-f]{40}", values["base_commit"]):
    raise SystemExit("component manifest base commit is invalid")
for key in expected_keys[8:]:
    if not re.fullmatch(r"[0-9a-f]{64}", values[key]):
        raise SystemExit(f"component manifest {key} is invalid")
print("control_component_manifest=PASS")
PY

target_metadata="$(docker image inspect "$target_image" --format '{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}} {{index .Config.Labels "com.memoria.release.kind"}}')"
[[ "$target_metadata" == "amd64 $release_commit $release_tag control-api control-api-source-overlay" ]]

# Schema/RLS is a pre-cutover gate, not an image build step. Refuse rather than
# guessing a database container when the authoritative Compose label is absent.
postgres_container="$(docker ps --filter label=com.docker.compose.project=memoria-data --filter label=com.docker.compose.service=postgres --format '{{.Names}}')"
[[ -n "$postgres_container" && "$(printf '%s\n' "$postgres_container" | wc -l)" -eq 1 ]] || {
  echo "authoritative PostgreSQL container is not uniquely discoverable" >&2
  exit 1
}
POSTGRES_CONTAINER="$postgres_container" sh "$remote_dir/verify_authoritative_postgres.sh"

current_image="$(docker inspect "$control_container" --format '{{.Config.Image}}')"
current_image_id="$(docker inspect "$control_container" --format '{{.Image}}')"
[[ "$current_image" =~ ^memoria-control-api:[A-Za-z0-9][A-Za-z0-9._-]*$ ]]
[[ "$(docker image inspect "$current_image" --format '{{.Id}}')" == "$current_image_id" ]] || {
  echo "running Control image tag is unavailable or has drifted" >&2
  exit 1
}
current_role="$(docker inspect "$control_container" --format '{{index .Config.Labels "com.memoria.release.role"}}')"
current_commit="$(docker inspect "$control_container" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')"
current_version="$(docker inspect "$control_container" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')"
[[ "$current_role" == control-api \
  && "$current_commit" =~ ^[0-9a-f]{40}$ \
  && "$current_version" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]

config_files="$(docker inspect "$control_container" --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}')"
working_dir="$(docker inspect "$control_container" --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}')"
project_name="$(docker inspect "$control_container" --format '{{index .Config.Labels "com.docker.compose.project"}}')"
[[ "$project_name" == memoria && -n "$config_files" ]]
[[ -d "$working_dir" ]] || working_dir="$(dirname "$fallback_base")"
IFS=',' read -r -a previous_files <<<"$config_files"

# Compose labels can retain historical component overrides and even paths that
# have since been pruned. Never guess their semantics. Require exactly one
# production base plus image-only Control overrides, verify every surviving
# authority, then collapse it to the reviewed base snapshot plus a frozen image
# override for the currently running Control container.
component_root="${remote_dir%/*}"
validate_control_override() {
  local file="$1"
  local parent
  parent="$(dirname "$file")"
  [[ "${parent%/*}" == "$component_root" ]] || return 1
  case "${file##*/}" in
    control-component.override.yml|control-component.rollback.override.yml|pre-cutover-control.override.yml)
      ;;
    *) return 1 ;;
  esac
  python3 - "$file" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
if re.fullmatch(
    r'services:\n'
    r'  control-api:\n'
    r'    image: "memoria-control-api:[A-Za-z0-9][A-Za-z0-9._-]*"\n',
    text,
) is None:
    raise SystemExit("Control component override is not image-only")
PY
}

base_file=""
for file in "${previous_files[@]}"; do
  if [[ "${file##*/}" == docker-compose.production.yml ]]; then
    [[ -z "$base_file" ]] || {
      echo "Control Compose stack has an ambiguous production base" >&2
      exit 1
    }
    if [[ ! -f "$file" ]]; then
      [[ -f "$fallback_base" ]] || {
        echo "Control Compose base is unavailable" >&2
        exit 1
      }
      file="$fallback_base"
    fi
    [[ -f "$file" && ! -L "$file" ]] || {
      echo "Control Compose base is unsafe" >&2
      exit 1
    }
    [[ "$(sha256sum "$file" | cut -d ' ' -f1)" == "$expected_compose_sha" ]] || {
      echo "production Compose file does not match the Control dependency base" >&2
      exit 1
    }
    base_file="$file"
    continue
  fi
  if [[ ! -f "$file" ]]; then
    parent="$(dirname "$file")"
    [[ "${parent%/*}" == "$component_root" ]] || {
      echo "unrecognized missing Control Compose authority: $file" >&2
      exit 1
    }
    case "${file##*/}" in
      control-component.override.yml|control-component.rollback.override.yml|pre-cutover-control.override.yml)
        echo "historical Control override was pruned; replacing it with a verified live-image snapshot: $file" >&2
        ;;
      *)
        echo "unrecognized missing Control Compose authority: $file" >&2
        exit 1
        ;;
    esac
    continue
  fi
  [[ ! -L "$file" ]] && validate_control_override "$file" || {
    echo "Control Compose override is unsafe or not image-only: $file" >&2
    exit 1
  }
done
[[ -n "$base_file" ]] || {
  echo "Control Compose stack is missing its production base" >&2
  exit 1
}

live_override="$remote_dir/pre-cutover-control.override.yml"
cat >"$live_override" <<EOF
services:
  control-api:
    image: "$current_image"
EOF
chmod 0600 "$live_override"
validate_control_override "$live_override"
previous_files=("$base_file" "$live_override")
previous_args=(--file "$base_file" --file "$live_override")
[[ -d "$working_dir" ]] || working_dir="$(dirname "$base_file")"
[[ -d "$working_dir" ]] || {
  echo "Control Compose working directory is unavailable" >&2
  exit 1
}

rollback_image="memoria-control-api:rollback-${release_tag}-pre-control"
if docker image inspect "$rollback_image" >/dev/null 2>&1; then
  rollback_metadata="$(docker image inspect "$rollback_image" --format '{{.Id}} {{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}}')"
  [[ "$rollback_metadata" == "$current_image_id amd64 $current_commit $current_version control-api" ]] || {
    echo "existing rollback tag does not identify the running Control release" >&2
    exit 1
  }
else
  docker tag "$current_image_id" "$rollback_image"
fi
rollback_metadata="$(docker image inspect "$rollback_image" --format '{{.Id}} {{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}} {{index .Config.Labels "org.opencontainers.image.version"}} {{index .Config.Labels "com.memoria.release.role"}}')"
[[ "$rollback_metadata" == "$current_image_id amd64 $current_commit $current_version control-api" ]]

override="$remote_dir/control-component.override.yml"
rollback_override="$remote_dir/control-component.rollback.override.yml"
cat >"$override" <<EOF
services:
  control-api:
    image: "$target_image"
EOF
cat >"$rollback_override" <<EOF
services:
  control-api:
    image: "$rollback_image"
EOF
chmod 0600 "$override" "$rollback_override"
validate_control_override "$override"
validate_control_override "$rollback_override"

container_env_value() {
  local key="$1"
  docker inspect "$control_container" --format '{{range .Config.Env}}{{println .}}{{end}}' \
    | sed -n "s/^${key}=//p" \
    | head -n1
}
stack_tag="$(container_env_value MEMORIA_RELEASE_TAG)"
stack_commit="$(container_env_value MEMORIA_RELEASE_COMMIT)"
[[ -n "$stack_tag" && "$stack_commit" =~ ^[0-9a-f]{40}$ ]]

# Resolve the exact live file chain plus this image-only override before the
# rollback trap and before Compose can replace anything.
resolve_output="$(
  cd "$working_dir"
  python3 "$remote_dir/resolve_target_images.py" \
    --release-dir "$(cd "$(dirname "$base_file")" && pwd)" \
    --stack-tag "$stack_tag" --release-commit "$stack_commit" \
    --expected-tag "$release_tag" --expected-image "control-api=$target_image" \
    --services control-api --profile '' \
    --stack-image "memoria-control-api:$stack_tag" \
    --docker-cmd docker --override "$live_override" --override "$override"
)"
printf '%s\n' "$resolve_output"

rollback() {
  exit_code=$?
  trap - ERR
  set +e
  echo "control component cutover failed; restoring frozen image" >&2
  (
    cd "$working_dir"
    env MEMORIA_RELEASE_TAG="$stack_tag" MEMORIA_RELEASE_COMMIT="$stack_commit" \
      docker compose --project-name "$project_name" "${previous_args[@]}" \
      --file "$rollback_override" up -d --no-deps --no-build control-api
  )
  rollback_status=$?
  if ((rollback_status == 0)); then
    echo "control component rollback=PASS" >&2
  else
    echo "control component rollback=FAILED status=$rollback_status" >&2
  fi
  exit "$exit_code"
}
trap rollback ERR

cd "$working_dir"
env MEMORIA_RELEASE_TAG="$stack_tag" MEMORIA_RELEASE_COMMIT="$stack_commit" \
  docker compose --project-name "$project_name" "${previous_args[@]}" \
  --file "$override" up -d --no-deps --no-build control-api

health=""
for _ in $(seq 1 36); do
  health="$(docker inspect "$control_container" --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}')"
  [[ "$health" == healthy ]] && break
  sleep 5
done
[[ "$health" == healthy ]]
[[ "$(docker inspect "$control_container" --format '{{.Config.Image}}')" == "$target_image" ]]
[[ "$(docker inspect "$control_container" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" == "$release_commit" ]]
[[ "$(docker inspect "$control_container" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')" == "$release_tag" ]]
trap - ERR
{
  printf 'control_component_cutover=PASS\n'
  printf 'target_image=%s\nrollback_image=%s\n' "$target_image" "$rollback_image"
  printf 'resolved_target_images=%s\n' "$(printf '%s' "$resolve_output" | paste -sd'|' -)"
} | tee "$remote_dir/CUTOVER_RESULT.txt"
sha256sum "$remote_dir/CUTOVER_RESULT.txt" >"$remote_dir/CUTOVER_RESULT.txt.sha256"
REMOTE_CUTOVER

printf 'control_component_cutover=PASS\n'
