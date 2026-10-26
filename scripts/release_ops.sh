#!/usr/bin/env bash
# Server-side full-stack release steps (run as root). Usage:
#   TAG=... COMMIT=... release_ops.sh <step>
#   steps: verify-load | freeze | env | schema | cutover | finish | rollback
#          cleanup-freeze-dumps (expire .cutover freeze dumps older than 3 days)
#          control-store (CONTROL_STORE_MODE=dry-run|apply; separate, authorized
#          SQLite-to-PostgreSQL move of the Control API store, see the runbook)
# Never prints secret values. Every step fails closed.
#
# Installed on the host as /root/memoria-release/release-ops.sh (root 0700).
# The script names no PREV release: freeze derives the stack this release
# replaces from the live containers and writes it to $S/prev-release, and
# cutover, finish and rollback read only that file.
set -Eeuo pipefail
: "${TAG:?}" "${COMMIT:?}"

# The Compose services this script owns, one row each, in cutover order.
#   ship     this release builds memoria-<role> and cutover recreates the service.
#   retired  the candidate compose must not define it. If PREV still runs it,
#            cutover stops it and rollback recreates it from PREV's compose file.
# memoria-agent is the Voice Core media bridge image. media-edge is released
# separately and is not touched here. Delete a retired row only once no
# rollback target runs that service.
STACK='
control-api              control-api    ship
voice-core-media-bridge  agent          ship
speaker-model            -              retired
agent                    -              retired
miniprogram-gateway      -              retired
device-media-gateway     -              retired
'
stack() { awk -v lifecycle="$1" -v column="$2" 'NF == 3 && $3 == lifecycle { print $column }' <<<"$STACK"; }
mapfile -t SHIP < <(stack ship 1)
mapfile -t ROLES < <(stack ship 2)
mapfile -t RETIRED < <(stack retired 1)
is_ship() { [[ " ${SHIP[*]} " == *" $1 "* ]]; }
container_of() { printf 'memoria-%s-1' "$1"; }
ship_images() { local i; for i in "${!SHIP[@]}"; do printf '%s=memoria-%s ' "${SHIP[$i]}" "${ROLES[$i]}"; done; }

# Prints "<service>|<persisted path>|<sha256>|<original path>" for the override
# row (host state: state/component-overrides.tsv) whose persisted or original
# path equals $1. Returns 1 for anything else. Production resolves the row to
# those absolute paths; the test host rebases them onto its own fake root (and
# RELEASE_OPS_TMP_ROOT onto its fake /tmp).
override_spec() {
  local key=$1 svc persistent sha origin path origin_path tmp_root=/tmp line_num=0
  local state_file="$(dirname "$RELEASES")/state/component-overrides.tsv"
  local release_root="$(dirname "$RELEASES")"

  if [[ ! -e "$state_file" ]]; then
    [[ -L "$state_file" ]] && fail "state file must be a regular file, not a symlink: $state_file"
    return 1
  fi
  [[ -f "$state_file" && ! -L "$state_file" ]] \
    || fail "state file must be a regular file, not a symlink: $state_file"

  local mode owner
  mode="$(stat -c '%a' "$state_file" 2>/dev/null || stat -f '%A' "$state_file" 2>/dev/null)"
  owner="$(stat -c '%u' "$state_file" 2>/dev/null || stat -f '%u' "$state_file" 2>/dev/null)"
  [[ "$owner" == "0" ]] || fail "state file must be owned by uid 0: $state_file"
  [[ "$mode" == "600" || "$mode" == "644" ]] \
    || fail "state file must be 0600 or 0644, not $mode: $state_file"

  # The fake host rebases these roots; /opt/memoria keeps the literal paths.
  [[ "$release_root" == /opt/memoria ]] || tmp_root="${RELEASE_OPS_TMP_ROOT:-/tmp}"

  while IFS=' ' read -r svc persistent sha origin; do
    line_num=$((line_num + 1))
    [[ -n "$svc" ]] || continue

    # Validate line structure.
    [[ -n "$persistent" && -n "$sha" && -n "$origin" ]] \
      || fail "malformed state file line $line_num: must have exactly 4 fields: $state_file"

    # Validate each row: 64-hex sha, persisted under component-releases/, original under /tmp/.
    [[ "$sha" =~ ^[0-9a-f]{64}$ ]] \
      || fail "malformed state file line $line_num: sha must be 64 hex chars: $state_file"
    [[ "$persistent" == "/opt/memoria/component-releases/"* ]] \
      || fail "malformed state file line $line_num: persisted path must be under /opt/memoria/component-releases/ - $persistent"
    [[ "$origin" == "/tmp/"* ]] \
      || fail "malformed state file line $line_num: original path must be under /tmp/ - $origin"

    path="${persistent/#\/opt\/memoria/$release_root}"
    origin_path="${origin/#\/tmp/$tmp_root}"
    if [[ "$key" == "$persistent" || "$key" == "$path" \
          || "$key" == "$origin" || "$key" == "$origin_path" ]]; then
      printf '%s|%s|%s|%s\n' "$svc" "$path" "$sha" "$origin_path"
      return 0
    fi
  done < "$state_file"
  return 1
}

file_sha256() { sha256sum "$1" | cut -d' ' -f1; }

# An override may set exactly services.<service>.image, to the image the
# container actually runs. Reject comments and every other key. The value may
# be written bare or double-quoted: deploy_agent_component.sh quotes it, and a
# quoting-only difference must not decide whether an override is registrable.
validate_override_file() {   # <file> <service> <image>
  python3 - "$1" "$2" "$3" <<'PY'
import re
import sys
from pathlib import Path

path, service, image = sys.argv[1:4]
text = Path(path).read_text(encoding="utf-8")
match = re.fullmatch(
    r'services:\n  ([A-Za-z0-9_.-]+):\n    image: (?:"([^"\s]+)"|([^\s"]+))\n', text
)
if match is None:
    raise SystemExit(f"override {path} is not exactly services.<service>.image")
covered, quoted, bare = match.groups()
selected = quoted or bare
if covered != service:
    raise SystemExit(f"override {path} covers {covered}, not {service}")
if selected != image:
    raise SystemExit(f"override {path} selects {selected}, but the container runs {image}")
PY
}

# Prints the release tree's commit, or the one commit all overridden images
# agree on. A component image's labels name its component, not the stack, so a
# missing release commit must not turn a uniform PREV into a guess.
prev_stack_commit() {   # <release dir> <revision>...
  local dir=$1 value unique
  shift
  value="$(sed -n 's/^MEMORIA_RELEASE_COMMIT=//p' "$dir/.env" 2>/dev/null | head -n 1 || true)"
  if [[ "$value" =~ ^[0-9a-f]{40}$ ]]; then
    printf '%s\n' "$value"
    return 0
  fi
  unique="$(printf '%s\n' "$@" | sort -u)"
  if [[ -n "$unique" && "$unique" != *$'\n'* && "$unique" =~ ^[0-9a-f]{40}$ ]]; then
    printf '%s\n' "$unique"
    return 0
  fi
  return 1
}

