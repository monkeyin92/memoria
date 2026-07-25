#!/usr/bin/env bash
set -Eeuo pipefail

release_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
release_tag="${MEMORIA_RELEASE_TAG:-}"
if [[ -z "$release_tag" ]]; then
  release_tag="$(basename "$release_dir")"
fi
compose=(docker compose -f "$release_dir/docker-compose.production.yml")
agent_env="${MEMORIA_AGENT_ENV:-/etc/memoria-agent.env}"

export MEMORIA_RELEASE_TAG="$release_tag"

run_agent() {
  "${compose[@]}" run --rm --no-deps \
    -T \
    --entrypoint /app/.venv/bin/python agent "$@"
}

run_control() {
  "${compose[@]}" run --rm --no-deps \
    -T \
    --entrypoint /app/.venv/bin/python control-api "$@"
}

wait_for_current_release_readiness() {
  local readiness_url="http://127.0.0.1:8791/health/ready"
  local payload
  for _ in $(seq 1 20); do
    if payload="$(curl -fsS "$readiness_url" 2>/dev/null)" \
      && printf '%s' "$payload" | python3 -c '
import json
import sys

expected = sys.argv[1]
payload = json.load(sys.stdin)
agent = payload.get("checks", {}).get("agent", {})
assert payload.get("status") == "ready"
assert payload.get("release_tag") == expected
assert agent.get("status") == "ready"
assert agent.get("release_tag") == expected
' "$release_tag" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "readiness did not receive a current Agent heartbeat for $release_tag" >&2
  return 1
}

run_required_provider_smoke() {
  "${compose[@]}" run --rm --no-deps \
    -T \
    -e MEMORIA_PROVIDER_SMOKE_REQUIRED=true \
    --entrypoint /app/.venv/bin/python agent -m scripts.provider_smoke_test
}

livekit_expected='livekit_smoke_test PASS: authenticated room-service access'
for attempt in 1 2; do
  if livekit_output="$(run_agent -m scripts.livekit_smoke_test 2>&1)" \
    && grep -Fqx "$livekit_expected" <<<"$livekit_output"; then
    printf '%s\n' "$livekit_output"
    break
  fi
  if [[ "$attempt" == 2 ]]; then
    printf '%s\n' "$livekit_output" >&2
    exit 1
  fi
  sleep 2
done

llm_provider="$(awk -F= '$1 == "LLM_PROVIDER" {print $2}' "$agent_env" | tail -1)"
case "${llm_provider:-qwen}" in
  qwen) llm_label=Qwen ;;
  deepseek) llm_label=DeepSeek ;;
  *) echo "invalid LLM_PROVIDER in $agent_env" >&2; exit 1 ;;
esac
provider_expected="provider_smoke_test PASS: FunASR, $llm_label, Doubao"
for attempt in 1 2; do
  if provider_output="$(run_required_provider_smoke 2>&1)" \
    && grep -Fqx "$provider_expected" <<<"$provider_output"; then
    printf '%s\n' "$provider_output"
    break
  fi
  if [[ "$attempt" == 2 ]]; then
    printf '%s\n' "$provider_output" >&2
    exit 1
  fi
  sleep 2
done

run_agent -m scripts.verify_env

# The Agent env intentionally has no MEMORIA_AUTH_SECRET.  Mark and verify
# readiness from a short-lived Control API container instead.
run_control -m scripts.mark_readiness \
  --control-api-url http://control-api:8000

wait_for_current_release_readiness

echo "readiness refresh PASS: $release_tag ($llm_provider)"
