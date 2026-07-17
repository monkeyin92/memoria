"""Control API settings."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.common.security_constants import DEV_AUTH_SECRET


class ControlSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = Field(default="development", alias="ENVIRONMENT")
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")
    allowed_origins: str = Field(default="http://localhost:5173", alias="ALLOWED_ORIGINS")

    livekit_url: str = Field(default="wss://YOUR_PROJECT.livekit.cloud", alias="LIVEKIT_URL")
    livekit_api_key: str = Field(default="", alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="", alias="LIVEKIT_API_SECRET")
    livekit_agent_name: str = Field(default="duplex-zh-agent", alias="LIVEKIT_AGENT_NAME")

    memoria_db_path: str = Field(default="data/memoria.sqlite3", alias="MEMORIA_DB_PATH")
    memoria_timezone: str = Field(default="Asia/Shanghai", alias="MEMORIA_TIMEZONE")

    llm_provider: Literal["qwen", "deepseek"] = Field(default="qwen", alias="LLM_PROVIDER")
    dashscope_api_key: SecretStr = Field(default=SecretStr(""), alias="DASHSCOPE_API_KEY")
    dashscope_workspace_id: str = Field(default="", alias="DASHSCOPE_WORKSPACE_ID")
    qwen_omni_voice: str = Field(default="Tina", alias="QWEN_OMNI_VOICE")
    qwen_omni_sdp_timeout_s: float = Field(
        default=10.0,
        ge=1.0,
        le=30.0,
        alias="QWEN_OMNI_SDP_TIMEOUT_S",
    )
    # Shared semantic_vad knobs for Omni Flash/Plus A/B sweeps.
    qwen_omni_vad_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        alias="QWEN_OMNI_VAD_THRESHOLD",
    )
    qwen_omni_prefix_padding_ms: int = Field(
        default=500,
        ge=0,
        le=2000,
        alias="QWEN_OMNI_PREFIX_PADDING_MS",
    )
    qwen_omni_silence_duration_ms: int = Field(
        default=800,
        ge=200,
        le=2500,
        alias="QWEN_OMNI_SILENCE_DURATION_MS",
    )
    # Optional Plus-only override; empty/unset reuses the shared silence window.
    qwen_omni_plus_silence_duration_ms: int | None = Field(
        default=None,
        ge=200,
        le=2500,
        alias="QWEN_OMNI_PLUS_SILENCE_DURATION_MS",
    )
    qwen_omni_plus_vad_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        alias="QWEN_OMNI_PLUS_VAD_THRESHOLD",
    )
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="DASHSCOPE_BASE_URL",
    )
    dashscope_summary_model: str = Field(default="qwen-plus", alias="DASHSCOPE_SUMMARY_MODEL")
    dashscope_summary_timeout_s: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        alias="DASHSCOPE_SUMMARY_TIMEOUT_S",
    )

    deepseek_api_key: SecretStr = Field(default=SecretStr(""), alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        alias="DEEPSEEK_BASE_URL",
    )
    deepseek_summary_model: str = Field(
        default="deepseek-chat",
        alias="DEEPSEEK_SUMMARY_MODEL",
    )
    deepseek_summary_timeout_s: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        alias="DEEPSEEK_SUMMARY_TIMEOUT_S",
    )

    session_token_ttl_s: int = Field(default=300, alias="SESSION_TOKEN_TTL_S")
    jwt_issuer: str = Field(default="voice-agent", alias="JWT_ISSUER")

    memoria_auth_secret: SecretStr = Field(
        default=SecretStr(DEV_AUTH_SECRET),
        alias="MEMORIA_AUTH_SECRET",
    )
    memoria_auth_issuer: str = Field(
        default="memoria-control-api",
        alias="MEMORIA_AUTH_ISSUER",
    )
    memoria_auth_audience: str = Field(
        default="memoria-h5",
        alias="MEMORIA_AUTH_AUDIENCE",
    )
    memoria_auth_token_ttl_s: int = Field(
        default=31_536_000,
        ge=300,
        le=31_536_000,
        alias="MEMORIA_AUTH_TOKEN_TTL_S",
    )
    memoria_release_tag: str = Field(default="development", alias="MEMORIA_RELEASE_TAG")
    readiness_gate_ttl_s: int = Field(
        default=86_400,
        ge=60,
        le=86_400,
        alias="READINESS_GATE_TTL_S",
    )
    offline_mock: bool = Field(default=False, alias="OFFLINE_MOCK")

    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    def validate_production(self) -> None:
        if self.environment != "production":
            return
        if "*" in self.origins_list():
            raise ValueError("production must not allow * CORS")
        if self.public_base_url.startswith("http://"):
            raise ValueError("production must not use plaintext PUBLIC_BASE_URL")
        if not self.livekit_url.startswith("wss://"):
            raise ValueError("production must use secure LIVEKIT_URL")
        if not self.livekit_api_key or not self.livekit_api_secret:
            raise ValueError("production requires LiveKit credentials")
        auth_secret = self.memoria_auth_secret.get_secret_value()
        if auth_secret == DEV_AUTH_SECRET or len(auth_secret) < 32:
            raise ValueError("production requires an independent MEMORIA_AUTH_SECRET (>=32 chars)")
        if auth_secret == self.livekit_api_secret:
            raise ValueError("MEMORIA_AUTH_SECRET must differ from LIVEKIT_API_SECRET")
        release_tag = self.memoria_release_tag.strip().lower()
        if release_tag in ("", "latest", "development"):
            raise ValueError("production requires an immutable MEMORIA_RELEASE_TAG")
