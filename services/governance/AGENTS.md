# services/governance — account export, deletion, recovery (score 11)

## OVERVIEW
Account data export, subject-scoped deletion, and recovery governance: whitelist export policy, resumable deletion saga with ledger, PG subject archive adapter under RLS.

## WHERE TO LOOK
| Task | Location |
|------|----------|
| Account export/deletion control plane | `account_data.py` — `AccountDataGovernance`, `_POSTGRES_ARCHIVE_EXPORT_TABLES` (61), `_POSTGRES_ARCHIVE_DELETE_ORDER` (145) |
| Deletion saga + ledger | `subject_deletion.py` — `SubjectDeletionService._run`, `_STEPS`, versioned ledger snapshots |
| Export reduction (pure, no DB) | `subject_export.py` — `build_subject_export`, `OmittedSection`, `manifest_sha256` |
| PG subject deletion adapter | `subject_archive.py` — `PostgresSubjectArchive(PostgresAccountRepository)` |
| Ports + scope value object | `subject_ports.py` — Protocols, `SubjectScope` |
| Table ownership constants | `lifecycle_tables.py` — authoritative/projection/evolution/controller lists |

## CONVENTIONS
- Ports are `typing.Protocol`s in `subject_ports.py`; adapters live in the owning service and receive the port.
- Two first-class backends: SQLite control store and PostgreSQL archive, parallel repository classes.
- Deletion is a resumable saga: fixed ordered `_STEPS`, ledger snapshot versioning, idempotent retry/replay.
- Export is field-whitelist only with stable `OmittedSection` reason codes; guardian audience gets fixed event buckets, never raw `event_type`.
- Every PG statement filtered by `account_id` under `app.account_id` scope — RLS is never bypassed.
- Tests double as fixtures for control_api (`tests.test_subject_deletion._service`, `test_account_data_postgres_bound_child.fixture`).

## ANTI-PATTERNS
- Rows are never exported whole; `bio`, `avatar_url`, `phone_number_masked`, `account_id` stay absent until named in a whitelist.
- Withdrawn evidence (`history_eligible=false`, `ephemeral_only`...) is dropped wholesale — never re-released from a pre-ceiling payload.
- NULL or foreign `subject_id` is never attributed to the requested subject; validate before any repository read.
- Subject-scoped deletion never targets the account itself.
- Projection rows with one `source_event_id` must not outlive it; lineage alone never deletes a merged child.
- Controller tables never enter account erasure; `POSTGRES_CONTROLLER_ONLY_RLS_TABLES` are not covered by the `app.account_id` probe.
- `merge_completed` adds missing snapshot rows, never overwrites.
- Consents released only when the requested subject is the account itself.
