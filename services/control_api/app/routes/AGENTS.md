# app/routes — HTTP adapters

**Score 14 — distinct domain: thin FastAPI routers; domain rules live in services, never here.**

## OVERVIEW
One module per surface; every module exports `router`, `main.py` mounts all ~24 with `include_router`.

## STRUCTURE
- No router-level prefix (absolute `/v1/...` paths): `multi_subject.py`, `identity_lifecycle.py`, `device_onboarding.py`, `readiness.py` — never add a prefix without rewriting paths.
- Extra routers: `media.py` (`device_router` `/v1/devices`, `internal_router` `/v1/internal/media-runtime`), `device_control.py` (`internal_router` `/v1/internal`), `guardian.py` re-includes `crisis_script_interrupt.router`.
- Shared prefixes: `/v1/devices` = `device_control` + `persona_assignment` + `media.device_router`; `/v1/digital-self` = `digital_self` + `self_preview`.
- Compact map: `interaction` `/v1/interaction` · `guardian` `/v1/guardian` · `archive` `/v1/archive` · `session` `/v1/sessions` · `media` `/v1/media` · `auth` `/v1/auth` · `memory` `/v1/memory` · `growth` `/v1/growth` · `persona` `/v1/persona` · `custom_personas` `/v1/personas` · `voice` `/v1/voices` · `account_insights` `/v1/account` · `child_session_recap` `/v1/internal/child-memory`.

## WHERE TO LOOK
| Surface | Module |
|---------|--------|
| Interaction/response-plan/crisis routing | `interaction.py` |
| Guardian consent, links, recap, push | `guardian.py` |
| Archive event ingest, lifecycle, deletion status | `archive.py` |
| Device binding, runtime-profile switching | `multi_subject.py` |
| Direct device media sessions, SLO reports | `media.py` |
| Session create/stop, omni SDP, stop requests | `session.py` |
| Registration, binding members, unbind, purge | `identity_lifecycle.py` |
| Device settings, firmware, close | `device_control.py` |
| Device bootstrap/claims/onboarding | `device_onboarding.py` |
| Health, smoke evidence, agent heartbeat | `readiness.py` |

## CONVENTIONS
- Thin adapters by contract (stated in module docstrings): domain rules live in `IdentityService`, `DeviceOnboardingService`, `SubjectDeletionService`, `GuardianConsentService`, `BoundSubjectConsentService`.
- Responses frequently `JSONResponse` / `dict[str, Any]` (archive, guardian); typed `*Response` models elsewhere.
- Header seams: `Idempotency-Key` (mutation idempotency), `X-Media-Reply-Delivery-Token`, `X-Device-Certificate-ID`, `X-Client-ID`, `X-Memoria-Evolution-Protocol`; HMAC comparisons via `hmac`.
- Module-level mutable state exists only in `session.py` (`_STOP_REQUESTS`, `_STOP_IDEMPOTENCY`, `_STOP_DISPATCH_PENDING`), reset by exported `reset_session_state()`.
- ~137 `cast()` calls across the 7 core files satisfy mypy strict for `request.app.state` accessors.
- Async fan-out: `asyncio.create_task` (interaction response plan), `BackgroundTasks` (archive session events), `httpx.AsyncClient` (session omni SDP).

## ANTI-PATTERNS
- Never copy the "deprecated but accepted" input fields into new endpoints (`purge_subject_data` kept only so older clients are not 422ed; `interaction.py` session-policy field).
- Never accept actor from the caller: always derived from `AuthenticatedUser.user_id` or the voice session.
- Memory query subject never defaults to `None` (None means the whole account); query text never enters the log.
- Crisis turn must not reuse a non-crisis snapshot; crisis notification failure never suppresses the fixed safety reply.
- Minor and unknown speakers never persist verbatim turn text; missing identity is never the account owner.
- A concurrent media reconnect that won the directory CAS must never be overwritten; activation manifest/ledger is diagnostic only, never overrides the projection.
- Guardian unbinding has no ACTIVE-binding prerequisite (must never strand); recap is model words, never the child's verbatim transcript.
- Derived binding roles never pass back as extra roles; a new version's `valid_from` must not predate the superseded version.
- `noqa: BLE001` allowed only in cleanup paths that must not mask the original failure.

## NOTES
- All 7 core files >1000 lines; budgets pin `interaction.py` 1495, `guardian.py` 1471, `archive.py` 1451, `media.py` 1289, `session.py` 1255 — downward-only.
- `multi_subject.py` (1390) and `identity_lifecycle.py` (1181) are unlisted but under the 1500-line discovery threshold.
- Largest functions: `session.create_session` 517, `media._create_direct_device_media_session` 398, `archive.append_session_event` 333, `interaction.response_plan` 263.
