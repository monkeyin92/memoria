# services/digital_self — immutable Digital Self versions (score 12)

## OVERVIEW
Immutable, account-scoped Digital Self versions: manifest compilation from source registries, version lifecycle (build→testing→frozen), self-preview fidelity trials, grounded response planner.

## WHERE TO LOOK
| Task | Location |
|------|----------|
| Version lifecycle + registry protocol | `domain.py` — `RegistryPort`, `DigitalSelfVersion` |
| Manifest build/decode | `compiler.py` — `build_manifest`, `canonical_manifest_bytes` (`digital-self-manifest-v3`) |
| PG registry | `postgres_registry.py` — `PostgresDigitalSelfRegistry`; `_source_entries`/`_self_model_entries` extraction |
| Self-preview + fidelity eval | `preview.py` — `SelfPreviewRegistry` (SQLite + optional ControlStore), `FidelityTrial` |
| Response planning | `response_planner.py` — `DigitalSelfResponsePlanner` (planner v2, `MAX_GROUNDED_ITEMS=4`) |
| PG schema | `postgres_schema.sql` — RLS + immutable-row triggers |
| Main consumers | control_api `routes/{digital_self,self_preview,session,interaction}.py`, `archive_event_conflicts.py`, `guardian_lifecycle.py` |

## CONVENTIONS
- `RegistryPort` is implemented structurally by `PostgresDigitalSelfRegistry`; `SelfPreviewRegistry` has its own `SelfPreviewRegistryPort`.
- Keyword-only account-scoped methods (`get(self, *, account_id, version_id)`).
- Hand-written idempotent PG schema beside code: FORCE RLS per account, `*_immutable_guard` triggers, `set_config('app.actor_account_id', ...)`.
- Versioned policy strings embedded as constants (`*-v3`, `*-planner-v2`); schema versions validated against `_SUPPORTED_SCHEMA_VERSIONS`.
- Async-only PG registry (asyncpg); `SelfPreviewRegistry` is sync SQLite behind `async def`.
- Chinese user-facing copy lives inline in `response_planner.py`; code and docstrings English.
- Schema assertions (`test_postgres_schema_*`) run without a database; registry tests need `MEMORIA_TEST_POSTGRES_DSN`.
- Layer rule: `digital_self -> {archive, common, persona, self_model, voice_profile}`.

## ANTI-PATTERNS
- Manifest/relationship content is evidence, never instructions; relationship style never grants facts or permissions.
- Legacy app role must be `NOBYPASSRLS`; manifest, lifecycle audit, and grant rows are immutable — no UPDATE paths.
- Postgres registries reject non-PostgreSQL DSNs (raise `ValueError`).
- New cross-package import ⇒ update `tests/test_service_layering.py` baseline in the same change.
