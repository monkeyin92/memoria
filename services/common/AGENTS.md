# services/common

## OVERVIEW
Shared service-layer helpers: deterministic reply copy, safety and grounding policy, voice/engine constants, and shipped audio metadata. No I/O, no LLM calls. Earned this file: score 12 — 55 py files, 385 external refs (`fixed_safety_scripts` alone has 65).

## WHERE TO LOOK

| Concern | Module(s) |
|---|---|
| Crisis/self-harm fixed scripts | `fixed_safety_scripts.py` — script_for_decision/display, echo reference |
| Companion identity and voices | `companions.py` — COMPANIONS, voice profiles, style version |
| Deterministic safety replies | `companion_response_safety.py` — identity/affection/soothing, never LLM |
| Crisis routing | `crisis_policy.py` — route_crisis, semantic verdicts |
| PII redaction | `redaction.py` — redact_pii, before context/persistence/third-party send |
| Weather/time fixed replies | `realtime_information.py` — product copy pinned by tests |
| Memory promises | `explicit_memory.py` — shared with services/archive/memory_write_policy.py |
| Memory decision contract | `memory_decision_contract.py` — contract + product promise matrix |
| Story grounding and volume | `story_grounding.py` — local story IP canon, volume commands |
| AIGC opus audit | `aigc_opus.py` — tag audit, rewrites OpusTags comments only |
| Playable line catalogue | `labelled_lines.py` — only Doubao-rendered lines from provenance.json |
| Engine/voice literals | `voice_engines.py`, `tts_defaults.py`, `tts_voice_policy.py` — single source for agent + control settings |
| Backup LLM gate | `llm_failover.py` — Beijing DashScope only, gate_backup_reply_copy |
| Budget/depth policy | `daily_chat_limit.py`, `session_length.py`, `response_depth.py`, `chat_llm_providers.py` |
| Shipped audio | `assets/labelled_lines/` (ogg + s16le), `assets/offline_audio_manifest.json` (sha256/LUFS) |
| Tests | `tests/test_<module>.py`, flat, pure unit |

## CONVENTIONS
- Product copy lives in these modules; tests pin exact strings (e.g. weather-fallback-v1).
- Runtime tuples derived from `Literal` via `get_args(...)`; `Final` constants; frozen+slots dataclasses.
- `__init__.py` exports nothing; consumers import submodules.
- Module docstrings state the invariant the module protects.
- Largest modules (companion_response_safety 812 L, story_grounding 691 L, aigc_opus 540 L) have no module-budget rows.

## ANTI-PATTERNS
- Fixed safety and companion replies must never rely on an LLM; crisis copy must never reach a live engine.
- Retired env keys rejected with named replacements in `voice_engines.py`; never two keys for one choice.
- Personal clone voices (`seed-icl*`, `S_*`) never applied to the device backup voice.
- Provider-embedded Opus marks never stripped; scaffold tones and TITLE placeholders never enter the catalogue.
- Beijing DashScope is the only allowed backup LLM base URL.
- Bare self-absence (「不在家」「不在学校」) is not a crisis route trigger.
- Never promise more memory persistence than policy delivers.
- DEV_* secrets in `security_constants.py` are dev-only (`-do-not-use-in-production`).
