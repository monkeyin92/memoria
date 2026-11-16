# services/legacy — legacy grants & grantee shells (score 9)

## OVERVIEW
Legacy grants and isolated grantee relationship shells: read-mostly legacy access surface with audit trail and account export, kept separate from Digital Self versions.

## WHERE TO LOOK
| Task | Location |
|------|----------|
| Grant/shell/audit entities + port | `domain.py` — `LegacyRegistryPort`, `LegacyGrant`, `LegacyRelationshipShell`, `LegacyAuditEvent` |
| Package surface | `__init__.py` — 12-name `__all__` (entities + `PostgresLegacyRegistry`) |
| PG registry | `postgres_registry.py` — `PostgresLegacyRegistry` (`issue`, `activate`, `revoke`, `resolve_access`, `append_shell_turn`, `update_shell_preferences`, `export_for_account`, `delete_for_account`) |
| Digest/sharing rules | `rules.py` — `_grant_digest` (imported cross-package despite `_` privacy) |
| Schema loader + DDL | `postgres_schema.py` (`read_postgres_schema`), `postgres_schema.sql` — 4 `*_immutable_guard` triggers (grant core, shell identity, shell turns, audit events), FORCE RLS on 5 tables |
| Tests | `tests/` — 4 modules: fixtures, PG contract, PG integration (682), registry (668) |
| External consumers | control_api `governance_wiring`/`guardian_lifecycle`/`session_evidence`/`frozen_turn_verification`/`archive_event_conflicts`/`routes/session`; governance `account_data` (`LegacyAccountExport`, `LegacyRegistryPort`) |

## CONVENTIONS
- Keyword-only actor-scoped methods (`get_grant(self, *, actor_account_id, grant_id)`).
- Domain entities are frozen slotted dataclasses; `PostgresLegacyRegistry` implements `LegacyRegistryPort` structurally.
- Account context via `set_config('app.actor_account_id', $1, true)` — actor-keyed (persona uses `app.account_id`).
- Hotspot: `postgres_registry.py` 1008 L — `issue` (104-240) and `_transition` (276-363) in one class.
- Schema beside code, loaded via `Path(__file__).with_suffix(".sql")`; RLS per account, `NOBYPASSRLS` app role.
- Errors: `LegacyNotFoundError`, `LegacyAccessDeniedError`, `LegacyGrantConflictError`, `LegacyIdempotencyConflictError`.
- Shared test fixtures in `tests/legacy_fixtures.py`; PG contract tests gate on `MEMORIA_TEST_POSTGRES_DSN`, schema static assertions run without a database.
- Registry state changes funnel through `_transition` with a `_TRANSITIONS` map in `postgres_registry.py`.
- Layer rule: `legacy -> {digital_self, self_model}` (imports `digital_self.domain`/`compiler`).

## ANTI-PATTERNS
- Grant/shell identity, turns, and audit rows are immutable — never add UPDATE paths.
- RLS is never bypassed; non-PostgreSQL DSN is rejected.
- `_grant_digest` crosses package boundary by private name — do not add new cross-package private-name imports without updating the layering baseline.
- No new grant semantics here: new product surface belongs in digital_self, not this package.
- Audit events are append-only (`append_runtime_audit`) — never rewrite history rows.
- Deleted or replaced implementations and tests are removed in the same change (no shadow paths).
