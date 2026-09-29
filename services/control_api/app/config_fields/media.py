"""Realtime media: Media Edge, the session directory and the direct device runtime."""

from __future__ import annotations

from pydantic import BaseModel, Field, SecretStr


class MediaFields(BaseModel):
    redis_url: str = Field(default="", alias="REDIS_URL")
    media_edge_id: str = Field(default="media-edge-local", alias="MEDIA_EDGE_ID")
    voice_core_id: str = Field(default="voice-core-local", alias="VOICE_CORE_ID")
    media_slo_snapshot_ttl_s: int = Field(
        default=120, ge=30, le=900, alias="MEDIA_SLO_SNAPSHOT_TTL_S"
    )
    media_slo_report_token: SecretStr = Field(default=SecretStr(""), alias="MEDIA_SLO_REPORT_TOKEN")
    media_reply_delivery_token: SecretStr = Field(
        default=SecretStr(""), alias="MEDIA_REPLY_DELIVERY_TOKEN"
    )
    media_edge_control_url: str = Field(default="", alias="MEDIA_EDGE_CONTROL_URL")
    media_edge_control_timeout_s: float = Field(
        default=2.0,
        ge=0.1,
        le=30.0,
        alias="MEDIA_EDGE_CONTROL_TIMEOUT_S",
    )
    media_edge_internal_control_url: str = Field(
        default="",
        alias="MEDIA_EDGE_INTERNAL_CONTROL_URL",
    )
    media_edge_internal_control_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEDIA_EDGE_INTERNAL_CONTROL_TOKEN",
    )
    media_edge_internal_control_ca_file: str = Field(
        default="",
        alias="MEDIA_EDGE_INTERNAL_CONTROL_CA_FILE",
    )
    media_edge_internal_control_client_cert_file: str = Field(
        default="",
        alias="MEDIA_EDGE_INTERNAL_CONTROL_CLIENT_CERT_FILE",
    )
    media_edge_internal_control_client_key_file: str = Field(
        default="",
        alias="MEDIA_EDGE_INTERNAL_CONTROL_CLIENT_KEY_FILE",
    )
    media_edge_device_close_report_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN",
    )
    streamcore_token_secret: SecretStr = Field(
        default=SecretStr(""), alias="STREAMCORE_TOKEN_SECRET"
    )
    streamcore_token_private_key_file: str = Field(
        default="", alias="STREAMCORE_TOKEN_PRIVATE_KEY_FILE"
    )
    streamcore_token_private_key_pem: SecretStr = Field(
        default=SecretStr(""), alias="STREAMCORE_TOKEN_PRIVATE_KEY_PEM"
    )
    streamcore_token_key_id: str = Field(default="streamcore-1", alias="STREAMCORE_TOKEN_KEY_ID")
    streamcore_token_ttl_s: int = Field(default=120, ge=30, le=300, alias="STREAMCORE_TOKEN_TTL_S")
    device_challenge_ttl_ms: int = Field(
        default=120_000, ge=10_000, le=600_000, alias="DEVICE_CHALLENGE_TTL_MS"
    )
    # The one hardware media endpoint: the Go Media Edge WSS, which bridges to
    # Python Voice Core and verifies EdDSA/JWKS device tickets.  An empty value
    # means this deployment serves no devices (no onboarding authority, no
    # device media sessions); production validation of the whole direct stack
    # is keyed to it being set.
    device_direct_media_wss_url: str = Field(
        default="",
        alias="DEVICE_DIRECT_MEDIA_WSS_URL",
    )
    device_runtime_profile_ttl_s: int = Field(
        default=3600,
        ge=300,
        le=86400,
        alias="DEVICE_RUNTIME_PROFILE_TTL_S",
    )
