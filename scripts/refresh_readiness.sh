#!/usr/bin/env bash
set -Eeuo pipefail

release_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
agent_env="${MEMORIA_AGENT_ENV:-/etc/memoria-agent.env}"
project_name="${MEMORIA_COMPOSE_PROJECT:-memoria}"
control_container="${MEMORIA_CONTROL_CONTAINER:-memoria-control-api-1}"
agent_container="${MEMORIA_AGENT_CONTAINER:-memoria-agent-1}"

container_env_value() {
  local container="$1" key="$2"
  docker inspect "$container" --format '{{range .Config.Env}}{{println .}}{{end}}' 2>/dev/null \
    | sed -n "s/^${key}=//p" \
    | head -n 1
}

container_config_files() {
  docker inspect "$1" \
    --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' 2>/dev/null
}

# The stack release tag carried by the running containers is the authority.  A
# component cutover swaps one service image while the stack keeps its tag, so
# the release directory name stops identifying the deployed release; deriving
# the tag from that name made every scheduled refresh fail its mark with HTTP
# 409 from the 2026-09-01 stack release until this fix, which left readiness
# permanently "smoke evidence expired" while the provider smokes were passing.
if [[ -z "${MEMORIA_RELEASE_TAG:-}" ]]; then
  MEMORIA_RELEASE_TAG="$(container_env_value "$control_container" MEMORIA_RELEASE_TAG)"
fi
if [[ -z "${MEMORIA_RELEASE_TAG:-}" ]]; then
  MEMORIA_RELEASE_TAG="$(basename "$release_dir")"
  echo "warning: $control_container is not running; falling back to release directory name '$MEMORIA_RELEASE_TAG'" >&2
fi
release_tag="$MEMORIA_RELEASE_TAG"

export MEMORIA_RELEASE_TAG="$release_tag"

# A component cutover installs a Compose base snapshot that declares
# MEMORIA_RELEASE_COMMIT with `:?`, so Compose refuses to render at all unless
# the live value is exported; that render failure used to surface only as a
# JSON decode error and left readiness evidence to expire silently.  The
# running Control API container carries the same identity pair the Agent
# heartbeat reports, so read it from there.
if [[ -z "${MEMORIA_RELEASE_COMMIT:-}" ]]; then
  MEMORIA_RELEASE_COMMIT="$(container_env_value "$control_container" MEMORIA_RELEASE_COMMIT)"
fi
if [[ -z "${MEMORIA_RELEASE_COMMIT:-}" ]]; then
  echo "warning: $control_container reports no MEMORIA_RELEASE_COMMIT; a Compose base that requires it will refuse to render" >&2
fi

export MEMORIA_RELEASE_COMMIT

# Smoke containers must run the very images the live stack runs, so reuse the
# Compose file set each live container was created with (recorded by Compose in
# its labels) instead of the base file alone.  A recorded file that has since
# disappeared, such as a /tmp overlay after a reboot, is skipped with a warning;
# the image equality check below then decides whether what remains still
# reproduces the deployed service.
COMPOSE_ARGS=(docker compose --project-name "$project_name")

compose_args_for() {
  local container="$1" recorded file
  COMPOSE_ARGS=(docker compose --project-name "$project_name")
  recorded="$(container_config_files "$container")"
  if [[ -z "$recorded" ]]; then
    COMPOSE_ARGS+=(-f "$release_dir/docker-compose.production.yml")
    return 0
  fi
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    if [[ ! -f "$file" ]]; then
      echo "warning: compose file '$file' recorded by $container is missing; skipping" >&2
      continue
    fi
    COMPOSE_ARGS+=(-f "$file")
  # The trailing newline matters: `read` drops a final field that has no line
  # terminator, which would silently drop the newest override file.
  done < <(printf '%s\n' "$recorded" | tr ',' '\n')
}

require_live_service_image() {
  local container="$1" service="$2"
  local live resolved compose_stderr
  live="$(docker inspect "$container" --format '{{.Config.Image}}')"
  compose_stderr="$(mktemp -t memoria-readiness-compose.XXXXXX)"
  if ! resolved="$("${COMPOSE_ARGS[@]}" config --format json 2>"$compose_stderr" \
    | python3 -c '
import json
import sys

try:
    payload = json.load(sys.stdin)
except json.JSONDecodeError as exc:
    raise SystemExit(f"compose config did not render JSON: {exc}")

services = payload.get("services", {})
print((services.get(sys.argv[1]) or {}).get("image") or "")
' "$service")"; then
    echo "cannot resolve the $service image from the Compose files recorded by $container" >&2
    sed 's/^/  compose: /' "$compose_stderr" >&2 || true
    rm -f "$compose_stderr"
    return 1
  fi
  rm -f "$compose_stderr"
  if [[ "$resolved" != "$live" ]]; then
    echo "refusing to collect smoke evidence: Compose resolves $service to '${resolved:-<none>}' but $container runs '$live'" >&2
    return 1
  fi
}

run_agent() {
  "${COMPOSE_ARGS[@]}" run --rm --no-deps \
    -T \
    --entrypoint /app/.venv/bin/python agent "$@"
}

run_control() {
  "${COMPOSE_ARGS[@]}" run --rm --no-deps \
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
  "${COMPOSE_ARGS[@]}" run --rm --no-deps \
    -T \
    -e MEMORIA_PROVIDER_SMOKE_REQUIRED=true \
    --entrypoint /app/.venv/bin/python agent -m scripts.provider_smoke_test
}

compose_args_for "$agent_container"
require_live_service_image "$agent_container" agent

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
case "${llm_provider:-bailian_deepseek}" in
  qwen) llm_label=Qwen ;;
  bailian_deepseek|deepseek) llm_label=DeepSeek ;;
  *) echo "invalid LLM_PROVIDER in $agent_env" >&2; exit 1 ;;
esac
provider_expected="provider_smoke_test PASS: FunASR, QwenRealtimeSearch, $llm_label, Doubao, InterruptSemantic"
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

compose_args_for "$control_container"
require_live_service_image "$control_container" control-api

# The Agent env intentionally has no MEMORIA_AUTH_SECRET.  Mark and verify
# readiness from a short-lived Control API container instead.
run_control -m scripts.mark_readiness \
  --control-api-url http://control-api:8000 \
  --skip-ready-check

wait_for_current_release_readiness

echo "readiness refresh PASS: $release_tag ($llm_provider)"
