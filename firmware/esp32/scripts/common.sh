#!/usr/bin/env bash
set -euo pipefail

MEMORIA_FIRMWARE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MEMORIA_LOCK_FILE="$MEMORIA_FIRMWARE_ROOT/upstream.lock"

if [[ ! -f "$MEMORIA_LOCK_FILE" ]]; then
    printf 'missing lock file: %s\n' "$MEMORIA_LOCK_FILE" >&2
    exit 1
fi

lock_value() {
    local key="$1"
    awk -F= -v wanted="$key" '
        $1 == wanted {
            sub(/^[^=]*=/, "")
            print
            found = 1
            exit
        }
        END { if (!found) exit 1 }
    ' "$MEMORIA_LOCK_FILE"
}

MEMORIA_UPSTREAM_REPOSITORY="$(lock_value UPSTREAM_REPOSITORY)"
MEMORIA_UPSTREAM_REF="$(lock_value UPSTREAM_REF)"
MEMORIA_UPSTREAM_VERSION="$(lock_value UPSTREAM_VERSION)"
MEMORIA_ESP_IDF_REPOSITORY="$(lock_value ESP_IDF_REPOSITORY)"
MEMORIA_ESP_IDF_VERSION="$(lock_value ESP_IDF_VERSION)"
MEMORIA_ESP_IDF_TARGET="$(lock_value ESP_IDF_TARGET)"
MEMORIA_PYTHON_PREFERRED_VERSION="$(lock_value PYTHON_PREFERRED_VERSION)"
MEMORIA_BOARD_PATH="$(lock_value BOARD_PATH)"
MEMORIA_BOARD_NAME="$(lock_value BOARD_NAME)"
MEMORIA_UPSTREAM_DIR="${MEMORIA_ESP32_UPSTREAM_DIR:-$MEMORIA_FIRMWARE_ROOT/.cache/upstream}"
MEMORIA_ARTIFACT_DIR="${MEMORIA_ESP32_ARTIFACT_DIR:-$MEMORIA_FIRMWARE_ROOT/artifacts}"

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

need_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command not found: $1"
}

assert_tls_verification() {
    case "${GIT_SSL_NO_VERIFY:-}" in
        1|true|TRUE|yes|YES) die "GIT_SSL_NO_VERIFY disables TLS verification" ;;
    esac
    local config_value
    config_value="$(git config --global --get http.sslVerify 2>/dev/null || true)"
    [[ "$config_value" != "false" ]] || die "global git http.sslVerify=false is not allowed"
    config_value="$(git config --system --get http.sslVerify 2>/dev/null || true)"
    [[ "$config_value" != "false" ]] || die "system git http.sslVerify=false is not allowed"
}

python_is_supported() {
    local python_bin="$1"
    "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
        >/dev/null 2>&1
}

python_has_certifi() {
    local python_bin="$1"
    "$python_bin" -c 'import certifi; print(certifi.where())' >/dev/null 2>&1
}

select_python() {
    local candidate
    local candidates=()
    if [[ -n "${MEMORIA_PYTHON_BIN:-}" ]]; then
        candidates+=("$MEMORIA_PYTHON_BIN")
    fi
    if command -v "python${MEMORIA_PYTHON_PREFERRED_VERSION}" >/dev/null 2>&1; then
        candidates+=("$(command -v "python${MEMORIA_PYTHON_PREFERRED_VERSION}")")
    fi
    if command -v python3.13 >/dev/null 2>&1; then
        candidates+=("$(command -v python3.13)")
    fi
    if command -v python3 >/dev/null 2>&1; then
        candidates+=("$(command -v python3)")
    fi
    if command -v python >/dev/null 2>&1; then
        candidates+=("$(command -v python)")
    fi

    for candidate in "${candidates[@]}"; do
        [[ -x "$candidate" ]] || continue
        python_is_supported "$candidate" || continue
        python_has_certifi "$candidate" || continue
        printf '%s\n' "$candidate"
        return 0
    done
    die "no supported Python with certifi CA bundle found; install Python 3.12 and certifi"
}

