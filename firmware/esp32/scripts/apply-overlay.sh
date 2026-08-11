#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

UPSTREAM_DIR="${1:-$MEMORIA_UPSTREAM_DIR}"
[[ $# -le 1 ]] || die "apply-overlay.sh accepts only an upstream directory"

need_command git
need_command cp
need_command find
need_command shasum

[[ -d "$UPSTREAM_DIR/.git" ]] || die "upstream git worktree not found: $UPSTREAM_DIR"
[[ "$(git -C "$UPSTREAM_DIR" rev-parse HEAD)" == "$MEMORIA_UPSTREAM_REF" ]] || \
    die "upstream HEAD is not locked commit $MEMORIA_UPSTREAM_REF"

overlay_hash="$(overlay_hash)"
marker="$UPSTREAM_DIR/.memoria-overlay.sha256"

if [[ -f "$marker" && "$(<"$marker")" == "$overlay_hash" ]]; then
    printf 'overlay already applied: %s\n' "$overlay_hash"
    exit 0
fi

[[ ! -e "$marker" ]] || die "overlay hash mismatch; bootstrap must create a fresh upstream cache"
[[ -z "$(git -C "$UPSTREAM_DIR" status --porcelain --untracked-files=all)" ]] || \
    die "upstream cache is not a fresh clean clone"

patches=("$MEMORIA_FIRMWARE_ROOT"/overlay/patches/*.patch)
for patch_file in "${patches[@]}"; do
    [[ -f "$patch_file" ]] || die "no overlay patch found: $patch_file"
    git -C "$UPSTREAM_DIR" apply --check "$patch_file"
    git -C "$UPSTREAM_DIR" apply --whitespace=nowarn "$patch_file"
done

files_root="$MEMORIA_FIRMWARE_ROOT/overlay/files"
while IFS= read -r source_file; do
    relative="${source_file#"$files_root"/}"
    destination="$UPSTREAM_DIR/$relative"
    mkdir -p "$(dirname "$destination")"
    cp "$source_file" "$destination"
done < <(find "$files_root" -type f -print | LC_ALL=C sort)

git -C "$UPSTREAM_DIR" diff --check
printf '%s\n' "$overlay_hash" > "$marker"
printf 'overlay applied to %s\n' "$UPSTREAM_DIR"
printf 'overlay hash: %s\n' "$overlay_hash"
