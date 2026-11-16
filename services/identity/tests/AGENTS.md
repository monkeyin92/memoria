# services/identity/tests

**Score: 12 (14 py files). Distinct domain: Postgres registration + schema-security contracts and cross-service fixture reuse.**

## OVERVIEW

Identity test suites: domain/authority units, person/binding lifecycle, PG registration + schema security, redact-bound-subject, relationships and personas.

## WHERE TO LOOK

| File | Role |
|---|---|
| `identity_pg_test_support.py` | DSN helpers, role bootstrap, `pytestmark` (asyncio + skipif) |
| `service_test_support.py` | `store`/`service` fixtures, `_now`, persona builders |
| `test_postgres_registration.py` (1074 L) | Registration role + age-verification contract |
| `test_postgres_schema_security.py` | RLS/trigger/role assertions on real schema |
| `test_postgres_schema_static.py` | Static SQL text checks, runs without a server |
| `test_postgres_revoke_classify.py` | `setup` fixture reused by session_runtime tests |

## CONVENTIONS

- Namespace package: no `__init__.py`, no `conftest.py`; fixtures live in the two support modules, imported by name.
- PG gate: `pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not _DSN, ...)]` re-imported with `# noqa: F401`.
- Schema read from source at test time: `Path(__file__).resolve().parents[1] / "postgres_schema.sql"` - run from repo root.
- Registration role is exercised as a real separate DSN; age verification is server-side only.

## ANTI-PATTERNS

- Static checks assert FORCE RLS, immutability triggers, delete guards, SECURITY DEFINER names - schema changes must update them together.
- `services/session_runtime/tests/test_current_voice_consent_postgres.py` imports this suite's `setup` fixture: renaming/removing it breaks another service.
- Do not instantiate parallel stores in unit tests; PG adapter has its own contract suite.

## COMMANDS

```bash
uv run pytest services/identity/tests                                   # skips PG without MEMORIA_TEST_POSTGRES_DSN
MEMORIA_TEST_POSTGRES_DSN=postgresql://... uv run pytest services/identity/tests/test_postgres_registration.py
uv run pytest services/identity/tests/test_postgres_schema_static.py    # no server needed
```
