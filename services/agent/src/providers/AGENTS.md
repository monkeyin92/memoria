# Provider Adapters (services/agent/src/providers)

## OVERVIEW
39 vendor adapter modules: streaming ASR (Volcengine, DashScope FunASR, SenseVoice rescue), TTS (StepFun primary, Doubao/CosyVoice backup), LLM (DeepSeek, OpenAI), weather, and semantic classifiers - all behind typed config + failover.

## WHERE TO LOOK

| Area | Modules |
|------|---------|
| ASR chain | `asr_failover.py` (per-segment failover + breaker), `volc_asr.py`, `funasr_stt.py`, `funasr_connection.py`, `sensevoice.py`, `asr_session.py` (Protocol) |
| TTS chain | `tts_failover.py` (`StepFunPrimaryTTS`), `stepfun_tts.py`, `doubao_tts.py`, `doubao_presynthesis.py`, `cosyvoice_tts.py`, `tts_factory.py`, `fixed_safety_audio.py` |
| LLM | `llm_failover.py`, `deepseek.py`, `openai_chat.py`, `handlers.py`, `deepseek_live_search.py`, `qwen_realtime_search.py` |
| Protocol codecs | `volc_protocol.py`, `doubao_protocol.py`, `funasr_protocol.py`, `cosyvoice_protocol.py`, `tts_stream.py` |
| Shared primitives | `provider_errors.py`, `reliability.py` (`CircuitBreaker`), `generation_budget.py`, `doubao_tts_support.py` (`DoubaoAuthLatch`) |
| Semantic classifiers | `crisis_semantic_classifier.py`, `live_lookup_semantic_classifier.py`, `close_intent_semantic_classifier.py`, `decision_model.py` |
| Data | `weather_places.py` (city alias table), `domestic_weather.py` (QWeather), `doubao_voice_catalog.py`, `cosyvoice_voice_catalog.py` |

## CONVENTIONS
- Configs are dataclasses with `__post_init__` range validation and `@classmethod from_env(env=None)`; env reads go through `_DeclaredEnv` against `ENV_KEYS: ClassVar[frozenset[str]]`.
- PCM contract: 24 kHz mono TTS (configs reject other rates), 16 kHz mono ASR.
- Broad classes split into mixin files (`funasr_connection.py`, `funasr_failures.py`, `doubao_presynthesis.py`) with a docstring naming the composing class.
- Module docstrings carry protocol citations with checked dates and measured-latency rationale.
- Callable-from-outside modules define `__all__`.

## ANTI-PATTERNS
- Failover is per VAD segment, never concatenated; degradation lasts one segment; no second provider selector.
- Doubao never speaks a crisis turn; only a complete take is forwarded - a half line never joins a whole line.
- Credentials come from the environment only; nothing logs a token; auth headers are built at factory time to fail the deploy, not sessions.
- DeepSeek never sends `parallel_tool_calls`; only content (never reasoning) reaches TTS; forbidden body fields are rejected.
- Post-audio timestamp failure is terminal, never retried; cancelled/failed pool connections are never returned to the pool.

Earned: score 11 (39 files, ~30-module vendor surface consumed across the agent) - distinct adapter domain.
