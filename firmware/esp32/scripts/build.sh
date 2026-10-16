#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

CLEAN=0
BENCH=0
KEEP_AWAKE=0
NO_OFFLINE_AUDIO=0
LANGUAGE="${MEMORIA_FIRMWARE_LANGUAGE:-zh-CN}"
WAKE_WORD_MODEL="${MEMORIA_FIRMWARE_WAKE_WORD_MODEL:-}"
NO_IDF_INSTALL=0

usage() {
    cat <<'EOF'
Usage: build.sh [--clean] [--bench [--no-offline-audio]] [--keep-awake] [--language LOCALE] [--wake-word MODEL|disabled] [--no-idf-install]

The product build uses zh-CN. Board config disables keyword wake
(CONFIG_WAKE_WORD_DISABLED); pass --wake-word only to override for a
diagnostic image. The upstream build.py still performs the real merge-bin.

--bench builds the test-rig image from the board's config.bench.json: the
product build plus CONFIG_MEMORIA_BENCH_SERIAL (the USB console also answers
`snap`, `status`, `pcmarm` and `pcmdump`, see TODOLIST M-2 and N-10). It is
written next to the product artifacts as memoria-esp-vocat-bench-*.bin,
carries a marker the firmware publisher refuses, and flash.sh flashes it only
with --bench.

--no-offline-audio, with --bench only, builds the bench image from
config.bench.no-offline-audio.json: the bench build plus
CONFIG_MEMORIA_NO_OFFLINE_AUDIO. The assets partition packs no .ogg, so
offline lines, stories and bedtime stay silent. Mascots, the wordmark, fonts
and emoji stay. Artifacts are memoria-esp-vocat-bench-no-offline-audio-*.bin.
flash.sh --bench flashes it. The firmware publisher refuses it.

--keep-awake builds the remote-test image from config.keep-awake.json: the
product build plus CONFIG_MEMORIA_KEEP_AWAKE. Chip deep sleep stays off so
USB serial remains enumerated. The microphone stays closed until a screen
tap or USB wake. Artifacts are memoria-esp-vocat-keep-awake-*.bin. The
firmware publisher refuses them. Do not combine with --bench.
EOF
}

