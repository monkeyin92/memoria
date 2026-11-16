# services/session_runtime

**Score: 24 (23 py files, facade used by every control_api session route). Persistent session runtime bounded context.**

## OVERVIEW

Multi-subject persistent session resolution, signed Runtime Profiles, and epoch/fence enforcement over PostgreSQL.

## WHERE TO LOOK

| File | Role |
|---|---|
| `service.py` (1322 L) | `PostgresSessionRuntimeService` facade + `build_postgres_session_runtime_service` |
| `postgres_store.py` (1441 L) | Store: schema verification, `_ActionExecutorConnection`, SECURITY DEFINER ports |
| `action_authority.py` | Deferred actions + policy receipts (mixin) |
| `issue_authority.py` | Profile issue/renew (mixin); `ProfileConsentWithdrawn` |
| `profile_service.py` | RuntimeProfile signing/verification, in-memory authority, payload schema |
| `wire.py` | Canonical JSON codec; `sign/verify_runtime_profile_payload` |
| `commands.py` | Frozen command/result dataclasses |
| `subject_resolver.py`, `device_trust.py`, `service_mode_resolver.py` | Subject resolution, trust levels, service mode |
| `errors.py` | `PersistentSession{Denied,Expired,NotFound,Unavailable}` hierarchy |
| `postgres_schema.sql` (117 KB) | 8 tables, ~61 functions, RLS, executor grants |

## CONVENTIONS

- One service over one caller-owned asyncpg transaction: `start` accepts a `before_commit` hook.
- Writes run as `memoria_action_executor` via SECURITY DEFINER ports (no direct table grants); reads via `memoria_session_api` RLS.
- Value types frozen slots dataclasses, tz-aware datetimes mandatory, bounded strings, sha256 regex checks.
- Untyped wire JSON fails closed (`PersistentSessionUnavailable`); `(asyncpg.PostgresError, SessionRuntimeAuthorityUnavailable, ActionAuthorizationError)` mapped likewise, denials re-raised.
- Trust levels `{trusted, verified, offline, untrusted, revoked}`; subject vocab closed set. Feature flag `MEMORIA_BOUND_DEVICE_TRUST_ENABLED` (default off).

## ANTI-PATTERNS

- **Module budget exact-match**: `service.py`=1322. Adding lines fails `make module-budget`; mixin growth must be balanced by cuts.
- No successful profile escapes before receipt batch, event and outbox row commit atomically.
- `start` denies unknown or resource-only capability sets; deferred capabilities never minted as allowed or policy receipts at start.
- `profile_ttl` capped at 1 day (`_MAX_PROFILE_TTL`); never widened for H5/mini-program paths.
- `confirm_sole_bound_subject` never set by device media path; `device_bound_subject_id` only by the device's own authenticated media session.
- Device trust authority never answers `trusted` itself; only strict JSON `true` upgrades `device_bound`.
- Signing key shorter than 32 bytes raises; missing `_SESSION_API_FUNCTION_SIGNATURES` grants silently break the fence - schema changes must update them.
- Store function-signature verification is string-matching; port changes require matching grant updates.

## COMMANDS

```bash
uv run pytest services/session_runtime           # run from repo root; PG suites default 127.0.0.1:55439
make module-budget
uv run mypy services --strict && uv run ruff check services/session_runtime
```
