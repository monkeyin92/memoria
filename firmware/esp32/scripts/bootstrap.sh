#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

REFRESH=0
NO_IDF_INSTALL=0
IDF_INSTALL_DIR="${MEMORIA_ESP_IDF_INSTALL_DIR:-$HOME/esp/esp-idf-v6.0.2}"

usage() {
    cat <<'EOF'
Usage: bootstrap.sh [--refresh] [--no-idf-install]

  --refresh          Move the existing upstream cache to a timestamped backup
                     and create a fresh locked clone.
  --no-idf-install   Locate/validate IDF only; do not clone or install it.
EOF
}

while (($# > 0)); do
    case "$1" in
        --refresh) REFRESH=1 ;;
        --no-idf-install) NO_IDF_INSTALL=1 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown option: $1" ;;
    esac
    shift
done

need_command git
need_command awk
need_command date
need_command find
need_command mv
need_command sed
need_command shasum
assert_tls_verification
python_bin="$(select_python)"
ca_file="$(python_certifi_ca "$python_bin")"
configure_python_tls "$python_bin" "$ca_file"

cache_parent="$(dirname "$MEMORIA_UPSTREAM_DIR")"
mkdir -p "$cache_parent"

move_cache_to_backup() {
    local stamp backup suffix
    stamp="$(date -u +%Y%m%d-%H%M%S)"
    backup="$cache_parent/previous-$stamp"
    suffix=1
    while [[ -e "$backup" || -L "$backup" ]]; do
        backup="$cache_parent/previous-$stamp-$suffix"
        suffix=$((suffix + 1))
    done
    mv "$MEMORIA_UPSTREAM_DIR" "$backup"
    printf 'moved previous upstream cache to %s\n' "$backup" >&2
}

cache_needs_recreate=0
if [[ -e "$MEMORIA_UPSTREAM_DIR" || -L "$MEMORIA_UPSTREAM_DIR" ]]; then
    if [[ "$REFRESH" == 1 ]]; then
        cache_needs_recreate=1
    elif [[ ! -d "$MEMORIA_UPSTREAM_DIR/.git" ]]; then
        cache_needs_recreate=1
    else
        existing_remote="$(git -C "$MEMORIA_UPSTREAM_DIR" remote get-url origin 2>/dev/null || true)"
        existing_head="$(git -C "$MEMORIA_UPSTREAM_DIR" rev-parse HEAD 2>/dev/null || true)"
        existing_marker="$MEMORIA_UPSTREAM_DIR/.memoria-overlay.sha256"
        desired_overlay_hash="$(overlay_hash)"
        existing_overlay_hash=""
        if [[ -f "$existing_marker" ]]; then
            existing_overlay_hash="$(<"$existing_marker")"
        fi
        if [[ "$existing_remote" != "$MEMORIA_UPSTREAM_REPOSITORY" || \
              "$existing_head" != "$MEMORIA_UPSTREAM_REF" || \
              "$existing_overlay_hash" != "$desired_overlay_hash" ]]; then
            cache_needs_recreate=1
        fi
    fi
fi

if [[ "$cache_needs_recreate" == 1 ]]; then
    move_cache_to_backup
fi

if [[ ! -d "$MEMORIA_UPSTREAM_DIR/.git" ]]; then
    git clone --filter=blob:none --no-checkout "$MEMORIA_UPSTREAM_REPOSITORY" "$MEMORIA_UPSTREAM_DIR"
    if ! git -C "$MEMORIA_UPSTREAM_DIR" cat-file -e "$MEMORIA_UPSTREAM_REF^{commit}" 2>/dev/null; then
        git -C "$MEMORIA_UPSTREAM_DIR" fetch --depth=1 origin "$MEMORIA_UPSTREAM_REF"
    fi
    git -C "$MEMORIA_UPSTREAM_DIR" checkout --detach "$MEMORIA_UPSTREAM_REF" >/dev/null
fi

