"""Agent runtime configuration with startup validation (ch.6.1)."""

from __future__ import annotations

import os
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.agent.src.contracts.errors import ConfigValidationError

DeploymentProfile = Literal["livekit_cloud", "cn_self_hosted"]
LLMProvider = Literal["qwen", "deepseek"]
TTSProvider = Literal["doubao"]


def _secure_internal_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme == "https" or (
        parsed.scheme == "http"
        and parsed.hostname in {"control-api", "localhost", "127.0.0.1", "::1"}
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


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = Field(default="development", alias="ENVIRONMENT")
    deployment_profile: DeploymentProfile = Field(
        default="livekit_cloud", alias="DEPLOYMENT_PROFILE"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    llm_provider: LLMProvider = Field(default="qwen", alias="LLM_PROVIDER")
    tts_provider: TTSProvider = Field(default="doubao", alias="TTS_PROVIDER")

    livekit_url: str = Field(default="", alias="LIVEKIT_URL")
    livekit_api_key: str = Field(default="", alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="", alias="LIVEKIT_API_SECRET")
    livekit_agent_name: str = Field(default="duplex-zh-agent", alias="LIVEKIT_AGENT_NAME")
    livekit_turn_detector_version: str = Field(default="v1", alias="LIVEKIT_TURN_DETECTOR_VERSION")
    # P1-5: adaptive interruption + Turn Detector (v1-mini on cn_self_hosted).
    livekit_adaptive_interruption: bool = Field(default=True, alias="LIVEKIT_ADAPTIVE_INTERRUPTION")
    # LiveKit preemptive LLM before EOU — default off; stream phrase TTS is the safe path.
    preemptive_generation: bool = Field(default=False, alias="PREEMPTIVE_GENERATION")

    dashscope_api_key: str = Field(default="", alias="DASHSCOPE_API_KEY")
    dashscope_ws_url: str = Field(default="", alias="DASHSCOPE_WS_URL")
    dashscope_compatible_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="DASHSCOPE_COMPATIBLE_BASE_URL",
    )
    qwen_fast_model: str = Field(default="qwen-turbo", alias="QWEN_FAST_MODEL")
    qwen_deep_model: str = Field(default="qwen-plus", alias="QWEN_DEEP_MODEL")

    funasr_model: str = Field(default="fun-asr-realtime", alias="FUNASR_MODEL")
    funasr_sample_rate: int = Field(default=16000, alias="FUNASR_SAMPLE_RATE")
    funasr_max_sentence_silence_ms: int = Field(default=550, alias="FUNASR_MAX_SENTENCE_SILENCE_MS")
    funasr_context_enabled: bool = Field(default=False, alias="FUNASR_CONTEXT_ENABLED")

    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_fast_model: str = Field(default="deepseek-v4-flash", alias="DEEPSEEK_FAST_MODEL")
    deepseek_deep_model: str = Field(default="deepseek-v4-pro", alias="DEEPSEEK_DEEP_MODEL")

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
    doubao_tts_sample_rate: int = Field(default=24000, alias="DOUBAO_TTS_SAMPLE_RATE")
    doubao_tts_pool_size: int = Field(default=4, alias="DOUBAO_TTS_POOL_SIZE")

    vad_min_silence_duration_s: float = Field(default=0.30, alias="VAD_MIN_SILENCE_DURATION_S")
    preemptive_tts: bool = Field(default=False, alias="PREEMPTIVE_TTS")
    # Listener cues: default OFF. BackgroundAudioPlayer is a second room track
    # (prod dual-voice). Keep off until mixed into the main CosyVoice path.
    listener_cues_enabled: bool = Field(default=False, alias="LISTENER_CUES_ENABLED")
    listener_cue_playback: str = Field(
        default="main_track",
        alias="LISTENER_CUE_PLAYBACK",
    )
    listener_cue_aec_validated: bool = Field(
        default=False,
        alias="LISTENER_CUE_AEC_VALIDATED",
    )
    listener_cue_pause_ms: int = Field(
        default=250,
        ge=150,
        le=350,
        alias="LISTENER_CUE_PAUSE_MS",
    )
    listener_cue_min_speech_ms: int = Field(
        default=1800,
        ge=1000,
        le=10000,
        alias="LISTENER_CUE_MIN_SPEECH_MS",
    )
    listener_cue_cooldown_ms: int = Field(
        default=5000,
        ge=3000,
        le=30000,
        alias="LISTENER_CUE_COOLDOWN_MS",
    )
    listener_cue_max_per_turn: int = Field(
        default=2,
        ge=0,
        le=2,
        alias="LISTENER_CUE_MAX_PER_TURN",
    )
    listener_cue_volume: float = Field(
        default=0.65,
        ge=0.1,
        le=0.85,
        alias="LISTENER_CUE_VOLUME",
    )
    qwen_emotion_enabled: bool = Field(default=True, alias="QWEN_EMOTION_ENABLED")

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
    memory_read_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_READ_TOKEN",
    )
    persona_read_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_PERSONA_READ_TOKEN",
    )
    voice_resolution_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_RESOLUTION_TOKEN",
    )
    interaction_policy_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_INTERACTION_POLICY_TOKEN",
    )
    interaction_policy_url: str = Field(
        default="http://control-api:8000/v1/interaction/session-policy",
        alias="MEMORIA_INTERACTION_POLICY_URL",
    )
    interaction_policy_timeout_s: float = Field(
        default=0.4,
        ge=0.05,
        le=2.0,
        alias="MEMORIA_INTERACTION_POLICY_TIMEOUT_S",
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
    persona_enabled: bool = Field(default=False, alias="MEMORIA_PERSONA_ENABLED")
    persona_capsule_url: str = Field(
        default="http://control-api:8000/v1/persona/session-capsule",
        alias="MEMORIA_PERSONA_CAPSULE_URL",
    )
    persona_timeout_s: float = Field(
        default=0.3,
        ge=0.05,
        le=2.0,
        alias="MEMORIA_PERSONA_TIMEOUT_S",
    )
    persona_cache_ttl_s: float = Field(
        default=60.0,
        ge=1.0,
        le=300.0,
        alias="MEMORIA_PERSONA_CACHE_TTL_S",
    )
    memory_context_enabled: bool = Field(
        default=False,
        alias="MEMORIA_MEMORY_CONTEXT_ENABLED",
    )
    memory_context_url: str = Field(
        default="http://control-api:8000/v1/archive/session-context",
        alias="MEMORIA_MEMORY_CONTEXT_URL",
    )
    memory_context_timeout_s: float = Field(
        default=0.3,
        ge=0.05,
        le=2.0,
        alias="MEMORIA_MEMORY_CONTEXT_TIMEOUT_S",
    )
    memory_context_cache_ttl_s: float = Field(
        default=60.0,
        ge=1.0,
        le=300.0,
        alias="MEMORIA_MEMORY_CONTEXT_CACHE_TTL_S",
    )
    memory_context_limit: int = Field(
        default=8,
        ge=1,
        le=20,
        alias="MEMORIA_MEMORY_CONTEXT_LIMIT",
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
        return self.deepseek_fast_model if self.llm_provider == "deepseek" else self.qwen_fast_model

    @property
    def llm_deep_model(self) -> str:
        return self.deepseek_deep_model if self.llm_provider == "deepseek" else self.qwen_deep_model

    def internal_token(
        self,
        capability: Literal[
            "archive_write",
            "agent_heartbeat",
            "memory_read",
            "persona_read",
            "voice_resolution",
            "interaction_policy",
        ],
    ) -> str:
        configured = {
            "archive_write": self.archive_write_token,
            "agent_heartbeat": self.agent_heartbeat_token,
            "memory_read": self.memory_read_token,
            "persona_read": self.persona_read_token,
            "voice_resolution": self.voice_resolution_token,
            "interaction_policy": self.interaction_policy_token,
        }[capability].get_secret_value()
        if configured or self.environment == "production":
            return configured
        return self.archive_internal_token.get_secret_value()

    @field_validator("funasr_sample_rate")
    @classmethod
    def _funasr_sr(cls, v: int) -> int:
        if v != 16000:
            raise ValueError("FUNASR_SAMPLE_RATE must be 16000")
        return v

    @field_validator("doubao_tts_sample_rate")
    @classmethod
    def _doubao_sr(cls, v: int) -> int:
        if v != 24000:
            raise ValueError("DOUBAO_TTS_SAMPLE_RATE must be 24000")
        return v

    @field_validator("vad_min_silence_duration_s")
    @classmethod
    def _vad_silence(cls, v: float) -> float:
        if v < 0.25:
            raise ValueError("VAD_MIN_SILENCE_DURATION_S must be >= 0.25")
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
        if self.doubao_tts_resource_id != "seed-tts-2.0":
            raise ValueError("DOUBAO_TTS_RESOURCE_ID must be seed-tts-2.0")
        if not _valid_websocket_url(
            self.doubao_tts_ws_url,
            require_tls=self.environment == "production",
        ):
            raise ValueError(
                "DOUBAO_TTS_WS_URL must use wss:// in production and contain no userinfo "
                "or fragment"
            )
        if self.deployment_profile == "livekit_cloud" and not self.livekit_adaptive_interruption:
            raise ValueError("livekit_cloud requires LIVEKIT_ADAPTIVE_INTERRUPTION=true")
        if self.deployment_profile == "cn_self_hosted":
            object.__setattr__(self, "livekit_turn_detector_version", "v1-mini")
        playback = (self.listener_cue_playback or "main_track").strip().lower()
        if playback not in {"main_track", "background"}:
            raise ValueError("LISTENER_CUE_PLAYBACK must be main_track or background")
        object.__setattr__(self, "listener_cue_playback", playback)
        # Second-track cues need AEC validation; otherwise force main_track safety.
        if (
            self.listener_cues_enabled
            and playback == "background"
            and not self.listener_cue_aec_validated
        ):
            object.__setattr__(self, "listener_cue_playback", "main_track")
        if self.environment == "production":
            if self.livekit_url.startswith("ws://") or self.livekit_url.startswith("http://"):
                raise ValueError("production forbids plaintext media/control URLs")
            heartbeat_token = self.internal_token("agent_heartbeat")
            if len(heartbeat_token) < 32:
                raise ValueError("production agent heartbeat requires a scoped token")
            capability_tokens: list[str] = [heartbeat_token]
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
            if self.persona_enabled:
                persona_token = self.internal_token("persona_read")
                if len(persona_token) < 32:
                    raise ValueError("production persona requires a scoped read token")
                if not _secure_internal_url(self.persona_capsule_url):
                    raise ValueError("production persona URL requires HTTPS or local Docker DNS")
                capability_tokens.append(persona_token)
            if self.memory_context_enabled:
                memory_token = self.internal_token("memory_read")
                if len(memory_token) < 32:
                    raise ValueError("production memory context requires a scoped read token")
                if not _secure_internal_url(self.memory_context_url):
                    raise ValueError("production memory URL requires HTTPS or local Docker DNS")
                capability_tokens.append(memory_token)
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
                ("LIVEKIT_URL", settings.livekit_url),
                ("LIVEKIT_API_KEY", settings.livekit_api_key),
                ("LIVEKIT_API_SECRET", settings.livekit_api_secret),
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
