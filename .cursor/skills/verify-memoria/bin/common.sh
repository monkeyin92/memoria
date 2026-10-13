#!/usr/bin/env bash
# Shared paths and helpers for verify-memoria. Sourced by launch/doctor/api/cleanup.
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$SKILL_DIR/../../.." && pwd)"
RUNS_DIR="$SKILL_DIR/runs"
EVIDENCE_DIR="$SKILL_DIR/evidence"

DEFAULT_PORT=18000
DEFAULT_HOST=127.0.0.1
VERIFY_PG_DB=memoria_verify
VERIFY_PG_DSN="postgresql://memoria_dev:dev@127.0.0.1:5432/${VERIFY_PG_DB}"

die() {
  echo "verify-memoria: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

active_run_file() {
  echo "$RUNS_DIR/ACTIVE"
}

resolve_run_id() {
  local run_id="${1:-}"
  if [[ -n "$run_id" ]]; then
    echo "$run_id"
    return
  fi
  if [[ -n "${MEMORIA_VERIFY_RUN_ID:-}" ]]; then
    echo "$MEMORIA_VERIFY_RUN_ID"
    return
  fi
  if [[ -f "$(active_run_file)" ]]; then
    cat "$(active_run_file)"
    return
  fi
  die "no active run; pass RUN_ID or run bin/launch first"
}

run_dir() {
  echo "$RUNS_DIR/$(resolve_run_id "${1:-}")"
}

load_run_env() {
  local dir
  dir="$(run_dir "${1:-}")"
  [[ -f "$dir/run.env" ]] || die "missing $dir/run.env"
  # shellcheck disable=SC1091
  source "$dir/run.env"
}

base_url() {
  load_run_env "${1:-}"
  echo "http://${MEMORIA_VERIFY_HOST}:${MEMORIA_VERIFY_PORT}"
}

listen_pid() {
  load_run_env "${1:-}"
  lsof -nP -iTCP:"$MEMORIA_VERIFY_PORT" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true
}