remote_url="$(git -C "$MEMORIA_UPSTREAM_DIR" remote get-url origin)"
[[ "$remote_url" == "$MEMORIA_UPSTREAM_REPOSITORY" ]] || \
    die "upstream remote mismatch: $remote_url"
[[ "$(git -C "$MEMORIA_UPSTREAM_DIR" rev-parse HEAD)" == "$MEMORIA_UPSTREAM_REF" ]] || \
    die "upstream HEAD is not locked at $MEMORIA_UPSTREAM_REF"

if git -C "$MEMORIA_UPSTREAM_DIR" show-ref --tags --verify --quiet "refs/tags/$MEMORIA_UPSTREAM_VERSION"; then
    [[ "$(git -C "$MEMORIA_UPSTREAM_DIR" rev-list -n 1 "$MEMORIA_UPSTREAM_VERSION")" == "$MEMORIA_UPSTREAM_REF" ]] || \
        die "upstream tag $MEMORIA_UPSTREAM_VERSION does not point at the locked commit"
fi

"$SCRIPT_DIR/apply-overlay.sh" "$MEMORIA_UPSTREAM_DIR"

if ! idf_path="$(find_idf_path)"; then
    if [[ "$NO_IDF_INSTALL" == 1 ]]; then
        printf 'ESP-IDF %s not found; upstream overlay is ready, build remains unavailable.\n' "$MEMORIA_ESP_IDF_VERSION" >&2
        exit 0
    fi
    mkdir -p "$(dirname "$IDF_INSTALL_DIR")"
    if [[ -e "$IDF_INSTALL_DIR" && ! -d "$IDF_INSTALL_DIR/.git" ]]; then
        die "IDF install path exists but is not a git checkout: $IDF_INSTALL_DIR"
    fi
    if [[ ! -d "$IDF_INSTALL_DIR/.git" ]]; then
        PATH="$(dirname "$python_bin"):$PATH" git clone --branch "$MEMORIA_ESP_IDF_VERSION" \
            --depth=1 --recurse-submodules "$MEMORIA_ESP_IDF_REPOSITORY" "$IDF_INSTALL_DIR"
    else
        [[ -z "$(git -C "$IDF_INSTALL_DIR" status --porcelain --untracked-files=all)" ]] || \
            die "ESP-IDF checkout is dirty; choose a fresh MEMORIA_ESP_IDF_INSTALL_DIR"
        if ! git -C "$IDF_INSTALL_DIR" cat-file -e "$MEMORIA_ESP_IDF_VERSION^{commit}" 2>/dev/null; then
            git -C "$IDF_INSTALL_DIR" fetch --depth=1 origin "$MEMORIA_ESP_IDF_VERSION"
        fi
        [[ "$(git -C "$IDF_INSTALL_DIR" rev-parse HEAD)" == "$(git -C "$IDF_INSTALL_DIR" rev-list -n 1 "$MEMORIA_ESP_IDF_VERSION")" ]] || \
            die "existing IDF checkout is not $MEMORIA_ESP_IDF_VERSION; choose a fresh install path"
    fi
    [[ "$(git -C "$IDF_INSTALL_DIR" rev-parse --verify HEAD)" == "$(git -C "$IDF_INSTALL_DIR" rev-list -n 1 "$MEMORIA_ESP_IDF_VERSION")" ]] || \
        die "ESP-IDF tag verification failed"
    PATH="$(dirname "$python_bin"):$PATH" "$IDF_INSTALL_DIR/install.sh" "$MEMORIA_ESP_IDF_TARGET"
    idf_path="$IDF_INSTALL_DIR"
fi

printf 'upstream: %s (%s)\n' "$MEMORIA_UPSTREAM_DIR" "$MEMORIA_UPSTREAM_REF"
printf 'overlay: applied\n'
printf 'python: %s\n' "$python_bin"
printf 'certifi CA: %s\n' "$ca_file"
printf 'esp-idf: %s (%s)\n' "$idf_path" "$MEMORIA_ESP_IDF_VERSION"
