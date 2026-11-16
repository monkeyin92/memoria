# services/archive/tests — archive contract suite

**Why this file:** score 9 (28 files, ~13k lines); distinct domain: pins production invariants and the Postgres role contracts.

## OVERVIEW
Contract suite for the PostgreSQL archive family; Postgres-gated tests skip without `MEMORIA_TEST_POSTGRES_DSN`.

## WHERE TO LOOK
| Need | File | Notes |
|------|------|-------|
| Shared PG fixtures | `conftest.py` | `archive`, `make_catalog`, `skill_catalog`, `owner_sql`; role constants `APP_ROLE`/`COMPILER_ROLE` |
| Compile/search/review contract | `test_memory_catalog.py`, `test_postgres_memory_catalog.py` | 33 + 14 tests; allowlist, retraction, aliases, episodes |
| Subject scope | `test_subject_scope.py`, `test_claim_subjects.py` | NULL-speaker and child-subject semantics |
| Vector repair & cleanup | `test_memory_vector_cleanup.py`, `test_memory_vector_backfill.py` | deadlock and embedding-outage scenarios |
| Restore drill | `test_restore_drill.py` | cross-service; also needs `MEMORIA_TEST_POSTGRES_CONTAINER` |
| Eval harness | `test_memory_evaluation.py`, `test_memory_distractor_evaluation.py` | pinned golden metrics |
| Skill catalog | `test_skill_catalog.py`, `test_postgres_skill_catalog.py` | RLS assertions |
| Gate & policy tables | `test_extraction_gate.py`, `test_memory_write_policy.py` | enum ↔ schema CHECK pinning |
| Schema slices | `test_postgres_schema_slices.py` | unified == slice concat |

## CONVENTIONS
- Plain module-level `def test_*` / `async def test_*`; no `class Test*`; stubs are private classes with `version` + `async def extract(self, event)`.
- `@pytest.mark.asyncio` everywhere despite `asyncio_mode = "auto"`.
- Postgres gate: `@pytest.mark.skipif(not os.getenv("MEMORIA_TEST_POSTGRES_DSN"))` per test; the two vector files skip at module import.
- Raw SQL inspection via `owner_sql` / local `OwnerSql` helpers (admin conn, RLS bypassed) — inspection and seeding only.
- Fixed literals over generated data: dates, account ids, policy versions, golden metrics; Chinese fixtures inline (ASCII rule overridden for data).
- Tests import private symbols (`restore_drill._controller_rls_metadata_failures`, `memory_evaluation._matches`) and each other (`_record`); renames break this suite.
- `AtomicToolExtractor` is imported by `services/control_api/tests/test_planner_catalog_r3.py` — treat as a shared helper.

## ANTI-PATTERNS
- Skipped Postgres tests never count as passing (`docs/runbooks.md`: 跳过不算通过).
- Never loosen minor-subject write confirmation — parametrized rejection tables in `test_memory_write_policy.py` / `test_student_memory_extractor.py`.
- Never strip entity aliases as lexical terms (`test_entity_scoping_keeps_the_alias_as_a_lexical_bridge`).
- Never guess time windows on ambiguous time language; `RecallPlanner` requires a timezone-aware clock.
- Migration tests: rerun yields duplicates (idempotence), a failed batch rolls back, owner context never returns uncertain/guest evidence.