python_certifi_ca() {
    local python_bin="$1"
    local ca_file
    ca_file="$($python_bin -c 'import certifi; print(certifi.where())')"
    [[ -s "$ca_file" ]] || die "certifi CA bundle is missing: $ca_file"
    printf '%s\n' "$ca_file"
}

configure_python_tls() {
    local python_bin="$1"
    local ca_file="$2"
    export MEMORIA_PYTHON_BIN="$python_bin"
    export PYTHON="$python_bin"
    export PYTHON3="$python_bin"
    export PIP_CERT="$ca_file"
    export SSL_CERT_FILE="$ca_file"
    export REQUESTS_CA_BUNDLE="$ca_file"
}

overlay_hash() {
    {
        find "$MEMORIA_FIRMWARE_ROOT/overlay" -type f -print | LC_ALL=C sort | while IFS= read -r file; do
            shasum -a 256 "$file"
        done
    } | shasum -a 256 | awk '{print $1}'
}

idf_version_from_path() {
    local path="$1"
    local major minor patch
    [[ -f "$path/tools/cmake/version.cmake" ]] || return 1
    major="$(sed -n 's/^set(IDF_VERSION_MAJOR[[:space:]]*\([0-9][0-9]*\)).*/\1/p' "$path/tools/cmake/version.cmake" | head -n 1)"
    minor="$(sed -n 's/^set(IDF_VERSION_MINOR[[:space:]]*\([0-9][0-9]*\)).*/\1/p' "$path/tools/cmake/version.cmake" | head -n 1)"
    patch="$(sed -n 's/^set(IDF_VERSION_PATCH[[:space:]]*\([0-9][0-9]*\)).*/\1/p' "$path/tools/cmake/version.cmake" | head -n 1)"
    [[ -n "$major" && -n "$minor" && -n "$patch" ]] || return 1
    printf 'v%s.%s.%s\n' "$major" "$minor" "$patch"
}

find_idf_path() {
    local candidate version
    local override="${MEMORIA_ESP_IDF_PATH:-${IDF_PATH:-}}"
    local candidates=()

    if [[ -n "$override" ]]; then
        candidates+=("$override")
    fi
    candidates+=(
        "$HOME/esp/esp-idf-v6.0.2"
        "$HOME/esp/esp-idf"
        "$HOME/.espressif/esp-idf-v6.0.2"
    )

    for candidate in "${candidates[@]}"; do
        [[ -d "$candidate" ]] || continue
        version="$(idf_version_from_path "$candidate" || true)"
        if [[ "$version" == "$MEMORIA_ESP_IDF_VERSION" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

list_serial_ports() {
    local port
    local ports=()
    shopt -s nullglob
    ports=(/dev/cu.*)
    shopt -u nullglob
    if ((${#ports[@]} == 0)); then
        printf 'no /dev/cu.* ports found\n' >&2
        return 1
    fi
    for port in "${ports[@]}"; do
        printf '%s\n' "$port"
    done
}

choose_serial_port() {
    local requested="${1:-auto}"
    local port
    local ports=()
    local preferred=()
    if [[ "$requested" != "auto" ]]; then
        [[ "$requested" == /dev/cu.* ]] || die "port must be an absolute /dev/cu.* path"
        [[ -e "$requested" ]] || die "serial port does not exist: $requested"
        printf '%s\n' "$requested"
        return 0
    fi

    shopt -s nullglob
    ports=(/dev/cu.*)
    shopt -u nullglob
    ((${#ports[@]} > 0)) || die "no /dev/cu.* port found; connect the board or pass --port"

    for port in "${ports[@]}"; do
        if [[ "$port" =~ (usbmodem|usbserial|wch|SLAB|UART) ]]; then
            preferred+=("$port")
        fi
    done

    if ((${#preferred[@]} == 1)); then
        printf '%s\n' "${preferred[0]}"
        return 0
    fi
    if ((${#preferred[@]} > 1)); then
        printf 'multiple likely ESP32 serial ports; pass --port explicitly:\n' >&2
        printf '  %s\n' "${preferred[@]}" >&2
        return 1
    fi
    printf 'no explicit USB serial candidate found; pass --port explicitly:\n' >&2
    printf '  %s\n' "${ports[@]}" >&2
    return 1
}
