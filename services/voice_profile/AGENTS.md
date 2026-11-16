# services/voice_profile

## OVERVIEW
Versioned voice-clone profile lifecycle: consent, enrollment, clone gate, kill switch, and preview rendering over PostgreSQL. Earned this file: score 11 — 54 refs across 23 external files; distinct clone-enrollment domain, separate from services/speaker runtime classification.

## WHERE TO LOOK

| Concern | Module |
|---|---|
| Domain and ports | `domain.py` — VoiceProfilePort, DTOs (489 L) |
| Manager and clone gate | `postgres_manager.py` — most SQL/gate logic (1146 L, hotspot) |
| Enrollment flow | `postgres_enrollment.py` (581 L) |
| Clone authority | `clone_authority.py` — VoiceCloneAuthority |
| Closed enrollment | `closed_enrollment.py` — ClosedVoiceEnrollmentProvider |
| Sample validation | `sample_validation.py` |
| Preview rendering | `cosyvoice_preview.py`, `sample_url.py` (signed URLs) |
| Records and fixtures | `enrollment_records.py`, `testing_audio.py` |
| Consent gating | `postgres_schema.sql` — voice_clone_consents (policy_version, granted_at/revoked_at) |
| Tests | `tests/test_kill_switch.py`, `test_postgres_clone_gate.py`, `test_clone_authority.py` |
| Consumers | control_api voice routes, agent voice_profile_client |

## CONVENTIONS
- Schema and gate logic co-located here; no separate migration tooling.
- Clone consent checked against voice_clone_consents before enrollment; revoked consent blocks new enrollments.
- Fail-closed errors: VoiceCloneDisabledError, VoiceConsentRequiredError, VoicePreviewUnavailableError.
- `__init__.py` exports nothing; consumers import submodules (control_api, agent voice_profile_client).
- postgres_manager/postgres_enrollment hold nearly all SQL — keep in sync with postgres_schema.sql.

## ANTI-PATTERNS
- Never UPDATE rows or drop the `notice_version` default in `postgres_schema.sql` — clone validity depends on it.
- Never reissue a voice the provider may already have created (`postgres_enrollment.py`).
- Kill switch and clone gate fail closed: VoiceCloneDisabledError / VoiceConsentRequiredError raised at the gate (`postgres_manager.py`).
