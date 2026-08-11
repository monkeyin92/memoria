#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

need_command bash
need_command git
need_command rg
assert_tls_verification
python_bin="$(select_python)"
ca_file="$(python_certifi_ca "$python_bin")"
configure_python_tls "$python_bin" "$ca_file"

for script in "$SCRIPT_DIR"/*.sh; do
    bash -n "$script"
    rg -q '^set -euo pipefail$' "$script" || die "missing strict shell mode: $script"
done

"$python_bin" -m json.tool \
    "$MEMORIA_FIRMWARE_ROOT/overlay/files/main/boards/memoria/atk-dnesp32s3-v1/config.json" \
    >/dev/null

"$SCRIPT_DIR/bootstrap.sh" --no-idf-install

board_dir="$MEMORIA_UPSTREAM_DIR/main/boards/memoria/atk-dnesp32s3-v1"
[[ -f "$board_dir/memoria_atk_dnesp32s3_v1.cc" ]] || die "board source missing"
[[ -f "$board_dir/config.h" ]] || die "board config missing"
[[ -f "$board_dir/config.json" ]] || die "board manifest missing"
[[ -f "$MEMORIA_UPSTREAM_DIR/main/memoria/device_identity.h" ]] || die "device identity header missing"
[[ -f "$MEMORIA_UPSTREAM_DIR/main/memoria/device_identity.cc" ]] || die "device identity source missing"
[[ -f "$MEMORIA_UPSTREAM_DIR/main/memoria/memoria_audio_frame.h" ]] || die "audio frame header missing"
[[ -f "$MEMORIA_UPSTREAM_DIR/main/memoria/memoria_audio_frame.cc" ]] || die "audio frame source missing"
[[ -f "$MEMORIA_UPSTREAM_DIR/partitions/v2/16m.csv" ]] || die "Memoria partition table missing"

rg -q '^memoria_identity,[[:space:]]*data,[[:space:]]*nvs,[[:space:]]*0x10000,[[:space:]]*64K' \
    "$MEMORIA_UPSTREAM_DIR/partitions/v2/16m.csv" || die "identity partition is not at 0x10000/64K"
rg -q 'espressif/libsodium:[[:space:]]*\^1\.0\.22' "$MEMORIA_UPSTREAM_DIR/main/idf_component.yml" || \
    die "libsodium dependency missing"
rg -q 'memoria/device_identity\.cc' "$MEMORIA_UPSTREAM_DIR/main/CMakeLists.txt" || die "device identity is not in CMake"
rg -q 'memoria/memoria_audio_frame\.cc' "$MEMORIA_UPSTREAM_DIR/main/CMakeLists.txt" || die "audio frame is not in CMake"
"$python_bin" -m py_compile "$MEMORIA_FIRMWARE_ROOT/scripts/provision_identity.py"
rg -q '"write-flash"' "$MEMORIA_FIRMWARE_ROOT/scripts/provision_identity.py" && \
rg -q 'PARTITION_OFFSET = 0x10000' "$MEMORIA_FIRMWARE_ROOT/scripts/provision_identity.py" || \
    die "identity provisioning script does not pin the flash range"

if rg -n 'esp_video|EspVideo|GetCamera|InitializeCamera|CAM_PIN|OV_' \
    "$board_dir/memoria_atk_dnesp32s3_v1.cc" "$board_dir/config.h"; then
    die "camera code/config leaked into Memoria board"
fi

rg -q 'memoria-atk-dnesp32s3-v1' "$board_dir/config.json" || die "wrong board identity"
(cd "$MEMORIA_UPSTREAM_DIR" && git diff --check)

board_json="$(cd "$MEMORIA_UPSTREAM_DIR" && "$python_bin" scripts/build.py --list-boards --json)"
printf '%s\n' "$board_json" | "$python_bin" -c '
import json
import sys

variants = json.load(sys.stdin)
match = [item for item in variants if item.get("name") == "memoria-atk-dnesp32s3-v1"]
if len(match) != 1 or match[0].get("type") != "memoria-atk-dnesp32s3-v1":
    raise SystemExit("Memoria board variant is not in upstream build manifest")
if match[0].get("target") != "esp32s3":
    raise SystemExit("Memoria board target is not esp32s3")
'

printf 'overlay check passed: %s\n' "$MEMORIA_BOARD_NAME"
