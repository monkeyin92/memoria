---
name: verify-memoria
description: Verify Memoria's Control API (the agent-driveable control plane behind the WeChat miniprogram) by launching an isolated local instance, exercising real HTTP user paths, and capturing request/response evidence. Use when proving auth, memory journal, companion personas, archive timeline, or device catalog behavior without touching the developer's port 8000 session.
---

# Verify Memoria (Control API)

Memoria's product surfaces are ESP32-S3 voice, the WeChat miniprogram, and Control API. Agents cannot reliably drive WeChat or the robot here. This skill verifies the **Control API HTTP control plane** that the miniprogram calls — real uvicorn, real routes, disposable data — not ASGI test transports or pytest fixtures.

Secondary surfaces (out of scope for default runs; say so if asked):

- WeChat miniprogram (`apps/miniprogram`) — needs WeChat DevTools / phone.
- ESP32 voice path (Media Edge + Voice Core) — needs hardware or a separate media soak harness.
- Offline voice pipeline mock (`make offline`) — useful smoke, not the miniprogram control plane.

## Launch

From the repo root:

```bash
.cursor/skills/verify-memoria/bin/launch
```

Optional: `MEMORIA_VERIFY_PORT=18001 .cursor/skills/verify-memoria/bin/launch my-run-id`

What it does:

1. `docker compose up -d postgres redis`
2. Ensures role `memoria_dev` and database `memoria_verify` exist (verification scaffolding; not production roles)
3. Starts `uv run uvicorn services.control_api.app.main:app` on `127.0.0.1:18000` by default
4. Forces `ENVIRONMENT=development`, `OFFLINE_MOCK=true`, blank `DASHSCOPE_API_KEY`/`DEEPSEEK_API_KEY`, disposable `MEMORIA_DB_PATH` under `runs/<id>/data/`, and guardian/archive/evolution DSNs on `memoria_verify`
5. Detaches uvicorn into its own session, so the instance survives the shell call that ran `launch`; drive it from later calls
6. Writes `runs/<id>/run.env`, `control-api.pid`, and `runs/ACTIVE`; stdout is only the run id

Ready when `GET /health/live` returns `{"status":"ok"}` (launch waits up to ~90s).

**Never drive** a Control API you did not start with this launcher. Port `8000` and the developer's `.env` data dirs are off-limits.

Teardown: `.cursor/skills/verify-memoria/bin/cleanup` (see Cleanup).

## Doctor

Run first whenever anything looks off:

```bash
.cursor/skills/verify-memoria/bin/doctor
```

Checks (read-only):

- Something listens on the run's host/port
- Listener PID matches `control-api.pid` (refuses a foreign process)
- `/health/live` → 200 + `status=ok`
- Configured DB path is under this run's `data/`
- `/docs` → 200 (development-only surface; production would 404)

Exit non-zero ⇒ do not drive.

## Drive

Harness: HTTP via `bin/api` (curl wrapper with evidence capture). Prefer stable route paths and JSON fields from this repo, not UI coordinates.

```bash
BIN=.cursor/skills/verify-memoria/bin
$BIN/doctor
$BIN/api POST /v1/auth/register --evidence auth/register.json \
  -H 'Content-Type: application/json' \
  -d '{"username":"verify_user_1","password":"verify-pass-12345"}'
# Write TOKEN=... and USER_ID=... into evidence/<run>/session.env, then:
$BIN/api --token-file .cursor/skills/verify-memoria/evidence/<run>/session.env \
  GET /v1/auth/me --evidence auth/me.json
```

Conventions:

- Always `doctor` before the first drive in a session, and again after any failed drive.
- Use a **registered** account (register without a Bearer token) for feature drives so evidence matches a real user.
- `bin/api` exits 1 on non-2xx but still saves `--evidence`; negative cases need no raw curl.
- Run driver scripts under `bash`. In zsh an unquoted `$VAR` holding `--token-file path` is passed as one argument and `bin/api` rejects it.
- Capture both the mutating call and a follow-up read.
- Feature recipes live in `features/`; the map lists every entry point — do not claim a feature verified from a different path.
- `OFFLINE_MOCK=true` skips external LLM/TTS providers. Persona AI structuring (`POST /v1/personas/structuring`) may return `503 persona_structuring_unavailable`; hand-filled `POST /v1/personas` is the offline user path.

## Evidence

Proof directory (survives cleanup):

```text
.cursor/skills/verify-memoria/evidence/<run-id>/
```

Standards:

- Exercise real HTTP user paths the miniprogram uses (`apps/miniprogram/utils/api.js`), not internal `/v1/internal/*` or readiness mark endpoints.
- Capture action + resulting state (`*.json` body plus `*.meta.json` with method/path/status).
- Side effects: re-GET the resource; for SQLite-backed memory, the run's `data/memoria.sqlite3` may be inspected before cleanup if needed — prefer HTTP re-reads.
- Mocks only at production boundaries already isolated by `OFFLINE_MOCK` (providers). Do not stub Control API handlers.

## Cleanup

```bash
.cursor/skills/verify-memoria/bin/cleanup
```

Kills only the recorded listen PID for this run (refuses if the port's listener PID differs). Removes `runs/<id>/` (process state + disposable SQLite). **Does not** delete `evidence/<id>/`. Does not stop shared docker postgres/redis (other work may use them). Does not `DROP DATABASE memoria_verify` by default.

After every failed iteration, run cleanup before relaunching so ports and ACTIVE locks are not stranded.

## Helpers

All under `.cursor/skills/verify-memoria/bin/` (executable):

| Helper | Invocation | Role |
|--------|------------|------|
| `launch` | `bin/launch [RUN_ID]` | Start isolated Control API; print run id |
| `doctor` | `bin/doctor [RUN_ID]` | Read-only instance check |
| `api` | `bin/api METHOD PATH [--evidence rel] [-H/-d …]` | Authenticated drive + evidence |
| `cleanup` | `bin/cleanup [RUN_ID]` | Stop our PID; keep evidence |

Shared env: `bin/common.sh` (sourced, not run). Active run id: `runs/ACTIVE` or `MEMORIA_VERIFY_RUN_ID`.

## Feature map

See [`features/README.md`](features/README.md). Start with **Account auth** for the prove-the-skill loop; cover other features in later runs.
