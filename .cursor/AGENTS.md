# .cursor — pstack agent-tooling plugin (vendored)

**Score 15 — distinct domain: agent skills/tooling for this repo, not application code.**

## OVERVIEW
Vendored Cursor "pstack" plugin (`PSTACK_VERSION` pins upstream commit `6b78e9f4`, 2026-10-19): 58 skills, poteto-mode playbooks, Bun/TS tooling, plus repo-specific verification skills (verify-memoria, verify-media-edge-late-receipts).

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Plugin dispatcher | `skills/poteto-mode/SKILL.md` | 22 playbooks; must-read-first for poteto-agent |
| Plan linter | `skills/poteto-mode/scripts/check-plan.mjs` | `node ... <plan.md>`; fixed markers, exit 2 on usage |
| TS tooling | `skills/poteto-mode/scripts/orch/`, `watch-pr/` | `store.ts` 1677 L, `policy.ts` 836 L; bun test suite |
| Routing table | `skills/poteto-help/SKILL.md` | links ~45 skills; update when renaming |
| Control API verification | `skills/verify-memoria/` | `bin/launch [RUN_ID]` → `bin/doctor` → drive with `bin/api` → `bin/cleanup` |
| Media edge formal check | `skills/verify-media-edge-late-receipts/bin/verify` | TLC over `services/media_edge/formal`; `--check`/`--mutants`/`--finding` |
| Principle leaves | `skills/principle-*/` | 25 leaves cited by poteto-mode, architect, figure-it-out |
| User guide | `docs/guide/` | pages 01–10 |
| Subagents | `agents/poteto-agent.md`, `agents/comment-sicko.md` | poteto-agent reads the parent skill first |

## CONVENTIONS
- Skill format: `<kebab-name>/SKILL.md` with YAML frontmatter `name` + `description`; `disable-model-invocation: true` on 50/53 (exceptions: verify-memoria, verify-media-edge-late-receipts, setup-pstack).
- Bun-first scripts, ESM, explicit `.ts` import extensions, `strict`; branded primitives (`PrNumber`) parsed once at boundary; `NonEmpty<T>` for non-empty invariants.
- Script tooling self-bootstraps: sha256 of `package.json`+`bun.lock` in `node_modules/.poteto-mode-tools-install-key`; drift → `bun install --frozen-lockfile` + re-exec.
- verify-memoria: real uvicorn + real HTTP only (no ASGI transport), Postgres DB `memoria_verify`, `OFFLINE_MOCK=true` at provider boundaries; `features/*.md` have exactly 4 H2s; evidence kept, `runs/` deleted on cleanup.
- verify-media-edge: hand-derived TLA+ model (never a code extractor); after model edits update `EXPECTED_BASE_DISTINCT` + the coverage record.
- Model-role config lives outside the repo in `~/.cursor/rules/pstack-models.mdc` (setup-pstack writes it).
- `unslop` rule numbers are stable ids cited by other skills; a removed rule leaves a gap.

## ANTI-PATTERNS
- Never drive a Control API not started by `bin/launch`; port `8000` and developer `.env` data dirs are off-limits; `doctor` exit non-zero ⇒ do not drive.
- Never weaken an invariant or model to make the TLA+ check pass; report counterexamples in code terms.
- Never spawn a `poteto-agent` to run the playbook itself; never override routed skills' (`how`, `why`, `interrogate`, `reflect`, `swarm`) `subagent_type`.
- Child-audible stories/lines/audio: never synthesize on your own — the PR must complete the 「儿童可听内容」 template section with 产品部/合规部 sign-off.
- Never fabricate links/citations/transcripts; never hand the human a check you could run; no em-dash and no mid-sentence colon in replies (unslop).
- Never resume an agent to check on it (probe read-only); never resume-chain a brief — respawn with consolidated scope.
- comment-sicko is report-only and never writes application code; no-comments never bolts on symptom guards.
