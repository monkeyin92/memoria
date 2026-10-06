#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

PORT=auto
MONITOR=0
BUILD=0
CLEAN=0
BENCH=0

usage() {
    cat <<'EOF'
Usage: flash.sh [--port /dev/cu.*|auto] [--monitor] [--build] [--clean] [--bench] [--list]

  --port       Use an explicit macOS serial device. Default is safe auto-select.
  --monitor    Flash and then attach idf.py monitor.
  --build      Rebuild before flashing.
  --clean      Clean before a --build.
  --bench      Flash the test-rig image (build.sh --bench, CONFIG_MEMORIA_BENCH_SERIAL).
               Without it a bench build in the tree is refused, and with it a product
               build is refused: the two never swap places by accident.
  --list       List /dev/cu.* and exit.
EOF
}

while (($# > 0)); do
    case "$1" in
        --port) shift; (($# > 0)) || die "--port requires a value"; PORT="$1" ;;
        --monitor) MONITOR=1 ;;
        --build) BUILD=1 ;;
        --clean) CLEAN=1 ;;
        --bench) BENCH=1 ;;
        --list) list_serial_ports; exit 0 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

if [[ "$BUILD" == 1 ]]; then
    build_args=()
    if [[ "$CLEAN" == 1 ]]; then
        build_args+=(--clean)
    fi
    if [[ "$BENCH" == 1 ]]; then
        build_args+=(--bench)
    fi
    # ${arr[@]+...}: an empty array is an unbound variable to bash 3.2 under set -u.
    "$SCRIPT_DIR/build.sh" ${build_args[@]+"${build_args[@]}"}
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
# What idf.py flashes is whatever was built last in this tree. Its generated sdkconfig says which kind that
# is, and the kind must be the one asked for: a bench image on a robot that is meant to run the product
# firmware (or the other way round) is stopped here.
tree_is_bench=0
if [[ -f "$MEMORIA_UPSTREAM_DIR/sdkconfig" ]] && \
    grep -q '^CONFIG_MEMORIA_BENCH_SERIAL=y$' "$MEMORIA_UPSTREAM_DIR/sdkconfig"; then
    tree_is_bench=1
fi
if [[ "$tree_is_bench" == 1 && "$BENCH" != 1 ]]; then
    die "the build in the tree is a BENCH image (CONFIG_MEMORIA_BENCH_SERIAL=y); rebuild with build.sh for the product, or pass --bench to flash it on purpose"
fi
if [[ "$tree_is_bench" != 1 && "$BENCH" == 1 ]]; then
    die "--bench was given but the build in the tree is a product image; run build.sh --bench first"
fi
[[ -f "$MEMORIA_UPSTREAM_DIR/CMakeLists.txt" ]] || \
    die "locked upstream project is missing CMakeLists.txt: $MEMORIA_UPSTREAM_DIR"
port="$(choose_serial_port "$PORT")"
cd "$MEMORIA_UPSTREAM_DIR"
[[ "$PWD" == "$MEMORIA_UPSTREAM_DIR" ]] || die "failed to enter locked upstream project"
printf 'upstream cwd: %s\n' "$PWD"
if [[ "$BENCH" == 1 ]]; then
    printf '*** flashing a BENCH image (snap and status over USB); put a product image back afterwards ***\n' >&2
fi
printf 'flashing %s on %s\n' "$MEMORIA_BOARD_NAME" "$port"
if [[ "$MONITOR" == 1 ]]; then
    idf.py -p "$port" flash monitor
else
    idf.py -p "$port" flash
fi
