"""Agent runtime configuration with startup validation (ch.6.1)."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.agent.src.contracts.errors import ConfigValidationError
from services.common.llm_failover import (
    DASHSCOPE_CN_COMPATIBLE_BASE_URL,
    dashscope_endpoint_in_use,
    normalize_dashscope_compatible_base_url,
)
from services.common.tts_defaults import (
    DOUBAO_TTS_FALLBACK_SPEAKER,
    STEPFUN_TTS_MODEL,
    STEPFUN_TTS_VOICE,
)
from services.common.voice_engines import ASREngine, TTSEngine

DeploymentProfile = Literal["livekit_cloud", "cn_self_hosted"]
LLMProvider = Literal["qwen", "bailian_deepseek", "deepseek"]
WeatherLookupProvider = Literal["off", "domestic"]
_QWEATHER_HOST = re.compile(r"(?:[a-z0-9-]+\.)+qweatherapi\.com")
DEFAULT_LLM_BACKUP_MODEL = "qwen3.8-flash"
DEFAULT_LLM_FIRST_TOKEN_TIMEOUT_S = 5.0
DEFAULT_LLM_INTER_TOKEN_TIMEOUT_S = 4.0

SELF_HOSTED_ENDPOINTING_MIN_DELAY_S = 1.50
SELF_HOSTED_ENDPOINTING_MAX_DELAY_S = 2.20
SELF_HOSTED_FALSE_INTERRUPTION_TIMEOUT_S = 1.70


def load_turn_timing(
    profile: DeploymentProfile | str,
    env: Mapping[str, str] | None = None,
) -> tuple[float, float, float]:
    """Read and validate the turn timing values used by LiveKit."""

    values = os.environ if env is None else env
    self_hosted = profile == "cn_self_hosted"
    defaults = (
        (
            SELF_HOSTED_ENDPOINTING_MIN_DELAY_S,
            SELF_HOSTED_ENDPOINTING_MAX_DELAY_S,
            SELF_HOSTED_FALSE_INTERRUPTION_TIMEOUT_S,
        )
        if self_hosted
        else (0.30, 2.00, 1.20)
    )
    try:
        min_delay = float(values.get("ENDPOINTING_MIN_DELAY_S", str(defaults[0])))
        max_delay = float(values.get("ENDPOINTING_MAX_DELAY_S", str(defaults[1])))
        false_timeout = float(values.get("FALSE_INTERRUPTION_TIMEOUT_S", str(defaults[2])))
    except ValueError as exc:
        raise ValueError("turn timing values must be finite numbers") from exc
    if not all(
        math.isfinite(value) and value > 0
        for value in (
            min_delay,
            max_delay,
            false_timeout,
        )
    ):
        raise ValueError("turn timing values must be finite positive numbers")
    if min_delay > max_delay:
        raise ValueError("ENDPOINTING_MIN_DELAY_S must be <= ENDPOINTING_MAX_DELAY_S")
    if false_timeout < min_delay:
        raise ValueError("FALSE_INTERRUPTION_TIMEOUT_S must be >= ENDPOINTING_MIN_DELAY_S")
    return min_delay, max_delay, false_timeout


def _secure_internal_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme == "https" or (
        parsed.scheme == "http"
        and parsed.hostname in {"control-api", "agent", "localhost", "127.0.0.1", "::1"}
    )


def _valid_websocket_url(value: str, *, require_tls: bool) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    schemes = {"wss"} if require_tls else {"ws", "wss"}
    return (
        parsed.scheme in schemes
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and "#" not in value
    )


def validate_doubao_auth(
    *,
    api_key: str,
    app_id: str,
    access_token: str,
    required: bool,
) -> None:
    """Require one complete Doubao authentication mode without silent precedence."""

    has_api_key = bool(api_key.strip())
    has_app_id = bool(app_id.strip())
    has_access_token = bool(access_token.strip())
    has_app_pair = has_app_id and has_access_token
    if (
        has_app_id != has_access_token
        or (has_api_key and (has_app_id or has_access_token))
        or (required and not (has_api_key or has_app_pair))
    ):
        raise ValueError(
            "Doubao TTS requires exactly one complete authentication mode: API Key or "
            "App ID plus Access Token"
        )


def pcm_tap_directory(environment: str, configured: str) -> str | None:
    directory = configured.strip()
    if not directory:
        return None
    if environment != "development":
        raise ValueError(
            "MEDIA_PCM_TAP_DIR is development-only: the tap writes unencrypted device audio"
        )
    return directory


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = Field(default="development", alias="ENVIRONMENT")
    deployment_profile: DeploymentProfile = Field(
        default="livekit_cloud", alias="DEPLOYMENT_PROFILE"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    llm_provider: LLMProvider = Field(default="qwen", alias="LLM_PROVIDER")
    tts_provider: TTSEngine = Field(default="doubao", alias="TTS_PROVIDER")
    asr_provider: ASREngine = Field(default="funasr", alias="ASR_PROVIDER")
    stepfun_tts_enabled: bool = Field(default=False, alias="STEPFUN_TTS_ENABLED")
    stepfun_tts_model: str = Field(
        default=STEPFUN_TTS_MODEL,
        min_length=1,
        alias="STEPFUN_TTS_MODEL",
    )
    stepfun_tts_voice: str = Field(
        default=STEPFUN_TTS_VOICE,
        min_length=1,
        alias="STEPFUN_TTS_VOICE",
    )
    # Approved fixed-safety and daily-limit copy plays from pre-synthesized
    # 24 kHz mono clips. A failed manifest or file falls back to the live TTS
    # chain with an alert; the image gate rejects a build without the clips.
    fixed_safety_audio_enabled: bool = Field(
        default=True, alias="FIXED_SAFETY_AUDIO_ENABLED"
    )
    fixed_safety_audio_dir: str = Field(
        default="infra/voices/fixed_safety", alias="FIXED_SAFETY_AUDIO_DIR"
    )
    # Live TTS order only when a stored clip is missing or fails its hash:
    # true starts on StepFun, false starts on Doubao. The clip always plays
    # before either engine.
    fixed_safety_primary_first: bool = Field(
        default=True, alias="STEPFUN_TTS_FIXED_SAFETY_PRIMARY_FIRST"
    )

    media_bridge_grpc_enabled: bool = Field(
        default=False, alias="MEDIA_BRIDGE_GRPC_ENABLED"
    )
    media_bridge_grpc_addr: str = Field(
        default="127.0.0.1:7001", alias="MEDIA_BRIDGE_GRPC_ADDR"
    )
    media_bridge_mtls: bool = Field(default=False, alias="MEDIA_BRIDGE_MTLS")
    media_bridge_tls_cert_file: str = Field(
        default="", alias="MEDIA_BRIDGE_TLS_CERT_FILE"
    )
    media_bridge_tls_key_file: str = Field(
        default="", alias="MEDIA_BRIDGE_TLS_KEY_FILE"
    )
    media_bridge_client_ca_file: str = Field(
        default="", alias="MEDIA_BRIDGE_CLIENT_CA_FILE"
    )
    media_bridge_max_pending_audio_frames: int = Field(
        default=20, ge=1, le=1000, alias="MEDIA_BRIDGE_MAX_PENDING_AUDIO_FRAMES"
    )
    media_bridge_max_pending_messages: int = Field(
        default=128, ge=1, le=2000, alias="MEDIA_BRIDGE_MAX_PENDING_MESSAGES"
    )
    media_output_generation_timeout_s: float = Field(
        default=45.0,
        ge=0.1,
        le=300.0,
        alias="MEDIA_OUTPUT_GENERATION_TIMEOUT_S",
        description=(
            "Stall watchdog for one generation provider iterator. Each accepted "
            "PCM chunk resets it; it is not a wall-clock cap on reply duration."
        ),
    )
    media_owner_silence_timeout_s: float = Field(
        default=180.0,
        ge=1.0,
        le=300.0,
        alias="MEDIA_OWNER_SILENCE_TIMEOUT_S",
    )
    media_listening_capture_window_s: float = Field(
        default=30.0,
        ge=0.0,
        le=300.0,
        alias="MEDIA_LISTENING_CAPTURE_WINDOW_S",
    )
    # Hard upper bound for one device utterance when the edge never emits VAD
    # end.  Registry direct/library callers retain their own disabled (0s)
    # default; normal AgentSettings-based bridge startup uses a bounded 60s
    # default and can explicitly set zero to disable it.
    media_max_user_speech_duration_s: float = Field(
        default=60.0,
        ge=0.0,
        le=600.0,
        alias="MEDIA_MAX_USER_SPEECH_DURATION_S",
    )
    # Compliance 2026-10-20: a device reply the server still believes is
    # speaking, with no accepted downlink PCM for this long and no generation
    # terminal, is closed with a CANCEL plus standby so the device stops
    # feeding the microphone.  Only the downlink resets it; provider
    # production does not.  Registry direct/library construction keeps its own
    # disabled (0s) default; the bridge wires this value explicitly.
    media_speaking_stall_timeout_s: float = Field(
        default=15.0,
        ge=0.0,
        le=120.0,
        alias="MEDIA_SPEAKING_STALL_TIMEOUT_S",
        description=(
            "Seconds without an accepted downlink PCM frame after which a "
            "speaking device reply is cancelled and the session stood by. "
            "Field evidence: longest healthy reply ~12 s, downlink frame "
            "interval 20 ms."
        ),
    )
    media_dtln_tail_confidence_enabled: bool = Field(
        default=False,
        alias="MEDIA_DTLN_TAIL_CONFIDENCE_ENABLED",
    )
    # AIGC notice hooks at the start and end of one device conversation round.
    # Off by default since 2026-10-24: the opening and closing tones are not
    # played unless a deployment sets AIGC_NOTICE_ENABLED. The mode and rhythm
    # stay configured, so turning it back on restores them unchanged.
    aigc_notice_enabled: bool = Field(default=False, alias="AIGC_NOTICE_ENABLED")
    aigc_notice_mode: Literal["tone", "speech", "none"] = Field(
        default="tone", alias="AIGC_NOTICE_MODE"
    )
    aigc_notice_opening_enabled: bool = Field(
        default=True, alias="AIGC_NOTICE_OPENING_ENABLED"
    )
    aigc_notice_closing_enabled: bool = Field(
        default=True, alias="AIGC_NOTICE_CLOSING_ENABLED"
    )
    aigc_notice_tone_pattern: str = Field(
        default="SL SS", alias="AIGC_NOTICE_TONE_PATTERN"
    )
    aigc_notice_tone_frequency_hz: float = Field(
        default=880.0, gt=0.0, le=8000.0, alias="AIGC_NOTICE_TONE_FREQUENCY_HZ"
    )
    aigc_notice_tone_short_ms: int = Field(
        default=120, ge=1, le=2000, alias="AIGC_NOTICE_TONE_SHORT_MS"
    )
    aigc_notice_tone_long_ms: int = Field(
        default=300, ge=1, le=3000, alias="AIGC_NOTICE_TONE_LONG_MS"
    )
    aigc_notice_tone_gap_ms: int = Field(
        default=90, ge=0, le=1000, alias="AIGC_NOTICE_TONE_GAP_MS"
    )
    aigc_notice_tone_group_gap_ms: int = Field(
        default=260, ge=0, le=2000, alias="AIGC_NOTICE_TONE_GROUP_GAP_MS"
    )
    # 0.20 of full scale sits at the volume of normal companion speech.
    aigc_notice_tone_volume: float = Field(
        default=0.20, ge=0.0, le=1.0, alias="AIGC_NOTICE_TONE_VOLUME"
    )
    # Interval between the opening marker and the following 「我在呢」 wake ack.
    aigc_notice_lead_interval_ms: int = Field(
        default=400, ge=0, le=5000, alias="AIGC_NOTICE_LEAD_INTERVAL_MS"
    )
    # Upper bound on waiting for one marker to finish; a stalled downlink must
    # not hold a round open.
    aigc_notice_marker_duration_ms: int = Field(
        default=3000, ge=1, le=30000, alias="AIGC_NOTICE_MARKER_DURATION_MS"
    )
    # Speech mode only plays an external pre-rendered resource at normal speed;
    # an empty id or a missing file plays nothing (never a synthesized stand-in).
    aigc_notice_speech_resource: str = Field(
        default="", alias="AIGC_NOTICE_SPEECH_RESOURCE"
    )
    aigc_notice_speech_root: str = Field(default="", alias="AIGC_NOTICE_SPEECH_ROOT")
    aigc_notice_speech_sample_rate_hz: int = Field(
        default=24000, alias="AIGC_NOTICE_SPEECH_SAMPLE_RATE_HZ"
    )
    aigc_generation_log_enabled: bool = Field(
        default=True, alias="AIGC_GENERATION_LOG_ENABLED"
    )
    # Internal per-round record: time, provider/engine, content id. Never child
    # speech or reply text. Empty path writes the structured log line only.
    aigc_generation_log_path: str = Field(
        default="", alias="AIGC_GENERATION_LOG_PATH"
    )
    media_pcm_tap_dir: str = Field(default="", alias="MEDIA_PCM_TAP_DIR")
    media_slo_report_enabled: bool = Field(
        default=False, alias="MEDIA_SLO_REPORT_ENABLED"
    )
    media_slo_report_url: str = Field(
        default="http://control-api:8000/v1/internal/media-runtime/slo",
        alias="MEDIA_SLO_REPORT_URL",
    )
    media_slo_metrics_url: str = Field(
        default="http://agent:9090/",
        alias="MEDIA_SLO_METRICS_URL",
    )
    media_slo_report_token: SecretStr = Field(
        default=SecretStr(""), alias="MEDIA_SLO_REPORT_TOKEN"
    )
    media_slo_report_interval_s: float = Field(
        default=30.0, ge=5.0, le=900.0, alias="MEDIA_SLO_REPORT_INTERVAL_S"
    )
    media_slo_report_timeout_s: float = Field(
        default=2.0, ge=0.1, le=30.0, alias="MEDIA_SLO_REPORT_TIMEOUT_S"
    )
    media_reply_delivery_enabled: bool = Field(
        default=False, alias="MEDIA_REPLY_DELIVERY_ENABLED"
    )
    media_reply_delivery_url: str = Field(
        default="http://control-api:8000/v1/internal/media-runtime/reply-delivery",
        alias="MEDIA_REPLY_DELIVERY_URL",
    )
    media_reply_delivery_token: SecretStr = Field(
        default=SecretStr(""), alias="MEDIA_REPLY_DELIVERY_TOKEN"
    )
    media_reply_delivery_spool_key: SecretStr = Field(
        default=SecretStr(""), alias="MEDIA_REPLY_DELIVERY_SPOOL_KEY"
    )
    media_reply_delivery_spool_path: str = Field(
        default="data/media-reply-delivery.spool",
        alias="MEDIA_REPLY_DELIVERY_SPOOL_PATH",
    )
    media_reply_delivery_spool_max_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=4096,
        le=1024 * 1024 * 1024,
        alias="MEDIA_REPLY_DELIVERY_SPOOL_MAX_BYTES",
    )
    media_stream_epoch_enabled: bool = Field(
        default=True, alias="MEDIA_STREAM_EPOCH_ENABLED"
    )
    otel_exporter_otlp_endpoint: str = Field(
        default="", alias="OTEL_EXPORTER_OTLP_ENDPOINT"
    )
    prometheus_port: int = Field(default=9090, ge=1, le=65535, alias="PROMETHEUS_PORT")

    dashscope_api_key: str = Field(default="", alias="DASHSCOPE_API_KEY")
    dashscope_ws_url: str = Field(default="", alias="DASHSCOPE_WS_URL")
    dashscope_compatible_base_url: str = Field(
        default=DASHSCOPE_CN_COMPATIBLE_BASE_URL,
        alias="DASHSCOPE_COMPATIBLE_BASE_URL",
    )
    llm_failover_enabled: bool = Field(default=False, alias="LLM_FAILOVER_ENABLED")
    llm_first_token_timeout_s: float = Field(
        default=DEFAULT_LLM_FIRST_TOKEN_TIMEOUT_S,
        gt=0.0,
        le=60.0,
        alias="LLM_FIRST_TOKEN_TIMEOUT_S",
    )
    llm_inter_token_timeout_s: float = Field(
        default=DEFAULT_LLM_INTER_TOKEN_TIMEOUT_S,
        gt=0.0,
        le=120.0,
        alias="LLM_INTER_TOKEN_TIMEOUT_S",
    )
    llm_backup_model: str = Field(
        default=DEFAULT_LLM_BACKUP_MODEL,
        min_length=1,
        alias="LLM_BACKUP_MODEL",
    )
    qwen_fast_model: str = Field(default="qwen3.7-flash", alias="QWEN_FAST_MODEL")
    qwen_deep_model: str = Field(default="qwen-plus", alias="QWEN_DEEP_MODEL")
    live_search_qwen_enabled: bool = Field(
        default=False, alias="LIVE_SEARCH_QWEN_ENABLED"
    )
    live_search_deepseek_enabled: bool = Field(
        default=False, alias="LIVE_SEARCH_DEEPSEEK_ENABLED"
    )
    weather_lookup_provider: WeatherLookupProvider = Field(
        default="off", alias="WEATHER_LOOKUP_PROVIDER"
    )
    qweather_api_key: SecretStr = Field(default=SecretStr(""), alias="QWEATHER_API_KEY")
    qweather_api_host: str = Field(default="", alias="QWEATHER_API_HOST")
    live_lookup_semantic_enabled: bool = Field(
        default=True,
        alias="LIVE_LOOKUP_SEMANTIC_ENABLED",
    )
    live_lookup_semantic_model: str = Field(
        default="qwen-flash",
        min_length=1,
        alias="LIVE_LOOKUP_SEMANTIC_MODEL",
    )
    live_lookup_semantic_timeout_s: float = Field(
        default=1.2,
        ge=0.1,
        le=2.0,
        alias="LIVE_LOOKUP_SEMANTIC_TIMEOUT_S",
    )
    conversation_close_semantic_enabled: bool = Field(
        default=True,
        alias="CONVERSATION_CLOSE_SEMANTIC_ENABLED",
    )
    conversation_close_semantic_model: str = Field(
        default="qwen-flash",
        min_length=1,
        alias="CONVERSATION_CLOSE_SEMANTIC_MODEL",
    )
    conversation_close_semantic_timeout_s: float = Field(
        default=1.2,
        ge=0.1,
        le=2.0,
        alias="CONVERSATION_CLOSE_SEMANTIC_TIMEOUT_S",
    )

    funasr_model: str = Field(default="fun-asr-realtime", alias="FUNASR_MODEL")
    funasr_sample_rate: int = Field(default=16000, alias="FUNASR_SAMPLE_RATE")
    funasr_max_sentence_silence_ms: int = Field(default=550, alias="FUNASR_MAX_SENTENCE_SILENCE_MS")
    funasr_context_enabled: bool = Field(default=False, alias="FUNASR_CONTEXT_ENABLED")
    funasr_vocabulary_id: str = Field(default="", alias="FUNASR_VOCABULARY_ID")
    funasr_speech_noise_threshold: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        alias="FUNASR_SPEECH_NOISE_THRESHOLD",
    )

    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        validation_alias=AliasChoices("DEEPSEEK_API_URL", "DEEPSEEK_BASE_URL"),
    )
    # deepseek-flash serves DeepSeek-V4.1-Flash; the old name deepseek-v4-flash is routed to it.
    deepseek_fast_model: str = Field(default="deepseek-flash", alias="DEEPSEEK_FAST_MODEL")
    deepseek_deep_model: str = Field(default="deepseek-flash", alias="DEEPSEEK_DEEP_MODEL")
    deepseek_summary_model: str = Field(default="deepseek-flash", alias="DEEPSEEK_SUMMARY_MODEL")
    deepseek_summary_timeout_s: float = Field(
        default=30.0, ge=1.0, le=120.0, alias="DEEPSEEK_SUMMARY_TIMEOUT_S"
    )
    dashscope_summary_model: str = Field(default="qwen-flash", alias="DASHSCOPE_SUMMARY_MODEL")
    dashscope_summary_timeout_s: float = Field(
        default=30.0, ge=1.0, le=120.0, alias="DASHSCOPE_SUMMARY_TIMEOUT_S"
    )

    doubao_tts_api_key: SecretStr = Field(default=SecretStr(""), alias="DOUBAO_TTS_API_KEY")
    doubao_tts_app_id: str = Field(default="", alias="DOUBAO_TTS_APP_ID")
    doubao_tts_access_token: SecretStr = Field(
        default=SecretStr(""),
        alias="DOUBAO_TTS_ACCESS_TOKEN",
    )
    doubao_tts_ws_url: str = Field(
        default="wss://openspeech.bytedance.com/api/v3/tts/bidirection",
        alias="DOUBAO_TTS_WS_URL",
    )
    doubao_tts_resource_id: str = Field(
        default="seed-tts-2.0",
        alias="DOUBAO_TTS_RESOURCE_ID",
    )
    doubao_tts_voice_profile: str = Field(
        default="warm_companion",
        alias="DOUBAO_TTS_VOICE_PROFILE",
    )
    doubao_tts_speaker: str = Field(default="", alias="DOUBAO_TTS_SPEAKER")
    # StepFun-primary main/backup mode: voice for the Doubao backup. Product
    # chose a designed female default; the pure-Doubao default is untouched and
    # an operator override still wins.
    doubao_tts_fallback_speaker: str = Field(
        default=DOUBAO_TTS_FALLBACK_SPEAKER, alias="DOUBAO_TTS_FALLBACK_SPEAKER"
    )
    doubao_tts_sample_rate: int = Field(default=24000, alias="DOUBAO_TTS_SAMPLE_RATE")
    doubao_tts_pool_size: int = Field(default=4, alias="DOUBAO_TTS_POOL_SIZE")

    # Legacy session log-mel is only a playback/noise guard; it is not identity authority.
    speaker_verify_enabled: bool = Field(default=False, alias="SPEAKER_VERIFY_ENABLED")
    speaker_enroll_speech_ms: int = Field(
        # ~2.5s voiced audio is enough for lightweight mel embedding; 3.5s
        # caused fail-open when users stopped just under the bar.
        default=2500,
        ge=1500,
        le=10000,
        alias="SPEAKER_ENROLL_SPEECH_MS",
    )
    speaker_enroll_timeout_ms: int = Field(
        default=12000,
        ge=5000,
        le=60000,
        alias="SPEAKER_ENROLL_TIMEOUT_MS",
    )
    speaker_accept_threshold: float = Field(
        # Balance: owner near-field often ~0.60–0.90; tablet/TV mid-band + quieter.
        # Dual-window consensus + far-field RMS + continuous-media thr boost.
        default=0.58,
        ge=0.35,
        le=0.95,
        alias="SPEAKER_ACCEPT_THRESHOLD",
    )
    speaker_min_verify_speech_ms: int = Field(
        default=450,
        ge=200,
        le=3000,
        alias="SPEAKER_MIN_VERIFY_SPEECH_MS",
    )
    speaker_authority_enabled: bool = Field(
        default=False,
        alias="MEMORIA_SPEAKER_AUTHORITY_ENABLED",
    )
    speaker_authority_url: str = Field(
        default="http://control-api:8000/v1/speakers/classify",
        alias="MEMORIA_SPEAKER_AUTHORITY_URL",
    )
    speaker_internal_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SPEAKER_INTERNAL_TOKEN",
    )
    speaker_authority_timeout_s: float = Field(
        default=0.4,
        ge=0.1,
        le=1.5,
        alias="MEMORIA_SPEAKER_AUTHORITY_TIMEOUT_S",
    )

    archive_sink_enabled: bool = Field(default=True, alias="MEMORIA_ARCHIVE_SINK_ENABLED")
    archive_session_events_url: str = Field(
        default="http://control-api:8000/v1/archive/session-events",
        alias="MEMORIA_ARCHIVE_SESSION_EVENTS_URL",
    )
    session_recap_url: str = Field(
        default="http://control-api:8000/v1/internal/child-memory/session-recap",
        alias="MEMORIA_SESSION_RECAP_URL",
    )
    session_recap_enabled: bool = Field(
        default=True, alias="MEMORIA_SESSION_RECAP_ENABLED"
    )
    archive_internal_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_INTERNAL_TOKEN",
    )
    archive_write_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_WRITE_TOKEN",
    )
    agent_heartbeat_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_AGENT_HEARTBEAT_TOKEN",
    )
    voice_resolution_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_RESOLUTION_TOKEN",
    )
    interaction_policy_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_INTERACTION_POLICY_TOKEN",
    )
    response_plan_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_RESPONSE_PLAN_TOKEN",
    )
    interaction_policy_url: str = Field(
        default="http://control-api:8000/v1/interaction/session-policy",
        alias="MEMORIA_INTERACTION_POLICY_URL",
    )
    transactional_effect_commit_url: str = Field(
        default="http://control-api:8000/v1/interaction/tool-effect/commit",
        alias="MEMORIA_TRANSACTIONAL_EFFECT_COMMIT_URL",
    )
    transactional_effect_reconcile_url: str = Field(
        default="http://control-api:8000/v1/interaction/tool-effect/reconcile",
        alias="MEMORIA_TRANSACTIONAL_EFFECT_RECONCILE_URL",
    )
    interaction_policy_timeout_s: float = Field(
        default=0.4,
        ge=0.05,
        le=2.0,
        alias="MEMORIA_INTERACTION_POLICY_TIMEOUT_S",
    )
    response_plan_url: str = Field(
        default="http://control-api:8000/v1/interaction/response-plan",
        alias="MEMORIA_RESPONSE_PLAN_URL",
    )
    response_plan_timeout_s: float = Field(
        default=0.3,
        ge=0.05,
        le=2.0,
        alias="MEMORIA_RESPONSE_PLAN_TIMEOUT_S",
    )
    speculative_reply_enabled: bool = Field(
        default=True,
        alias="MEMORIA_SPECULATIVE_REPLY_ENABLED",
    )
    speculative_speech_enabled: bool = Field(
        default=True,
        alias="MEMORIA_SPECULATIVE_SPEECH_ENABLED",
    )
    archive_spool_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_SPOOL_KEY",
    )
    archive_spool_path: str = Field(
        default="data/archive-events.spool",
        alias="MEMORIA_ARCHIVE_SPOOL_PATH",
    )
    archive_spool_max_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=4096,
        le=1024 * 1024 * 1024,
        alias="MEMORIA_ARCHIVE_SPOOL_MAX_BYTES",
    )
    voice_profile_enabled: bool = Field(
        default=False,
        alias="MEMORIA_VOICE_PROFILE_ENABLED",
    )
    voice_profile_url: str = Field(
        default="http://control-api:8000/v1/voices/session-resolution",
        alias="MEMORIA_VOICE_PROFILE_URL",
    )
    voice_profile_timeout_s: float = Field(
        default=0.3,
        ge=0.05,
        le=2.0,
        alias="MEMORIA_VOICE_PROFILE_TIMEOUT_S",
    )

    offline_mock: bool = Field(default=False, alias="OFFLINE_MOCK")

    @property
    def llm_api_key(self) -> str:
        return self.deepseek_api_key if self.llm_provider == "deepseek" else self.dashscope_api_key

    @property
    def llm_base_url(self) -> str:
        return (
            self.deepseek_base_url
            if self.llm_provider == "deepseek"
            else self.dashscope_compatible_base_url
        )

    @property
    def llm_fast_model(self) -> str:
        return (
            self.deepseek_fast_model
            if self.llm_provider in {"bailian_deepseek", "deepseek"}
            else self.qwen_fast_model
        )

    @property
    def llm_deep_model(self) -> str:
        return (
            self.deepseek_deep_model
            if self.llm_provider in {"bailian_deepseek", "deepseek"}
            else self.qwen_deep_model
        )

    def internal_token(
        self,
        capability: Literal[
            "archive_write",
            "agent_heartbeat",
            "voice_resolution",
            "interaction_policy",
            "response_plan",
        ],
    ) -> str:
        configured = {
            "archive_write": self.archive_write_token,
            "agent_heartbeat": self.agent_heartbeat_token,
            "voice_resolution": self.voice_resolution_token,
            "interaction_policy": self.interaction_policy_token,
            "response_plan": self.response_plan_token,
        }[capability].get_secret_value()
        if configured or self.environment == "production":
            return configured
        return self.archive_internal_token.get_secret_value()

    @field_validator("llm_backup_model")
    @classmethod
    def _backup_model_id(cls, v: str) -> str:
        cleaned = v.strip()
        if cleaned != DEFAULT_LLM_BACKUP_MODEL:
            raise ValueError(
                f"LLM_BACKUP_MODEL must be {DEFAULT_LLM_BACKUP_MODEL} "
                "(compliance-locked DashScope model id)"
            )
        return cleaned

    @field_validator("qweather_api_host")
    @classmethod
    def _qweather_host(cls, v: str) -> str:
        host = v.strip().lower()
        if host and _QWEATHER_HOST.fullmatch(host) is None:
            raise ValueError(
                "QWEATHER_API_HOST must be a bare *.qweatherapi.com hostname "
                "(no scheme, port, path or userinfo)"
            )
        return host

    @field_validator("funasr_sample_rate")
    @classmethod
    def _funasr_sr(cls, v: int) -> int:
        if v != 16000:
            raise ValueError("FUNASR_SAMPLE_RATE must be 16000")
        return v

    @field_validator("aigc_notice_mode")
    @classmethod
    def _aigc_notice_mode(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if cleaned not in {"tone", "speech", "none"}:
            raise ValueError("AIGC_NOTICE_MODE must be tone, speech or none")
        return cleaned

    @field_validator("aigc_notice_tone_pattern")
    @classmethod
    def _aigc_notice_tone_pattern(cls, v: str) -> str:
        from services.agent.src.voice_core.aigc_notice import validate_tone_pattern

        return validate_tone_pattern(v)

    @field_validator("doubao_tts_sample_rate")
    @classmethod
    def _doubao_sr(cls, v: int) -> int:
        if v != 24000:
            raise ValueError("DOUBAO_TTS_SAMPLE_RATE must be 24000")
        return v

    @field_validator("deepseek_fast_model", "deepseek_deep_model")
    @classmethod
    def _no_deprecated_models(cls, v: str) -> str:
        if v in ("deepseek-chat", "deepseek-reasoner"):
            raise ValueError(f"deprecated DeepSeek model: {v}")
        return v

    @model_validator(mode="after")
    def _profile_rules(self) -> AgentSettings:
        validate_doubao_auth(
            api_key=self.doubao_tts_api_key.get_secret_value(),
            app_id=self.doubao_tts_app_id,
            access_token=self.doubao_tts_access_token.get_secret_value(),
            required=False,
        )
        if self.llm_failover_enabled and not self.dashscope_api_key.strip():
            raise ValueError(
                "LLM_FAILOVER_ENABLED requires DASHSCOPE_API_KEY for the "
                "China North 2 (Beijing) backup endpoint"
            )
        if dashscope_endpoint_in_use(
            llm_provider=self.llm_provider,
            llm_failover_enabled=self.llm_failover_enabled,
        ):
            self.dashscope_compatible_base_url = normalize_dashscope_compatible_base_url(
                self.dashscope_compatible_base_url
            )
        if self.doubao_tts_resource_id != "seed-tts-2.0":
            raise ValueError("DOUBAO_TTS_RESOURCE_ID must be seed-tts-2.0")
        if self.aigc_notice_mode == "speech" and not self.aigc_notice_speech_resource.strip():
            raise ValueError(
                "AIGC_NOTICE_MODE=speech requires AIGC_NOTICE_SPEECH_RESOURCE "
                "(an external, product-approved clip); nothing is synthesized"
            )
        if not _valid_websocket_url(
            self.doubao_tts_ws_url,
            require_tls=self.environment == "production",
        ):
            raise ValueError(
                "DOUBAO_TTS_WS_URL must use wss:// in production and contain no userinfo "
                "or fragment"
            )
        pcm_tap_directory(self.environment, self.media_pcm_tap_dir)
        if self.environment == "production":
            if self.media_slo_report_enabled:
                if not _secure_internal_url(self.media_slo_report_url):
                    raise ValueError("production media SLO reporter requires a secure URL")
                if not _secure_internal_url(self.media_slo_metrics_url):
                    raise ValueError("production media SLO reporter requires an internal metrics URL")
                if len(self.media_slo_report_token.get_secret_value()) < 32:
                    raise ValueError("production media SLO reporter requires MEDIA_SLO_REPORT_TOKEN")
            if self.media_reply_delivery_enabled:
                if not _secure_internal_url(self.media_reply_delivery_url):
                    raise ValueError(
                        "production reply delivery reporter requires a secure URL"
                    )
                if len(self.media_reply_delivery_token.get_secret_value()) < 32:
                    raise ValueError(
                        "production reply delivery reporter requires MEDIA_REPLY_DELIVERY_TOKEN"
                    )
                spool_key = self.media_reply_delivery_spool_key.get_secret_value()
                try:
                    from cryptography.fernet import Fernet

                    Fernet(spool_key.encode("ascii"))
                except (ValueError, UnicodeEncodeError) as exc:
                    raise ValueError(
                        "production reply delivery reporter requires a valid Fernet spool key"
                    ) from exc
            if self.media_bridge_grpc_enabled:
                if not self.media_bridge_mtls:
                    raise ValueError("production media bridge requires MEDIA_BRIDGE_MTLS=true")
                if not all(
                    (
                        self.media_bridge_tls_cert_file.strip(),
                        self.media_bridge_tls_key_file.strip(),
                        self.media_bridge_client_ca_file.strip(),
                    )
                ):
                    raise ValueError(
                        "production media bridge requires certificate, key and client CA files"
                    )
            heartbeat_token = self.internal_token("agent_heartbeat")
            if len(heartbeat_token) < 32:
                raise ValueError("production agent heartbeat requires a scoped token")
            capability_tokens: list[str] = [heartbeat_token]
            if self.media_reply_delivery_enabled:
                capability_tokens.append(self.media_reply_delivery_token.get_secret_value())
            if self.archive_sink_enabled:
                token = self.internal_token("archive_write")
                spool_key = self.archive_spool_key.get_secret_value()
                if len(token) < 32:
                    raise ValueError("production archive requires a scoped write token")
                if not _secure_internal_url(self.archive_session_events_url):
                    raise ValueError("production archive URL requires HTTPS or local Docker DNS")
                capability_tokens.append(token)
                try:
                    from cryptography.fernet import Fernet

                    Fernet(spool_key.encode("ascii"))
                except (ValueError, UnicodeEncodeError) as exc:
                    raise ValueError(
                        "production archive requires a valid Fernet spool key"
                    ) from exc
            if self.session_recap_enabled:
                recap_token = self.internal_token("archive_write")
                if len(recap_token) < 32:
                    raise ValueError("production session recap requires a scoped write token")
                if not _secure_internal_url(self.session_recap_url):
                    raise ValueError(
                        "production session recap URL requires HTTPS or local Docker DNS"
                    )
                if recap_token not in capability_tokens:
                    capability_tokens.append(recap_token)
            if self.voice_profile_enabled:
                voice_token = self.internal_token("voice_resolution")
                if len(voice_token) < 32:
                    raise ValueError("production voice profile requires a scoped resolution token")
                if not _secure_internal_url(self.voice_profile_url):
                    raise ValueError("production voice URL requires HTTPS or local Docker DNS")
                capability_tokens.append(voice_token)
            interaction_token = self.internal_token("interaction_policy")
            if len(interaction_token) < 32:
                raise ValueError("production interaction policy requires a scoped token")
            if not _secure_internal_url(self.interaction_policy_url):
                raise ValueError(
                    "production interaction policy URL requires HTTPS or local Docker DNS"
                )
            capability_tokens.append(interaction_token)
            response_plan_token = self.internal_token("response_plan")
            if len(response_plan_token) < 32:
                raise ValueError("production response plan requires a scoped token")
            if not _secure_internal_url(self.response_plan_url):
                raise ValueError("production response plan URL requires HTTPS or local Docker DNS")
            capability_tokens.append(response_plan_token)
            if len(capability_tokens) != len(set(capability_tokens)):
                raise ValueError("production internal capability tokens must be independent")
            if self.speaker_authority_enabled:
                speaker_token = self.speaker_internal_token.get_secret_value()
                if len(speaker_token) < 32 or speaker_token in capability_tokens:
                    raise ValueError(
                        "production speaker authority requires an independent internal token"
                    )
                if not _secure_internal_url(self.speaker_authority_url):
                    raise ValueError("production speaker URL requires HTTPS or local Docker DNS")
        return self


def load_settings(*, require_keys: bool = False) -> AgentSettings:
    settings = AgentSettings()
    if require_keys and not settings.offline_mock:
        missing = [
            name
            for name, val in [
                ("DASHSCOPE_API_KEY", settings.dashscope_api_key),
                ("DASHSCOPE_WS_URL", settings.dashscope_ws_url),
                (
                    "DOUBAO_TTS_AUTH",
                    settings.doubao_tts_api_key.get_secret_value()
                    or (
                        settings.doubao_tts_app_id
                        and settings.doubao_tts_access_token.get_secret_value()
                    ),
                ),
            ]
            if not val
        ]
        if missing:
            raise ConfigValidationError(f"missing required env: {', '.join(missing)}")
        if settings.llm_provider == "deepseek" and not settings.deepseek_api_key:
            raise ConfigValidationError("missing required env: DEEPSEEK_API_KEY")
    return settings


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")
