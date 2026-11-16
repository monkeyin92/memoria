# services/consent — Consent Authority Domain

## OVERVIEW
Authoritative multi-subject consent lifecycle (fail-closed rules 1-8) with hash-verified evidence, FORCE-RLS PostgreSQL store, and a read-only legacy migration planner.

## WHY THIS FILE
Score 11 (>8, distinct domain): evidence/authority/store kernel + migration tooling; external consumers include identity, control_api, session_runtime, policy, memory_scope, agent/src.

## WHERE TO LOOK
| Task | Location |
|------|----------|
| Rule kernel: whitelists, grant/revoke verification | `consent_rules.py` |
| Authority lifecycle, sync and async | `authority.py` / `async_authority.py` |
| Evidence and snapshot DTOs, canonical hashing | `evidence.py` |
| Store seams, unit of work, invariants | `store.py` |
| Store adapters: InMemory (fixture) / Postgres (production) | `in_memory_store.py` / `postgres_store.py` |
| Binding consent stores (SQLite + Postgres) | `binding_snapshot.py` |
| Bound-subject service and session capability lists | `bound_subject.py` |
| Transaction-bound consent fence | `transaction_authorizer.py` |
| Legacy row migration planner (read-only) | `migration/` |
| DDL: roles, FORCE RLS, SECURITY DEFINER APIs | `postgres_schema.sql` |
| Cross-module test helpers | `tests/fakes.py`, `tests/schema_static.py`, `tests/test_evidence.py` (base_* fixtures) |

## CONVENTIONS
- Absolute `services.consent.*` imports everywhere (`pythonpath=["."]`); no relative imports.
- Ports-and-adapters: `*Port` `typing.Protocol` seam + InMemory / SQLite / asyncpg adapter; sync and async variants both supported.
- Evidence integrity: canonical JSON + sha256 over the full stored field set (tamper detection, not a signature); datetimes tz-aware UTC only; hash pattern `^[a-f0-9]{64}$`.
- Every mutation writes version rows + snapshot + audit entry + outbox event in one unit of work; superseded chains recorded, old rows never modified.
- Rejections raise `ConsentDeniedError` (PermissionError); store errors subclass RuntimeError/LookupError; `ConsentConsistencyError` = invariant violated, fail closed never auto-heal.
- Subject/binding/relationship authority is Identity-owned; evidence re-resolved from Identity on every call, never trusted from caller payloads.
- Migration planner: no wall clock (`now` injectable), `_require_aware` rejects naive datetimes, plan items sorted by `old_id`; bad rows become `QuarantineEntry` (`QuarantineReason` is a `Literal` alias), never raise.
- Manifest = JSONL with `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=True`; every line `executed: False`.
- Tests: module docstring states the contract; one behavior per `test_*`; Postgres contracts gated on `MEMORIA_TEST_POSTGRES_DSN` via `skipif`; helpers in `tests/` are not `test_`-prefixed and imported by absolute path; `# noqa` only for pytest fixture import reasons (`F401`/`F811`).

## ANTI-PATTERNS
- NEVER treat `InMemoryConsentStore` or SQLite stores as a production authority — local fixtures only.
- Store never writes caller-controlled GUCs; per-request authority lives in `ConsentAuthority`, not RLS.
- `migration/` never writes to any store and never executes a migration; manifests never emit `executed: true`; legacy guardian/persona rows never auto-migrate (human confirmation only).
- `crisis_notification` has no standing-grant path (`NOT_GRANTABLE`); cloning is adult-only `voice_clone_use`; delegate grants recorded as delegate, never as the parent's own consent; generic contacts never encoded in endpoint role fields.
- `tests/schema_static.py` is deliberately narrower than a SQL parser — do not extend it beyond the DDL forms in `postgres_schema.sql`.
- No xfail or in-body `pytest.skip`; DSN absence is the only skip reason.
