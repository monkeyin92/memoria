# services/session_runtime/tests

**Score: 9 (9 py files). Distinct domain: live-Postgres fence/lifecycle contract suites (~8.8k L).**

## OVERVIEW

Session runtime contracts: PG session lifecycle, action authority, fence readiness, runtime profile service, current-voice consent, subject resolver, service mode, device trust.

## WHERE TO LOOK

| File | Role |
|---|---|
| `postgres_store_test_support.py` (709 L) | `postgres_runtime`/`postgres_runtime_with_consent` fixtures, `_seed_verified_device`, `_SIGNING_KEY` |
| `test_postgres_session_lifecycle.py` (1778 L) | Full start/switch/commit/decide lifecycle |
| `test_postgres_action_authority.py` (1359 L) | Deferred action fence |
| `test_postgres_fence_readiness.py` (1038 L) | Schema readiness + re-parses `postgres_schema.sql` with regex |
| `test_runtime_profile_service.py` (933 L) | Profile signing/HMAC contract |
| `test_current_voice_consent_postgres.py` | Imports `setup` fixture from `services.identity.tests.test_postgres_revoke_classify` |

## CONVENTIONS

- Shared fixtures in `postgres_store_test_support.py`, imported with `# noqa: F401` by 3 PG suites.
- Heavy PG suites create a disposable DB `memoria_session_<10hex>` from `MEMORIA_TEST_POSTGRES_DSN` and drop it on teardown.
- Schema fixtures use root-relative paths - run pytest from repo root.
- Frozen contract assertions: "must never relabel to closed", "must not produce a mixed pair", unavailable authority "never upgraded".

## ANTI-PATTERNS

- Fence readiness re-parses the runtime schema with `re` (executor must never be granted consent-mutation lock ports): schema port/GRANT changes require updating that test.
- Do not rename `setup` in `services/identity/tests/test_postgres_revoke_classify.py` - cross-service import here.
- Test support exported under `_`-prefixed names (`_SIGNING_KEY`, `_seed_verified_device`) yet imported across modules; treat as shared contract.

## COMMANDS

```bash
uv run pytest services/session_runtime/tests
MEMORIA_TEST_POSTGRES_DSN=postgresql://... uv run pytest services/session_runtime/tests/test_postgres_fence_readiness.py
```
