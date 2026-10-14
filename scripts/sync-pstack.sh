#!/usr/bin/env bash
# Re-sync vendored pstack skills from https://github.com/cursor/plugins.
# Does not modify skill contents. Preserves non-pstack entries under .cursor/skills/
# (e.g. verify-memoria). Updates .cursor/skills/PSTACK_VERSION.
# Writes only under .cursor/skills/. Upstream agents, README, and docs are skipped.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_DST="${ROOT}/.cursor/skills"
VERSION_FILE="${SKILLS_DST}/PSTACK_VERSION"
UPSTREAM_REPO="${PSTACK_UPSTREAM_REPO:-https://github.com/cursor/plugins.git}"
UPSTREAM_REF="${PSTACK_UPSTREAM_REF:-main}"

assert_under_skills() {
  local path="$1"
  local skills_real path_real
  skills_real="$(realpath -m "${SKILLS_DST}")"
  path_real="$(realpath -m "${path}")"
  case "${path_real}" in
    "${skills_real}"|"${skills_real}"/*) ;;
    *)
      echo "error: refuse path outside ${SKILLS_DST}: ${path}" >&2
      exit 1
      ;;
  esac
}

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

assert_under_skills "${SKILLS_DST}"
assert_under_skills "${VERSION_FILE}"
mkdir -p "${SKILLS_DST}"

# Previously managed skill dirs (so removals upstream are reflected locally).
PREV_SKILLS=()
if [[ -f "${VERSION_FILE}" ]]; then
  while IFS= read -r line; do
    case "${line}" in
      skills:*)
        # shellcheck disable=SC2206
        PREV_SKILLS=(${line#skills:})
        ;;
      agents:*|extras:*)
        echo "Ignoring legacy manifest line (sync is skills-only): ${line}"
        ;;
    esac
  done < "${VERSION_FILE}"
fi

NEW_SKILLS=()
while IFS= read -r -d '' dir; do
  name="$(basename "${dir}")"
  NEW_SKILLS+=("${name}")
done < <(find "${SRC_SKILLS}" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

# Remove managed skill dirs that disappeared upstream (never touch unmanaged locals).
if ((${#PREV_SKILLS[@]})); then
  for name in "${PREV_SKILLS[@]}"; do
    keep=0
    for n in "${NEW_SKILLS[@]}"; do
      if [[ "${n}" == "${name}" ]]; then keep=1; break; fi
    done
    dest="${SKILLS_DST}/${name}"
    assert_under_skills "${dest}"
    if [[ "${keep}" -eq 0 && -d "${dest}" ]]; then
      echo "Removing obsolete pstack skill: ${name}"
      rm -rf "${dest}"
    fi
  done
fi

echo "Syncing ${#NEW_SKILLS[@]} skill folders into ${SKILLS_DST} ..."
for name in "${NEW_SKILLS[@]}"; do
  dest="${SKILLS_DST}/${name}"
  assert_under_skills "${dest}"
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
