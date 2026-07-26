"""Configuration for the isolated Mini Program media gateway."""

from __future__ import annotations

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.common.security_constants import DEV_MINIPROGRAM_GATEWAY_TICKET_SECRET


class MiniProgramGatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    environment: str = Field(default="development", alias="ENVIRONMENT")
    livekit_url: str = Field(default="", alias="LIVEKIT_URL")
    livekit_api_key: str = Field(default="", alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="", alias="LIVEKIT_API_SECRET")
    livekit_agent_name: str = Field(default="duplex-zh-agent", alias="LIVEKIT_AGENT_NAME")
    memoria_miniprogram_gateway_ticket_secret: SecretStr = Field(
        default=SecretStr(DEV_MINIPROGRAM_GATEWAY_TICKET_SECRET),
        alias="MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET",
    )
    miniprogram_gateway_ticket_max_ttl_s: int = Field(
        default=300,
        ge=30,
        le=600,
        alias="MINIPROGRAM_GATEWAY_TICKET_MAX_TTL_S",
    )
    miniprogram_gateway_livekit_token_ttl_s: int = Field(
        default=300,
        ge=60,
        le=900,
        alias="MINIPROGRAM_GATEWAY_LIVEKIT_TOKEN_TTL_S",
    )
    miniprogram_gateway_handshake_timeout_s: float = Field(
        default=10.0,
        ge=1.0,
        le=30.0,
        alias="MINIPROGRAM_GATEWAY_HANDSHAKE_TIMEOUT_S",
    )
    miniprogram_gateway_uplink_sample_rate: int = Field(
        default=16_000,
        alias="MINIPROGRAM_GATEWAY_UPLINK_SAMPLE_RATE",
    )
    miniprogram_gateway_downlink_sample_rate: int = Field(
        default=24_000,
        alias="MINIPROGRAM_GATEWAY_DOWNLINK_SAMPLE_RATE",
    )
    miniprogram_gateway_frame_ms: int = Field(
        default=20,
        alias="MINIPROGRAM_GATEWAY_FRAME_MS",
    )
    miniprogram_gateway_aec_enabled: bool = Field(
        default=False,
        alias="MINIPROGRAM_GATEWAY_AEC_ENABLED",
    )
    miniprogram_gateway_aec_stream_delay_ms: int = Field(
        default=120,
        ge=0,
        le=500,
        alias="MINIPROGRAM_GATEWAY_AEC_STREAM_DELAY_MS",
    )
    miniprogram_gateway_aec_active_window_ms: int = Field(
        default=750,
        ge=100,
        le=5_000,
        alias="MINIPROGRAM_GATEWAY_AEC_ACTIVE_WINDOW_MS",
    )
    miniprogram_gateway_generation_quarantine_ms: int = Field(
        default=400,
        ge=20,
        le=1_000,
        alias="MINIPROGRAM_GATEWAY_GENERATION_QUARANTINE_MS",
    )
    miniprogram_gateway_audio_queue_frames: int = Field(
        default=20,
        ge=10,
        le=500,
        alias="MINIPROGRAM_GATEWAY_AUDIO_QUEUE_FRAMES",
    )
    miniprogram_gateway_event_queue_size: int = Field(
        default=64,
        ge=8,
        le=256,
        alias="MINIPROGRAM_GATEWAY_EVENT_QUEUE_SIZE",
    )

    def validate_production(self) -> None:
        if self.environment != "production":
            return
        if not self.livekit_url.startswith("wss://"):
            raise ValueError("production Mini Program gateway requires secure LIVEKIT_URL")
        if not self.livekit_api_key or not self.livekit_api_secret:
            raise ValueError("production Mini Program gateway requires LiveKit credentials")
        secret = self.memoria_miniprogram_gateway_ticket_secret.get_secret_value()
        if (
            secret == DEV_MINIPROGRAM_GATEWAY_TICKET_SECRET
            or len(secret) < 32
            or secret == self.livekit_api_secret
        ):
            raise ValueError(
                "production Mini Program gateway requires an independent ticket secret"
            )
        if self.miniprogram_gateway_uplink_sample_rate != 16_000:
            raise ValueError("Mini Program gateway uplink must be PCM16/16 kHz")
        if self.miniprogram_gateway_downlink_sample_rate != 24_000:
            raise ValueError("Mini Program gateway downlink must be PCM16/24 kHz")
        if self.miniprogram_gateway_frame_ms != 20:
            raise ValueError("Mini Program gateway frame duration must be 20 ms")
        if (
            self.miniprogram_gateway_audio_queue_frames
            * self.miniprogram_gateway_frame_ms
            > 400
        ):
            raise ValueError("Mini Program gateway downlink queue must not exceed 400 ms")
        if (
            self.miniprogram_gateway_generation_quarantine_ms
            < self.miniprogram_gateway_audio_queue_frames
            * self.miniprogram_gateway_frame_ms
        ):
            raise ValueError(
                "Mini Program generation quarantine must cover the downlink queue"
            )