declare -A PREV_OVERRIDE_PATH=() PREV_OVERRIDE_SHA=() PREV_OVERRIDE_IMAGE=()

release_paths() {
  RELEASES=$1/releases
  CURRENT=$1/current
  U=$1/incoming/$TAG
  R=$RELEASES/$TAG
  S=$R/.cutover
  # PostgreSQL still bind-mounts its schema files from this older tree, so schema
  # upgrades are written there (in place, keeping the inode) -- never into PREV.
  DATA_TREE=$RELEASES/20260827-architecture-split-v1
}
release_paths /opt/memoria

log() { printf '[%s] %s\n' "$(date +%T)" "$*"; }
fail() { log "$*" >&2; exit 1; }

# The host port the Control API publishes: an explicit MEMORIA_CONTROL_API_PORT,
# else the port the live container already publishes, else the repo default
# 8791. 110.42.235.198 shares the host with hr-tracker, which holds 8791, so
# there the Control API sits on 18791; verify-load copies the answer into the
# release dir's .env so Compose publishes the same port after cutover.
# (refresh_readiness.sh carries the same function.)
control_api_port() {
  local port
  port="${MEMORIA_CONTROL_API_PORT:-}"
  if [[ -z "$port" ]]; then
    port="$(docker port memoria-control-api-1 8000/tcp 2>/dev/null \
      | sed -n 's/^127\.0\.0\.1:\([0-9][0-9]*\)$/\1/p' | head -n 1 || true)"
  fi
  printf '%s' "${port:-8791}"
}

# The container facts freeze and cutover decide on. An absent container fails.
inspect_container() {   # <container> chain|version|revision|image|image-id|running
  local format
  case "$2" in
    chain) format='{{index .Config.Labels "com.docker.compose.project.config_files"}}' ;;
    version|revision) format="{{index .Config.Labels \"org.opencontainers.image.$2\"}}" ;;
    image) format='{{.Config.Image}}' ;;
    image-id) format='{{.Image}}' ;;
    # Filter inside docker inspect so no other env value leaves the daemon.
    release-tag) format='{{range .Config.Env}}{{if and (ge (len .) 20) (eq (slice . 0 20) "MEMORIA_RELEASE_TAG=")}}{{slice . 20}}{{end}}{{end}}' ;;
    running) format='{{.State.Running}}' ;;
    *) fail "inspect_container: unknown field $2" ;;
  esac
  docker inspect "$1" --format "$format"
}

# One status line per container. Some memoria containers (the SenseVoice
# sidecar, a LiveKit server left on the host) define no healthcheck, and a bare .State.Health.Status
# template aborts the whole step under set -e/pipefail.
container_state() {
  docker inspect "$1" --format '{{.Name}} {{.Config.Image}} {{.Image}} {{.State.StartedAt}} {{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}} restarts={{.RestartCount}}'
}

memoria_container_states() {
  local c
  for c in $(docker ps --format '{{.Names}}' | grep '^memoria' | sort); do
    container_state "$c"
  done
}

# P1-11: a device serves one bound subject and voiceprint authority is off.
# 20260925-full-stack-v1 shipped with a stale MEMORIA_SPEAKER_AUTHORITY_ENABLED=true
# in /etc/memoria-agent.env and the first device greeting was dropped as
# target_non_owner. Check the value the candidate bridge actually sees (the
# LiveKit worker that also read it is retired), accepting only values that every
# reader treats as off.
assert_speaker_authority_disabled() {
  local svc
  for svc in voice-core-media-bridge; do
    new_compose run --rm --no-deps -T --entrypoint /app/.venv/bin/python "$svc" -c '
import os, sys
raw = os.environ.get("MEMORIA_SPEAKER_AUTHORITY_ENABLED", "")
if raw.strip().lower() not in {"", "0", "false", "no", "off", "f", "n"}:
    print("INVALID MEMORIA_SPEAKER_AUTHORITY_ENABLED must be false (P1-11); got a value that enables it")
    sys.exit(1)
print("speaker_authority_disabled=PASS")' || { log "speaker authority must be disabled for $svc"; exit 1; }
  done
}

HOST_LOG_DIR=/var/log/memoria

host_log_size() {
  local service="$1"
  stat -c %s "$HOST_LOG_DIR/$service.log" 2>/dev/null || echo 0
}

assert_host_log_has() {
  local service="$1" before="$2" marker="$3"
  local attempt size skip current older
  for attempt in 1 2 3 4 5; do
    size="$(host_log_size "$service")"
    older=""
    if [[ "$size" -lt "$before" ]]; then
      skip=0
      # The active file was replaced. Bytes past the snapshot now live in .1.
      older="$(tail -c +$((before + 1)) "$HOST_LOG_DIR/$service.log.1" 2>/dev/null || true)"
    else
      skip=$before
    fi
    current="$(tail -c +$((skip + 1)) "$HOST_LOG_DIR/$service.log" 2>/dev/null || true)"
    if printf '%s\n%s\n' "$older" "$current" | grep -Eq "^[^ ]+ ${service}/[0-9a-f]{12} .*${marker}"; then
      return 0
    fi
    [[ "$attempt" == 5 ]] || sleep 1
  done
  fail "no $service line with $marker reached $HOST_LOG_DIR/$service.log; install the host log sink (docs/runbooks/release-rollback.md, 容器日志落盘) and rerun env"
}

wait_healthy() {
  local status
  for _ in $(seq 1 60); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$1" 2>/dev/null || true)"
    [[ "$status" == healthy ]] && return 0
    [[ "$status" == no-healthcheck ]] && { log "NO HEALTHCHECK: $1 cannot be waited on"; return 1; }
    sleep 3
  done
  log "UNHEALTHY: $1"; docker logs --tail 40 "$1" 2>&1 | sed 's/^/  | /'; return 1
}

