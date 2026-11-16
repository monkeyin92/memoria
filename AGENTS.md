# PROJECT KNOWLEDGE BASE

**Generated:** 2026-11-16
**Commit:** 4079c081
**Branch:** main

## OVERVIEW
Memoria is a family AI memory system with a real-time full-duplex voice agent core: devices and guardians interact over voice, shared family memory is captured under consent, verified, projected into searchable archives, and reused in later conversations. Python 3.12 services (uv), one Go media-edge device service, an ESP32-S3 firmware overlay, and a WeChat Mini Program client.

## STRUCTURE
```
memoria/
├── services/          # Python backend services; agent + control_api + domain contexts
│   ├── agent/         # Real-time voice agent (DuplexRuntime, reply pipeline, providers)
│   ├── control_api/   # Sole HTTP entry point; wires all ports into one app graph
│   ├── media_edge/    # Go WSS/Opus edge bridging devices to Voice Core gRPC
│   ├── memory_scope/  # Family shared-memory authority (proposal/vote/promotion)
│   └── archive/       # Evidence ledger + pgvector memory/skill projections
├── packages/          # Contract authority: multi-subject schema -> generated Python/JS + buf proto
├── scripts/           # Ops/release/acceptance CLIs (CI, Makefile, deploy gates)
├── firmware/esp32/    # xiaozhi-esp32 upstream lock + patch overlay + board logic
├── apps/miniprogram/  # WeChat Mini Program client (only client app; no realtime voice)
├── infra/             # Production deployment: images, Postgres role bootstrap, nginx/backup
├── devicetest/        # Physical-device soak/gate/score tooling (USB serial, not CI)
├── tests/             # Repo-wide contract/wording/budget gates + PG parity harness
├── docs/              # Aggregated acceptance/runbook docs (see docs/AGENTS.md)
├── evaluation/        # Graded eval artifacts per run (no code)
├── share/             # Untracked team memory bank; self-documented via share/README.md
└── outputs/           # Evidence tree; only acceptance receipts are git-allowed
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Real-time voice agent | `services/agent/` | Entry: `scripts/run_media_bridge.py`; see services/agent/AGENTS.md |
| HTTP API + service wiring | `services/control_api/` | Entry: `services.control_api.app.main:app` |
| Device media streaming | `services/media_edge/` | Go module, CI-gated only on its own changes |
| Shared family memory | `services/memory_scope/` | Fence-verified capture, family vote/promotion, child layers |
| Evidence ledger + memory search | `services/archive/` | pgvector projections; search never sees unapproved evidence |
| Device onboarding / QR bootstrap | `services/device_fleet/` | Sync-over-asyncpg, RLS roles |
| Identity / guardian / sessions | `services/identity/`, `services/guardian/`, `services/session_runtime/` | Per-dir AGENTS.md |
| Consent + policy decisions | `services/consent/`, `services/policy/` | Engine decides, receipts record |
| Voice identity | `services/speaker/`, `services/voice_profile/`, `services/speaker_model/` | Clone -> classify -> embed |
| Self-evolution / persona | `services/evolution/`, `services/self_model/`, `services/persona/` | Offline auditable contracts |
| Contracts + regeneration | `packages/` | Single-source chain; never hand-edit generated code |
| Ops / release / deploy | `scripts/` | Hard-coded allowlists; deploy gates in CI |
| Device firmware | `firmware/esp32/` | Locked upstream + overlay; identity partition is never erased |
| Client app | `apps/miniprogram/` | Offline-first tests; copy gates |
| Repo-wide gates | `tests/` | Budget/contract/parity suites; root `conftest.py` owns PG fixtures |
| Deployment surface | `infra/` | Postgres role bootstrap + drift tests |

## CODE MAP
| Symbol | Type | Location | Refs | Role |
|--------|------|----------|------|------|
| `GenerationFence` | class | `services/agent/src/contracts/ids.py` | ~160 importers | Repo-wide staleness fence; flows through every agent layer |
| `MemoryScopeService` | class | `services/memory_scope/service.py` | - | Memory-authority facade |
| `MemoryStore` | Protocol | `services/memory_scope/repository.py` | - | Storage seam for memory scope |
| `PostgresMemoryStore` | class | `services/memory_scope/postgres_store.py` | - | asyncpg RLS adapter |
| `MemoryScopeResolver` | class | `services/memory_scope/resolver.py` | - | Pure decision logic |
| `PostgresLifeArchive` | class | `services/archive/` | - | Evidence/consent ledger + outbox |
| `PostgresMemoryCatalog` | class | `services/archive/` | - | Mixin-composed pgvector projections |
| `PostgresSkillCatalog` | class | `services/archive/` | - | Procedural memory |
| `MemoryStore` (control) | class | `services/control_api/app/database/` | - | Dual-dialect SQLite/PG, FORCE RLS |
| `build_production_media_session_factory` | function | `services/agent/src/media_agent_factory.py` | - | Production voice-session wiring |
| `DuplexRuntime` | class | `services/agent/` | - | Runtime assembled by `run_media_bridge.py` |
| `MetricsRegistry` | class | `services/agent/src/observability/` | 28 | Singleton metrics hub |
| `TransactionBoundConsentAuthorizer` | class | `services/policy/production_wiring.py` | - | PolicyEngine input seam |

## CONVENTIONS
- Python 3.12 + uv; root Makefile is the gate. Go 1.26 (media_edge), Node 20-24 (miniprogram), hatchling `packages = ["services"]`.
- Postgres security: FORCE RLS everywhere; app roles NOSUPERUSER/NOBYPASSRLS, never table owners or DDL runners; writes via SECURITY DEFINER ports; per-account context installed via `set_config` GUCs.
- Module budgets: `[tool.memoria.module-budgets]` pins line counts (35 entries); measured must equal budget, updates downward-only; growth = split into mixins. New >1500-line module registered in the same commit.
- Test naming: behavior-sentence names with round tags and traceability ids (`P0-04`, `#283`, `A06`); no xfail.
- PG tests gated on `MEMORIA_TEST_POSTGRES_DSN` (default `127.0.0.1:55439`, user `memoria_test`); skip is not a pass.
- Error contract: `HTTPException(status_code=4xx, detail={"code": "<snake_case>"})`, exact-literal codes asserted repo-wide.
- Contracts: single-source chain `packages/contracts/schemas/...json -> scripts/generate_multi_subject_contracts.py -> generated/` (+ buf for media proto); CI checks generated output never diverges.
- CI: `pull_request` + `workflow_dispatch` only (never push-to-main); `python` job is the required all-shards gate; coverage 85 global / 90 orchestration / 90 provider-protocols; uv ops `--frozen`.
- Privacy: no child audio or raw transcripts persisted or logged; no subject/person ids as metric labels or log keys.

