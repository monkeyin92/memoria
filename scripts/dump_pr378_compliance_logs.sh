#!/usr/bin/env sh
# Dump the PR #378 safety log sample outside the repo and hash it.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
OUT=${PR378_LOG_SAMPLE_DIR:-"$HOME/projects/memoria/share/合规留证/20261022-pr378"}

mkdir -p "$OUT"
cd "$ROOT"
PR378_LOG_SAMPLE_DIR="$OUT" uv run pytest -q \
  services/agent/tests/unit/test_pr378_compliance_logs.py
cd "$OUT"
shasum -a 256 log-sample.txt > SHA256SUMS.txt
printf 'wrote %s/log-sample.txt and SHA256SUMS.txt\n' "$OUT"
