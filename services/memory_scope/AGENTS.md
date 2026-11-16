# Memory Scope Service

## OVERVIEW
Family shared-memory authority: fence-verified capture, family proposal/vote/promotion, child layers, subject erasure over an RLS-enforced Postgres store; in-memory adapter for dev/test. Earned this file: score 14, distinct domain (the repo's memory authority).

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Public surface | `__init__.py` | re-exports contracts, errors, stores, resolver, service |
| Domain contracts, errors, obligations | `domain.py` | imports generated ADR-0033 contracts, never local copies |
| Storage seam + ports + InMemory doubles | `repository.py` | `MemoryStore` Protocol; service depends only on this |
| Postgres adapter | `postgres_store.py` | `PostgresMemoryStore`; `pg_has_role` authority; bootstrap |
| Schema: roles, DDL, RLS, triggers | `postgres_schema.sql` | FORCE RLS, append-only guards, SECURITY DEFINER erase |
| Service facade | `service.py` | `MemoryScopeService` = `DurableCaptureMixin` + `SharedVoteMixin` |
| Family shared actions | `shared_actions.py` | `PostgresFamilySharedActionExecutor` (propose/confirm/object/withdraw) |
| Pure scope decisions | `resolver.py` | never touches storage; cannot-prove -> `unknown` |
| Child dialogue/archive layers | `child_layers.py` | `ChildLayerStore` |
| Production stack, settings, outbox | `production.py`, `wiring.py`, `redis_outbox.py` | 503 fail-closed; Redis Streams outbox |
| Capture policy | `capture_policy.py` | PolicyContext builder for capture transactions |
| Subject erasure | `subject_erasure.py` | SQLite dev plan; `PostgresSubjectMemoryScope` (maintenance role) |

## CONVENTIONS
- Fail closed everywhere: unknown scope -> `unknown`; missing authority -> `MemoryProductionUnavailableError` (503); never fall back to legacy SQLite/Archive.
- Authority comes from real DB roles via `pg_has_role`, never from GUCs. `app.memory.*` GUCs carry row context only, set per transaction with `set_config(..., true)`.
- Roles: `memoria_memory_owner` (NOLOGIN object owner), `_api`, `_worker`, `_action_executor`, `_maintenance` (erase ports only). Schema applied under `SET ROLE memoria_memory_owner`.
- Canonical enums re-exported unchanged from `packages.contracts.generated.python.multi_subject_contracts` (ADR-0033); no parallel enums.
- Outbox: Redis Streams `memoria:memory:events`, one Lua dedup script; duplicate delivery counts as success.
- Mixins compose the facade; owner attributes declared `TYPE_CHECKING`-only. Idempotent DDL (`IF NOT EXISTS` + recreate drops).

## ANTI-PATTERNS
- Never `USING (true)` RLS; schema must `FORCE ROW LEVEL SECURITY`. Never GUC as credential; runtime logins never object owners.
- API role must not execute worker commands; worker adapter must not touch subject-API-only operations.
- `memory_capture` never authorizes family proposal/promotion: dedicated `family_shared_memory_proposal` / `_approval` / `_promotion` capabilities.
- A receipt id is never an owner. Caller-supplied obligations, fingerprints, cover tuples, grants are never trusted; verifiers recompute the canonical 64-hex hash.
- Never UPDATE/DELETE durable rows (sole exception: DELETE inside `memory_subject_erase` as owner). Never commit inside an authority lock.
- Never fake atomicity (`requires_same_transaction_authority`; cross-DB refused). Never promote on CAS failure.
- Guardians get aggregate summaries only, never verbatim child chat.
- `InMemoryMemoryStore` (`DEV_TEST_ONLY`) must never back production traffic.