while (($# > 0)); do
    case "$1" in
        --clean) CLEAN=1 ;;
        --bench) BENCH=1 ;;
        --keep-awake) KEEP_AWAKE=1 ;;
        --no-offline-audio) NO_OFFLINE_AUDIO=1 ;;
        --language) shift; (($# > 0)) || die "--language requires a value"; LANGUAGE="$1" ;;
        --wake-word) shift; (($# > 0)) || die "--wake-word requires a value"; WAKE_WORD_MODEL="$1" ;;
        --no-idf-install) NO_IDF_INSTALL=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

if [[ "$BENCH" == 1 && "$KEEP_AWAKE" == 1 ]]; then
    die "pass only one of --bench and --keep-awake"
fi
if [[ "$NO_OFFLINE_AUDIO" == 1 && "$BENCH" != 1 ]]; then
    die "--no-offline-audio builds a bench image only; pass --bench --no-offline-audio"
fi

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

# A bench image never takes a product artifact's name: the product files stay what publish_firmware_release.py
# and flash.sh expect, and a bench file is recognisable from its name alone.
artifact_name="$MEMORIA_BOARD_NAME"
if [[ "$BENCH" == 1 ]]; then
    artifact_name="$MEMORIA_BOARD_NAME-bench"
    printf '%s\n' \
        '*** BENCH BUILD: the USB console answers snap, status, pcmarm and pcmdump (CONFIG_MEMORIA_BENCH_SERIAL). ***' \
        '*** For the test rig only: never publish it, never ship it. ***' >&2
    if [[ "$NO_OFFLINE_AUDIO" == 1 ]]; then
        artifact_name="$artifact_name-no-offline-audio"
        printf '%s\n' \
            '*** NO OFFLINE AUDIO: the assets partition packs no .ogg (CONFIG_MEMORIA_NO_OFFLINE_AUDIO). Offline lines, stories and bedtime stay silent. ***' >&2
    fi
elif [[ "$KEEP_AWAKE" == 1 ]]; then
    artifact_name="$MEMORIA_BOARD_NAME-keep-awake"
    printf '%s\n' \
        '*** KEEP-AWAKE BUILD: chip deep sleep is off (CONFIG_MEMORIA_KEEP_AWAKE). The mic stays closed until a screen tap or USB wake. ***' \
        '*** For a remote test device only: never publish it, never ship it. ***' >&2
fi

cd "$MEMORIA_UPSTREAM_DIR"
build_args=(
    "$python_bin"
    scripts/build.py
    "$MEMORIA_BOARD_PATH"
    --name "$MEMORIA_BOARD_NAME"
    --language "$LANGUAGE"
)
if [[ "$BENCH" == 1 && "$NO_OFFLINE_AUDIO" == 1 ]]; then
    build_args+=(--config config.bench.no-offline-audio.json)
elif [[ "$BENCH" == 1 ]]; then
    build_args+=(--config config.bench.json)
elif [[ "$KEEP_AWAKE" == 1 ]]; then
    build_args+=(--config config.keep-awake.json)
fi
if [[ -n "$WAKE_WORD_MODEL" ]]; then
    build_args+=(--wake-word "$WAKE_WORD_MODEL")
fi
"${build_args[@]}"

# The generated sdkconfig says what was really built. A product build must not carry the bench switch and a
# bench build must: either mismatch stops here, before any artifact is copied.
sdkconfig_file="$MEMORIA_UPSTREAM_DIR/sdkconfig"
[[ -s "$sdkconfig_file" ]] || die "upstream did not produce sdkconfig: $sdkconfig_file"
built_bench=0
if grep -q '^CONFIG_MEMORIA_BENCH_SERIAL=y$' "$sdkconfig_file"; then
    built_bench=1
fi
if [[ "$BENCH" == 1 && "$built_bench" != 1 ]]; then
    die "bench build requested, but the generated sdkconfig lacks CONFIG_MEMORIA_BENCH_SERIAL=y"
fi
if [[ "$BENCH" != 1 && "$built_bench" == 1 ]]; then
    die "product build carries CONFIG_MEMORIA_BENCH_SERIAL=y; the bench switch must stay out of config.json"
fi
built_keep_awake=0
if grep -q '^CONFIG_MEMORIA_KEEP_AWAKE=y$' "$sdkconfig_file"; then
    built_keep_awake=1
fi
if [[ "$KEEP_AWAKE" == 1 && "$built_keep_awake" != 1 ]]; then
    die "keep-awake build requested, but the generated sdkconfig lacks CONFIG_MEMORIA_KEEP_AWAKE=y"
fi
if [[ "$KEEP_AWAKE" != 1 && "$built_keep_awake" == 1 ]]; then
    die "product build carries CONFIG_MEMORIA_KEEP_AWAKE=y; the keep-awake switch must stay out of config.json"
fi
built_no_offline_audio=0
if grep -q '^CONFIG_MEMORIA_NO_OFFLINE_AUDIO=y$' "$sdkconfig_file"; then
    built_no_offline_audio=1
fi
if [[ "$NO_OFFLINE_AUDIO" == 1 && "$built_no_offline_audio" != 1 ]]; then
    die "no-offline-audio build requested, but the generated sdkconfig lacks CONFIG_MEMORIA_NO_OFFLINE_AUDIO=y"
fi
if [[ "$NO_OFFLINE_AUDIO" != 1 && "$built_no_offline_audio" == 1 ]]; then
    die "this build carries CONFIG_MEMORIA_NO_OFFLINE_AUDIO=y without --no-offline-audio; only config.bench.no-offline-audio.json may set it"
fi
if [[ "$NO_OFFLINE_AUDIO" == 1 ]]; then
    "$python_bin" - "$MEMORIA_UPSTREAM_DIR/build/generated_assets.bin" \
        "$MEMORIA_UPSTREAM_DIR/main/boards/memoria/esp-vocat/assets" <<'PY' || \
        die "the no-offline-audio assets partition failed its check; nothing was copied"
import json
import pathlib
import struct
import sys

# Upstream pack_assets_simple layout, little endian, CONFIG_MMAP_FILE_NAME_LENGTH=32.
#   header  u32 count, u32 checksum, u32 data_length                  12 bytes
#   table   count rows of name[32], u32 size, u32 offset, u16 w, u16 h  44 bytes each
#   data    file i starts at 12 + 44 * count + offset + 2 (0x5A5A prefix)
NAME_LEN = 32
ROW_LEN = 44
HEADER_LEN = 12

blob_path = pathlib.Path(sys.argv[1])
board_assets = pathlib.Path(sys.argv[2])
blob = blob_path.read_bytes() if blob_path.is_file() else b""
if not blob:
    raise SystemExit(
        f"{blob_path} generated_assets.bin missing; Memoria no longer packs through FLASH_DEFAULT_ASSETS?"
    )
if len(blob) < HEADER_LEN:
    raise SystemExit(f"{blob_path} is too small to be a packed assets image")
count, _checksum, _data_length = struct.unpack_from("<III", blob, 0)
table_end = HEADER_LEN + ROW_LEN * count
if count > 100_000 or table_end > len(blob):
    raise SystemExit(f"{blob_path} has an implausible asset count {count}")
names = []
index_payload = None
for index in range(count):
    at = HEADER_LEN + index * ROW_LEN
    name = blob[at : at + NAME_LEN].split(b"\0", 1)[0].decode("utf-8", "replace")
    size, data_offset = struct.unpack_from("<II", blob, at + NAME_LEN)
    names.append(name)
    if name == "index.json" and index_payload is None:
        start = table_end + data_offset + 2
        end = start + size
        if end > len(blob):
            raise SystemExit(f"{blob_path} index.json extends past the packed image")
        index_payload = blob[start:end]
if index_payload is None:
    raise SystemExit(f"{blob_path} has no packed index.json")
try:
    index = json.loads(index_payload)
except json.JSONDecodeError as exc:
    raise SystemExit(f"{blob_path} packed index.json is not JSON: {exc}") from exc
extra = index.get("extra_files", [])
if not isinstance(extra, list):
    extra = []
oggs = sorted(
    {
        item
        for item in [*names, *extra]
        if isinstance(item, str) and item.lower().endswith(".ogg")
    }
)
want = {
    path.name
    for path in board_assets.rglob("*")
    if path.is_file()
    and not path.name.startswith(".")
    and not path.name.lower().endswith(".ogg")
} - {"index.json"}
missing = sorted(want - set(names))
absent = [key for key in ("text_font", "emoji_collection") if key not in index]
if oggs or missing or absent:
    parts = []
    if oggs:
        parts.append("ogg: " + ", ".join(oggs))
    if missing:
        parts.append("missing: " + ", ".join(missing))
    if absent:
        parts.append("absent: " + ", ".join(absent))
    raise SystemExit("; ".join(parts))
PY
fi

merged="$MEMORIA_UPSTREAM_DIR/build/merged-binary.bin"
[[ -s "$merged" ]] || die "upstream did not produce a non-empty merged binary: $merged"
mkdir -p "$MEMORIA_ARTIFACT_DIR"
cp "$merged" "$MEMORIA_ARTIFACT_DIR/$artifact_name-merged.bin"
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
        cp "$image_path" "$MEMORIA_ARTIFACT_DIR/$artifact_name-$image"
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
cp "$app_image" "$MEMORIA_ARTIFACT_DIR/$artifact_name-app.bin"
# Remove stale product-level aliases only. Identity/NVS data and rollback
# backups live outside this top-level artifact glob and are not touched.
find "$MEMORIA_ARTIFACT_DIR" -maxdepth 1 -type f -name '*-xiaozhi.bin' -delete
printf 'merged binary: %s\n' "$MEMORIA_ARTIFACT_DIR/$artifact_name-merged.bin"
printf 'application binary: %s\n' "$MEMORIA_ARTIFACT_DIR/$artifact_name-app.bin"
if [[ "$BENCH" == 1 && "$NO_OFFLINE_AUDIO" == 1 ]]; then
    printf 'BENCH image without offline audio: flash it with flash.sh --bench; the firmware publisher refuses it\n'
elif [[ "$BENCH" == 1 ]]; then
    printf 'BENCH image: flash it with flash.sh --bench; the firmware publisher refuses it\n'
elif [[ "$KEEP_AWAKE" == 1 ]]; then
    printf 'KEEP-AWAKE image: flash it with flash.sh --keep-awake; the firmware publisher refuses it\n'
fi
