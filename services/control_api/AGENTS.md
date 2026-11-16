# services/control_api — Control Plane

**Score 14 — service anchor (always create).**

## OVERVIEW
FastAPI control plane for the memoria product: the only HTTP entry point for guardian/mobile apps, device firmware, and internal agent services. All other `services/*` are back-end; this service composes them.

## STRUCTURE
```
control_api/
├── app/                # domain core: authorities, ledgers, directories, runtimes (own file)
│   ├── routes/         # thin HTTP adapters, one module per surface (own file)
│   ├── config_fields/  # ControlSettings mixins, fields only — parent covers
│   └── database/       # MemoryStore: SQLite dev / PostgreSQL prod (own file)
└── tests/              # contract suite that doubles as a helper library (own file)
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| App factory, lifespan, router mounts | `app/main.py` | module-level `app = create_app()` |
| Settings, env, production invariants | `app/config.py` + `app/config_fields/` | `validate_production()` |
| HTTP surface | `app/routes/` | prefix map in `main.py` mount block |
| Persistence | `app/database/` | dual-dialect SQL, FORCE RLS |
| Test harness and fixtures | `tests/` | support modules, hermetic cwd |
| Cross-service ports | `app/*_authority`, `*_directory`, `*_runtime_control` | via `request.app.state` |

## CONVENTIONS
- Python 3.12, pydantic v2, strict mypy on `services`; ruff E,F,I,UP,B, line-length 100, E501 ignored.
- Request models: `ConfigDict(extra="forbid", str_strip_whitespace=True)`; bodies never carry actor fields.
- Errors: `HTTPException(status_code=4xx, detail={"code": "<snake_case>"})`; exact codes are the test contract.
- Dependencies fetched via `request.app.state.<port>` with per-module `_x(request)` accessors, not FastAPI `Depends`.
- Auth: `require_authenticated_user` / `mint_memoria_access_token`; internal endpoints use `X-Memoria-Internal-Token` capability tokens compared with `hmac.compare_digest`.
- Module line budgets pinned in `pyproject.toml [tool.memoria.module-budgets]`: both growth and shrinkage fail `make module-budget`.
- Policy versions are string constants (`"s2-v1"`, `"s8-v1"`, `"s9-v1"`), not semver; signed payloads are Ed25519 JWTs with domain-separated prefixes.

## ANTI-PATTERNS (THIS PROJECT)
- Never fall back silently: Redis directory never falls back to in-memory; production wiring never falls back to in-memory or client-supplied authorities; unconfigured stores fail loud.
- Never trust client-supplied actor/account/subject; derive from `AuthenticatedUser` or the voice session.
- Never a silent HS256 downgrade: the JWKS contract forbids HS256 signatures.
- Production DB roles must not be shared between control, archive, evolution, guardian, identity, and compiler.
- Never log payload content: queries, openid, tokens, message data stay out of logs.
- Capability decisions funnel through one matrix, `SUBJECT_CAPABILITY_RULES` (account_gate); no parallel rule tables.

## COMMANDS
```bash
make dev-api        # uv run uvicorn services.control_api.app.main:app --host 0.0.0.0 --port 8000 --reload
make test           # ruff check + module-budget + mypy services --strict + pytest (cov fail_under=85)
make lint / make typecheck / make module-budget
uv run pytest --import-mode=importlib --no-cov services/control_api/tests/test_readiness.py
```

## NOTES
- Single-process locks (`main.py` `growth_task_lock`, `multi_subject_runtime.py` `_lock`) carry in-code markers: a distributed deployment must replace them with advisory locks or revision projection.
- Tests are the spec: behavior-sentence names, `detail.code` literals, no xfail; PG suites gate on `MEMORIA_TEST_POSTGRES_DSN`.
