# services/persona — persona trait engine & custom personas (score 12)

## OVERVIEW
Persona learning: deterministic rule extractor + Qwen LLM extractor feed a trait lifecycle (observe→review→publish version) in Postgres; plus structured custom-persona envelopes.

## WHERE TO LOOK
| Task | Location |
|------|----------|
| Traits, versions, consent + `PersonaEnginePort` | `domain.py` — 11-method Protocol; `PersonaRequest.effective_subject_id` |
| Rule extraction + auto-promote | `rules.py` — TICS, `OWNER_AUTO_PROMOTE_WEIGHT=2.7`, `UNCERTAIN_AUTO_PROMOTE_WEIGHT=5.4`, `exclusive_auto_promote_target` |
| LLM extraction | `qwen_extractor.py` — `QwenPersonaExtractor` (`qwen-persona-json:<model>:v2`) |
| PG engine (lifecycle) | `postgres_engine.py` — `PostgresPersonaEngine` (`observe`, `review`, `rollback`, `capsule`); 1218 L hotspot: `observe` 150-453, `_publish_version` 691, `rollback` 802 |
| Capsule composition | `capsule.py` — `persona_capsule_from_snapshot` (single composer) |
| Custom persona envelope | `custom_persona_fields.py` — `StructuredPersona` (extra=forbid), `companion_definition_from_envelope` |
| LLM structuring | `custom_persona_structurer.py` — `QwenCustomPersonaStructurer` |
| Package surface | `__init__.py` — docstring only, no `__all__`; import direct submodules |
| Tests | `tests/` — 8 modules; PG fixtures connect as `memoria_app` (archive DSN role), skip without `MEMORIA_TEST_POSTGRES_DSN`; persona data PostgreSQL-only |
| External consumers | control_api `text_models`/`session_companion`/`subject_persona`/`persona_learning`/routes `persona`/`custom_personas`/`interaction`/`readiness`; agent `mode_policy_client`; digital_self registry + growth reader (`LEGACY_COGNITIVE_TRAIT_CATEGORIES`) |
| PG schema | `postgres_schema.sql` — FORCE RLS per table; backfill window temporarily lifts FORCE RLS then re-enables |

## CONVENTIONS
- Traits keyed `(account_id, subject_id)`; account holder keeps legacy `persona_trait_key` formula; per-account advisory lock; RLS via `set_config('app.account_id', $1, true)`.
- Pydantic v2 everywhere: `extra="forbid"`, bounded fields; out-of-domain LLM output → 422 error, never clamped.
- Capsules composed only by `persona_capsule_from_snapshot`; `PostgresPersonaEngine` implements the Protocol structurally, extractors are sync, engine is asyncpg.
- Chinese user-facing prompt fragments inline; extractor version strings embedded (`persona-rules-zh-v1`, `persona-structuring-v1`).

## ANTI-PATTERNS
- `decision_habit`/`value_priority` traits require a counterexample (`require_persona_counterexample`); drop unevidenced, reject the rest.
- Minor persona learns style only — never decision habits or value priorities.
- Never read another subject's persona; capsule requires owner or confirmed-style-only uncertain speaker.
- Persona changes apply at `next_session` — never cut a conversation mid-reply.
- Companion binding default is never changed to apply a persona pick; per-subject overrides only.
- Persona rows are versioned via `persona_versions` and intentionally mutable — no immutable-row triggers here (unlike legacy/digital_self); version history is the audit trail.
