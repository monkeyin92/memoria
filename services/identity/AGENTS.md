# services/identity

**Score: 24 (33 py files, >30 exports, 23 importer sites of `IdentityService`). Person/binding/relationship/permission bounded context.**

## OVERVIEW

Identity and multi-subject: person registration, age evidence, device bindings, relationships, personas, and two-party ownership transfers. Single `IdentityService` facade over one `IdentityStore` protocol.

## WHERE TO LOOK

| File | Role |
|---|---|
| `repository.py` | `IdentityStore` Protocol + `AuditEvent`/`OutboxEvent`; the only persistence contract |
| `service.py` | `IdentityService` facade (23 methods), composes relationship/transfer/persona/account-lifecycle mixins |
| `domain.py` | Pure contracts: dataclasses, Literals, permission matrix, error hierarchy |
| `postgres_store.py` | asyncpg adapter: FORCE RLS, RLS context, SECURITY DEFINER ports |
| `in_memory_store.py` | In-memory adapter mirroring the protocol 1:1 (tests/fixtures) |
| `relationships.py`, `transfers.py`, `personas.py`, `account_lifecycle.py` | Lifecycle seams |
| `authority.py` | Consent/transfer authorities; fail-closed `Rejecting*` variants are production wiring |
| `postgres_bindings.py`, `postgres_relationships.py`, `postgres_rows.py`, `postgres_schema_bootstrap.py` | Store mixins composed by `postgres_store.py` |
| `postgres_schema.sql` | 11 tables, RLS, SECURITY DEFINER functions, immutability triggers |

## CONVENTIONS

- **Namespace package: no `__init__.py`.** Import as `services.identity.<module>`.
- Both stores mirror `IdentityStore` method-for-method; protocol changes require both adapters.
- RLS context always via `_apply_context(connection, actor_person_id=..., scope="api")` setting `app.identity_actor`/`app.identity_scope` GUCs.
- Audit/outbox rows written by DB triggers; adapters never synthesize them. Mutation ports accept `audit_event`/`outbox_event` committed in the SAME transaction.
- Age verification server-side only via registration role (`registration_dsn`); `_registration_ready` raises without it.
- Canonical multi-subject enums from generated `packages/contracts/generated/python/multi_subject_contracts.py` (DO NOT EDIT); `domain.py` adapts to PEP 695 type aliases + Literals.
- Chinese user-facing strings (`REDACTED_DISPLAY_NAME`); English docstrings/errors.

## ANTI-PATTERNS

- **Module budgets exact-match**: `service.py`=1243, `postgres_store.py`=1145. Adding lines fails `make module-budget`; move code to a mixin.
- `PersonSubject` defaults `unknown`/`unverified`; never silently adult. Age/status fields never writable via profile.
- Non-self relationships require two-party confirmation; a known `person_id` alone never grants a role.
- Delegation permissions must be a subset of parent's; `MAX_DELEGATION_DEPTH = 3`.
- Only current owner creates/cancels a transfer, only target owner accepts; raw HMAC ticket never persisted.
- Only the attesting owner may redact a bound subject; adults/account holders never redacted.
- Core rows immutable: binding/person/relationship/role/transfer/persona updates rejected by triggers/guards.
- API role must never hold direct write privileges; GUC is not a credential and never unlocks raw DML.

## COMMANDS

```bash
uv run pytest services/identity/tests          # PG suites need MEMORIA_TEST_POSTGRES_DSN
make module-budget                             # exact-match check
uv run mypy services --strict && uv run ruff check services/identity
```
