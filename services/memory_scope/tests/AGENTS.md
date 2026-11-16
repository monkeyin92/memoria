# Memory Scope Tests

## OVERVIEW
Suite for MemoryScopeService: in-memory/offline default plus opt-in real-Postgres contracts, sharing builders, fixtures, and scenarios across modules. Earned this file: score 9, distinct domain (PG-contract harness conventions).

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| PG env + store init | `test_postgres_store.py` | `pg_env`, `_initialize_store()`; imported by 5 PG tests |
| `service` fixture + builders | `service_helpers.py` | subject/policy/fence/consent/receipt builders for the 3 `test_service_*` suites |
| Receipt builder | `receipt_helpers.py` | `make_memory_receipt()` for `PolicyReceiptV2` |
| Roles + DSN helpers | `postgres_helpers.py` | role constants, `dsn_with()` |
| Temp-DB fixture | `conftest.py` | `cross_db`: temp DB + 3 cluster roles |
| Schema install | `test_cross_domain_sensitive_write.py` | `_install_schemas()` reused by 3 PG tests |
| Adapter contract scenarios | `test_adapter_contract.py` | scenarios re-run against in-memory/SQLite/PG |
| Legacy schema upgrade | `fixtures/postgres_schema_before_b01.sql` | `_SCHEMA_PATH` monkeypatched in `test_purpose_heads_postgres.py` |

## CONVENTIONS
- `test_*_postgres.py` = real-Postgres contracts; plain `test_*.py` = in-memory/offline. PG tests self-skip unless `MEMORIA_TEST_POSTGRES_DSN` is set.
- Every PG test provisions its own temp database (`memory_scope_pg_<uuid8>`), drops it on exit; cluster-global roles are created if missing and reused.
- Deterministic shared password `memoria_local_test_password` — deliberate; per-run `ALTER ROLE` passwords break concurrent runs.
- PG contracts reuse in-memory scenario functions instead of duplicating tests.
- Cross-module imports of private helpers are normal (`_install_schemas`, `_initialize_store`, `_layers`); import via absolute `services.memory_scope.tests.*` (no `__init__.py`; works under `--import-mode=importlib`).
- Canonical fixture hashes are fixed (`"a"*64`, `"b"*64`); only generated `PolicyObligation` / `PolicyObligationSpec` accepted.
- One `asyncio.Lock` in the `service` fixture serializes in-memory verifiers and the promotion commit authority.

## ANTI-PATTERNS
- In test builders: derive action fences via `services.policy.action_fence.build_*`; never hand-fabricate them or derive `context_hash` from a local fence fingerprint.
- Proposal ids must be unique per scenario (`uuid4` suffix) — PG contracts share one database.
- Promotion finalizer must not write before commit; the store write shares the lock so revoke cannot race.
- Do not re-implement `dsn_with` (duplicate at `test_postgres_store.py:57`).
- Legacy fixture schema keeps RLS real: FORCE RLS, no `USING (true)`, no GUC-as-credential.
