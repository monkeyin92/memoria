# services/archive — evidence archive & memory projections

**Why this file:** score 14 (38 files, >30 symbols, `EvidenceEvent` imported by ~60 files); distinct domain: durable evidence ledger + reconstructable memory projections.

## OVERVIEW
PostgreSQL-only evidence archive: consent/evidence ledger with idempotent outbox, mixin-composed memory catalog compiling approved evidence into pgvector projections, procedural skill catalog, restore drill, memory eval harness.

## WHERE TO LOOK
| Need | File | Notes |
|------|------|-------|
| Evidence ledger, consent, outbox | `postgres_archive.py` | `PostgresLifeArchive` |
| Public types & Protocols | `memory_domain.py` | hub of 19+ importers |
| Catalog composition root | `postgres_memory_catalog.py` | review/search/timeline/write mixins; compiler must not bypass RLS |
| Write path | `memory_write.py` | `_write_extraction` ~350 lines: persons, aliases, claims, conflicts, episodes, search docs |
| Reviews & corrections | `memory_review.py` | superseding events; corrections re-embed |
| Search SQL | `memory_search.py`, `memory_lexical_sql.py`, `memory_sql.py` | `_bind_dollar` shared with review/write |
| Skill catalog | `postgres_skill_catalog.py`, `skill_domain.py`, `skill_executor.py` | procedural memory |
| Restore drill | `restore_drill.py` | needs SUPERUSER/BYPASSRLS |
| Memory eval | `memory_evaluation.py`, `memory_distractor_evaluation.py` | datasets in `evaluation/` |
| Vector repair | `memory_vector_backfill.py`, `memory_vector_cleanup.py` | missing-vector worker |
| Extractors | `memory_extractor.py`, `qwen_memory_extractor.py` | rule-based + Qwen JSON fallback |
| Gate & policy | `extraction_gate.py`, `memory_write_policy.py`, `claim_subjects.py` | subject-category filter |

## CONVENTIONS
- Catalog built by mixin composition; mixins reference the host via `TYPE_CHECKING`.
- RLS per connection: `SELECT set_config('app.account_id', $1, true)`; caller-supplied account filter never trusted alone.
- Schema slices: `postgres_schema.sql` == concat of `postgres_archive_schema.sql` + `postgres_memory_schema.sql` + `postgres_skill_schema.sql` (test-enforced). Edit the slice, re-concat, never edit the unified file alone.
- DDL idempotent at startup (`CREATE TABLE IF NOT EXISTS`, idempotent policy blocks); SQL loaded via `Path(__file__).with_name(...)`.
- Deterministic ids: `_stable_uuid(kind, *parts)` derived from event_id (duplicated in `memory_write.py` / `postgres_skill_catalog.py`).
- DB roles by login name: `memoria_app` (archive), `memoria_compiler` (NOLOGIN, compile tasks only).
- Eval cases run in throwaway schemas (`search_path` URL rewrite); eval DSN must start `postgresql://`/`postgres://`.
- Eval datasets loaded by `Path(__file__).parents[1] / "evaluation" / name`, never package-imported.

## ANTI-PATTERNS
- Search must never receive evidence that has not been explicitly approved (`memory_domain.py:358`).
- `NULL` subject = unclaimed speaker, never the account owner; never matches named scope.
- Corrections supersede via `supersedes_event_id`; original evidence never rewritten.
- Vector repair reads the current claim row only — never transcripts, payloads, or search-row context.
- Retracted claims never resurrected by backfill, replay, or vector repair; replay never re-embeds.
- Episodes never compiled against a named subject; aliases never shared between two matched people.
- Validation error strings (`"… must not be blank/precede/follow"`) are load-bearing test assertions — do not reword.
- `# pragma: no cover` only for defensive unreachable branches, never to skip tests.