release_compose() {   # <release dir> <tag> <commit> [--extra-file <path>]... <compose args...>
  local dir=$1 tag=$2 commit=$3
  shift 3
  local files=(-f "$dir/docker-compose.production.yml")
  while [[ "${1:-}" == --extra-file ]]; do
    files+=(-f "$2")
    shift 2
  done
  (cd "$dir" && env MEMORIA_RELEASE_TAG="$tag" MEMORIA_RELEASE_COMMIT="$commit" \
    docker compose -p memoria --project-directory "$dir" "${files[@]}" \
    --profile media-runtime "$@")
}
new_compose() { release_compose "$R" "$TAG" "$COMMIT" "$@"; }
# <--base|service> <compose args...>, or the legacy <compose args...> form.
# --base adds no override; a service adds only its own. A legacy batch adds
# every recorded override, which is fine for queries but must not recreate
# containers: Compose stamps the whole -f chain on each container it starts, so
# the next freeze would see two overrides and reject the chain.
prev_compose() {
  local service=""
  case "${1:-}" in
    --base) shift ;;
    *)
      if [[ -n "${1:-}" && " ${PREV_SERVICES[*]:-} " == *" $1 "* ]]; then
        service=$1
        shift
      fi
      ;;
  esac
  verify_prev_overrides
  local files=() s
  if [[ -n "$service" ]]; then
    [[ -z "${PREV_OVERRIDE_PATH[$service]:-}" ]] \
      || files=(--extra-file "${PREV_OVERRIDE_PATH[$service]}")
  else
    for s in "${PREV_SERVICES[@]}"; do
      [[ -z "${PREV_OVERRIDE_PATH[$s]:-}" ]] \
        || files+=(--extra-file "${PREV_OVERRIDE_PATH[$s]}")
    done
  fi
  release_compose "$PREV_DIR" "$PREV_TAG" "$PREV_COMMIT" "${files[@]}" "$@"
}

# Refuses unless every recorded persisted override still holds the frozen bytes.
verify_prev_overrides() {
  local s path actual
  for s in "${PREV_SERVICES[@]}"; do
    path="${PREV_OVERRIDE_PATH[$s]:-}"
    [[ -n "$path" ]] || continue
    [[ -f "$path" ]] || fail "PREV override is missing: $path"
    actual="$(file_sha256 "$path")"
    [[ "$actual" == "${PREV_OVERRIDE_SHA[$s]}" ]] \
      || fail "PREV override changed: $path is $actual, recorded ${PREV_OVERRIDE_SHA[$s]}"
  done
}

verify_prev_image_ids() {
  local s c actual
  for s in "${PREV_SERVICES[@]}"; do
    [[ -n "${PREV_OVERRIDE_PATH[$s]:-}" ]] || continue
    c="$(container_of "$s")"
    actual="$(inspect_container "$c" image-id)" || fail "cannot read the image id of $c after rollback"
    [[ "$actual" == "${PREV_OVERRIDE_IMAGE[$s]}" ]] \
      || fail "$c runs image $actual, recorded ${PREV_OVERRIDE_IMAGE[$s]}"
  done
}

