# services/guardian/tests

**Score: 14 (29 py files, 27 suites). Distinct domain: frozen ticket-suffixed contract suites + A02 holdout fixtures.**

## OVERVIEW

Guardian test contracts: PG subject scoping/RLS, crisis push + delivery ledger, consent, subject deletion, retention, parent alert/summary, session recap, weekly report, tutor store, corpus, child memory/archive review/recall, preference extract.

## WHERE TO LOOK

| File | Role |
|---|---|
| `guardian_postgres_support.py` | `_postgres_dsn`/`_role_dsn`/`_ensure_roles` shared helpers |
| `preference_extract_cases.py` | A02 annotated + holdout case fixtures |
| `fixtures/postgres_schema_before_b01.sql` (2343 L) | Prior-schema snapshot for upgrade tests |
| `test_guardian_postgres_subject_scoping.py` (885 L) | Subject scoping invariants |
| `test_crisis_push.py` (730 L) | Claim/settle/reclaim ledger contract |
| `test_memory_closed_loop_a05.py`, `test_preference_extract_a02.py` | Frozen contract suites (ticket suffixes) |

## CONVENTIONS

- Flat layout, no `__init__.py`/`conftest.py`; namespace imports `services.guardian.tests.<module>`.
- Frozen-contract suites carry ticket ids (`_a05`, `_a02`, `_b01`) - treat as pinned behavior.
- PG gate: `MEMORIA_TEST_POSTGRES_DSN`, skipif per module; schema read via `Path(__file__).parents[1]`.
- A02 rules: wrong subject or wrong polarity is a hard fail; miss rate is reported only, never flips a case to pass.

## ANTI-PATTERNS

- Public weekly payload never contains transcript or diagnosis; live corpus sample reported, never deleted.
- Push ledger: API role can never claim/settle; concurrent claimers never hold the same notification; delivered rows never claimed again; `pin_only` relay alert never claimed for push.
- Account-less subject can never confirm a guardian link; a different subject never inherits scope; scope never targets the account.
- Parent alert copy must stay free of `PRODUCT_FORBIDDEN_COPY` (cross-checked against the miniprogram forbidden-copy script).
- Parent summary adversarial fixtures pin forbidden substrings (hospital/needle, names, address, ...) - extend carefully.

## COMMANDS

```bash
uv run pytest services/guardian/tests --import-mode=importlib --no-cov
MEMORIA_TEST_POSTGRES_DSN=postgresql://... uv run pytest services/guardian/tests/test_crisis_push.py -k reclaim
```
