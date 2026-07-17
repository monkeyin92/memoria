#!/usr/bin/env bash
set -Eeuo pipefail

release_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
release_tag="${MEMORIA_RELEASE_TAG:-$(basename "$release_dir")}"
compose=(docker compose -f "$release_dir/docker-compose.production.yml")

export MEMORIA_RELEASE_TAG="$release_tag"

run_agent() {
  "${compose[@]}" run --rm --no-deps \
    -T \
    --entrypoint /app/.venv/bin/python agent "$@"
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

llm_provider="$(awk -F= '$1 == "LLM_PROVIDER" {print $2}' /etc/memoria.env | tail -1)"
case "${llm_provider:-qwen}" in
  qwen) llm_label=Qwen ;;
  deepseek) llm_label=DeepSeek ;;
  *) echo "invalid LLM_PROVIDER in /etc/memoria.env" >&2; exit 1 ;;
esac
provider_expected="provider_smoke_test PASS: FunASR, $llm_label, CosyVoice"
for attempt in 1 2; do
  if provider_output="$(run_agent -m scripts.provider_smoke_test 2>&1)" \
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

run_agent -m scripts.verify_env \
  --mark-smokes-passed \
  --check-ready \
  --control-api-url http://control-api:8000

echo "readiness refresh PASS: $release_tag ($llm_provider)"
