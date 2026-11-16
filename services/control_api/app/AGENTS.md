# app — control-plane domain core

**Score 14 — distinct domain: business logic with no HTTP and no SQL; `routes/` and `database/` have their own files.**

## OVERVIEW
Flat package of authorities, ledgers, directories, and runtime controls wired into the FastAPI app by `main.py`; `__init__.py` is intentionally empty.

## STRUCTURE
```
app/
├── main.py / config.py            # app factory, wiring, settings
├── config_fields/                 # 8 field mixins composing ControlSettings (no logic)
├── routes/  database/             # separate files
├── *_runtime* / *_directory* / *_authority* / *_ledger*   # domain modules
└── small shards                   # one concern per file (~30-230 lines)
```

## WHERE TO LOOK
| Need | Module |
|------|--------|
| App factory, lifespan, `_wire_services` | `main.py` |
| Settings, env, `validate_production()` | `config.py` |
| Session directory CAS/epoch/ownership | `session_directory.py` |
| Subject switch, runtime profile lifecycle | `multi_subject_runtime.py` |
| Mode/capability policy (`s2-v1`/`s8-v1`/`s9-v1`) | `mode_policy.py` |
| Device settings, acoustic ledger, profiles | `device_control.py` |
| Archive event provenance conflicts | `archive_event_conflicts.py` |
| Session close pipeline | `device_session_close.py` |
| Media tickets / stream tokens | `media_runtime.py` |
| WeChat push; guardian recap | `guardian_push.py`; `guardian_recap.py` |
| Auth guards, access tokens | `security.py` |
| Firmware release validation; update tracking | `device_firmware.py`; `firmware_update.py` |
| Subject deletion / governance wiring | `governance_wiring.py` |
| Memory-scope authority | `memory_scope_authority.py` |

## CONVENTIONS
- Naming: `*Authority` decides, `*Ledger` records, `*Directory` maps, `*RuntimeControl` orchestrates, `*Port` protocols; domain errors subclass stdlib exceptions (`PermissionError`, `ValueError`, `RuntimeError`).
- `ControlSettings` = multiple inheritance over `config_fields/*` mixins (`MediaFields` … `AuthFields`); `validate_production()` is a ~900-line method raising one `ValueError` per production invariant, called explicitly at startup.
- Service graph: `Wiring` frozen dataclass; eager (SQLite, tests) vs live (PostgreSQL, lifespan); `run_eagerly` must never suspend.
- Shared Postgres pools per DSN (`SharedPostgresPools`, max_size 20, command timeout 15 s), closed newest-first via `LifespanResources`.
- Dataclasses `frozen=True, slots=True` for value objects; `logger = logging.getLogger(__name__)`, never `print`.
- Half-signed tokens are hand-rolled HMAC-SHA256 (no PyJWT), e.g. `device_binding_token` `v1.<b64>.<b64>`; signing prefixes domain-separated (`b"memoria-device-display-profile-v1\0"`, `b"memoria-firmware-release-v1\n"`).

## ANTI-PATTERNS
- Runtime must not invent a second runtime; session ids are never reused; a binding member must not take a second profile.
- Legacy shell evidence fields are never copied directly into archive evidence; evidence payloads stay under 64 KiB.
- Custom persona contributes controlled fields only — never free text, never facts/identity/permissions.
- H5 stream tokens can never open a device socket; device tickets can never be replayed against WHIP; dev fallback secrets never used in prod.
- A display-profile signature never authenticates the endpoint or the reverse; `wake_word_display` must not be empty.
- Guardian recap never quotes the child verbatim and is never stored; push logging never includes openid, token, or message data.
- Firmware restart loses history and never implies success.
- Idempotent archive duplicates never reach conflict canonicalization; claim certainty derives from verified source kinds, never caller labels.

## NOTES
- Hotspots >500 lines: `main.py` 1077, `multi_subject_runtime.py` 1073, `config.py` 983, `archive_event_conflicts.py` 901, `session_directory.py` 855, `device_control.py` 740, `mode_policy.py` 647.
- `main.py` `_wire_services` spans ~400 lines; `config.py` `validate_production` is the production-role-separation cluster.
