# services/guardian

**Score: 24 (52 py files, 26 modules, `GuardianStorePort` imported by 11 non-test files). Guardian/consent/safety bounded context.**

## OVERVIEW

Guardian relationships, consent boundaries, crisis notification, child memory review/recall, session recap, weekly reports and tutor persistence - all for minor accounts.

## WHERE TO LOOK

| File | Role |
|---|---|
| `__init__.py` | Package surface: re-exports domain types, consent service, store port |
| `domain.py` | Pure contracts: consent kinds, subject categories, transitions |
| `consent.py` | `GuardianConsentService` |
| `postgres_store.py` (1374 L) | Store facade composing 4 mixins: consent, crisis, links, person consent |
| `postgres_links.py`, `postgres_consent.py`, `postgres_person_consent.py`, `postgres_crisis_store.py`, `postgres_helpers.py` | Store mixins |
| `postgres_schema.sql` (~110 KB) | 14 tables, FORCE RLS, SECURITY DEFINER functions |
| `crisis.py`, `push.py`, `crisis_delivery_receipt.py` | Crisis notification + push ledger + receipts |
| `parent_alert.py`, `weekly_report.py`, `session_recap.py`, `preference_extract.py` | Parent copy, weekly projection, recap extraction |
| `child_memory.py`, `child_archive_review.py`, `child_archive_recall.py` | Child memory policy + archive review/recall |
| `retention.py`, `retention_notices.py`, `corpus.py`, `subject_activity.py` | Retention rules, notices, corpus, activity |

## CONVENTIONS

- Store built from 4 role-separated DSNs: `dsn` (API `memoria_guardian`), `bootstrap_dsn` (DDL), `maintenance_dsn`, `worker_dsn`; prod requires `initialize_schema=False` + `_verify_production_schema` (fails closed).
- Pure-domain modules (`domain.py`, `retention.py`) keep zero framework/persistence deps; persistence behind `typing.Protocol` ports.
- Distinct tables for link-scoped vs person-scoped consent (never nullable `link_id`).
- Account export/delete via SECURITY DEFINER functions gated on maintenance role.
- Fixed CJK copy lives in `retention_notices.py`, `parent_alert.py`, `preference_extract.py` regexes.

## ANTI-PATTERNS

- **Module budget exact-match**: `postgres_store.py`=1374. Adding lines fails `make module-budget`; split into a mixin.
- Durable child audio and verbatim transcripts never cross the child_memory boundary (`strip_transcript_fields`, `TRANSCRIPT_KEYS`).
- Session recap never persists raw utterances; archive only on separate consent.
- Push payload never carries conversation content or the child's name; per-event value is event time only.
- Crisis payload/rows never contain child utterances; account-less subject never upgraded to verified link.
- Subject transitions never upgrade capabilities by age band; `parent_for_child` never confirms consent.
- Delivered push rows never rewritten/reclaimed; person consents never deleted by subject deletion.
- API role never sets broad-scope GUC to widen RLS; never bypasses `_verify_production_schema` in prod.
- Retention notices are exact owner-approved wording - never reword.

## COMMANDS

```bash
uv run pytest services/guardian/tests          # PG suites need MEMORIA_TEST_POSTGRES_DSN
make module-budget
uv run mypy services --strict && uv run ruff check services/guardian
```
