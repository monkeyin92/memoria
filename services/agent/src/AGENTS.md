# Agent Runtime Core (services/agent/src)

## OVERVIEW
67 flat modules forming the agent runtime: mixin-composed `DuplexRuntime` and `ReplyPipeline`, policy/planner clients, signed profile gate, prompt and effect-commit seams. Subdirectories below have their own AGENTS.md.

## WHERE TO LOOK

| Area | Modules |
|------|---------|
| Runtime entry / 12-mixin hub | `duplex_runtime.py` (1462 L, budget 1461), `media_agent_factory.py` |
| Reply turn pipeline | `reply_pipeline.py`, `reply_*.py` mixins, `generation_output_policy.py`, `agent_voice_profile.py` |
| Signed policy inputs | `runtime_profile.py`, `runtime_profile_gate.py`, `mode_policy_client.py`, `persona_renderer.py` |
| Control clients | `response_planner_client.py`, `action_policy_client.py`, `transactional_effect_commit.py` |
| Config / prompts | `config.py` (`AgentSettings`), `prompt_composition.py`, `prompts.py` |
| Session state | `generation_records.py`, `voice_floor.py`, `classifier_inflight.py`, `event_identity.py` |
| Routers/wiring | `live_lookup_router.py`, `conversation_close_router.py`, `policy_runtime_wiring.py`, `response_plan_sidecar.py`, `weather_thread.py`, `tutor_session.py` |
| Persistence/telemetry seams | `archive_sink.py`, `obligation_executor.py`, `receipt_evidence.py`, `session_recap_sink.py` |
| Subpackages | `voice_core/` (media session), `providers/` (vendor adapters), `orchestration/` (turn planning), `contracts/` (fence/event/error types), `observability/` (metrics) |

`contracts/ids.py::GenerationFence` is the repo-wide staleness fence (~160 importing files). `observability/metrics.py::MetricsRegistry` (28 refs) owns all metric names.

## CONVENTIONS
- Mixin decomposition: `<Concern>Mixin` classes prefixed by host (`DuplexRuntime*`, `Reply*`, `Media*`); mixins declare host state only under `TYPE_CHECKING`.
- Client family: `XxxClientConfig` + `XxxClient` + module-level `parse_xxx()` wire parser; transport/shape errors map to typed `*Unavailable`; `Default*`-prefixed deny ports are production fallbacks.
- `Verified*` types are proof wrappers only the owning module factory can build.
- Env config: pydantic-settings with explicit `Field(alias="MEMORIA_*")` per field; legacy vendor names via `AliasChoices`.
- Line budgets are downward-only; current pins: `duplex_runtime.py`=1461, `reply_pipeline.py`=1379, `agent.py`=457, `providers/funasr_stt.py`=1089. Growth = split into mixins, never raise the budget.

## ANTI-PATTERNS
- The runtime never writes effects directly - only through the effect-commit port.
- `RuntimeProfile` never proves server issuance; owner/voice-speaker never substitutes for the profile subject.
- Persona definitions never carry permissions; prompt persona blocks reject permission markers.
- AIGC round logs reject child/reply text keys (`ValueError`); the reply-text debug log is off by default and subject-gated.
- Response-plan sidecar: one request, one log line, no retry.

Earned: score 14 (67 files, `DuplexRuntime` imported by 86 modules, `ModePolicy` by 40) - distinct runtime domain.
