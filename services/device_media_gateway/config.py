"""Configuration for the independent hardware-device media gateway."""

from __future__ import annotations

from pydantic import Field, SecretStr

from services.common.security_constants import DEV_DEVICE_GATEWAY_TICKET_SECRET
from services.miniprogram_gateway.config import MiniProgramGatewaySettings


class DeviceMediaGatewaySettings(MiniProgramGatewaySettings):
    """Shared LiveKit/bridge settings with a device-only ticket namespace.

    The bridge deliberately accepts the existing Mini Program settings shape.  This
    subclass keeps that compatibility local to the device adapter while giving
    hardware tickets their own environment variable and TTL controls.
    """

    memoria_device_gateway_ticket_secret: SecretStr = Field(
        default=SecretStr(DEV_DEVICE_GATEWAY_TICKET_SECRET),
        alias="MEMORIA_DEVICE_GATEWAY_TICKET_SECRET",
    )
    device_media_gateway_ticket_max_ttl_s: int = Field(
        default=300,
        ge=30,
        le=600,
        alias="DEVICE_MEDIA_GATEWAY_TICKET_MAX_TTL_S",
    )
    device_media_gateway_handshake_timeout_s: float = Field(
        default=10.0,
        ge=1.0,
        le=30.0,
        alias="DEVICE_MEDIA_GATEWAY_HANDSHAKE_TIMEOUT_S",
    )
    device_media_gateway_max_opus_payload_bytes: int = Field(
        default=4_096,
        ge=64,
        le=65_535,
        alias="DEVICE_MEDIA_GATEWAY_MAX_OPUS_PAYLOAD_BYTES",
    )

    def validate_production(self) -> None:
        """Fail closed when the adapter is accidentally started in production."""
        if self.environment != "production":
            return
        if not self.livekit_url.startswith("wss://"):
            raise ValueError("production device gateway requires secure LIVEKIT_URL")
        if not self.livekit_api_key or not self.livekit_api_secret:
            raise ValueError("production device gateway requires LiveKit credentials")
        secret = self.memoria_device_gateway_ticket_secret.get_secret_value()
        if (
            secret == DEV_DEVICE_GATEWAY_TICKET_SECRET
            or len(secret) < 32
            or secret == self.livekit_api_secret
        ):
            raise ValueError("production device gateway requires an independent ticket secret")
        if self.miniprogram_gateway_uplink_sample_rate != 16_000:
            raise ValueError("device gateway uplink must be Opus/PCM16 at 16 kHz")
        if self.miniprogram_gateway_downlink_sample_rate != 24_000:
            raise ValueError("device gateway downlink must be Opus/PCM16 at 24 kHz")
        if self.miniprogram_gateway_frame_ms != 20:
            raise ValueError("device gateway frame duration must be 20 ms")
