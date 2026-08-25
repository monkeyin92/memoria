#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

CLEAN=0
LANGUAGE="${MEMORIA_FIRMWARE_LANGUAGE:-zh-CN}"
WAKE_WORD_MODEL="${MEMORIA_FIRMWARE_WAKE_WORD_MODEL:-}"
NO_IDF_INSTALL=0

usage() {
    cat <<'EOF'
Usage: build.sh [--clean] [--language LOCALE] [--wake-word MODEL|disabled] [--no-idf-install]

The product build uses zh-CN and the board's Memoria wake word ("茉莉") by
default. Pass --wake-word disabled only for an explicit bring-up diagnostic
build, or pass an upstream ESP-SR model to override the product default. The
upstream build.py still performs the real merge-bin.
EOF
}

while (($# > 0)); do
    case "$1" in
        --clean) CLEAN=1 ;;
        --language) shift; (($# > 0)) || die "--language requires a value"; LANGUAGE="$1" ;;
        --wake-word) shift; (($# > 0)) || die "--wake-word requires a value"; WAKE_WORD_MODEL="$1" ;;
        --no-idf-install) NO_IDF_INSTALL=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

if [[ "$NO_IDF_INSTALL" == 1 ]]; then
    "$SCRIPT_DIR/bootstrap.sh" --no-idf-install
else
    "$SCRIPT_DIR/bootstrap.sh"
fi

assert_tls_verification
python_bin="$(select_python)"
ca_file="$(python_certifi_ca "$python_bin")"
configure_python_tls "$python_bin" "$ca_file"
configure_idf_python_env "$python_bin"
idf_path="$(find_idf_path || true)"
[[ -n "$idf_path" ]] || die "ESP-IDF $MEMORIA_ESP_IDF_VERSION not found; run bootstrap.sh"
export IDF_PATH="$idf_path"
# shellcheck disable=SC1090
source "$IDF_PATH/export.sh"
configure_python_tls "$python_bin" "$ca_file"

need_command idf.py

if [[ "$CLEAN" == 1 ]]; then
    (cd "$MEMORIA_UPSTREAM_DIR" && idf.py fullclean)
fi

cd "$MEMORIA_UPSTREAM_DIR"
build_args=(
    "$python_bin"
    scripts/build.py
    "$MEMORIA_BOARD_PATH"
    --name "$MEMORIA_BOARD_NAME"
    --language "$LANGUAGE"
)
if [[ -n "$WAKE_WORD_MODEL" ]]; then
    build_args+=(--wake-word "$WAKE_WORD_MODEL")
fi
"${build_args[@]}"

merged="$MEMORIA_UPSTREAM_DIR/build/merged-binary.bin"
[[ -s "$merged" ]] || die "upstream did not produce a non-empty merged binary: $merged"
mkdir -p "$MEMORIA_ARTIFACT_DIR"
cp "$merged" "$MEMORIA_ARTIFACT_DIR/$MEMORIA_BOARD_NAME-merged.bin"
for image in bootloader.bin partition-table.bin; do
    image_path="$MEMORIA_UPSTREAM_DIR/build/$image"
    if [[ "$image" == "bootloader.bin" ]]; then
        image_path="$MEMORIA_UPSTREAM_DIR/build/bootloader/bootloader.bin"
    fi
    if [[ "$image" == "partition-table.bin" ]]; then
        image_path="$MEMORIA_UPSTREAM_DIR/build/partition_table/partition-table.bin"
        if [[ ! -s "$image_path" ]]; then
            image_path="$MEMORIA_UPSTREAM_DIR/build/partition-table.bin"
        fi
    fi
    if [[ -s "$image_path" ]]; then
        cp "$image_path" "$MEMORIA_ARTIFACT_DIR/$MEMORIA_BOARD_NAME-$image"
    fi
done
metadata="$MEMORIA_UPSTREAM_DIR/build/project_description.json"
[[ -s "$metadata" ]] || die "upstream did not produce build metadata: $metadata"
app_image_name="$("$python_bin" - "$metadata" <<'PY'
import json
import pathlib
import sys

metadata_path = pathlib.Path(sys.argv[1])
with metadata_path.open(encoding="utf-8") as handle:
    metadata = json.load(handle)

project_name = metadata.get("project_name")
app_bin = metadata.get("app_bin")
if project_name != "memoria":
    raise SystemExit(f"unexpected product project_name: {project_name!r}")
if not isinstance(app_bin, str) or not app_bin or pathlib.Path(app_bin).name != app_bin:
    raise SystemExit(f"invalid application image name in build metadata: {app_bin!r}")
if app_bin == "xiaozhi.bin":
    raise SystemExit("upstream application image still exposes xiaozhi.bin")
print(app_bin)
PY
)"
app_image="$MEMORIA_UPSTREAM_DIR/build/$app_image_name"
[[ -s "$app_image" ]] || die "upstream did not produce a non-empty application image: $app_image"
cp "$app_image" "$MEMORIA_ARTIFACT_DIR/$MEMORIA_BOARD_NAME-app.bin"
# Remove stale product-level aliases only. Identity/NVS data and rollback
# backups live outside this top-level artifact glob and are not touched.
find "$MEMORIA_ARTIFACT_DIR" -maxdepth 1 -type f -name '*-xiaozhi.bin' -delete
printf 'merged binary: %s\n' "$MEMORIA_ARTIFACT_DIR/$MEMORIA_BOARD_NAME-merged.bin"
printf 'application binary: %s\n' "$MEMORIA_ARTIFACT_DIR/$MEMORIA_BOARD_NAME-app.bin"
