# services/device_fleet

**Score: 24 (36 py files, >30 symbols, wide fan-in). Device fleet + bootstrap onboarding bounded context.**

## OVERVIEW

Device trust, binding lifecycle, remote commands/OTA and device onboarding (QR bootstrap -> binding) for physical Memoria devices. Stores: asyncpg Postgres (prod) + SQLite (dev/test only).

## WHERE TO LOOK

| File | Role |
|---|---|
| `service.py` | `DeviceFleetService` facade; composes Trust/SimAuthority/RemoteCommand mixins |
| `bootstrap_service.py` | `DeviceOnboardingService`, `install_device_onboarding`, QR encode |
| `bootstrap_domain.py` | Wire/domain primitives: state machine, QR/PoP payloads, 28-class exception tree; zero framework deps |
| `bootstrap_store.py` / `bootstrap_postgres_store.py` | Onboarding store ports (SQLite dev / asyncpg prod) |
| `trust.py`, `sim_authority.py`, `remote_commands.py` | Mixin seams composed by the facade |
| `authority.py`, `crypto.py` | Command authorization, attestation/command/OTA signature helpers |
| `postgres_store.py`, `bootstrap_*.py` mixins | RLS store, rows, schema, session mixins |
| `postgres_schema.sql`, `bootstrap_postgres_schema.sql` | DDL: `device_fleet_*` + `device_onboarding_*`; RLS, roles, SECURITY DEFINER functions |
| `dispatcher.py`, `binding_release.py` | Remote dispatch worker; binding release |
| `scripts/provision_dev_device.py`, `scripts/register_test_device.py` | Dev provisioning; test registration (see tests/AGENTS.md) |

## CONVENTIONS

- Facades split by mixin seams; shared state declared `TYPE_CHECKING`-only, no import cycles.
- Sync onboarding API over asyncpg: private event-loop thread in `bootstrap_sync.py`; Control API handlers are sync.
- Domain/wire layer dependency-free; exact frozenset key sets, canonical JSON, reject duplicates.
- TTLs, `minimum_firmware_security_version`, `now_fn` constructor-injected; bad values raise `ValueError`.
- Errors are `OnboardingError` subclasses with class-level `code`/`status_code`.
- Wire versions explicit: `_QR_PREFIX = "memoria-bootstrap:v1:"`; hash decoded nonce/PoP bytes.
- Postgres roles never own tables, never run DDL; RLS FORCE with actor/device/lookup scope per transaction.
- Onboarding schema namespace separate (`device_onboarding_*`), forward-only, idempotent DDL.

## ANTI-PATTERNS

- **Module budgets exact-match**: `service.py`=1064, `bootstrap_store.py`=1285, `bootstrap_postgres_store.py`=1280 in `[tool.memoria.module-budgets]`. Any edit here requires `make module-budget` (`update` is downward-only) - move code to a mixin instead.
- No method accepts Wi-Fi creds, device private keys, or raw QR/PoP after verification; private key is never a store argument (public keys only).
- QR nonce never returned after introspection; proof carries only a hash.
- SQLite store is dev-only: `OFFLINE_MOCK` and local tests, never prod.
- Device attestations never mutate SIM authority rows (server-owned).
- Adapter fails closed: missing actor/device/lookup scope -> `PostgresBootstrapStoreContextError`.
- Trust callback receives authority facts only; it never grants.

## COMMANDS

```bash
uv run pytest services/device_fleet/tests            # PG suites need MEMORIA_TEST_POSTGRES_DSN (default 127.0.0.1:55439)
make module-budget                                     # exact-match check; update = downward-only
uv run mypy services --strict && uv run ruff check services/device_fleet
```
