#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

PORT=auto
MONITOR=0
BUILD=0
CLEAN=0

usage() {
    cat <<'EOF'
Usage: flash.sh [--port /dev/cu.*|auto] [--monitor] [--build] [--clean] [--list]

  --port       Use an explicit macOS serial device. Default is safe auto-select.
  --monitor    Flash and then attach idf.py monitor.
  --build      Rebuild before flashing.
  --clean      Clean before a --build.
  --list       List /dev/cu.* and exit.
EOF
}

while (($# > 0)); do
    case "$1" in
        --port) shift; (($# > 0)) || die "--port requires a value"; PORT="$1" ;;
        --monitor) MONITOR=1 ;;
        --build) BUILD=1 ;;
        --clean) CLEAN=1 ;;
        --list) list_serial_ports; exit 0 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

if [[ "$BUILD" == 1 ]]; then
    if [[ "$CLEAN" == 1 ]]; then
        "$SCRIPT_DIR/build.sh" --clean
    else
        "$SCRIPT_DIR/build.sh"
    fi
else
    "$SCRIPT_DIR/bootstrap.sh" --no-idf-install
fi

python_bin="$(select_python)"
ca_file="$(python_certifi_ca "$python_bin")"
configure_python_tls "$python_bin" "$ca_file"
configure_idf_python_env "$python_bin"
idf_path="$(find_idf_path || true)"
[[ -n "$idf_path" ]] || die "ESP-IDF $MEMORIA_ESP_IDF_VERSION not found"
export IDF_PATH="$idf_path"
# shellcheck disable=SC1090
source "$IDF_PATH/export.sh"
configure_python_tls "$python_bin" "$ca_file"
need_command idf.py

[[ -s "$MEMORIA_UPSTREAM_DIR/build/merged-binary.bin" ]] || \
    die "no built binary; run build.sh or flash.sh --build first"
[[ -f "$MEMORIA_UPSTREAM_DIR/CMakeLists.txt" ]] || \
    die "locked upstream project is missing CMakeLists.txt: $MEMORIA_UPSTREAM_DIR"
port="$(choose_serial_port "$PORT")"
cd "$MEMORIA_UPSTREAM_DIR"
[[ "$PWD" == "$MEMORIA_UPSTREAM_DIR" ]] || die "failed to enter locked upstream project"
printf 'upstream cwd: %s\n' "$PWD"
printf 'flashing %s on %s\n' "$MEMORIA_BOARD_NAME" "$port"
if [[ "$MONITOR" == 1 ]]; then
    idf.py -p "$port" flash monitor
else
    idf.py -p "$port" flash
fi
