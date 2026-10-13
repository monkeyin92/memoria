# Memoria Control API verification map

This directory is the maintained source for verifying user-facing Control API behavior that backs the WeChat miniprogram. Read the index before driving, then use the matching feature file as the recipe.

## Baseline preconditions

- Launch with `.cursor/skills/verify-memoria/bin/launch` (default `http://127.0.0.1:18000`).
- Run `.cursor/skills/verify-memoria/bin/doctor` and require PASS.
- Disposable SQLite lives under `runs/<id>/data/`; guardian/archive use Postgres DB `memoria_verify`.
- Never drive port `8000` or any instance not started by `bin/launch`.
- Prefer a freshly registered account per feature that mutates data.

## Driving conventions

- Harness command prefix: `.cursor/skills/verify-memoria/bin/api`.
- Stable handles are route paths and JSON field names from `apps/miniprogram/utils/api.js` / Control API routers.
- Start every recipe from baseline unless its preconditions say otherwise.
- After mutations, re-GET the resource; status alone is insufficient proof.
- Write evidence under `evidence/<run-id>/` via `--evidence`; cleanup must not remove it.
- `OFFLINE_MOCK=true` is expected; provider-backed structuring/summary may fall back or 503.

## Proof and skip reporting

- Capture the user action and the resulting state (request meta + body).
- HTTP proof includes status, response JSON, and a follow-up read where applicable.
- Record the feature ID and entry point with every artifact (folder name = feature).
- Report an unreachable path with the attempted command and unmet precondition.
- Do not report a skipped entry point as verified through a different path.

## Feature entry contract

Each feature file starts with an H1 and one paragraph of user-visible behavior, then exactly four H2s:

1. `Sub-features`
2. `How to get to it (user POV)`
3. `Driving it with bin/api`
4. `Gotchas`

## Features

- [Account auth](./account-auth.md) — anonymous session, register/login, `/v1/auth/me`.
- [Memory journal](./memory-journal.md) — profile, companion selection, messages, daily list/summary (回顾 / 我的 / 伙伴).
- [Companion personas](./companion-personas.md) — server built-in catalogue and custom persona create/list/delete.
- [Archive timeline](./archive-timeline.md) — archive read gate (401/403); the 200 path needs an adult subject via WeChat.
- [Device wake catalog](./device-wake-catalog.md) — wake-word catalogue and custom wake-word validation.

Not yet mapped (user-facing, needs its own recipe): voice consent and sample revoke on 「隐私」/「我的」 (`apps/miniprogram/pages/privacy`, `/v1/archive/raw-voice-consent`, `/v1/voices/...`), and guardian minor management (`pages/guardian`, `/v1/guardian/...`).
