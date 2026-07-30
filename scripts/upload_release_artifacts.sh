#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage: upload_release_artifacts.sh \
  --artifact-dir DIR --remote SSH_TARGET \
  --release-tag TAG --base-tag TAG [--remote-root DIR] [--dry-run]
EOF
}

artifact_dir=""
remote=""
release_tag=""
base_tag=""
remote_root="/opt/memoria/incoming"
dry_run=false
while (($#)); do
  case "$1" in
    --artifact-dir) artifact_dir="${2:-}"; shift 2 ;;
    --remote) remote="${2:-}"; shift 2 ;;
    --release-tag) release_tag="${2:-}"; shift 2 ;;
    --base-tag) base_tag="${2:-}"; shift 2 ;;
    --remote-root) remote_root="${2:-}"; shift 2 ;;
    --dry-run) dry_run=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$artifact_dir" && -n "$remote" && -n "$release_tag" && -n "$base_tag" ]] || {
  usage >&2
  exit 2
}
[[ "$remote" =~ ^[A-Za-z0-9._@-]+$ ]] || {
  echo "invalid SSH target" >&2
  exit 2
}
for tag in "$release_tag" "$base_tag"; do
  [[ "$tag" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || {
    echo "invalid release tag: $tag" >&2
    exit 2
  }
done
[[ "$release_tag" != "$base_tag" ]] || {
  echo "release tag must differ from base tag" >&2
  exit 2
}
[[ "$remote_root" =~ ^/[A-Za-z0-9._/-]+$ && "/$remote_root/" != *"/../"* ]] || {
  echo "invalid remote root" >&2
  exit 2
}
artifact_dir="$(cd "$artifact_dir" && pwd -P)"

artifacts=(
  source.tar
  source.tar.sha256
  images.tar
  images.tar.sha256
  h5-dist.tar.gz
  h5-dist.tar.gz.sha256
  release-manifest.json
  release-verifier.pyz
)
artifact_paths=()
for artifact in "${artifacts[@]}"; do
  path="$artifact_dir/$artifact"
  [[ -f "$path" && ! -L "$path" ]] || {
    echo "missing release artifact: $artifact" >&2
    exit 1
  }
  artifact_paths+=("$path")
done

if command -v sha256sum >/dev/null 2>&1; then
  (cd "$artifact_dir" && sha256sum -c source.tar.sha256 images.tar.sha256 h5-dist.tar.gz.sha256)
else
  (cd "$artifact_dir" && shasum -a 256 -c source.tar.sha256 images.tar.sha256 h5-dist.tar.gz.sha256)
fi

remote_dir="$remote_root/$release_tag"
if [[ "$dry_run" == true ]]; then
  printf 'release_upload_dry_run=PASS\n'
  printf 'release_tag=%s\nbase_tag=%s\nremote=%s:%s/\nartifact_count=%s\n' \
    "$release_tag" "$base_tag" "$remote" "$remote_dir" "${#artifacts[@]}"
  exit 0
fi

command -v ssh >/dev/null || { echo "ssh is required" >&2; exit 1; }
command -v rsync >/dev/null || { echo "rsync is required" >&2; exit 1; }

upload_mode="$(
  ssh "$remote" sudo -n bash -s -- "$remote_root" "$release_tag" "$base_tag" <<'REMOTE'
set -Eeuo pipefail
root="$1"
release_tag="$2"
base_tag="$3"
destination="$root/$release_tag"
seed="$root/$base_tag"
[[ -d "$root" && ! -L "$root" && ! -L "$destination" ]] || {
  echo "release upload root or destination is unsafe" >&2
  exit 1
}
install -d -o root -g root -m 0700 "$destination"
if find "$destination" -maxdepth 1 -type l -print -quit | grep -q .; then
  echo "release upload directory contains a symlink" >&2
  exit 1
fi
if [[ -e "$destination/images.tar" ]]; then
  [[ -f "$destination/images.tar" && ! -L "$destination/images.tar" ]] || exit 1
  printf 'resume\n'
elif [[
  -d "$seed" && ! -L "$seed"
  && -f "$seed/images.tar" && ! -L "$seed/images.tar"
  && -f "$seed/images.tar.sha256" && ! -L "$seed/images.tar.sha256"
]]; then
  (cd "$seed" && sha256sum -c images.tar.sha256 >/dev/null)
  if ln "$seed/images.tar" "$destination/images.tar"; then
    printf 'seeded\n'
  else
    printf 'full\n'
  fi
else
  printf 'full\n'
fi
REMOTE
)"
case "$upload_mode" in
  seeded|resume|full) ;;
  *) echo "invalid remote upload mode" >&2; exit 1 ;;
esac

rsync \
  --archive \
  --no-owner \
  --no-group \
  --checksum \
  --compress \
  --partial \
  --protect-args \
  --chmod=D700,F600 \
  --stats \
  --human-readable \
  "--rsync-path=sudo -n rsync" \
  "${artifact_paths[@]}" \
  "$remote:$remote_dir/"

ssh "$remote" sudo -n bash -s -- "$remote_root" "$release_tag" "$base_tag" "$upload_mode" <<'REMOTE'
set -Eeuo pipefail
root="$1"
release_tag="$2"
base_tag="$3"
upload_mode="$4"
destination="$root/$release_tag"
required=(
  source.tar source.tar.sha256 images.tar images.tar.sha256
  h5-dist.tar.gz h5-dist.tar.gz.sha256 release-manifest.json release-verifier.pyz
)
for artifact in "${required[@]}"; do
  path="$destination/$artifact"
  [[ -f "$path" && ! -L "$path" ]] || {
    echo "uploaded artifact is missing or unsafe: $artifact" >&2
    exit 1
  }
  [[ "$(stat -c '%U:%G %a' "$path")" == "root:root 600" ]] || {
    echo "uploaded artifact permissions are invalid: $artifact" >&2
    exit 1
  }
done
(cd "$destination" && sha256sum -c source.tar.sha256 images.tar.sha256 h5-dist.tar.gz.sha256)
if [[ "$upload_mode" == seeded ]]; then
  (cd "$root/$base_tag" && sha256sum -c images.tar.sha256 >/dev/null)
fi
printf 'release_upload_verified=PASS\n'
REMOTE

printf 'release_upload_mode=%s\nrelease_upload=PASS\n' "$upload_mode"
