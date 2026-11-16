# services/device_fleet/tests

**Score: 9 (12 py files). Distinct domain: flat per-domain suites with cross-file fixture sharing; 61 top-level tests.**

## OVERVIEW

Device fleet/onboarding test suites: crypto contracts, Postgres attestation/commands/OTA/SIM/binding lifecycle, bootstrap vertical slice + reprovision, test-device registration.

## WHERE TO LOOK

| File | Role |
|---|---|
| `test_postgres_attestation.py` | Owns shared `pg_services`/`PgServices` fixture used by 4 PG suites |
| `test_bootstrap_postgres.py` | Postgres onboarding store contract |
| `test_bootstrap_vertical_slice.py` | End-to-end bootstrap; `_fixture`/`NOW` re-imported by reprovision |
| `test_register_test_device.py` (1147 L) | Registration gate: env + key-file invariants |
| `test_crypto_contracts.py` | Signature round-trips pinned |

## CONVENTIONS

- Flat package (`__init__.py` exists) so tests import helpers across files (`# noqa: F811`).
- PG suites seeded from `test_postgres_attestation` fixtures; SQLite suites marked `sqlite_only`.
- PG gate: `MEMORIA_TEST_POSTGRES_DSN` (default `127.0.0.1:55439`), else `pytest.skip`.
- Schema under test applied with maintenance role; runtime roles never get DDL.
- Test registration requires `MEMORIA_ENVIRONMENT=memoria-test` AND `--environment memoria-test`; key file must be a regular 32-byte file.

## ANTI-PATTERNS

- Run the whole directory, not single files: suites depend on sibling fixtures and helpers.
- Never add a test file that isn't part of the suite list CI runs (shards cover listed files only).
- Registration tests assert "never updates the row it found" (ON CONFLICT DO NOTHING).

## COMMANDS

```bash
uv run pytest services/device_fleet/tests
MEMORIA_TEST_POSTGRES_DSN=postgresql://... uv run pytest services/device_fleet/tests/test_postgres_attestation.py
```