# Prints the PREV record for the live stack: the release `current` names, its
# commit, and the services it runs. Every ship row must run on PREV's compose
# file, optionally with the one whitelisted image-only override recorded for it.
# A retired row counts only if its container is on that file too.
derive_prev() {
  local dir tag compose s c chain base extra spec osvc opath osha image image_id
  local version revision commit="" services=() overrides=() overridden
  local -a revisions=()
  dir="$(readlink -f "$CURRENT")"
  [[ "$(dirname "$dir")" == "$(readlink -f "$RELEASES")" ]] || fail "current is not a release dir: $dir"
  tag="$(basename "$dir")"
  [[ "$tag" != "$TAG" ]] || fail "current already points at $TAG; PREV would be this release"
  # Compose labels the chain with the -f path as release_compose passes it.
  compose=$RELEASES/$tag/docker-compose.production.yml
  for s in "${SHIP[@]}" "${RETIRED[@]}"; do
    c="$(container_of "$s")"
    chain="$(inspect_container "$c" chain 2>/dev/null)" || chain=""
    overridden=""
    if [[ "$chain" == *,* ]]; then
      base=${chain%%,*}
      extra=${chain#*,}
      if [[ "$extra" == *,* || "$base" != "$compose" || "$extra" != /* ]]; then
        is_ship "$s" && fail "unexpected live chain for $c: ${chain:-absent}"
        continue
      fi
      if ! is_ship "$s"; then
        continue   # retired services are never rollback targets
      fi
      spec="$(override_spec "$extra")" || fail "override for $c is not whitelisted: $extra"
      IFS='|' read -r osvc opath osha _ <<<"$spec"
      [[ "$osvc" == "$s" ]] || fail "override $extra covers $osvc, not $c"
      [[ -f "$opath" ]] || fail "PREV override is missing: $opath"
      image="$(inspect_container "$c" image)" || fail "cannot read the running image of $c"
      validate_override_file "$opath" "$s" "$image" \
        || fail "override $opath is not the running image of $c"
      [[ "$(file_sha256 "$opath")" == "$osha" ]] || fail "PREV override changed: $opath"
      # The /tmp original is accepted only while it still equals the persisted copy.
      if [[ "$extra" != "$opath" ]]; then
        [[ -f "$extra" ]] || fail "PREV override is missing: $extra"
        [[ "$(file_sha256 "$extra")" == "$osha" ]] \
          || fail "PREV override $extra does not match the persisted copy $opath"
      fi
      image_id="$(inspect_container "$c" image-id)" || fail "cannot read the image id of $c"
      revision="$(inspect_container "$c" revision 2>/dev/null || true)"
      [[ -z "$revision" ]] || revisions+=("$revision")
      overridden=1
    elif [[ "$chain" != "$compose" ]]; then
      is_ship "$s" && fail "unexpected live chain for $c: ${chain:-absent}"
      continue
    fi
    if [[ -z "$overridden" ]]; then
      version="$(inspect_container "$c" version)" || fail "cannot read the image version of $c"
      revision="$(inspect_container "$c" revision)" || fail "cannot read the image revision of $c"
      # 20261016-readiness-b54189c reached production outside this script, built by
      # hand with no OCI version label on control-api or the bridge. Accept only
      # that shape: empty version, a 40-hex revision, a tag ending in the
      # revision's short sha, and the container's own MEMORIA_RELEASE_TAG equal
      # to the tag. Every other mismatch fails as before.
      if [[ "$version" != "$tag" && -z "$version" \
            && "$revision" =~ ^[0-9a-f]{40}$ \
            && "$tag" == *-"${revision:0:7}" \
            && "$(inspect_container "$c" release-tag)" == "$tag" ]]; then
        log "accepting empty version label for $c: revision ${revision:0:7} matches tag suffix" >&2
      else
        [[ "$version" == "$tag" ]] || fail "$c runs $version, not $tag"
      fi
      [[ "$revision" =~ ^[0-9a-f]{40}$ ]] || fail "$c image carries no commit label"
      [[ -z "$commit" || "$revision" == "$commit" ]] || fail "PREV images disagree on commit: $commit vs $revision"
      commit=$revision
    fi
    services+=("$s")
    [[ -z "$overridden" ]] || overrides+=("$s $opath $osha $image_id")
  done
  # A component image's OCI version/revision label names its component, not the
  # stack. When every ship image is overridden the stack commit comes from the
  # release tree the live base compose belongs to, else the one revision every
  # overridden image still agrees on.
  if [[ -z "$commit" ]]; then
    commit="$(prev_stack_commit "$dir" "${revisions[@]}")" \
      || fail "cannot derive the PREV commit: every ship image carries an override and $dir/.env has no MEMORIA_RELEASE_COMMIT"
  fi
  printf 'tag=%s\ncommit=%s\nservices=%s\n' "$tag" "$commit" "${services[*]}"
  [[ "${#overrides[@]}" -eq 0 ]] || printf 'overrides=%s\n' "${overrides[*]}"
}

# Writes $S/prev-release once. A re-run must derive the same record: after
# cutover the live stack is this release, and PREV must not follow it.
freeze_prev() {
  local record note
  record="$(derive_prev)" || exit 1
  if [[ -e "$S/prev-release" ]]; then
    if [[ "$(<"$S/prev-release")" != "$record" ]]; then
      diff "$S/prev-release" <(printf '%s\n' "$record") >&2 || true
      fail "live stack no longer matches $S/prev-release; refusing to rewrite it"
    fi
    note="prev already frozen (unchanged)"
  else
    printf '%s\n' "$record" > "$S/prev-release.tmp"
    mv -f "$S/prev-release.tmp" "$S/prev-release"
    note="prev frozen"
  fi
  load_prev
  log "prev=$PREV_TAG@$PREV_COMMIT services=${PREV_SERVICES[*]}"
  log "$note: $S/prev-release"
}

# Sets PREV_TAG, PREV_COMMIT, PREV_SERVICES and PREV_DIR from $S/prev-release,
# plus the per-service override maps when the record carries an overrides= line.
load_prev() {
  local key tag commit services s line osvc opath osha oid spec wsvc wpath wsha i
  local -a lines=() overrides=()
  [[ -f "$S/prev-release" ]] || fail "no $S/prev-release: run freeze first; the rollback target is unknown"
  mapfile -t lines < "$S/prev-release"
  [[ "${#lines[@]}" -eq 3 || "${#lines[@]}" -eq 4 ]] || fail "malformed $S/prev-release"
  IFS='=' read -r key tag <<<"${lines[0]}" && [[ "$key" == tag ]] || fail "malformed $S/prev-release"
  IFS='=' read -r key commit <<<"${lines[1]}" && [[ "$key" == commit ]] || fail "malformed $S/prev-release"
  IFS='=' read -r key services <<<"${lines[2]}" && [[ "$key" == services ]] || fail "malformed $S/prev-release"
  [[ "$tag" =~ ^[0-9]{8}-[a-z0-9.-]+$ && "$tag" != "$TAG" ]] || fail "malformed $S/prev-release: tag=$tag"
  [[ "$commit" =~ ^[0-9a-f]{40}$ ]] || fail "malformed $S/prev-release: commit=$commit"
  read -ra PREV_SERVICES <<<"$services"
  for s in "${PREV_SERVICES[@]}"; do
    [[ " ${SHIP[*]} ${RETIRED[*]} " == *" $s "* ]] || fail "malformed $S/prev-release: unknown service $s"
  done
  PREV_OVERRIDE_PATH=(); PREV_OVERRIDE_SHA=(); PREV_OVERRIDE_IMAGE=()
  if [[ "${#lines[@]}" -eq 4 ]]; then
    IFS='=' read -r key line <<<"${lines[3]}" && [[ "$key" == overrides ]] \
      || fail "malformed $S/prev-release"
    read -ra overrides <<<"$line"
    [[ "${#overrides[@]}" -ge 4 && $(( ${#overrides[@]} % 4 )) -eq 0 ]] \
      || fail "malformed $S/prev-release: overrides must be service/path/sha256/image-id groups"
    for ((i = 0; i < ${#overrides[@]}; i += 4)); do
      osvc=${overrides[i]} opath=${overrides[i + 1]} osha=${overrides[i + 2]} oid=${overrides[i + 3]}
      [[ -n "$osvc" && -n "$opath" && -n "$osha" && -n "$oid" ]] \
        || fail "malformed $S/prev-release: incomplete override entry for $osvc"
      [[ -z "${PREV_OVERRIDE_PATH[$osvc]:-}" ]] \
        || fail "malformed $S/prev-release: duplicate override for $osvc"
      [[ " ${PREV_SERVICES[*]} " == *" $osvc "* ]] \
        || fail "malformed $S/prev-release: override for unknown service $osvc"
      # Only a whitelisted persisted path may be named here: /tmp originals are
      # accepted from a live chain, never from the frozen record.
      spec="$(override_spec "$opath")" || fail "malformed $S/prev-release: no whitelist entry for $opath"
      IFS='|' read -r wsvc wpath wsha _ <<<"$spec"
      [[ "$wsvc" == "$osvc" && "$opath" == "$wpath" && "$osha" == "$wsha" ]] \
        || fail "malformed $S/prev-release: override for $osvc is not the whitelisted copy"
      PREV_OVERRIDE_PATH[$osvc]=$opath
      PREV_OVERRIDE_SHA[$osvc]=$osha
      PREV_OVERRIDE_IMAGE[$osvc]=$oid
    done
  fi
  PREV_TAG=$tag
  PREV_COMMIT=$commit
  PREV_DIR=$RELEASES/$tag
  [[ -f "$PREV_DIR/docker-compose.production.yml" ]] || fail "PREV release tree is missing: $PREV_DIR"
}

step_verify_load() {
  : "${VERIFIER_SHA:?}" "${MANIFEST_SHA:?}"
  printf '%s  %s\n' "$VERIFIER_SHA" "$U/release-verifier.pyz" | sha256sum -c -
  printf '%s  %s\n' "$MANIFEST_SHA" "$U/release-manifest.json" | sha256sum -c -
  python3 "$U/release-verifier.pyz" --manifest "$U/release-manifest.json" --artifact-dir "$U" \
    --expected-tag "$TAG" --expected-commit "$COMMIT"
  for r in "${ROLES[@]}"; do
    if docker image inspect "memoria-$r:$TAG" >/dev/null 2>&1; then log "image already present: memoria-$r:$TAG"; exit 1; fi
  done
  docker load -i "$U/images.tar"
  python3 "$U/release-verifier.pyz" --manifest "$U/release-manifest.json" --artifact-dir "$U" \
    --expected-tag "$TAG" --expected-commit "$COMMIT" --verify-imported-images
  [[ ! -e "$R" ]] || { log "release dir exists: $R"; exit 1; }
  install -d -o root -g root -m 0755 "$R"
  tar --extract --file "$U/source.tar" --directory "$R" --strip-components=1 --no-same-owner
  printf 'MEMORIA_RELEASE_COMMIT=%s\nMEMORIA_RELEASE_TAG=%s\nMEMORIA_CONTROL_API_PORT=%s\n' \
    "$COMMIT" "$TAG" "$(control_api_port)" > "$R/.env"
  chmod 0644 "$R/.env"
  bash "$R/scripts/smoke_server_deployment.sh" "$TAG"
  log "verify_load=PASS"
}

step_freeze() {
  [[ -d "$R" ]]
  install -d -m 0700 "$S"
  freeze_prev
  local s c img
  for s in "${PREV_SERVICES[@]}"; do
    c="$(container_of "$s")"
    img="$(inspect_container "$c" image)"
    docker tag "$(inspect_container "$c" image-id)" "${img%%:*}:rollback-$TAG-pre"
  done
  for c in $(docker ps --format '{{.Names}}' | grep '^memoria' | sort); do
    docker inspect "$c" --format '{{.Name}} {{.Config.Image}} {{.Image}} {{.State.StartedAt}} {{index .Config.Labels "com.docker.compose.project.config_files"}}'
    docker inspect "$c" --format '{{json .HostConfig.PortBindings}} {{json .Mounts}}' | sha256sum | sed "s|^|$c ports+mounts |"
    docker inspect "$c" --format '{{range .Config.Env}}{{println .}}{{end}}' | cut -d= -f1 | sort | sha256sum | sed "s|^|$c envkeys |"
  done > "$S/pre-state.txt"
  cp -p /etc/memoria-postgres.env /etc/memoria-control-api.env /etc/memoria-agent.env "$S/"
  docker exec memoria-data-postgres-1 pg_dump -U memoria_admin -d memoria -Fc > "$S/memoria-pre-$TAG.dump"
  # Control-store snapshot stays inside this release's .cutover (not /var/lib/memoria),
  # so freeze-dump retention can expire it with the pg_dump and never leave the releases tree.
  docker exec memoria-control-api-1 /app/.venv/bin/python -c "import sqlite3; s=sqlite3.connect('/data/memoria.sqlite3'); d=sqlite3.connect('/data/memoria.pre-$TAG.sqlite3'); s.backup(d); d.close()"
  docker cp "memoria-control-api-1:/data/memoria.pre-$TAG.sqlite3" "$S/memoria.pre-$TAG.sqlite3"
  docker exec memoria-control-api-1 rm -f "/data/memoria.pre-$TAG.sqlite3"
  ls -la "$S" | sed 's/^/  /'
  cleanup_freeze_dumps
  log "freeze=PASS"
}

# Expire freeze dumps under /opt/memoria/releases/*/.cutover only (3-day window).
cleanup_freeze_dumps() {
  local script root
  root="$(dirname "$RELEASES")"
  script="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/cleanup_freeze_dumps.py"
  [[ -f "$script" ]] || script="$R/scripts/cleanup_freeze_dumps.py"
  [[ -f "$script" ]] || fail "cleanup_freeze_dumps.py missing"
  python3 "$script" --root "$root" --retention-days 3 --apply
}

step_cleanup_freeze_dumps() {
  cleanup_freeze_dumps
  log "cleanup-freeze-dumps=PASS"
}

step_env() {
  [[ -f "$S/memoria-control-api.env" && -f "$S/memoria-postgres.env" ]] || { log "run freeze first"; exit 1; }
  local control_log_before bridge_log_before
  control_log_before="$(host_log_size control-api)"
  bridge_log_before="$(host_log_size voice-core-media-bridge)"
  if ! grep -q '^MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD=' /etc/memoria-postgres.env; then
    local pw; pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf 'MEMORIA_DB_MEMORY_MAINTENANCE_PASSWORD=%s\n' "$pw" >> /etc/memoria-postgres.env
    grep -q '^MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL=' /etc/memoria-control-api.env \
      || printf 'MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL=postgresql://memoria_memory_maintenance:%s@memoria-postgres:5432/memoria\n' "$pw" >> /etc/memoria-control-api.env
    unset pw
  fi
  # The control store role exists before its data moves; its DSN reaches the
  # control-api env only in the separate, authorized SQLite-to-PostgreSQL cutover.
  if ! grep -q '^MEMORIA_DB_CONTROL_PASSWORD=' /etc/memoria-postgres.env; then
    local control_pw; control_pw="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    printf 'MEMORIA_DB_CONTROL_PASSWORD=%s\n' "$control_pw" >> /etc/memoria-postgres.env
    unset control_pw
  fi
  # The DSN host must match the one the other maintenance DSNs use.
  local host_other host_new
  host_other="$(sed -n 's/^MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL=postgresql:\/\/[^@]*@\([^/]*\)\/.*/\1/p' /etc/memoria-control-api.env)"
  host_new="$(sed -n 's/^MEMORIA_MEMORY_MAINTENANCE_DATABASE_URL=postgresql:\/\/[^@]*@\([^/]*\)\/.*/\1/p' /etc/memoria-control-api.env)"
  [[ -n "$host_other" && "$host_other" == "$host_new" ]] || { log "maintenance DSN host mismatch: '$host_new' vs '$host_other'"; exit 1; }
  sed -i -E "s/^MEMORIA_RELEASE_COMMIT=.*/MEMORIA_RELEASE_COMMIT=$COMMIT/; s/^MEMORIA_RELEASE_TAG=.*/MEMORIA_RELEASE_TAG=$TAG/" /etc/memoria-control-api.env
  for f in postgres control-api agent; do
    echo "key diff $f:"; diff <(cut -d= -f1 "$S/memoria-$f.env" | sort) <(cut -d= -f1 "/etc/memoria-$f.env" | sort) | sed 's/^/  /' || true
  done
  new_compose run --rm --no-deps -T --entrypoint /app/.venv/bin/python control-api -c '
from pydantic import ValidationError
from services.control_api.app.config import ControlSettings
try:
    ControlSettings().validate_production()
except ValidationError as e:
    print("INVALID", [(".".join(map(str, x["loc"])), x["msg"]) for x in e.errors()]); raise SystemExit(1)
except ValueError as e:
    print("INVALID", e); raise SystemExit(1)
print("control_env_valid=PASS")'
  new_compose run --rm --no-deps -T --entrypoint /app/.venv/bin/python voice-core-media-bridge -m scripts.verify_env
  assert_speaker_authority_disabled
  assert_host_log_has control-api "$control_log_before" control_env_valid=PASS
  assert_host_log_has voice-core-media-bridge "$bridge_log_before" speaker_authority_disabled=PASS
  new_compose run --rm --no-deps -T -e MEMORIA_PROVIDER_SMOKE_REQUIRED=true \
    --entrypoint /app/.venv/bin/python voice-core-media-bridge -m scripts.provider_smoke_test
  log "env=PASS"
}

step_schema() {
  local BK="$DATA_TREE/.pre-$TAG-schema-backup"
  local files=(infra/postgres/init-memoria.sh services/identity/postgres_schema.sql services/consent/postgres_schema.sql
    services/policy/postgres_receipt_schema.sql services/device_fleet/postgres_schema.sql
    services/session_runtime/postgres_schema.sql services/evolution/postgres_schema.sql
    services/guardian/postgres_schema.sql services/memory_scope/postgres_schema.sql
    services/device_fleet/bootstrap_postgres_schema.sql)
  install -d -m 0700 "$BK"
  for f in "${files[@]}"; do
    if ! cmp -s "$DATA_TREE/$f" "$R/$f"; then
      [[ -e "$BK/$f" ]] || install -D -m 0644 "$DATA_TREE/$f" "$BK/$f"
      cp "$R/$f" "$DATA_TREE/$f"   # cp keeps the inode the running container bind-mounts
      log "schema file updated: $f"
    fi
  done
  (set -a; . /etc/memoria-postgres.env; set +a
    POSTGRES_CONTAINER=memoria-data-postgres-1 sh "$R/scripts/upgrade_authoritative_postgres.sh") \
    2>&1 | { grep -vE '^(psql:.*NOTICE|NOTICE|SET|CREATE|ALTER|GRANT|REVOKE|DO|COMMENT|DROP|INSERT|BEGIN|COMMIT|RESET)' || true; } | tail -5
  # The running container may predate the 011-control-schema.sql mount; the
  # file is idempotent, so apply it over stdin every release. A clean apply
  # prints only NOTICEs: grep then matches nothing and exits 1, which pipefail
  # would turn into a failed step (first seen on 20260928-review-batches-v1).
  docker exec -i memoria-data-postgres-1 psql -U memoria_admin -d memoria -v ON_ERROR_STOP=1 -q \
    < "$R/services/control_api/app/database/postgres_schema.sql" \
    2>&1 | { grep -vE '^(psql:.*NOTICE|NOTICE)' || true; } | tail -5
  # Versioned control migrations (database/migrations.py): each pending file
  # runs once, with its ledger row, in one transaction (psql -1).
  local mig v name
  for mig in "$R"/services/control_api/app/database/migrations/[0-9][0-9][0-9][0-9]_*.sql; do
    [[ -e "$mig" ]] || continue
    name="$(basename "$mig" .sql)"; v=$((10#${name:0:4}))
    if [[ "$(docker exec memoria-data-postgres-1 psql -U memoria_admin -d memoria -tAc \
        "SELECT count(*) FROM control_schema_migrations WHERE version >= $v")" != 0 ]]; then
      continue
    fi
    { cat "$mig"; printf "\nINSERT INTO control_schema_migrations (version, name, applied_at) VALUES (%d, '%s', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"+00:00\"'));\n" "$v" "$name"; } \
      | docker exec -i memoria-data-postgres-1 psql -U memoria_admin -d memoria -1 -v ON_ERROR_STOP=1 -q
    log "control migration applied: $name"
  done
  POSTGRES_CONTAINER=memoria-data-postgres-1 sh "$R/scripts/verify_authoritative_postgres.sh"
  log "schema=PASS"
}

step_cutover() {
  load_prev
  # The recreated Control API must publish the port the live one holds: a
  # candidate that falls back to 8791 on a shared host cannot bind, and by then
  # the old container is already gone.
  new_compose config --format json \
    | CONTROL_PORT="$(control_api_port)" SHIP_IMAGES="$(ship_images)" RETIRED="${RETIRED[*]}" python3 -c '
import json, sys, os
t = os.environ["TAG"]; s = json.load(sys.stdin)["services"]
want = dict(pair.split("=", 1) for pair in os.environ["SHIP_IMAGES"].split())
bad = [k for k, v in want.items() if s[k]["image"] != f"{v}:{t}"]
assert not bad, bad
published = sorted({str(p.get("published")) for p in s["control-api"].get("ports", [])})
assert published == [os.environ["CONTROL_PORT"]], (published, os.environ["CONTROL_PORT"])
retired = sorted(set(os.environ["RETIRED"].split()) & set(s))
assert not retired, f"retired services still defined: {retired}"
print("resolve=PASS")'
  local s
  for s in "${SHIP[@]}"; do
    log "$s"
    # Two statements: set -e ignores a failure on the left of &&.
    new_compose up -d --no-deps --no-build --force-recreate "$s"
    wait_healthy "$(container_of "$s")"
  done
  stop_orphans
  log "cutover=PASS"
}

# Stops (never removes) what PREV runs and this release no longer ships, once
# the ship rows are healthy; rollback recreates it from PREV's compose file.
stop_orphans() {
  local s c
  for s in "${PREV_SERVICES[@]}"; do
    is_ship "$s" && continue
    c="$(container_of "$s")"
    [[ "$(inspect_container "$c" running 2>/dev/null || true)" == true ]] || continue
    docker stop "$c" >/dev/null
    log "orphan stopped (rollback recreates it from PREV): $c"
  done
}

step_finish() {
  load_prev
  ln -sfn "$R" "$CURRENT.new" && mv -T "$CURRENT.new" "$CURRENT"
  log "current -> $(readlink -f "$CURRENT")"
  "$CURRENT/scripts/refresh_readiness.sh"
  systemctl reset-failed memoria-readiness-refresh.service || true
  systemctl start memoria-readiness-refresh.service
  systemctl show -p Result,ExecMainStatus memoria-readiness-refresh.service
  curl -fsS "http://127.0.0.1:$(control_api_port)/health/ready" | python3 -c 'import json,sys; b=json.load(sys.stdin); print("ready:", b["status"], b["release_tag"], b["checks"]["agent"]["status"])'
  curl -fsS -o /dev/null https://aginice.cn:8443/memoria-api/health/ready && echo external_ready=200
  memoria_container_states | tee "$S/post-state.txt"
  if grep -E ' (unhealthy|starting) restarts=' "$S/post-state.txt"; then
    log "containers not healthy after finish (see above)"; exit 1
  fi
  docker logs --since 10m memoria-media-edge-1 2>&1 | grep -iE 'voice.?core|grpc|connect' | tail -5 || true
  log "rollback target: $PREV_TAG@$PREV_COMMIT"
  log "finish=PASS"
}

# Rollback returns to PREV as a whole: the services and identity freeze wrote
# to $S/prev-release, PREV's compose file, its images (freeze tagged each
# rollback-$TAG-pre) and the env files freeze snapshotted.
step_rollback() {
  load_prev
  # Fail before the env files or the current symlink move: a rollback that cannot
  # load the frozen override bytes must leave the live stack untouched.
  verify_prev_overrides
  log "ROLLBACK to $PREV_TAG@$PREV_COMMIT: restoring env files"
  cp -p "$S/memoria-control-api.env" /etc/memoria-control-api.env
  cp -p "$S/memoria-agent.env" /etc/memoria-agent.env
  ln -sfn "$PREV_DIR" "$CURRENT.new" && mv -T "$CURRENT.new" "$CURRENT"
  local s
  if [[ "${#PREV_OVERRIDE_PATH[@]}" -eq 0 ]]; then
    prev_compose --base up -d --no-deps --no-build --force-recreate "${PREV_SERVICES[@]}"
  else
    # One compose invocation per service: Compose stamps the whole -f chain on
    # every container it starts, so a batch would leave two overrides on each.
    for s in "${PREV_SERVICES[@]}"; do
      prev_compose "$s" up -d --no-deps --no-build --force-recreate "$s"
    done
  fi
  for s in "${PREV_SERVICES[@]}"; do
    wait_healthy "$(container_of "$s")"
  done
  verify_prev_image_ids
  MEMORIA_RELEASE_TAG="$PREV_TAG" "$CURRENT/scripts/refresh_readiness.sh" || true
  memoria_container_states
  log "rollback done; compare image ids with $S/pre-state.txt"
}

# The Control API store moves from /data/memoria.sqlite3 into PostgreSQL once,
# after a release whose schema step installed the memoria_control role and
# tables. dry-run copies a snapshot of the live file into PostgreSQL, verifies
# every table and rolls back; apply stops control-api, backs up both stores,
# migrates, then points control-api at PostgreSQL. Any failure after the stop
# restores the env file and starts control-api on SQLite again.
control_store_dsn() {
  python3 - <<'PY'
import urllib.parse
values = {}
for path in ("/etc/memoria-postgres.env", "/etc/memoria-control-api.env"):
    for line in open(path, encoding="utf-8"):
        key, sep, value = line.rstrip("\n").partition("=")
        if sep:
            values[key] = value
password = values.get("MEMORIA_DB_CONTROL_PASSWORD", "")
other = values.get("MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL", "")
host = urllib.parse.urlsplit(other).netloc.rpartition("@")[2]
if not password or not host:
    raise SystemExit("missing MEMORIA_DB_CONTROL_PASSWORD or the PostgreSQL host")
print(f"postgresql://memoria_control:{urllib.parse.quote(password, safe='')}@{host}/memoria")
PY
}

step_control_store() {
  local mode="${CONTROL_STORE_MODE:-}" env=/etc/memoria-control-api.env
  [[ "$mode" == dry-run || "$mode" == apply ]] || { log "set CONTROL_STORE_MODE=dry-run or apply"; exit 1; }
  if grep -q '^MEMORIA_CONTROL_DATABASE_URL=.' "$env"; then log "control store is already on PostgreSQL"; exit 1; fi
  [[ "$(readlink -f /opt/memoria/current)" == "$R" ]] || { log "run on the live release ($R is not current)"; exit 1; }
  POSTGRES_CONTAINER=memoria-data-postgres-1 sh "$R/scripts/verify_authoritative_postgres.sh"
  local out; out="$R/.control-store-$(date -u +%Y%m%dT%H%M%SZ)"
  install -d -m 0700 "$out"
  MEMORIA_CONTROL_DATABASE_URL="$(control_store_dsn)"; export MEMORIA_CONTROL_DATABASE_URL
  if [[ "$mode" == dry-run ]]; then
    local snap=/data/memoria.control-store-dry-run.sqlite3
    docker exec memoria-control-api-1 /app/.venv/bin/python -c "import sqlite3; s=sqlite3.connect('file:/data/memoria.sqlite3?mode=ro', uri=True); d=sqlite3.connect('$snap'); s.backup(d); d.close()"
    docker exec -e MEMORIA_CONTROL_DATABASE_URL memoria-control-api-1 /app/.venv/bin/python \
      scripts/migrate_control_sqlite_to_postgres.py --sqlite "$snap" --dry-run > "$out/dry-run-receipt.json"
    docker exec memoria-control-api-1 rm -f "$snap"
    unset MEMORIA_CONTROL_DATABASE_URL
    log "receipt: $out/dry-run-receipt.json"
    log "control-store dry-run=PASS"
    return
  fi
  local data; data="$(docker inspect memoria-control-api-1 --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}')"
  [[ -n "$data" && -f "$data/memoria.sqlite3" ]] || { log "control SQLite file not found on the host"; exit 1; }
  cp -p "$env" "$out/memoria-control-api.env"
  restore_sqlite_control() {
    log "control-store FAILED: restoring the env file and starting control-api on SQLite"
    cp -p "$out/memoria-control-api.env" "$env"
    new_compose up -d --no-deps --no-build --force-recreate control-api || true
    wait_healthy memoria-control-api-1 || true
  }
  trap restore_sqlite_control ERR
  new_compose stop control-api
  local f
  for f in memoria.sqlite3 memoria.sqlite3-wal memoria.sqlite3-shm; do
    [[ -e "$data/$f" ]] && cp -p "$data/$f" "$out/$f"
  done
  docker exec memoria-data-postgres-1 pg_dump -U memoria_admin -d memoria -Fc > "$out/memoria-pre-control-store.dump"
  new_compose run --rm --no-deps -T -e MEMORIA_CONTROL_DATABASE_URL --entrypoint /app/.venv/bin/python \
    control-api scripts/migrate_control_sqlite_to_postgres.py --sqlite /data/memoria.sqlite3 --apply \
    > "$out/apply-receipt.json"
  printf 'MEMORIA_CONTROL_DATABASE_URL=%s\n' "$MEMORIA_CONTROL_DATABASE_URL" >> "$env"
  unset MEMORIA_CONTROL_DATABASE_URL
  new_compose up -d --no-deps --no-build --force-recreate control-api
  wait_healthy memoria-control-api-1
  trap - ERR
  log "receipt: $out/apply-receipt.json; the SQLite file stays untouched for rollback"
  log "control-store apply=PASS"
}

# Registers a component-switch override in host state so the next full-stack
# freeze honors its chain without a code PR. Overrides must already be on the
# host in production coordinates; every claim is re-proven here before the row
# is appended under a lock.
step_record_override() {
  local service=$1 arg_p=$2 arg_o=$3
  local release_root="$(dirname "$RELEASES")"
  local state_dir="$release_root/state"
  local state_file="$state_dir/component-overrides.tsv"
  local lock_file="$state_file.lock"

  [[ -n "$service" && -n "$arg_p" && -n "$arg_o" ]] \
    || fail "record-override requires <service> <persisted-path> <original-path>"
  # Arguments speak production coordinates (/opt/memoria/..., /tmp/...); they
  # resolve onto this host the same way override_spec resolves its rows.
  [[ "$arg_p" == "/opt/memoria/component-releases/"* ]] \
    || fail "persisted path must be under /opt/memoria/component-releases/ - $arg_p"
  [[ "$arg_o" == "/tmp/"* ]] \
    || fail "original path must be under /tmp/ - $arg_o"

  local persisted="${arg_p/#\/opt\/memoria/$release_root}"
  local tmp_root=/tmp
  [[ "$release_root" == /opt/memoria ]] || tmp_root="${RELEASE_OPS_TMP_ROOT:-/tmp}"
  local original="$arg_o"
  [[ "$release_root" == /opt/memoria ]] || original="${arg_o/#\/tmp/$tmp_root}"

  [[ -f "$persisted" && ! -L "$persisted" ]] \
    || fail "persisted file must be a regular file: $persisted"

  [[ -f "$original" ]] || fail "original file does not exist: $original"

  cmp -s "$persisted" "$original" \
    || fail "persisted and original files are not byte-identical"

  local container="$(container_of "$service")"
  local running_image
  running_image="$(inspect_container "$container" image)" \
    || fail "cannot read the running image of $container"

  validate_override_file "$persisted" "$service" "$running_image" \
    || fail "override $persisted is not image-only for $service"

  local sha
  sha="$(file_sha256 "$persisted")"

  local normalized_persisted="/opt/memoria/${persisted#$release_root/}"
  local row="$service $normalized_persisted $sha $arg_o"

  (
    flock -x 200

    install -d -o root -m 0700 "$state_dir"

    if [[ -f "$state_file" ]]; then
      local existing_sha
      existing_sha="$(awk -v svc="$service" -v path="$normalized_persisted" \
        '$1 == svc && $2 == path {print $3; exit}' "$state_file" || true)"

      if [[ -n "$existing_sha" ]]; then
        if [[ "$existing_sha" == "$sha" ]]; then
          log "override already recorded: $row"
          return 0
        else
          fail "refusing to change existing override sha256 for $service $normalized_persisted: recorded $existing_sha, file is $sha"
        fi
      fi
    fi

    local temp_file="$state_dir/.component-overrides.tsv.tmp.$$"
    if [[ -f "$state_file" ]]; then
      cat "$state_file" > "$temp_file"
    fi
    printf '%s\n' "$row" >> "$temp_file"
    chmod 0600 "$temp_file"
    mv -f "$temp_file" "$state_file"

    log "recorded override: $row"
  ) 200>"$lock_file"
}

# One-shot bootstrap: seeds host state with the rows the retired
# PREV_OVERRIDE_WHITELIST constant held (including the pr376 row a wrong merge
# dropped). A seeded file is left alone; a different one is a manual edit.
step_migrate_component_overrides() {
  local release_root="$(dirname "$RELEASES")"
  local state_dir="$release_root/state"
  local state_file="$state_dir/component-overrides.tsv"

  local expected='control-api /opt/memoria/component-releases/20261021-a94c438-pr345-pr346-pr348-pr352/override-control-pr352.yml 95a5e03319cafcf7fcf7325f00b2cc753ea444c563bfd5de5472692ce1d67486 /tmp/override-control-pr352.yml
voice-core-media-bridge /opt/memoria/component-releases/20261021-a94c438-pr345-pr346-pr348-pr352-pr353-pr355/override-pr355.yml 895b17fefa3401e75ba17e1c3f08aac9cab920f7b25e55aa540ffcc5a7a22da0 /tmp/override-pr355.yml
control-api /opt/memoria/component-releases/20261021-stepfun-c272af5/override-control-stepfun-c272af5.yml 53db50c436f582120ea3a292be1606f11fed46e5e329e30e73f4d55caba97204 /tmp/override-control-stepfun-c272af5.yml
voice-core-media-bridge /opt/memoria/component-releases/20261021-stepfun-c272af5/override-bridge-stepfun-c272af5.yml a1b51ef44d4f87160649ee2015de815089a27f15d27a88d678196ef507fa5c3c /tmp/override-bridge-stepfun-c272af5.yml
control-api /opt/memoria/component-releases/20261021-pr368-dd98c4a/override-control-pr368-dd98c4a.yml bde575b693d379e47af7bbfbcbbcd857487532b78faef2d2957b4facaf1217c0 /tmp/override-control-pr368-dd98c4a.yml
voice-core-media-bridge /opt/memoria/component-releases/20261021-main-f14be64/override-bridge-20261021-main-f14be64.yml 4cfa1733236927709435c3f068276835a009de8e3603ba2bba08571cc96610cf /tmp/override-bridge-20261021-main-f14be64.yml
voice-core-media-bridge /opt/memoria/component-releases/20261021-pr376-8dda764/override-bridge-20261021-pr376-8dda764.yml f7a3173d46ec1feee656d8bb6abffbdf509c6b1301c930c50614b5c8c9cf60a1 /tmp/override-bridge-20261021-pr376-8dda764.yml'

  if [[ -f "$state_file" ]]; then
    local actual
    actual="$(cat "$state_file")"
    if [[ "$actual" == "$expected" ]]; then
      log "state file already migrated (unchanged)"
      return 0
    else
      fail "state file exists with different content; refusing to overwrite: $state_file"
    fi
  fi

  install -d -o root -m 0700 "$state_dir"
  printf '%s\n' "$expected" > "$state_file"
  chmod 0600 "$state_file"
  log "migrated component overrides to $state_file"
}

main() {
  case "${1:-}" in
    verify-load) step_verify_load ;;
    freeze) step_freeze ;;
    env) step_env ;;
    schema) step_schema ;;
    cutover) step_cutover ;;
    finish) step_finish ;;
    rollback) step_rollback ;;
    cleanup-freeze-dumps) step_cleanup_freeze_dumps ;;
    control-store) step_control_store ;;
    record-override)
      shift
      step_record_override "$@"
      ;;
    migrate-component-overrides) step_migrate_component_overrides ;;
    *) echo "usage: $0 verify-load|freeze|env|schema|cutover|finish|rollback|cleanup-freeze-dumps|control-store|record-override|migrate-component-overrides" >&2; exit 2 ;;
  esac
}
# Sourcing only defines (the tests run single steps against a fake host).
[[ "${BASH_SOURCE[0]}" != "$0" ]] || main "$@"
