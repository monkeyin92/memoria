# services/speaker

## OVERVIEW
Persistent three-state speaker authority: enrollment, embedding classification, and owner/guest voice gates for the runtime voice loop. Earned this file: score 11 — `SpeakerDecision`/`permissions_for_speaker` imported at 50 sites (agent, control_api, session_runtime).

## WHERE TO LOOK

| Concern | Module |
|---|---|
| Decision and port contracts | `domain.py` — SpeakerDecision, permissions_for_speaker, SpeakerAuthorityPort, SpeakerEmbeddingAdapter |
| Enrollment DTOs | `domain.py` — EnrollmentRequest/Result/Sample, EmbeddingResult, EnrollmentQualityError, SpeakerProfileNotFoundError |
| SQLite authority (local/dev) | `authority.py` — SpeakerAuthority.sqlite, Fernet-encrypted templates |
| Production authority | `postgres_authority.py` — PostgresSpeakerAuthority |
| Quality thresholds, similarity | `policy.py` — hard-coded: speech_ms<800, snr_db<8, quality<0.5, replay_risk>=0.5; cosine/template similarity |
| CAM++ HTTP adapter | `campplus_http.py` — CampPlusHTTPEmbeddingAdapter, UnavailableSpeakerEmbeddingAdapter |
| DDL | `postgres_schema.sql` — speaker_identities, speaker_profiles (status shadow/active/revoked) |
| Tests | `tests/test_policy.py` — only test module |
| Consumers | agent voice_core (speaker_verify, orchestration), control_api, session_runtime; speaker_model/app.py |

## CONVENTIONS
- Ports-and-adapters: `SpeakerAuthorityPort` Protocol + SQLite (`authority.py`) and Postgres (`postgres_authority.py`) implementations.
- Production authority is PostgresSpeakerAuthority; SQLite is local/dev only.
- Thresholds in policy.py are hard-coded constants, not env-driven settings.
- Profile status is three-state shadow/active/revoked (schema CHECK constraint).
- Enrollment/classify logic is duplicated between the two authorities — edit both or extract (known hotspot).
- `__init__.py` exports nothing; consumers import submodules.
- Frozen+slots DTOs; `SpeakerDecision` raises ValueError unless permissions match the classification.

## ANTI-PATTERNS
- Device-bound claims are never voice authority; robot echo must never pass an owner-voice gate (`domain.py` `is_voice_authority`).
- Shadow candidates are not formal decisions; shadow output never changes authority.
- DEVICE_BOUND_SUBJECT_REASON/MODEL kept so gates recognize legacy claims — not voice evidence.
