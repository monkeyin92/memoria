#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

PORT=auto
while (($# > 0)); do
    case "$1" in
        --port) shift; (($# > 0)) || die "--port requires a value"; PORT="$1" ;;
        --list) list_serial_ports; exit 0 ;;
        -h|--help)
            printf 'Usage: monitor.sh [--port /dev/cu.*|auto] [--list]\n'
            exit 0
            ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

"$SCRIPT_DIR/bootstrap.sh" --no-idf-install
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
[[ -f "$MEMORIA_UPSTREAM_DIR/CMakeLists.txt" ]] || \
    die "locked upstream project is missing CMakeLists.txt: $MEMORIA_UPSTREAM_DIR"
port="$(choose_serial_port "$PORT")"
cd "$MEMORIA_UPSTREAM_DIR"
[[ "$PWD" == "$MEMORIA_UPSTREAM_DIR" ]] || die "failed to enter locked upstream project"
printf 'upstream cwd: %s\n' "$PWD"
printf 'monitoring %s on %s\n' "$MEMORIA_BOARD_NAME" "$port"
idf.py -p "$port" monitor
