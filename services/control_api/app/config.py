"""Control API settings."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from services.common.security_constants import (
    DEV_AUTH_SECRET,
    DEV_DEVICE_GATEWAY_TICKET_SECRET,
    DEV_MESSAGE_IDEMPOTENCY_SECRET,
    DEV_MINIPROGRAM_GATEWAY_TICKET_SECRET,
)
from services.evolution.release_policy import parse_runtime_prompt_families


def _read_key_map(value: str, *, label: str) -> dict[str, str]:
    if not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} read key map must be valid JSON") from exc
    if not isinstance(parsed, dict) or any(
        not isinstance(version, str)
        or not version.strip()
        or not isinstance(key, str)
        or not key.strip()
        for version, key in parsed.items()
    ):
        raise ValueError(f"{label} read key map must be a version-to-key object")
    return parsed


class ControlSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = Field(default="development", alias="ENVIRONMENT")
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")
    allowed_origins: str = Field(default="http://localhost:5173", alias="ALLOWED_ORIGINS")

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
    miniprogram_media_gateway_url: str = Field(
        default="",
        alias="MINIPROGRAM_MEDIA_GATEWAY_URL",
    )
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

    memoria_db_path: str = Field(default="data/memoria.sqlite3", alias="MEMORIA_DB_PATH")
    archive_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_DATABASE_URL",
    )
    archive_compiler_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_COMPILER_DATABASE_URL",
    )
    archive_compiler_role: str = Field(default="", alias="MEMORIA_ARCHIVE_COMPILER_ROLE")
    evolution_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_EVOLUTION_DATABASE_URL",
    )
    guardian_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_GUARDIAN_DATABASE_URL",
    )
    # PR-13 governance roles: separate LOGIN/NOBYPASSRLS credentials for the
    # account export/delete path and the outbox worker.  Never reuse the API
    # guardian DSN; unset means those operations fail closed (503/error).
    guardian_maintenance_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL",
    )
    guardian_worker_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_GUARDIAN_WORKER_DATABASE_URL",
    )
    identity_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_IDENTITY_DATABASE_URL",
    )
    consent_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_CONSENT_DATABASE_URL",
    )
    device_onboarding_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_DEVICE_ONBOARDING_DATABASE_URL",
    )
    device_activation_signing_seed_b64: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_DEVICE_ACTIVATION_SIGNING_SEED_B64",
    )
    identity_registration_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_IDENTITY_REGISTRATION_DATABASE_URL",
    )
    session_runtime_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SESSION_RUNTIME_DATABASE_URL",
    )
    action_executor_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ACTION_EXECUTOR_DATABASE_URL",
    )
    memory_api_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_API_DATABASE_URL",
    )
    memory_worker_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_WORKER_DATABASE_URL",
    )
    memory_bootstrap_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_BOOTSTRAP_DATABASE_URL",
    )
    memory_schema_managed_externally: bool = Field(
        default=False,
        alias="MEMORIA_MEMORY_SCHEMA_MANAGED_EXTERNALLY",
    )
    session_runtime_bootstrap_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SESSION_RUNTIME_BOOTSTRAP_DATABASE_URL",
    )
    session_runtime_schema_managed_externally: bool = Field(
        default=False,
        alias="MEMORIA_SESSION_RUNTIME_SCHEMA_MANAGED_EXTERNALLY",
    )
    session_runtime_projector_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SESSION_RUNTIME_PROJECTOR_DATABASE_URL",
    )
    session_runtime_worker_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SESSION_RUNTIME_WORKER_DATABASE_URL",
    )
    session_runtime_maintenance_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SESSION_RUNTIME_MAINTENANCE_DATABASE_URL",
    )
    identity_db_path: str = Field(
        default="",
        alias="MEMORIA_IDENTITY_DB_PATH",
    )
    consent_db_path: str = Field(
        default="",
        alias="MEMORIA_CONSENT_DB_PATH",
    )
    runtime_profile_signing_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET",
    )
    device_binding_token_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_DEVICE_BINDING_TOKEN_SECRET",
    )
    transfer_evidence_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_TRANSFER_EVIDENCE_SECRET",
    )
    evolution_db_path: str = Field(
        default="",
        alias="MEMORIA_EVOLUTION_DB_PATH",
    )
    evolution_trusted_root_sha256: str = Field(
        default="",
        alias="MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256",
    )
    evolution_min_new_signals: int = Field(
        default=10,
        ge=1,
        le=10_000,
        alias="MEMORIA_EVOLUTION_MIN_NEW_SIGNALS",
    )
    evolution_min_failure_support: int = Field(
        default=2,
        ge=2,
        le=100,
        alias="MEMORIA_EVOLUTION_MIN_FAILURE_SUPPORT",
    )
    evolution_stale_after_days: int = Field(
        default=30,
        ge=1,
        le=3650,
        alias="MEMORIA_EVOLUTION_STALE_AFTER_DAYS",
    )
    evolution_canary_percent: int = Field(
        default=0,
        ge=0,
        le=100,
        alias="MEMORIA_EVOLUTION_CANARY_PERCENT",
    )
    evolution_runtime_prompt_families: str = Field(
        default="weather",
        alias="MEMORIA_EVOLUTION_RUNTIME_PROMPT_FAMILIES",
    )
    evolution_sleep_interval_s: float = Field(
        default=3600.0,
        ge=0,
        le=86_400,
        alias="MEMORIA_EVOLUTION_SLEEP_INTERVAL_S",
    )
    memoria_timezone: str = Field(default="Asia/Shanghai", alias="MEMORIA_TIMEZONE")
    memoria_archive_internal_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_INTERNAL_TOKEN",
    )
    memoria_archive_write_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_WRITE_TOKEN",
    )
    memoria_agent_heartbeat_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_AGENT_HEARTBEAT_TOKEN",
    )
    memoria_memory_read_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_READ_TOKEN",
    )
    memoria_persona_read_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_PERSONA_READ_TOKEN",
    )
    memoria_voice_resolution_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_RESOLUTION_TOKEN",
    )
    memoria_voice_cleanup_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_CLEANUP_TOKEN",
    )
    memoria_interaction_policy_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_INTERACTION_POLICY_TOKEN",
    )
    memoria_response_plan_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_RESPONSE_PLAN_TOKEN",
    )
    memoria_evolution_control_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_EVOLUTION_CONTROL_TOKEN",
    )
    memoria_evolution_validator_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_EVOLUTION_VALIDATOR_TOKEN",
    )
    archive_object_store_path: str = Field(
        default="data/archive-objects",
        alias="MEMORIA_ARCHIVE_OBJECT_STORE_PATH",
    )
    archive_object_encryption_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY",
    )
    archive_object_key_version: str = Field(
        default="archive-object-v1",
        alias="MEMORIA_ARCHIVE_OBJECT_KEY_VERSION",
    )
    archive_object_read_keys: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_OBJECT_READ_KEYS",
    )
    archive_object_bucket: str = Field(default="", alias="MEMORIA_ARCHIVE_OBJECT_BUCKET")
    archive_object_endpoint: str = Field(default="", alias="MEMORIA_ARCHIVE_OBJECT_ENDPOINT")
    archive_object_access_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY",
    )
    archive_object_secret_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_ARCHIVE_OBJECT_SECRET_KEY",
    )
    archive_object_region: str = Field(
        default="cn-beijing",
        alias="MEMORIA_ARCHIVE_OBJECT_REGION",
    )
    archive_object_prefix: str = Field(
        default="archive",
        alias="MEMORIA_ARCHIVE_OBJECT_PREFIX",
    )
    archive_compile_interval_s: float = Field(
        default=1.0,
        ge=0.1,
        le=300,
        alias="MEMORIA_ARCHIVE_COMPILE_INTERVAL_S",
    )
    archive_compile_batch_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        alias="MEMORIA_ARCHIVE_COMPILE_BATCH_SIZE",
    )
    archive_compile_lease_s: float = Field(
        default=300.0,
        ge=5.0,
        le=3600.0,
        alias="MEMORIA_ARCHIVE_COMPILE_LEASE_S",
    )
    archive_compile_max_attempts: int = Field(
        default=8,
        ge=1,
        le=100,
        alias="MEMORIA_ARCHIVE_COMPILE_MAX_ATTEMPTS",
    )
    archive_compile_retry_base_s: float = Field(
        default=2.0,
        ge=0.1,
        le=300.0,
        alias="MEMORIA_ARCHIVE_COMPILE_RETRY_BASE_S",
    )
    archive_compile_retry_max_s: float = Field(
        default=300.0,
        ge=0.1,
        le=3600.0,
        alias="MEMORIA_ARCHIVE_COMPILE_RETRY_MAX_S",
    )
    corpus_retention_interval_s: float = Field(
        default=300.0,
        ge=10.0,
        le=3600.0,
        alias="MEMORIA_CORPUS_RETENTION_INTERVAL_S",
    )
    memory_embedding_url: str = Field(default="", alias="MEMORIA_MEMORY_EMBEDDING_URL")
    memory_embedding_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_MEMORY_EMBEDDING_API_KEY",
    )
    memory_embedding_model: str = Field(default="", alias="MEMORIA_MEMORY_EMBEDDING_MODEL")
    memory_embedding_dimensions: int = Field(
        default=1024,
        ge=1,
        le=2000,
        alias="MEMORIA_MEMORY_EMBEDDING_DIMENSIONS",
    )
    memory_embedding_timeout_s: float = Field(
        default=5.0,
        ge=0.1,
        le=30.0,
        alias="MEMORIA_MEMORY_EMBEDDING_TIMEOUT_S",
    )
    speaker_database_path: str = Field(
        default="data/speakers.sqlite3",
        alias="MEMORIA_SPEAKER_DB_PATH",
    )
    speaker_database_url: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SPEAKER_DATABASE_URL",
    )
    speaker_template_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SPEAKER_TEMPLATE_KEY",
    )
    speaker_internal_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SPEAKER_INTERNAL_TOKEN",
    )
    speaker_embedding_url: str = Field(
        default="",
        alias="MEMORIA_SPEAKER_EMBEDDING_URL",
    )
    speaker_embedding_token: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_SPEAKER_EMBEDDING_TOKEN",
    )
    speaker_embedding_model: str = Field(
        default="campplus-unconfigured",
        alias="MEMORIA_SPEAKER_EMBEDDING_MODEL",
    )
    speaker_embedding_timeout_s: float = Field(
        default=1.0,
        ge=0.1,
        le=5.0,
        alias="MEMORIA_SPEAKER_EMBEDDING_TIMEOUT_S",
    )
    speaker_owner_threshold: float = Field(
        default=0.78,
        ge=0.5,
        le=0.99,
        alias="MEMORIA_SPEAKER_OWNER_THRESHOLD",
    )
    speaker_guest_threshold: float = Field(
        default=0.40,
        ge=0.0,
        le=0.8,
        alias="MEMORIA_SPEAKER_GUEST_THRESHOLD",
    )
    voice_sample_store_path: str = Field(
        default="data/voice-samples",
        alias="MEMORIA_VOICE_SAMPLE_STORE_PATH",
    )
    voice_sample_encryption_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY",
    )
    voice_sample_key_version: str = Field(
        default="voice-sample-v1",
        alias="MEMORIA_VOICE_SAMPLE_KEY_VERSION",
    )
    voice_sample_read_keys: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_SAMPLE_READ_KEYS",
    )
    voice_object_bucket: str = Field(default="", alias="MEMORIA_VOICE_OBJECT_BUCKET")
    voice_object_endpoint: str = Field(default="", alias="MEMORIA_VOICE_OBJECT_ENDPOINT")
    voice_object_access_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_OBJECT_ACCESS_KEY",
    )
    voice_object_secret_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_OBJECT_SECRET_KEY",
    )
    voice_object_region: str = Field(default="cn-beijing", alias="MEMORIA_VOICE_OBJECT_REGION")
    voice_object_prefix: str = Field(default="voice-clone", alias="MEMORIA_VOICE_OBJECT_PREFIX")
    voice_sample_url_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_VOICE_SAMPLE_URL_SECRET",
    )
    voice_sample_url_ttl_s: int = Field(
        default=300,
        ge=30,
        le=1800,
        alias="MEMORIA_VOICE_SAMPLE_URL_TTL_S",
    )
    voice_provider_region: str = Field(
        default="cn-beijing",
        alias="MEMORIA_VOICE_PROVIDER_REGION",
    )
    voice_clone_provider: Literal["alibaba_model_studio", "volcengine_doubao"] = Field(
        default="alibaba_model_studio",
        alias="MEMORIA_VOICE_CLONE_PROVIDER",
    )
    voice_enrollment_url: str = Field(
        default="https://dashscope.aliyuncs.com/api/v1/services/audio/tts/customization",
        alias="MEMORIA_VOICE_ENROLLMENT_URL",
    )
    voice_enrollment_timeout_s: float = Field(
        default=120.0,
        ge=5.0,
        le=180.0,
        alias="MEMORIA_VOICE_ENROLLMENT_TIMEOUT_S",
    )
    voice_target_model: str = Field(
        default="cosyvoice-v3.5-flash",
        alias="MEMORIA_VOICE_TARGET_MODEL",
    )
    doubao_voice_api_key: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_DOUBAO_VOICE_API_KEY",
    )
    doubao_voice_clone_url: str = Field(
        default="https://openspeech.bytedance.com/api/v3/tts/voice_clone",
        alias="MEMORIA_DOUBAO_VOICE_CLONE_URL",
    )
    doubao_voice_query_url: str = Field(
        default="https://openspeech.bytedance.com/api/v3/tts/get_voice",
        alias="MEMORIA_DOUBAO_VOICE_QUERY_URL",
    )
    doubao_voice_clone_poll_interval_s: float = Field(
        default=1.0,
        ge=0.05,
        le=30.0,
        alias="MEMORIA_DOUBAO_VOICE_CLONE_POLL_INTERVAL_S",
    )
    doubao_voice_synth_ready_id_mode: Literal[
        "unverified", "custom_speaker_id", "response_field"
    ] = Field(
        default="unverified",
        alias="MEMORIA_DOUBAO_VOICE_SYNTH_READY_ID_MODE",
    )
    doubao_voice_synth_ready_id_field: str = Field(
        default="",
        alias="MEMORIA_DOUBAO_VOICE_SYNTH_READY_ID_FIELD",
    )
    doubao_voice_expires_at_field: str = Field(
        default="",
        alias="MEMORIA_DOUBAO_VOICE_EXPIRES_AT_FIELD",
    )
    doubao_voice_expires_at_format: Literal["epoch_ms", "rfc3339"] = Field(
        default="epoch_ms",
        alias="MEMORIA_DOUBAO_VOICE_EXPIRES_AT_FORMAT",
    )

    llm_provider: Literal["qwen", "bailian_deepseek", "deepseek"] = Field(
        default="bailian_deepseek",
        alias="LLM_PROVIDER",
    )
    tts_provider: Literal["cosyvoice", "doubao"] = Field(
        default="doubao",
        alias="TTS_PROVIDER",
    )
    dashscope_api_key: SecretStr = Field(default=SecretStr(""), alias="DASHSCOPE_API_KEY")
    dashscope_ws_url: str = Field(default="", alias="DASHSCOPE_WS_URL")
    dashscope_workspace_id: str = Field(default="", alias="DASHSCOPE_WORKSPACE_ID")
    # P1-8: fixed Memoria persona voice on Omni (DashScope preset; not free-form clone).
    # Qwen3.5-Omni Realtime voice (not Qwen-TTS names like Cherry).
    # Liora Mira = 清欢，温柔女声（官方 Omni 音色表）.
    qwen_omni_voice: str = Field(default="Liora Mira", alias="QWEN_OMNI_VOICE")
    # Optional custom/cloned voice id from DashScope voice-clone product; wins over preset.
    qwen_omni_voice_clone_id: str = Field(default="", alias="QWEN_OMNI_VOICE_CLONE_ID")
    qwen_omni_persona_label: str = Field(
        default="Memoria 人设声",
        alias="QWEN_OMNI_PERSONA_LABEL",
    )
    qwen_omni_sdp_timeout_s: float = Field(
        default=10.0,
        ge=1.0,
        le=30.0,
        alias="QWEN_OMNI_SDP_TIMEOUT_S",
    )
    # Shared semantic_vad knobs for Omni Flash A/B sweeps.
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
    dashscope_base_url: str = Field(
        default="https://dashscope.aliyuncs.com/compatible-mode/v1",
        alias="DASHSCOPE_BASE_URL",
    )
    dashscope_summary_model: str = Field(
        default="deepseek-v4-flash",
        alias="DASHSCOPE_SUMMARY_MODEL",
    )
    dashscope_summary_timeout_s: float = Field(
        default=30.0,
        ge=1.0,
        le=120.0,
        alias="DASHSCOPE_SUMMARY_TIMEOUT_S",
    )
    crisis_semantic_enabled: bool = Field(
        default=True,
        alias="CRISIS_SEMANTIC_ENABLED",
    )
    crisis_semantic_model: str = Field(
        default="deepseek-v4-flash",
        min_length=1,
        alias="CRISIS_SEMANTIC_MODEL",
    )
    crisis_semantic_timeout_s: float = Field(
        default=0.8,
        gt=0.0,
        le=2.0,
        alias="CRISIS_SEMANTIC_TIMEOUT_S",
    )
    memory_extraction_model: str = Field(
        default="deepseek-v4-flash",
        alias="MEMORIA_MEMORY_EXTRACTION_MODEL",
    )
    memory_extraction_timeout_s: float = Field(
        default=20.0,
        ge=1.0,
        le=120.0,
        alias="MEMORIA_MEMORY_EXTRACTION_TIMEOUT_S",
    )

    deepseek_api_key: SecretStr = Field(default=SecretStr(""), alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(
        default="https://api.deepseek.com",
        alias="DEEPSEEK_BASE_URL",
    )
    deepseek_summary_model: str = Field(
        default="deepseek-v4-flash",
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
    memoria_message_idempotency_secret: SecretStr = Field(
        default=SecretStr(DEV_MESSAGE_IDEMPOTENCY_SECRET),
        alias="MEMORIA_MESSAGE_IDEMPOTENCY_SECRET",
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
        default=900,
        ge=600,
        le=900,
        alias="MEMORIA_AUTH_TOKEN_TTL_S",
    )
    memoria_auth_refresh_ttl_s: int = Field(
        default=2_592_000,
        ge=86_400,
        le=2_592_000,
        alias="MEMORIA_AUTH_REFRESH_TTL_S",
    )
    memoria_refresh_cookie_name: str = Field(
        default="memoria_refresh",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        alias="MEMORIA_REFRESH_COOKIE_NAME",
    )
    wechat_miniprogram_appid: str = Field(
        default="",
        alias="WECHAT_MINIPROGRAM_APPID",
    )
    wechat_miniprogram_appsecret: SecretStr = Field(
        default=SecretStr(""),
        alias="WECHAT_MINIPROGRAM_APPSECRET",
    )
    memoria_wechat_identity_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_WECHAT_IDENTITY_SECRET",
    )
    wechat_jscode2session_endpoint: str = Field(
        default="https://api.weixin.qq.com/sns/jscode2session",
        alias="WECHAT_JSCODE2SESSION_ENDPOINT",
    )
    wechat_access_token_endpoint: str = Field(
        default="https://api.weixin.qq.com/cgi-bin/token",
        alias="WECHAT_ACCESS_TOKEN_ENDPOINT",
    )
    wechat_phone_number_endpoint: str = Field(
        default="https://api.weixin.qq.com/wxa/business/getuserphonenumber",
        alias="WECHAT_PHONE_NUMBER_ENDPOINT",
    )
    wechat_auth_timeout_s: float = Field(
        default=8.0,
        ge=1.0,
        le=30.0,
        alias="WECHAT_AUTH_TIMEOUT_S",
    )
    wechat_avatar_max_bytes: int = Field(
        default=2 * 1024 * 1024,
        ge=1024,
        le=4 * 1024 * 1024,
        alias="WECHAT_AVATAR_MAX_BYTES",
    )
    wechat_avatar_public_base_url: str = Field(
        default="",
        alias="WECHAT_AVATAR_PUBLIC_BASE_URL",
    )
    legacy_auth_compat_until: datetime | None = Field(
        default=None,
        alias="MEMORIA_LEGACY_AUTH_COMPAT_UNTIL",
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

    def coturn_urls_list(self) -> list[str]:
        return [url.strip() for url in self.coturn_urls.split(",") if url.strip()]

    @field_validator("evolution_runtime_prompt_families")
    @classmethod
    def validate_evolution_runtime_prompt_families(cls, value: str) -> str:
        parse_runtime_prompt_families(value)
        return value

    @field_validator("legacy_auth_compat_until", mode="before")
    @classmethod
    def require_absolute_utc_legacy_auth_cutoff(cls, value: object) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("MEMORIA_LEGACY_AUTH_COMPAT_UNTIL must be absolute UTC") from exc
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() != timedelta(0)
        ):
            raise ValueError("MEMORIA_LEGACY_AUTH_COMPAT_UNTIL must be absolute UTC")
        cutoff = value.astimezone(UTC)
        remaining = cutoff - datetime.now(UTC)
        if remaining > timedelta(hours=24):
            raise ValueError("MEMORIA_LEGACY_AUTH_COMPAT_UNTIL must be within 24 hours")
        return cutoff

    def legacy_auth_compat_active(self, *, now: datetime | None = None) -> bool:
        cutoff = self.legacy_auth_compat_until
        current = now or datetime.now(UTC)
        return cutoff is not None and current < cutoff

    def wechat_identity_secret(self) -> str:
        configured = self.memoria_wechat_identity_secret.get_secret_value().strip()
        if configured:
            return configured
        return self.memoria_auth_secret.get_secret_value()

    def wechat_avatar_base_url(self) -> str:
        return (self.wechat_avatar_public_base_url.strip() or self.public_base_url).rstrip("/")

    def archive_object_read_key_map(self) -> dict[str, str]:
        return _read_key_map(
            self.archive_object_read_keys.get_secret_value(),
            label="archive object",
        )

    def evolution_trusted_root(self) -> str:
        """Return an immutable release anchor for candidate manifests.

        Operators should set ``MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256`` to the
        reviewed release manifest digest.  Development and older deployments
        fall back to a deterministic digest of the immutable release tag, so
        candidate records remain auditable without silently accepting an empty
        trust root.
        """

        configured = self.evolution_trusted_root_sha256.strip().lower()
        if configured:
            if len(configured) != 64 or any(
                character not in "0123456789abcdef" for character in configured
            ):
                raise ValueError("MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256 must be a sha256 digest")
            return configured
        release_tag = self.memoria_release_tag.strip() or "development"
        return hashlib.sha256(f"memoria:evolution:{release_tag}".encode()).hexdigest()

    def evolution_internal_token(self) -> str:
        configured = self.memoria_evolution_control_token.get_secret_value().strip()
        if configured or self.environment == "production":
            return configured
        return self.memoria_archive_internal_token.get_secret_value()

    def evolution_validator_token(self) -> str:
        configured = self.memoria_evolution_validator_token.get_secret_value().strip()
        if configured or self.environment == "production":
            return configured
        return self.evolution_internal_token()

    def evolution_sqlite_path(self) -> str:
        configured = self.evolution_db_path.strip()
        if configured:
            return configured
        memory_path = Path(self.memoria_db_path)
        return str(memory_path.with_name(f"{memory_path.stem}-evolution.sqlite3"))

    def identity_sqlite_path(self) -> str:
        configured = self.identity_db_path.strip()
        if configured:
            return configured
        memory_path = Path(self.memoria_db_path)
        return str(memory_path.with_name(f"{memory_path.stem}-identity.sqlite3"))

    def consent_sqlite_path(self) -> str:
        configured = self.consent_db_path.strip()
        if configured:
            return configured
        memory_path = Path(self.memoria_db_path)
        return str(memory_path.with_name(f"{memory_path.stem}-consent.sqlite3"))

    def runtime_profile_signing_key(self) -> bytes:
        configured = self.runtime_profile_signing_secret.get_secret_value().strip()
        if configured:
            return configured.encode("utf-8")
        if self.environment == "production":
            return b""
        return self.memoria_auth_secret.get_secret_value().encode("utf-8")

    def device_binding_token_key(self) -> bytes:
        configured = self.device_binding_token_secret.get_secret_value().strip()
        if configured:
            return configured.encode("utf-8")
        if self.environment == "production":
            return b""
        return self.memoria_auth_secret.get_secret_value().encode("utf-8")

    def transfer_evidence_key(self) -> bytes:
        configured = self.transfer_evidence_secret.get_secret_value().strip()
        if configured:
            return configured.encode("utf-8")
        return b""

    def voice_sample_read_key_map(self) -> dict[str, str]:
        return _read_key_map(
            self.voice_sample_read_keys.get_secret_value(),
            label="voice sample",
        )

    def internal_token(
        self,
        capability: Literal[
            "archive_write",
            "agent_heartbeat",
            "memory_read",
            "persona_read",
            "voice_resolution",
            "voice_cleanup",
            "interaction_policy",
            "response_plan",
        ],
    ) -> str:
        configured = {
            "archive_write": self.memoria_archive_write_token,
            "agent_heartbeat": self.memoria_agent_heartbeat_token,
            "memory_read": self.memoria_memory_read_token,
            "persona_read": self.memoria_persona_read_token,
            "voice_resolution": self.memoria_voice_resolution_token,
            "voice_cleanup": self.memoria_voice_cleanup_token,
            "interaction_policy": self.memoria_interaction_policy_token,
            "response_plan": self.memoria_response_plan_token,
        }[capability].get_secret_value()
        if configured or self.environment == "production":
            return configured
        return self.memoria_archive_internal_token.get_secret_value()

    def validate_device_direct_media(self) -> None:
        """Fail closed for the direct hardware media runtime in production.

        Direct mode requires an Ed25519 private key (the Go edge JWKS verifier
        only carries Ed25519 keys), the independent device WSS URL, a signing
        key id for JWKS rotation, and an authenticated HTTPS edge control
        channel. The private key must actually parse as Ed25519 and the
        Control-to-Edge mTLS files must be readable, matching, and signed by
        the configured CA. An HS256 token can never satisfy the JWKS contract,
        so a missing or unparseable private key is a startup error instead of a
        silent downgrade.
        """

        direct_url = self.device_direct_media_wss_url.strip()
        if not direct_url.startswith("wss://"):
            raise ValueError(
                "production direct device media requires secure "
                "DEVICE_DIRECT_MEDIA_WSS_URL"
            )
        self._validate_direct_ed25519_private_key()
        if not self.streamcore_token_key_id.strip():
            raise ValueError(
                "production direct device media requires STREAMCORE_TOKEN_KEY_ID"
            )
        if not self.media_edge_internal_control_url.strip().startswith("https://"):
            raise ValueError(
                "production direct device media requires HTTPS "
                "MEDIA_EDGE_INTERNAL_CONTROL_URL (mTLS control channel to the Go "
                "media edge); direct mode fails closed without it"
            )
        if len(self.media_edge_internal_control_token.get_secret_value().strip()) < 32:
            raise ValueError(
                "production direct device media requires "
                "MEDIA_EDGE_INTERNAL_CONTROL_TOKEN with at least 32 characters"
            )
        close_token = self.media_edge_device_close_report_token.get_secret_value().strip()
        if len(close_token) < 32:
            raise ValueError(
                "production direct device media requires independent "
                "MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN with at least 32 characters"
            )
        if hmac.compare_digest(
            close_token,
            self.media_edge_internal_control_token.get_secret_value().strip(),
        ):
            raise ValueError(
                "production direct device media close-report token must be "
                "independent from MEDIA_EDGE_INTERNAL_CONTROL_TOKEN"
            )
        mtls_paths = (
            self.media_edge_internal_control_ca_file.strip(),
            self.media_edge_internal_control_client_cert_file.strip(),
            self.media_edge_internal_control_client_key_file.strip(),
        )
        if not all(mtls_paths):
            raise ValueError(
                "production direct device media requires Control-to-Edge mTLS "
                "CA, client certificate and client key files"
            )
        self._validate_control_mtls_files(
            self.media_edge_internal_control_ca_file.strip(),
            self.media_edge_internal_control_client_cert_file.strip(),
            self.media_edge_internal_control_client_key_file.strip(),
        )
        if self.device_media_direct_rollout_mode == "allowlist":
            from services.control_api.app.media_runtime import direct_canary_device_ids

            if not direct_canary_device_ids(self):
                raise ValueError(
                    "production direct device media allowlist mode requires "
                    "DEVICE_MEDIA_DIRECT_CANARY_DEVICE_IDS"
                )

    def _validate_direct_ed25519_private_key(self) -> None:
        """Parse the configured StreamCore/device private key at startup.

        Direct device tickets are EdDSA-only; an unreadable, unparseable or
        non-Ed25519 key is a startup error, never a silent HS256 downgrade.
        """
        path = self.streamcore_token_private_key_file.strip()
        inline = self.streamcore_token_private_key_pem.get_secret_value().strip()
        if path and inline:
            raise ValueError(
                "production direct device media requires exactly one "
                "StreamCore private key source"
            )
        material = ""
        if inline:
            material = inline
        elif path:
            try:
                material = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(
                    "production direct device media requires a readable "
                    "STREAMCORE_TOKEN_PRIVATE_KEY_FILE"
                ) from exc
        else:
            raise ValueError(
                "production direct device media requires an Ed25519 private key "
                "(STREAMCORE_TOKEN_PRIVATE_KEY_FILE or "
                "STREAMCORE_TOKEN_PRIVATE_KEY_PEM); HS256 is not accepted "
                "for direct device media"
            )
        if len(material) > 16_384:
            raise ValueError("production direct device media private key is too large")
        try:
            key = serialization.load_pem_private_key(material.encode("utf-8"), password=None)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise ValueError(
                "production direct device media requires a parseable unencrypted "
                "Ed25519 private key"
            ) from exc
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(
                "production direct device media requires an Ed25519 private key"
            )

    @staticmethod
    def _validate_control_mtls_files(ca_file: str, cert_file: str, key_file: str) -> None:
        """Read and cross-check the Control-to-Edge mTLS bundle at startup.

        The client certificate must parse, match its private key, and be
        directly issued by the configured CA; any mismatch fails closed before
        a single device ticket is minted.
        """
        try:
            ca_cert = x509.load_pem_x509_certificates(Path(ca_file).read_bytes())[0]
            client_cert = x509.load_pem_x509_certificates(Path(cert_file).read_bytes())[0]
            client_key = serialization.load_pem_private_key(
                Path(key_file).read_bytes(), password=None
            )
        except (OSError, ValueError, TypeError, binascii.Error) as exc:
            raise ValueError(
                "production direct device media requires readable, parseable "
                "Control-to-Edge mTLS files"
            ) from exc
        cert_public = client_cert.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key_public = client_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if cert_public != key_public:
            raise ValueError(
                "production direct device media Control-to-Edge client "
                "certificate and key do not match"
            )
        try:
            client_cert.verify_directly_issued_by(ca_cert)
        except (ValueError, InvalidSignature) as exc:
            raise ValueError(
                "production direct device media Control-to-Edge client "
                "certificate is not signed by the configured CA"
            ) from exc

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
        streamcore_rollout_enabled = (
            self.media_runtime_default == "streamcore"
            and self.streamcore_experiment_percent > 0
            and not self.streamcore_kill_switch
        )
        if streamcore_rollout_enabled:
            if not self.streamcore_whip_url.startswith("https://"):
                raise ValueError("production StreamCore rollout requires HTTPS WHIP URL")
            has_private_key = bool(
                self.streamcore_token_private_key_file.strip()
                or self.streamcore_token_private_key_pem.get_secret_value().strip()
            )
            if not has_private_key and len(self.streamcore_token_secret.get_secret_value()) < 32:
                raise ValueError(
                    "production StreamCore rollout requires an Ed25519 private key "
                    "or STREAMCORE_TOKEN_SECRET fallback"
                )
            if has_private_key and not self.streamcore_token_key_id.strip():
                raise ValueError("production StreamCore rollout requires STREAMCORE_TOKEN_KEY_ID")
            if not self.streamcore_slo_gate_enabled:
                raise ValueError(
                    "production StreamCore rollout requires STREAMCORE_SLO_GATE_ENABLED"
                )
            if len(self.media_slo_report_token.get_secret_value()) < 32:
                raise ValueError("production StreamCore rollout requires MEDIA_SLO_REPORT_TOKEN")
        auth_secret = self.memoria_auth_secret.get_secret_value()
        if auth_secret == DEV_AUTH_SECRET or len(auth_secret) < 32:
            raise ValueError("production requires an independent MEMORIA_AUTH_SECRET (>=32 chars)")
        if auth_secret == self.livekit_api_secret:
            raise ValueError("MEMORIA_AUTH_SECRET must differ from LIVEKIT_API_SECRET")
        if self.miniprogram_media_gateway_url.strip() and (
            not self.wechat_miniprogram_appid.strip()
            or not self.wechat_miniprogram_appsecret.get_secret_value().strip()
        ):
            raise ValueError(
                "production Mini Program requires WECHAT_MINIPROGRAM_APPID "
                "and WECHAT_MINIPROGRAM_APPSECRET"
            )
        if (
            self.miniprogram_media_gateway_url.strip()
            and not self.wechat_avatar_base_url().startswith("https://")
        ):
            raise ValueError(
                "production Mini Program requires an HTTPS "
                "WECHAT_AVATAR_PUBLIC_BASE_URL or PUBLIC_BASE_URL"
            )
        wechat_identity_secret = self.memoria_wechat_identity_secret.get_secret_value().strip()
        if self.miniprogram_media_gateway_url.strip() and (
            len(wechat_identity_secret) < 32
            or wechat_identity_secret in {auth_secret, self.livekit_api_secret}
        ):
            raise ValueError(
                "production Mini Program requires an independent "
                "MEMORIA_WECHAT_IDENTITY_SECRET (>=32 chars)"
            )
        gateway_url = self.miniprogram_media_gateway_url.strip()
        gateway_ticket_secret = self.memoria_miniprogram_gateway_ticket_secret.get_secret_value()
        if gateway_url:
            if not gateway_url.startswith("wss://"):
                raise ValueError("production Mini Program gateway must use secure WSS")
            if (
                gateway_ticket_secret == DEV_MINIPROGRAM_GATEWAY_TICKET_SECRET
                or len(gateway_ticket_secret) < 32
                or gateway_ticket_secret in {auth_secret, self.livekit_api_secret}
            ):
                raise ValueError(
                    "production requires an independent Mini Program gateway ticket secret"
                )
        device_gateway_url = self.device_media_gateway_url.strip()
        device_gateway_ticket_secret = (
            self.memoria_device_gateway_ticket_secret.get_secret_value()
        )
        if device_gateway_url:
            if not device_gateway_url.startswith("wss://"):
                raise ValueError("production device media gateway must use secure WSS")
            if (
                device_gateway_ticket_secret == DEV_DEVICE_GATEWAY_TICKET_SECRET
                or len(device_gateway_ticket_secret) < 32
                or device_gateway_ticket_secret
                in {
                    auth_secret,
                    self.livekit_api_secret,
                    gateway_ticket_secret,
                }
            ):
                raise ValueError(
                    "production requires an independent device gateway ticket secret"
                )
            onboarding_url = self.device_onboarding_database_url.get_secret_value().strip()
            if not onboarding_url.startswith(("postgresql://", "postgres://")):
                raise ValueError(
                    "production device media gateway requires "
                    "MEMORIA_DEVICE_ONBOARDING_DATABASE_URL for PostgreSQL"
                )
            if (urlsplit(onboarding_url).username or "") != "memoria_device_onboarding_api":
                raise ValueError(
                    "production MEMORIA_DEVICE_ONBOARDING_DATABASE_URL must use the "
                    "independent memoria_device_onboarding_api role"
                )
            signing_seed_b64 = (
                self.device_activation_signing_seed_b64.get_secret_value().strip()
            )
            try:
                signing_seed = base64.b64decode(signing_seed_b64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError(
                    "production requires a valid independent "
                    "MEMORIA_DEVICE_ACTIVATION_SIGNING_SEED_B64"
                ) from exc
            if len(signing_seed) != 32 or signing_seed == hashlib.sha256(
                b"memoria-device-activation-v1\0" + auth_secret.encode("utf-8")
            ).digest():
                raise ValueError(
                    "production requires an independent 32-byte "
                    "MEMORIA_DEVICE_ACTIVATION_SIGNING_SEED_B64"
                )
        if self.device_media_runtime == "direct_voice_core":
            self.validate_device_direct_media()
        capability_tokens = {
            "MEMORIA_ARCHIVE_WRITE_TOKEN": self.internal_token("archive_write"),
            "MEMORIA_AGENT_HEARTBEAT_TOKEN": self.internal_token("agent_heartbeat"),
            "MEMORIA_MEMORY_READ_TOKEN": self.internal_token("memory_read"),
            "MEMORIA_PERSONA_READ_TOKEN": self.internal_token("persona_read"),
            "MEMORIA_VOICE_RESOLUTION_TOKEN": self.internal_token("voice_resolution"),
            "MEMORIA_VOICE_CLEANUP_TOKEN": self.internal_token("voice_cleanup"),
            "MEMORIA_INTERACTION_POLICY_TOKEN": self.internal_token("interaction_policy"),
            "MEMORIA_RESPONSE_PLAN_TOKEN": self.internal_token("response_plan"),
            "MEMORIA_EVOLUTION_CONTROL_TOKEN": self.evolution_internal_token(),
            "MEMORIA_EVOLUTION_VALIDATOR_TOKEN": self.evolution_validator_token(),
        }
        if any(len(token) < 32 for token in capability_tokens.values()):
            raise ValueError("production requires ten capability-scoped internal tokens")
        if len(set(capability_tokens.values())) != len(capability_tokens) or any(
            token in {auth_secret, self.livekit_api_secret} for token in capability_tokens.values()
        ):
            raise ValueError("production internal capability tokens must be independent")
        if gateway_url and gateway_ticket_secret in capability_tokens.values():
            raise ValueError(
                "Mini Program gateway ticket secret must differ from internal capability tokens"
            )
        if (
            device_gateway_url
            and device_gateway_ticket_secret in capability_tokens.values()
        ):
            raise ValueError(
                "device gateway ticket secret must differ from internal capability tokens"
            )
        if wechat_identity_secret and (
            wechat_identity_secret in capability_tokens.values()
            or wechat_identity_secret == gateway_ticket_secret
        ):
            raise ValueError(
                "WeChat identity secret must differ from gateway and capability tokens"
            )
        message_idempotency_secret = self.memoria_message_idempotency_secret.get_secret_value()
        if (
            message_idempotency_secret == DEV_MESSAGE_IDEMPOTENCY_SECRET
            or len(message_idempotency_secret) < 32
            or message_idempotency_secret
            in {
                auth_secret,
                wechat_identity_secret,
                self.livekit_api_secret,
                *capability_tokens.values(),
            }
        ):
            raise ValueError(
                "production requires an independent MEMORIA_MESSAGE_IDEMPOTENCY_SECRET (>=32 chars)"
            )
        archive_url = self.archive_database_url.get_secret_value()
        if not archive_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("production requires MEMORIA_ARCHIVE_DATABASE_URL for PostgreSQL")
        evolution_url = self.evolution_database_url.get_secret_value()
        if not evolution_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("production requires MEMORIA_EVOLUTION_DATABASE_URL for PostgreSQL")
        evolution_user = urlsplit(evolution_url).username or ""
        archive_user = urlsplit(archive_url).username or ""
        if (
            evolution_url == archive_url
            or evolution_user != "memoria_evolution"
            or evolution_user == archive_user
        ):
            raise ValueError(
                "production requires MEMORIA_EVOLUTION_DATABASE_URL to use the independent "
                "memoria_evolution role"
            )
        guardian_url = self.guardian_database_url.get_secret_value()
        if not guardian_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("production requires MEMORIA_GUARDIAN_DATABASE_URL for PostgreSQL")
        identity_url = self.identity_database_url.get_secret_value()
        if not identity_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("production requires MEMORIA_IDENTITY_DATABASE_URL for PostgreSQL")
        consent_url = self.consent_database_url.get_secret_value()
        if not consent_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("production requires MEMORIA_CONSENT_DATABASE_URL for PostgreSQL")
        consent_user = urlsplit(consent_url).username or ""
        if (
            consent_user != "memoria_consent"
            or consent_url
            in {
                archive_url,
                evolution_url,
                guardian_url,
                identity_url,
            }
            or consent_user
            in {
                archive_user,
                evolution_user,
                urlsplit(guardian_url).username or "",
                urlsplit(identity_url).username or "",
            }
        ):
            raise ValueError(
                "production requires MEMORIA_CONSENT_DATABASE_URL to use the "
                "independent memoria_consent role"
            )
        identity_registration_url = self.identity_registration_database_url.get_secret_value()
        if not identity_registration_url.startswith(("postgresql://", "postgres://")):
            raise ValueError(
                "production requires MEMORIA_IDENTITY_REGISTRATION_DATABASE_URL "
                "(dedicated memoria_identity_registration role) for PostgreSQL "
                "person registration; missing registration authority fails closed"
            )
        identity_registration_user = urlsplit(identity_registration_url).username or ""
        if identity_registration_user != "memoria_identity_registration":
            raise ValueError(
                "production requires MEMORIA_IDENTITY_REGISTRATION_DATABASE_URL "
                "to use the independent memoria_identity_registration role"
            )
        runtime_profile_secret = self.runtime_profile_signing_secret.get_secret_value()
        device_binding_secret = self.device_binding_token_secret.get_secret_value()
        transfer_secret = self.transfer_evidence_secret.get_secret_value()
        if len(runtime_profile_secret) < 32 or runtime_profile_secret in {
            auth_secret,
            self.livekit_api_secret,
            *capability_tokens.values(),
        }:
            raise ValueError(
                "production requires an independent "
                "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET (>=32 chars)"
            )
        if len(device_binding_secret) < 32 or device_binding_secret in {
            auth_secret,
            self.livekit_api_secret,
            runtime_profile_secret,
            *capability_tokens.values(),
        }:
            raise ValueError(
                "production requires an independent "
                "MEMORIA_DEVICE_BINDING_TOKEN_SECRET (>=32 chars)"
            )
        if len(transfer_secret) < 32 or transfer_secret in {
            auth_secret,
            self.livekit_api_secret,
            runtime_profile_secret,
            device_binding_secret,
            *capability_tokens.values(),
        }:
            raise ValueError(
                "production requires an independent MEMORIA_TRANSFER_EVIDENCE_SECRET (>=32 chars)"
            )
        guardian_user = urlsplit(guardian_url).username or ""
        identity_user = urlsplit(identity_url).username or ""
        if (
            guardian_url in {archive_url, evolution_url}
            or guardian_user != "memoria_guardian"
            or guardian_user in {archive_user, evolution_user}
        ):
            raise ValueError(
                "production requires MEMORIA_GUARDIAN_DATABASE_URL to use the independent "
                "memoria_guardian role"
            )
        guardian_maintenance_url = self.guardian_maintenance_database_url.get_secret_value()
        if not guardian_maintenance_url.startswith(("postgresql://", "postgres://")):
            raise ValueError(
                "production requires MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL for "
                "PostgreSQL (dedicated memoria_guardian_maintenance role); account "
                "export/delete fails closed without it"
            )
        guardian_worker_url = self.guardian_worker_database_url.get_secret_value()
        if not guardian_worker_url.startswith(("postgresql://", "postgres://")):
            raise ValueError(
                "production requires MEMORIA_GUARDIAN_WORKER_DATABASE_URL for "
                "PostgreSQL (dedicated memoria_guardian_worker role); outbox "
                "delivery fails closed without it"
            )
        guardian_maintenance_user = urlsplit(guardian_maintenance_url).username or ""
        guardian_worker_user = urlsplit(guardian_worker_url).username or ""
        if (
            guardian_maintenance_user != "memoria_guardian_maintenance"
            or guardian_worker_user != "memoria_guardian_worker"
            or guardian_maintenance_url == guardian_url
            or guardian_worker_url == guardian_url
            or guardian_maintenance_url == guardian_worker_url
            or guardian_maintenance_user == guardian_user
            or guardian_worker_user == guardian_user
            or guardian_maintenance_user == guardian_worker_user
        ):
            raise ValueError(
                "production requires MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL and "
                "MEMORIA_GUARDIAN_WORKER_DATABASE_URL to use the independent "
                "memoria_guardian_maintenance / memoria_guardian_worker roles, "
                "distinct from each other and from the API memoria_guardian role"
            )
        if (
            identity_url in {archive_url, evolution_url, guardian_url}
            or identity_user != "memoria_identity"
            or identity_user in {archive_user, evolution_user, guardian_user}
        ):
            raise ValueError(
                "production requires MEMORIA_IDENTITY_DATABASE_URL to use the independent "
                "memoria_identity role"
            )
        speaker_token = self.speaker_internal_token.get_secret_value()
        embedding_token = self.speaker_embedding_token.get_secret_value()
        template_key = self.speaker_template_key.get_secret_value()
        if len(speaker_token) < 32 or speaker_token in {
            auth_secret,
            self.livekit_api_secret,
            *capability_tokens.values(),
        }:
            raise ValueError("production requires an independent speaker internal token")
        if len(embedding_token) < 32 or embedding_token in {
            auth_secret,
            self.livekit_api_secret,
            *capability_tokens.values(),
            speaker_token,
        }:
            raise ValueError("production requires an independent speaker model token")
        try:
            from cryptography.fernet import Fernet

            Fernet(template_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("production requires a valid speaker template key") from exc
        if not self.speaker_embedding_url.startswith(("http://", "https://")):
            raise ValueError("production requires a speaker embedding service URL")
        if self.speaker_guest_threshold >= self.speaker_owner_threshold:
            raise ValueError("speaker thresholds must satisfy guest < owner")
        speaker_database_url = self.speaker_database_url.get_secret_value() or archive_url
        if not speaker_database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("production requires PostgreSQL for speaker profiles")
        voice_key = self.voice_sample_encryption_key.get_secret_value()
        voice_url_secret = self.voice_sample_url_secret.get_secret_value()
        if not voice_key or not self.voice_sample_key_version.strip():
            raise ValueError("production requires encrypted voice sample storage")
        try:
            from cryptography.fernet import Fernet

            Fernet(voice_key.encode("ascii"))
            voice_read_keys = self.voice_sample_read_key_map()
            if self.voice_sample_key_version in voice_read_keys:
                raise ValueError("active version cannot be read-only")
            for read_key in voice_read_keys.values():
                Fernet(read_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("production requires valid voice sample object keys") from exc
        if voice_key == template_key:
            raise ValueError(
                "production requires independent voice sample and speaker template keys"
            )
        if len(voice_url_secret) < 32 or voice_url_secret in {
            auth_secret,
            self.livekit_api_secret,
            *capability_tokens.values(),
            speaker_token,
            embedding_token,
            template_key,
            voice_key,
        }:
            raise ValueError("production requires an independent voice sample URL secret")
        if not self.voice_object_bucket.strip():
            raise ValueError("production requires an S3-compatible voice object bucket")
        voice_access_key = self.voice_object_access_key.get_secret_value().strip()
        voice_secret_key = self.voice_object_secret_key.get_secret_value().strip()
        if bool(voice_access_key) != bool(voice_secret_key) or (
            self.voice_object_endpoint.strip() and not voice_access_key
        ):
            raise ValueError("production requires a complete voice object credential pair")
        if self.voice_clone_provider == "alibaba_model_studio":
            if not self.voice_target_model.startswith("cosyvoice-v3.5-"):
                raise ValueError("CosyVoice voice cloning requires a CosyVoice v3.5 target")
        else:
            if self.voice_target_model != "seed-icl-2.0":
                raise ValueError("Doubao voice cloning requires seed-icl-2.0")
            if not self.doubao_voice_api_key.get_secret_value().strip():
                raise ValueError("Doubao voice cloning requires an independent API key")
            if self.doubao_voice_synth_ready_id_mode == "unverified":
                raise ValueError("Doubao voice cloning requires a smoke-verified synth ID mapping")
            if (
                self.doubao_voice_synth_ready_id_mode == "response_field"
                and not self.doubao_voice_synth_ready_id_field.strip()
            ):
                raise ValueError("Doubao response-field synth ID mapping requires a field path")
            if not self.doubao_voice_expires_at_field.strip():
                raise ValueError("Doubao voice cloning requires a provider expiry field path")
        archive_object_key = self.archive_object_encryption_key.get_secret_value()
        if not archive_object_key or not self.archive_object_key_version.strip():
            raise ValueError("production requires encrypted archive object storage")
        try:
            from cryptography.fernet import Fernet

            Fernet(archive_object_key.encode("ascii"))
            archive_read_keys = self.archive_object_read_key_map()
            if self.archive_object_key_version in archive_read_keys:
                raise ValueError("active version cannot be read-only")
            for read_key in archive_read_keys.values():
                Fernet(read_key.encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("production requires valid archive object keys") from exc
        if archive_object_key in {voice_key, template_key}:
            raise ValueError("production requires an independent archive object key")
        key_material = [
            template_key,
            voice_key,
            *voice_read_keys.values(),
            archive_object_key,
            *archive_read_keys.values(),
        ]
        decoded_key_material = {
            base64.urlsafe_b64decode(value.encode("ascii")) for value in key_material
        }
        if len(decoded_key_material) != len(key_material):
            raise ValueError(
                "production encryption key material must be unique across active and read-only keyrings"
            )
        if not self.archive_object_bucket.strip():
            raise ValueError("production requires an S3-compatible archive object bucket")
        archive_access_key = self.archive_object_access_key.get_secret_value().strip()
        archive_secret_key = self.archive_object_secret_key.get_secret_value().strip()
        if bool(archive_access_key) != bool(archive_secret_key) or (
            self.archive_object_endpoint.strip() and not archive_access_key
        ):
            raise ValueError("production requires a complete archive object credential pair")
        if self.archive_object_bucket.strip() == self.voice_object_bucket.strip():
            raise ValueError("production requires independent archive and voice object buckets")
        release_tag = self.memoria_release_tag.strip().lower()
        if release_tag in ("", "latest", "development"):
            raise ValueError("production requires an immutable MEMORIA_RELEASE_TAG")
        session_runtime_url = self.session_runtime_database_url.get_secret_value().strip()
        action_executor_url = self.action_executor_database_url.get_secret_value().strip()
        session_runtime_bootstrap_url = (
            self.session_runtime_bootstrap_database_url.get_secret_value().strip()
        )
        session_runtime_projector_url = (
            self.session_runtime_projector_database_url.get_secret_value().strip()
        )
        session_runtime_worker_url = (
            self.session_runtime_worker_database_url.get_secret_value().strip()
        )
        session_runtime_maintenance_url = (
            self.session_runtime_maintenance_database_url.get_secret_value().strip()
        )
        session_runtime_dsns = {
            "MEMORIA_SESSION_RUNTIME_DATABASE_URL": (
                session_runtime_url,
                "memoria_session_api",
            ),
            "MEMORIA_ACTION_EXECUTOR_DATABASE_URL": (
                action_executor_url,
                "memoria_action_executor",
            ),
            "MEMORIA_SESSION_RUNTIME_PROJECTOR_DATABASE_URL": (
                session_runtime_projector_url,
                "memoria_session_projector",
            ),
            "MEMORIA_SESSION_RUNTIME_WORKER_DATABASE_URL": (
                session_runtime_worker_url,
                "memoria_session_worker",
            ),
            "MEMORIA_SESSION_RUNTIME_MAINTENANCE_DATABASE_URL": (
                session_runtime_maintenance_url,
                "memoria_session_maintenance",
            ),
        }
        for field_name, (dsn, expected_role) in session_runtime_dsns.items():
            if not dsn.startswith(("postgresql://", "postgres://")):
                raise ValueError(
                    f"production requires {field_name} for PostgreSQL "
                    f"(dedicated {expected_role} role)"
                )
            if (urlsplit(dsn).username or "") != expected_role:
                raise ValueError(
                    f"production requires {field_name} to use the independent {expected_role} role"
                )
        externally_managed = self.session_runtime_schema_managed_externally
        has_bootstrap_url = session_runtime_bootstrap_url.startswith(
            ("postgresql://", "postgres://")
        )
        if has_bootstrap_url == externally_managed:
            raise ValueError(
                "production requires exactly one Session Runtime schema management "
                "mode: MEMORIA_SESSION_RUNTIME_BOOTSTRAP_DATABASE_URL or "
                "MEMORIA_SESSION_RUNTIME_SCHEMA_MANAGED_EXTERNALLY=true"
            )
        runtime_roles = {expected_role for _, expected_role in session_runtime_dsns.values()}
        runtime_users = {urlsplit(dsn).username or "" for dsn, _ in session_runtime_dsns.values()}
        if len(runtime_users) != len(runtime_roles):
            raise ValueError("production Session Runtime PostgreSQL roles must be distinct")
        existing_domain_roles = {
            archive_user,
            evolution_user,
            urlsplit(guardian_url).username or "",
            identity_user,
            identity_registration_user,
            consent_user,
            urlsplit(self.archive_compiler_database_url.get_secret_value()).username or "",
        }
        if runtime_users & existing_domain_roles:
            raise ValueError(
                "production Session Runtime PostgreSQL roles must not be reused "
                "by archive, evolution, guardian, identity, or compiler"
            )
        if has_bootstrap_url:
            bootstrap_user = urlsplit(session_runtime_bootstrap_url).username or ""
            if (
                not bootstrap_user
                or bootstrap_user in runtime_roles
                or session_runtime_bootstrap_url
                in {dsn for dsn, _ in session_runtime_dsns.values()}
            ):
                raise ValueError(
                    "production requires an independent Session Runtime bootstrap "
                    "owner/admin DSN distinct from all runtime login roles"
                )
        memory_api_url = self.memory_api_database_url.get_secret_value().strip()
        memory_worker_url = self.memory_worker_database_url.get_secret_value().strip()
        memory_bootstrap_url = (
            self.memory_bootstrap_database_url.get_secret_value().strip()
        )
        memory_dsns = {
            "MEMORIA_MEMORY_API_DATABASE_URL": (
                memory_api_url,
                "memoria_memory_api",
            ),
            "MEMORIA_MEMORY_WORKER_DATABASE_URL": (
                memory_worker_url,
                "memoria_memory_worker",
            ),
        }
        for field_name, (dsn, expected_role) in memory_dsns.items():
            if not dsn.startswith(("postgresql://", "postgres://")):
                raise ValueError(
                    f"production requires {field_name} for PostgreSQL "
                    f"(dedicated {expected_role} role)"
                )
            if (urlsplit(dsn).username or "") != expected_role:
                raise ValueError(
                    f"production requires {field_name} to use the independent "
                    f"{expected_role} role"
                )
        memory_external = self.memory_schema_managed_externally
        has_memory_bootstrap = memory_bootstrap_url.startswith(
            ("postgresql://", "postgres://")
        )
        if has_memory_bootstrap == memory_external:
            raise ValueError(
                "production requires exactly one MemoryScope schema management "
                "mode: MEMORIA_MEMORY_BOOTSTRAP_DATABASE_URL or "
                "MEMORIA_MEMORY_SCHEMA_MANAGED_EXTERNALLY=true"
            )
        memory_users = {
            urlsplit(memory_api_url).username or "",
            urlsplit(memory_worker_url).username or "",
        }
        if len(memory_users) != 2 or memory_users & (
            runtime_users | existing_domain_roles
        ):
            raise ValueError(
                "production MemoryScope PostgreSQL roles must be distinct and "
                "must not reuse another domain role"
            )
        if has_memory_bootstrap:
            memory_bootstrap_user = urlsplit(memory_bootstrap_url).username or ""
            if (
                not memory_bootstrap_user
                or memory_bootstrap_user in memory_users
                or memory_bootstrap_user in runtime_users
            ):
                raise ValueError(
                    "production requires an independent MemoryScope bootstrap "
                    "owner/admin DSN"
                )
        if not self.evolution_trusted_root_sha256.strip():
            raise ValueError(
                "production requires MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256 from the release manifest"
            )
        if self.evolution_sleep_interval_s == 0:
            raise ValueError("production requires an enabled evolution sleep scheduler")
        compiler_url = self.archive_compiler_database_url.get_secret_value()
        if not compiler_url.startswith(("postgresql://", "postgres://")):
            raise ValueError(
                "production requires MEMORIA_ARCHIVE_COMPILER_DATABASE_URL for PostgreSQL"
            )
        compiler_role = self.archive_compiler_role.strip()
        compiler_user = urlsplit(compiler_url).username or ""
        if (
            compiler_url == archive_url
            or not compiler_role
            or compiler_role != compiler_user
            or compiler_role == archive_user
        ):
            raise ValueError("production requires an independent archive compiler database role")
        if (
            not self.memory_embedding_url.startswith(("http://", "https://"))
            or not self.memory_embedding_api_key.get_secret_value()
            or not self.memory_embedding_model.strip()
            or not 1 <= self.memory_embedding_dimensions <= 2000
        ):
            raise ValueError(
                "production requires MEMORIA_MEMORY_EMBEDDING_URL, "
                "MEMORIA_MEMORY_EMBEDDING_API_KEY, MEMORIA_MEMORY_EMBEDDING_MODEL "
                "and MEMORIA_MEMORY_EMBEDDING_DIMENSIONS"
            )
        if streamcore_rollout_enabled:
            coturn_urls = self.coturn_urls_list()
            if not coturn_urls:
                raise ValueError("production StreamCore rollout requires independent COTURN_URLS")
            if any(not url.startswith(("turn:", "turns:")) for url in coturn_urls):
                raise ValueError("production COTURN_URLS must use turn: or turns:")
            coturn_secret = self.coturn_shared_secret.get_secret_value().strip()
            if len(coturn_secret) < 32:
                raise ValueError(
                    "production StreamCore rollout requires COTURN_SHARED_SECRET (>=32 chars)"
                )
            if coturn_secret in {auth_secret, self.livekit_api_secret, *capability_tokens.values()}:
                raise ValueError("production COTURN_SHARED_SECRET must be independent")
