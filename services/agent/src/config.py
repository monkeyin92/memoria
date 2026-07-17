"""Agent runtime configuration with startup validation (ch.6.1)."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.agent.src.contracts.errors import ConfigValidationError

DeploymentProfile = Literal["livekit_cloud", "cn_self_hosted"]
LLMProvider = Literal["qwen", "deepseek"]


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = Field(default="development", alias="ENVIRONMENT")
    deployment_profile: DeploymentProfile = Field(
        default="livekit_cloud", alias="DEPLOYMENT_PROFILE"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    llm_provider: LLMProvider = Field(default="qwen", alias="LLM_PROVIDER")

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

    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_fast_model: str = Field(default="deepseek-v4-flash", alias="DEEPSEEK_FAST_MODEL")
    deepseek_deep_model: str = Field(default="deepseek-v4-pro", alias="DEEPSEEK_DEEP_MODEL")

    cosyvoice_model: str = Field(default="cosyvoice-v3-flash", alias="COSYVOICE_MODEL")
    cosyvoice_voice: str = Field(default="longanyang", alias="COSYVOICE_VOICE")
    cosyvoice_sample_rate: int = Field(default=24000, alias="COSYVOICE_SAMPLE_RATE")
    cosyvoice_word_timestamps: bool = Field(default=True, alias="COSYVOICE_WORD_TIMESTAMPS")
    cosyvoice_pool_size: int = Field(default=4, alias="COSYVOICE_POOL_SIZE")
    # When true, inject CosyVoice markup like [laughter]/[breath] on delivery.
    # Keep false for longanyang PlainText unless the deployed model is verified.
    cosyvoice_paralinguistic_tags: bool = Field(
        # P0-2: laugh/breath/emphasis markup via DeliveryPlan; serious scenes strip.
        default=True,
        alias="COSYVOICE_PARALINGUISTIC_TAGS",
    )

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

    # Session-scoped target speaker enrollment (reject nearby talkers).
    speaker_verify_enabled: bool = Field(default=True, alias="SPEAKER_VERIFY_ENABLED")
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

    @field_validator("funasr_sample_rate")
    @classmethod
    def _funasr_sr(cls, v: int) -> int:
        if v != 16000:
            raise ValueError("FUNASR_SAMPLE_RATE must be 16000")
        return v

    @field_validator("cosyvoice_sample_rate")
    @classmethod
    def _cosy_sr(cls, v: int) -> int:
        if v != 24000:
            raise ValueError("COSYVOICE_SAMPLE_RATE must be 24000")
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
        if not self.cosyvoice_word_timestamps:
            raise ValueError("COSYVOICE_WORD_TIMESTAMPS must be true")
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
