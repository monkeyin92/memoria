# app/database — MemoryStore persistence

**Score 11 — distinct domain: dual-dialect SQL store, SQLite for dev/test, PostgreSQL FORCE RLS for production.**

## OVERVIEW
`MemoryStore` composes one `*Mixin` per domain (Schema/Device/AuthSession/Account/VoiceSession/Record); the same SQL text runs on both dialects. `__init__.py` holds `__all__`, the only import surface.

## STRUCTURE
```
database/
├── __init__.py          # MemoryStore composition + package exports
├── schema.py            # SQLite DDL + imperative migrations (1064 L)
├── postgres_schema.sql  # PG baseline: 29 tables, roles, RLS policies
├── account_store.py     # account lifecycle, deletion/export (977 L)
├── device_store.py      # device/media session epochs (948 L)
├── session_store.py / record_store.py / auth_session_store.py / chat_usage.py
├── backend.py           # dialect rewriting, IntegrityError, connections
├── migrations.py        # PG versioned NNNN_*.sql ledger
└── settings.py          # validate_control_database_url, memoria_control role
```

## WHERE TO LOOK
| Need | Location |
|------|----------|
| Account deletion/export lifecycle | `account_store.py` (470-888) |
| Media session epoch/rotation | `device_store.py` (`stream_epoch` logic 644-924) |
| SQLite schema evolution | `SchemaMixin.initialize` (PRAGMA table_info + ALTER/backfill) |
| PG schema evolution | `NNNN_*.sql` + `control_schema_migrations` ledger + advisory lock |
| Role/RLS setup | `postgres_schema.sql`, `settings.py` |

## CONVENTIONS
- One SQL text, two dialects: SQLite-style `?` placeholders and `BEGIN IMMEDIATE`; `PostgresConnection` rewrites to `%s`; `translate_sql` rejects literal `%` and quoted `?`.
- `MemoryStore(path)` = SQLite (dev/test); `MemoryStore(dsn=...)` = PostgreSQL (prod); both empty → `ValueError`.
- SQLite pragmas always: WAL, foreign_keys=ON, busy_timeout=5000; PG: `statement_timeout=15000` merged into DSN options (never pool kwargs), every write a savepoint, `INSERT ... RETURNING` (no lastrowid).
- Timestamps are ISO-8601 TEXT compared as strings; booleans are 0/1 BIGINT with CHECK; the PG schema mirrors SQLite column for column.
- FORCE RLS: NOLOGIN owner `memoria_control_owner` owns objects; runtime role is `memoria_control`; `initialize_postgres` fails closed on missing table, missing FORCE RLS, or behind schema version; ahead is allowed.
- Mixin methods keyword-only (`*, user_id`); writers open short connections via `MemoryStore.connection()`.

## ANTI-PATTERNS
- Released migration files are immutable: fix forward with a new number, `NNNN >= 2`; the runtime role only verifies, an admin role applies (scripts/release_ops.sh schema step).
- `migrations/` dir does not exist yet — a numbered migration requires creating the dir; misnamed/out-of-order files raise RuntimeError.
- Writes inside an outer transaction are savepoints — never their own commit.
- External identity subject hash + receipt are server-only: never returned from a client route.
- Archive ownership is server-side: never accept `account_id` from an Agent.
- `active_subject_id` NULL is an intentional unknown-safe fence and must stay NULL (backfilled only once).
- `MEMORIA_CONTROL_DATABASE_URL` must be PostgreSQL using the independent `memoria_control` role, never a shared role.
- `stream_epoch` is a positive uint32 and must advance on rotate.

## NOTES
- Hotspots: `schema.py` 1064, `account_store.py` 977, `device_store.py` 948; this dir has no module-budget entries.
- `MemoryStore` is imported by ~35 modules; `IntegrityError` (sqlite+psycopg tuple) is the auth route's conflict signal.