## ANTI-PATTERNS (THIS PROJECT)
- NEVER bypass RLS or run DDL as an app role; no legacy fallback when Postgres is down (fail closed with 503).
- NEVER persist child audio/transcripts; memory search must never expose unapproved evidence; guardians see summaries, never verbatim child chat; minor subjects never reach the extractor.
- NEVER reword fixed copy: safety scripts, `REDACTED_DISPLAY_NAME`, guardian retention/parent-alert regexes are frozen.
- NEVER hand-edit generated contracts (Python/JS/md) or aggregate docs (`scripts/consolidate_docs.py` refuses re-merge).
- NEVER release bench or keep-awake firmware images; never erase the `memoria_identity` partition.
- NEVER mark a PG test skipped as passing; never xfail; never weaken a failing gate test.

## UNIQUE STYLES
- Facade + mixin seam: `TYPE_CHECKING`-declared shared state, store handle owned by facade; the repo's answer to module budgets, no import cycles.
- Frozen contract suites: ticket-suffixed names (a02/a05/b01) with hard-fail rules; golden traces are byte-exact.
- Chinese user-facing copy lives inline as ASCII-escaped strings in Python/JS source; firmware offline assets flow manifest -> id-ordered headers, any hand-edit breaks the gate.
- Doc budget: exactly three long-lived root docs (README/HANDOFF/TODOLIST); durable conclusions live under `docs/acceptance/run-*/findings.md`.

## COMMANDS
```bash
make test                      # ruff + module-budget + mypy services --strict + pytest (coverage 85)
make module-budget             # verify line budgets (downward-only updates)
make dev-api                   # control_api dev server
uv run pytest services/<name>  # per-service suites; add MEMORIA_TEST_POSTGRES_DSN for PG
cd firmware/esp32 && ./scripts/bootstrap.sh && ./scripts/build.sh --clean && ./scripts/check-overlay.sh
cd apps/miniprogram && npm test   # plus check:forbidden-copy, check:release-gate
# media_edge: Go CI job (gofmt/vet/test -race/golangci-lint/proto diff) triggers on services/media_edge/** only
```

## NOTES
- docs digest flags `tests/test_documentation_budget.py` currently FAIL: AGENTS.md files and `.omo/init-deep` artifacts are not in its whitelist. Committed-mode PR must fix that test first (exclude AGENTS.md by basename) or CI breaks.
- Repo is zh-CN docs, English code/commands; project name `voice-agent` in pyproject/package.json, `memoria` in docs/compose.
- `services/media_edge` is its own Go module with cgo + libopus; the Edge<->Voice Core seam is the repo's only protobuf, public HTTP/WSS edge is JSON.
- `share/` and `data/` are untracked local state; `outputs/` is gitignored except acceptance anti-manipulation receipts.
- Firmware `TODO(release-blocker)` items must mirror into `HANDOFF.md` (enforced by `test_release_blockers.py`); current blocker: USB serial wake.
