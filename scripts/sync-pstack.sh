#!/usr/bin/env bash
# Re-sync vendored pstack skills/agents from https://github.com/cursor/plugins.
# Does not modify skill contents. Preserves non-pstack entries under .cursor/skills/
# (e.g. verify-memoria). Updates .cursor/skills/PSTACK_VERSION.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILLS_DST="${ROOT}/.cursor/skills"
AGENTS_DST="${ROOT}/.cursor/agents"
VERSION_FILE="${SKILLS_DST}/PSTACK_VERSION"
UPSTREAM_REPO="${PSTACK_UPSTREAM_REPO:-https://github.com/cursor/plugins.git}"
UPSTREAM_REF="${PSTACK_UPSTREAM_REF:-main}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/sync-pstack.XXXXXX")"
cleanup() { rm -rf "${WORK}"; }
trap cleanup EXIT

echo "Cloning ${UPSTREAM_REPO} @ ${UPSTREAM_REF} ..."
git clone --depth 1 --branch "${UPSTREAM_REF}" "${UPSTREAM_REPO}" "${WORK}/plugins"
SHA="$(git -C "${WORK}/plugins" rev-parse HEAD)"
DATE="$(git -C "${WORK}/plugins" show -s --format=%cI HEAD)"

SRC_SKILLS="${WORK}/plugins/pstack/skills"
SRC_AGENTS="${WORK}/plugins/pstack/agents"
SRC_DOCS="${WORK}/plugins/pstack/docs"
SRC_README="${WORK}/plugins/pstack/README.md"

if [[ ! -d "${SRC_SKILLS}" ]]; then
  echo "error: upstream missing pstack/skills" >&2
  exit 1
fi

mkdir -p "${SKILLS_DST}" "${AGENTS_DST}"

# Previously managed skill dirs (so removals upstream are reflected locally).
PREV_SKILLS=()
if [[ -f "${VERSION_FILE}" ]]; then
  while IFS= read -r line; do
    case "${line}" in
      skills:*)
        # shellcheck disable=SC2206
        PREV_SKILLS=(${line#skills:})
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
    if [[ "${keep}" -eq 0 && -d "${SKILLS_DST}/${name}" ]]; then
      echo "Removing obsolete pstack skill: ${name}"
      rm -rf "${SKILLS_DST}/${name}"
    fi
  done
fi

echo "Syncing ${#NEW_SKILLS[@]} skill folders into ${SKILLS_DST} ..."
for name in "${NEW_SKILLS[@]}"; do
  rm -rf "${SKILLS_DST}/${name}"
  cp -a "${SRC_SKILLS}/${name}" "${SKILLS_DST}/${name}"
done

NEW_AGENTS=()
if [[ -d "${SRC_AGENTS}" ]]; then
  while IFS= read -r -d '' f; do
    NEW_AGENTS+=("$(basename "${f}")")
  done < <(find "${SRC_AGENTS}" -mindepth 1 -maxdepth 1 -type f -name '*.md' -print0 | sort -z)

  PREV_AGENTS=()
  if [[ -f "${VERSION_FILE}" ]]; then
    while IFS= read -r line; do
      case "${line}" in
        agents:*)
          # shellcheck disable=SC2206
          PREV_AGENTS=(${line#agents:})
          ;;
      esac
    done < "${VERSION_FILE}"
  fi

  for name in "${PREV_AGENTS[@]+"${PREV_AGENTS[@]}"}"; do
    keep=0
    for n in "${NEW_AGENTS[@]+"${NEW_AGENTS[@]}"}"; do
      if [[ "${n}" == "${name}" ]]; then keep=1; break; fi
    done
    if [[ "${keep}" -eq 0 && -f "${AGENTS_DST}/${name}" ]]; then
      echo "Removing obsolete pstack agent: ${name}"
      rm -f "${AGENTS_DST}/${name}"
    fi
  done

  echo "Syncing ${#NEW_AGENTS[@]} agent file(s) into ${AGENTS_DST} ..."
  for name in "${NEW_AGENTS[@]}"; do
    cp -a "${SRC_AGENTS}/${name}" "${AGENTS_DST}/${name}"
  done
fi

# poteto-help links to ../../README.md and ../../docs/guide/* from skills/.
# Place those at .cursor/ so the relative paths resolve without editing skills.
if [[ -f "${SRC_README}" ]]; then
  cp -a "${SRC_README}" "${ROOT}/.cursor/README.md"
fi
if [[ -d "${SRC_DOCS}" ]]; then
  rm -rf "${ROOT}/.cursor/docs"
  cp -a "${SRC_DOCS}" "${ROOT}/.cursor/docs"
fi

{
  echo "repo: ${UPSTREAM_REPO}"
  echo "ref: ${UPSTREAM_REF}"
  echo "commit: ${SHA}"
  echo "date: ${DATE}"
  echo "skills: ${NEW_SKILLS[*]}"
  echo "agents: ${NEW_AGENTS[*]:-}"
  echo "extras: .cursor/README.md .cursor/docs (for poteto-help ../../ relative links)"
} > "${VERSION_FILE}"

echo "Wrote ${VERSION_FILE}"
echo "pstack @ ${SHA} (${DATE})"
echo "Done."
