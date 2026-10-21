#!/usr/bin/env bash
# Re-sync vendored pstack skills from https://github.com/cursor/plugins.
# Preserves non-pstack entries under .cursor/skills/
# (e.g. verify-memoria). Updates .cursor/skills/PSTACK_VERSION.
# Writes only under .cursor/skills/. Upstream agents, README, and docs are skipped.
#
# Dry-run guards: `sync-pstack.sh --check-name NAME` validates NAME and the
# resolved destination, then exits without cloning or writing.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_DST="${ROOT}/.cursor/skills"
VERSION_FILE="${SKILLS_DST}/PSTACK_VERSION"
UPSTREAM_REPO="${PSTACK_UPSTREAM_REPO:-https://github.com/cursor/plugins.git}"
UPSTREAM_REF="${PSTACK_UPSTREAM_REF:-main}"
OVERLAY_ANCHOR='Remaining triggers:'
CHILD_AUDIO_RULE='- Child-facing stories, lines, or audio → never synthesize or add from draft scripts on your own. Such PRs need 产品部 and 合规部 sign-off before merge. Fill `.github/pull_request_template.md` section 「儿童可听内容（故事/台词/音频）」. See also `AGENTS.md`.'

# Skill folder names must be a single path segment under .cursor/skills/.
assert_skill_name() {
  local name="$1"
  if [[ -z "${name}" || "${name}" == "." || "${name}" == ".." || "${name}" == "*" || "${name}" == */* ]]; then
    printf "error: refuse skill name: %q\n" "${name}" >&2
    exit 1
  fi
}

# `realpath -m` is GNU-only; os.path.realpath resolves symlinks in existing
# components and normalizes dot segments without requiring the path to exist.
resolve_path() {
  python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1"
}

# Destination must resolve to exactly one directory entry under SKILLS_DST.
# Never the skills root itself (blocks empty / '.' names that would rm -rf it).
assert_skill_dest() {
  local path="$1"
  local skills_real path_real parent_real
  skills_real="$(resolve_path "${SKILLS_DST}")"
  path_real="$(resolve_path "${path}")"
  parent_real="$(dirname "${path_real}")"
  if [[ "${path_real}" == "${skills_real}" ]]; then
    echo "error: refuse skills root as target: ${path}" >&2
    exit 1
  fi
  if [[ "${parent_real}" != "${skills_real}" ]]; then
    echo "error: refuse path not exactly one level under ${SKILLS_DST}: ${path}" >&2
    exit 1
  fi
}

require_child_audio_rule() {
  local file="$1"
  local anchor="$2"
  local text="$3"
  local rel line prev headings hits
  rel="${file#"${WORK}/plugins/"}"
  if [[ ! -f "${file}" ]]; then
    echo "error: refuse sync: overlay target missing upstream: ${rel}" >&2
    exit 1
  fi
  prev=""
  headings=0
  hits=0
  while IFS= read -r line || [[ -n "${line}" ]]; do
    if [[ "${line}" == "${text}" ]]; then
      continue
    fi
    if [[ "${line}" == "${anchor}" ]]; then
      headings=$((headings + 1))
    fi
    printf '%s\n' "${line}"
    if [[ "${prev}" == "${anchor}" && -z "${line}" ]]; then
      hits=$((hits + 1))
      printf '%s\n' "${text}"
    fi
    prev="${line}"
  done < "${file}" > "${file}.overlay"
  if [[ "${headings}" -ne 1 ]]; then
    rm -f "${file}.overlay"
    echo "error: refuse sync: need exactly one '${anchor}' line in ${rel}, found ${headings}" >&2
    exit 1
  fi
  if [[ "${hits}" -ne 1 ]]; then
    rm -f "${file}.overlay"
    echo "error: refuse sync: need exactly one '${anchor}' line followed by a blank line in ${rel}, found ${hits}" >&2
    exit 1
  fi
  mv "${file}.overlay" "${file}"
}

if [[ "${1:-}" == "--check-name" ]]; then
  name="${2-}"
  assert_skill_name "${name}"
  assert_skill_dest "${SKILLS_DST}/${name}"
  echo "ok ${name}"
  exit 0
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/sync-pstack.XXXXXX")"
cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT

echo "Cloning ${UPSTREAM_REPO} @ ${UPSTREAM_REF} ..."
git clone --depth 1 --branch "${UPSTREAM_REF}" "${UPSTREAM_REPO}" "${WORK}/plugins"
SHA="$(git -C "${WORK}/plugins" rev-parse HEAD)"
DATE="$(git -C "${WORK}/plugins" show -s --format=%cI HEAD)"

SRC_SKILLS="${WORK}/plugins/pstack/skills"

if [[ ! -d "${SRC_SKILLS}" ]]; then
  echo "error: upstream missing pstack/skills" >&2
  exit 1
fi

assert_skill_dest "${VERSION_FILE}"
mkdir -p "${SKILLS_DST}"

# Previously managed skill dirs (so removals upstream are reflected locally).
PREV_SKILLS=()
if [[ -f "${VERSION_FILE}" ]]; then
  while IFS= read -r line; do
    case "${line}" in
      skills:*)
        set -f
        # Intentionally unquoted after skills:: split on IFS only; set -f blocks globs.
        read -r -a PREV_SKILLS <<< "${line#skills:}"
        set +f
        ;;
      agents:*|extras:*)
        echo "Ignoring legacy manifest line (sync is skills-only): ${line}"
        ;;
    esac
  done < "${VERSION_FILE}"
fi

for name in "${PREV_SKILLS[@]+"${PREV_SKILLS[@]}"}"; do
  assert_skill_name "${name}"
done

NEW_SKILLS=()
while IFS= read -r -d '' dir; do
  name="$(basename "${dir}")"
  assert_skill_name "${name}"
  NEW_SKILLS+=("${name}")
done < <(find "${SRC_SKILLS}" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

require_child_audio_rule "${SRC_SKILLS}/poteto-mode/SKILL.md" "${OVERLAY_ANCHOR}" "${CHILD_AUDIO_RULE}"

# Remove managed skill dirs that disappeared upstream (never touch unmanaged locals).
if ((${#PREV_SKILLS[@]})); then
  for name in "${PREV_SKILLS[@]}"; do
    keep=0
    for n in "${NEW_SKILLS[@]}"; do
      if [[ "${n}" == "${name}" ]]; then keep=1; break; fi
    done
    dest="${SKILLS_DST}/${name}"
    assert_skill_name "${name}"
    assert_skill_dest "${dest}"
    if [[ "${keep}" -eq 0 && -d "${dest}" ]]; then
      echo "Removing obsolete pstack skill: ${name}"
      rm -rf "${dest}"
    fi
  done
fi

echo "Syncing ${#NEW_SKILLS[@]} skill folders into ${SKILLS_DST} ..."
for name in "${NEW_SKILLS[@]}"; do
  dest="${SKILLS_DST}/${name}"
  assert_skill_name "${name}"
  assert_skill_dest "${dest}"
  rm -rf "${dest}"
  cp -a "${SRC_SKILLS}/${name}" "${dest}"
done

# Upstream pstack/agents, README, and docs are intentionally not synced.

{
  echo "repo: ${UPSTREAM_REPO}"
  echo "ref: ${UPSTREAM_REF}"
  echo "commit: ${SHA}"
  echo "date: ${DATE}"
  echo "skills: ${NEW_SKILLS[*]}"
} > "${VERSION_FILE}"

echo "Wrote ${VERSION_FILE}"
echo "pstack @ ${SHA} (${DATE})"
echo "Done."
