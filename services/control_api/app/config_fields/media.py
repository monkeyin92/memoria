"""Realtime media: LiveKit/TURN, StreamCore, Media Edge, gateways and device runtime."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from services.common.security_constants import (
    DEV_DEVICE_GATEWAY_TICKET_SECRET,
    DEV_MINIPROGRAM_GATEWAY_TICKET_SECRET,
)


class MediaFields(BaseModel):
    livekit_url: str = Field(default="wss://YOUR_PROJECT.livekit.cloud", alias="LIVEKIT_URL")
    livekit_api_key: str = Field(default="", alias="LIVEKIT_API_KEY")
    livekit_api_secret: str = Field(default="", alias="LIVEKIT_API_SECRET")
    livekit_agent_name: str = Field(default="duplex-zh-agent", alias="LIVEKIT_AGENT_NAME")
    # Production clients use independent coturn credentials, never a static
    # TURN password. URLs are comma-separated (turn:/turns:).
    coturn_urls: str = Field(default="", alias="COTURN_URLS")
    coturn_realm: str = Field(default="memoria", alias="COTURN_REALM")
    coturn_shared_secret: SecretStr = Field(default=SecretStr(""), alias="COTURN_SHARED_SECRET")
    coturn_credential_ttl_s: int = Field(
        default=300, ge=30, le=3600, alias="COTURN_CREDENTIAL_TTL_S"
    )
    redis_url: str = Field(default="", alias="REDIS_URL")
    media_edge_id: str = Field(default="media-edge-local", alias="MEDIA_EDGE_ID")
    voice_core_id: str = Field(default="voice-core-local", alias="VOICE_CORE_ID")
    media_runtime_default: Literal["livekit", "streamcore"] = Field(
        default="livekit", alias="MEDIA_RUNTIME_DEFAULT"
    )
    streamcore_experiment_percent: int = Field(
        default=0, ge=0, le=100, alias="STREAMCORE_EXPERIMENT_PERCENT"
    )
    streamcore_kill_switch: bool = Field(default=False, alias="STREAMCORE_KILL_SWITCH")
    streamcore_slo_gate_enabled: bool = Field(default=False, alias="STREAMCORE_SLO_GATE_ENABLED")
    media_slo_snapshot_ttl_s: int = Field(
        default=120, ge=30, le=900, alias="MEDIA_SLO_SNAPSHOT_TTL_S"
    )
    media_slo_report_token: SecretStr = Field(default=SecretStr(""), alias="MEDIA_SLO_REPORT_TOKEN")
    media_reply_delivery_token: SecretStr = Field(
        default=SecretStr(""), alias="MEDIA_REPLY_DELIVERY_TOKEN"
    )
    streamcore_whip_url: str = Field(default="", alias="STREAMCORE_WHIP_URL")
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
    miniprogram_media_gateway_url: str = Field(default="", alias="MINIPROGRAM_MEDIA_GATEWAY_URL")
    miniprogram_gateway_ticket_ttl_s: int = Field(
        default=90,
        ge=30,
        le=300,
        alias="MINIPROGRAM_GATEWAY_TICKET_TTL_S",
    )
    miniprogram_post_playout_guard_ms: int = Field(
        default=150,
        ge=0,
        le=2_000,
        alias="MINIPROGRAM_POST_PLAYOUT_GUARD_MS",
    )
    memoria_miniprogram_gateway_ticket_secret: SecretStr = Field(
        default=SecretStr(DEV_MINIPROGRAM_GATEWAY_TICKET_SECRET),
        alias="MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET",
    )
    device_media_gateway_url: str = Field(
        default="",
        alias="DEVICE_MEDIA_GATEWAY_URL",
    )
    device_gateway_ticket_ttl_s: int = Field(
        default=300,
        ge=30,
        le=300,
        alias="DEVICE_GATEWAY_TICKET_TTL_S",
    )
    memoria_device_gateway_ticket_secret: SecretStr = Field(
        default=SecretStr(DEV_DEVICE_GATEWAY_TICKET_SECRET),
        alias="MEMORIA_DEVICE_GATEWAY_TICKET_SECRET",
    )
    # Server-owned hardware media runtime selection (ADR-0035 / plan 4.3):
    # livekit_compat keeps the legacy Python device gateway + HS256 device
    # ticket; direct_voice_core selects the Go Media Edge WSS path with an
    # EdDSA/JWKS device ticket. Clients can never force either side.
    device_media_runtime: Literal["livekit_compat", "direct_voice_core"] = Field(
        default="livekit_compat",
        alias="DEVICE_MEDIA_RUNTIME",
    )
    # Direct rollout is independently fenced from the runtime capability.
    # Production defaults to an exact device allowlist so setting
    # DEVICE_MEDIA_RUNTIME=direct_voice_core can never become an accidental
    # fleet-wide switch.  "all" is an explicit later release action.
    device_media_direct_rollout_mode: Literal["allowlist", "all"] = Field(
        default="allowlist",
        alias="DEVICE_MEDIA_DIRECT_ROLLOUT_MODE",
    )
    device_media_direct_canary_device_ids: str = Field(
        default="",
        alias="DEVICE_MEDIA_DIRECT_CANARY_DEVICE_IDS",
    )
    # Independent WSS endpoint for the direct hardware media path. It is
    # distinct from DEVICE_MEDIA_GATEWAY_URL (compat gateway) and from the
    # H5 StreamCore WHIP URL; the direct path never shares a LiveKit room.
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
