#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

usage() {
    cat <<'EOF2'
Usage: build_ota_rollback_drill.sh <drill build number>

Builds the OTA rollback drill image: the shipping firmware with a newer build number that never confirms
itself, so that after an over-the-air install the bootloader has to roll the board back (see
make_ota_rollback_drill_sources.py). The image lands in artifacts/ota-rollback-drill/app-<build>.bin; sign
it with `publish_firmware_release.py sign --image <that file> --build <build> --out <scratch dir>`. The build
cache's sources are restored afterwards, and the next normal build rebuilds the two files. Build the
shipping image and sign it BEFORE this: the drill overwrites artifacts/memoria-esp-vocat-app.bin.
EOF2
}

build="${1:-}"
case "$build" in -h|--help) usage; exit 0 ;; esac
[[ "$build" =~ ^[0-9]+$ ]] || { usage >&2; exit 2; }

release_source="$MEMORIA_FIRMWARE_ROOT/overlay/files/main/memoria/memoria_firmware_release.h"
shipping="$(sed -n 's/^#define MEMORIA_FIRMWARE_BUILD \([0-9][0-9]*\)$/\1/p' "$release_source")"
[[ -n "$shipping" ]] || die "MEMORIA_FIRMWARE_BUILD not found in $release_source"
((build > shipping)) || die "the drill build must be newer than the shipping build $shipping"

release_header="$MEMORIA_UPSTREAM_DIR/main/memoria/memoria_firmware_release.h"
update_source="$MEMORIA_UPSTREAM_DIR/main/memoria/memoria_firmware_update.cc"
[[ -f "$release_header" && -f "$update_source" ]] || die "upstream cache is not prepared; run bootstrap.sh"

backup_dir="$(mktemp -d)"
cp "$release_header" "$backup_dir/release.h"
cp "$update_source" "$backup_dir/update.cc"
restore_sources() {
    cp "$backup_dir/release.h" "$release_header"
    cp "$backup_dir/update.cc" "$update_source"
    rm -rf "$backup_dir"
}
trap restore_sources EXIT

python_bin="$(select_python)"
"$python_bin" "$SCRIPT_DIR/make_ota_rollback_drill_sources.py" "$release_header" "$update_source" "$build"
"$SCRIPT_DIR/build.sh" --no-idf-install

drill_dir="$MEMORIA_ARTIFACT_DIR/ota-rollback-drill"
mkdir -p "$drill_dir"
cp "$MEMORIA_ARTIFACT_DIR/$MEMORIA_BOARD_NAME-app.bin" "$drill_dir/app-$build.bin"
printf 'drill image: %s\n' "$drill_dir/app-$build.bin"
