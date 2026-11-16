# tests — repo-wide contract & harness tests

**Score 8 — distinct domain: cross-service acceptance/contract tests, not service-local units.**

## OVERVIEW
Repo-root pytest tree for cross-cutting invariants (layering, budgets, report wording, device contracts) plus e2e wrappers, simulator contracts, and the PostgreSQL parity path.

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Import-graph freeze | `test_service_layering.py` | `ALLOWED_EDGES` baseline; graph only shrinks |
| Line/document budgets | `test_module_budget.py`, `test_documentation_budget.py` | read `pyproject.toml [tool.memoria.module-budgets]` |
| Report wording contracts | `test_voice_session_report.py`, `test_voice_session_capture.py` | exact strings + absent-wording asserted |
| Device media contract | `test_device_media_v2_contract.py` | schema + binary frames, shared with firmware |
| Cost ledger | `test_household_cost_ledger.py` + `fixtures/cost_ledger/` | golden values, never-invented-prices |
| PG parity fixtures | root `conftest.py` | opt-in DSN env; see CONVENTIONS |
| Test-support backend | `testing/postgres_harness.py`, `testing/app_store.py`, `testing/guardian_seed.py` | production-shaped harness; consumers below |
| E2E wrappers | `e2e/` | thin re-exports of `services/*/tests/integration` + scenario tests |
| Simulator contracts | `voice_coordination/` | Protocol/dataclass specs only; no runner yet |
| Other SUTs pinned | `test_sync_pstack.py`, `test_auto_audio_tool.py`, `test_r02_gates.py`, `test_media_runtime_replay.py` | bash-script and hardware-script drivers |

## CONVENTIONS
- Opt-in PostgreSQL: default is SQLite; `MEMORIA_TEST_CONTROL_STORE=postgres` + `MEMORIA_TEST_POSTGRES_DSN` repoint `MemoryStore.__init__` per path. `MEMORIA_TEST_APP_POSTGRES_SKIP` lists DSNs left unset.
- Serial only: `pytest -n` breaks Postgres tests (shared cluster roles, import-time app-build locks).
- `testing/` is helper code, not a testpath: namespace package, collected nowhere, driven by root `conftest.py`. Its harness interprets a psql subset (`\if`, `\gexec`, `\connect`, `\i`); unknown meta-commands raise.
- `testing/app_store.py` reads run as harness admin — RLS does not apply; assertions only, never model app behavior on them. `bypass_triggers` only to force states the DB guards refuse.
- No sys.path hacks: `pythonpath = ["."]`, imports via `scripts.*` / `services.*` absolute paths.
- Test names: `test_<sentence>`, one-sentence docstring states the invariant; private helpers `_name`; fixtures under `tests/fixtures/<domain>/`.
- Wording/budget/layering baselines are edited on purpose when behavior changes; removed edges must leave the baseline in the same change.
- Not type-checked: mypy excludes test dirs; `asyncio_mode = "auto"` (e2e still marks `@pytest.mark.asyncio`).

## ANTI-PATTERNS
- Never assert inferred values: absent evidence prints `unknown` / `not observed`; a commit with no delivery is not "never reached the device".
- Never guess receipt paths or overwrite existing out dirs; fail closed on unreadable/invalid receipts; unknown exit code never claims success.
- Never weaken module-budget, document-budget, or layering baselines to go green.
- Never invent prices in cost-ledger fixtures; unallocated fees are not force-split into household totals.
- `test_sync_pstack.py`: bad manifest names (`""`, `.`, `..`, `a/b`, `*`) must delete nothing.
- `voice_coordination/__init__.py` re-exports 17 symbols from `simulator_contracts.py`; `cases/*.yaml` have no runner — not executable tests.
