# control_api/tests — API contract suite

**Score 15 — distinct domain: tests are the spec (behavior-sentence names, exact `detail.code` literals, fail-closed assertions).**

## OVERVIEW
pytest + httpx `ASGITransport` against `create_app()`, never a live server. Test modules double as helper libraries: sibling suites import each other's private helpers and the shared support modules.

## STRUCTURE
```
tests/
├── conftest.py                 # autouse _hermetic_cwd only
├── test_*.py                   # ~100 behavior suites; some double as helper shards
├── interaction_api_test_support.py / policy_chain_test_support.py / archive_api_test_support.py
├── multi_subject_test_support.py / child_layer_test_support.py / identity_test_helpers.py / policy_r2_support.py
└── data/                       # static fixtures
```

## WHERE TO LOOK
| Need | Location |
|------|----------|
| Policy-chain + PG runtime fixtures | `policy_chain_test_support.py` (re-exports `postgres_runtime*`) |
| Identity/transfer/consent fakes | `identity_test_helpers.py` (TEST-ONLY authorities) |
| Archive builders, provenance | `archive_api_test_support.py` |
| Binding/guardian fixture shards | `test_bound_subject_binding.py`, `test_guardian_api.py` |
| Device profile/app fixtures | `test_device_control_api.py` (`_profile_app`, `_close_report_fixture`) |
| Deploy/compose contract | `test_production_compose.py` (reads repo files via `ROOT`) |
| P0-04 capability matrix | `test_policy_chain_subject_matrix.py` (`P0_04_MATRIX_DUMP=<path>`) |

## CONVENTIONS
- Autouse `_hermetic_cwd` (conftest) chdirs to `tmp_path`: the developer's repo `.env` must never leak into `ControlSettings(env_file=".env")`; files needing real repo paths resolve off `Path(__file__).resolve().parents[N]`.
- Env per test via `monkeypatch.setenv`: `MEMORIA_DB_PATH`, `MEMORIA_AUTH_SECRET` (long enough), `MEMORIA_ARCHIVE_INTERNAL_TOKEN`, `OFFLINE_MOCK=true`; PG suites gate on `MEMORIA_TEST_POSTGRES_DSN` (default `postgresql://memoria_test:memoria_test_local@127.0.0.1:55439/postgres`) via module-level `pytestmark = pytest.mark.skipif(...)`.
- Fixtures imported by name with `# noqa: F401 - pytest discovers imported fixtures by name`; never invent a duplicate fixture.
- Cross-test imports of `_`-private helpers are the norm — renaming breaks importers. Two distinct `_configure` implementations exist; never conflate them.
- `asyncio_mode = "auto"` yet `@pytest.mark.asyncio` is still written explicitly; module-level `pytestmark = pytest.mark.asyncio` acceptable.
- Names are full sentences with round tags (`_r2`, `_r3`) and traceability ids (`P0-04`, `#283`, `A06`); docstrings pin the regression with dates.
- Test-side secret strings are intentional (`"test-auth-material-that-is-long-enough"`); do not centralize.
- Markers: `hermetic` = no external services; `sqlite_only` = skipped when a PG DSN is set.

## ANTI-PATTERNS
- No `xfail`; the only skip is PG-DSN gating. Adding skips to hide failures is forbidden.
- CI runs test dirs whole — never enumerate individual test files in workflows ("a test file left off every list never ran").
- Retired surfaces are asserted absent (`test_retired_media_session_routes_are_gone`, removed raw-upload routes): reintroducing them breaks the contract.
- Fail-closed is the contract: missing subject category never defaults to adult; unconfigured stores never read as no-consent; unreadable DB identity never becomes absence.
- Autouse `reset_session_routes` fixture keeps module-level session state from leaking between tests.
- `type: ignore` and `noqa: BLE001` allowed only where the exception is the observation; payload content must never reach logs (planted-text assertions).

## NOTES
- Files >1000 lines: `test_student_safety_loop.py` 1635, `test_archive_session_events.py` 1631, `test_archive_lifecycle.py` 1597, `test_account_auth.py` 1557, `test_device_onboarding_api.py` 1423, `test_production_compose.py` 1413, `test_identity_lifecycle_api.py` 1309, `test_policy_chain_subject_matrix.py` 1206, `test_session_api.py` 1200, `test_interaction_response_plan_subjects.py` 1190, `test_archive_subject_scope.py` 1137, `test_archive_voice_and_reads.py` 1023, `test_bound_subject_binding.py` 1017.
- Module budgets do not regulate `tests/` (checker excludes it); `scripts/ci_test_durations.json` feeds CI shard assignment (4 shards, `ci_test_shards.py`).
- PG tests connect with distinct role passwords per suite and self-seed via asyncpg DDL + schema file.
