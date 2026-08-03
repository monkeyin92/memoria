#!/usr/bin/env python3
"""Generate validated least-privilege service envs for the P0-P6 upgrade."""

from __future__ import annotations

import argparse
import base64
import secrets
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote, urlsplit

from services.agent.src.config import (
    SELF_HOSTED_ENDPOINTING_MAX_DELAY_S,
    SELF_HOSTED_ENDPOINTING_MIN_DELAY_S,
    SELF_HOSTED_FALSE_INTERRUPTION_TIMEOUT_S,
    AgentSettings,
    validate_doubao_auth,
)
from services.control_api.app.config import ControlSettings
from services.miniprogram_gateway.config import MiniProgramGatewaySettings

from scripts.split_production_env import (
    _AGENT_EXTRA_KEYS,
    _CONTROL_EXTRA_KEYS,
    _GATEWAY_EXTRA_KEYS,
    _MEDIA_EDGE_EXTRA_KEYS,
    _aliases,
    _read_env,
    _write_env,
    split_env,
)

_MODEL_VERSION = "campplus-cn-common@v1.0.0+ckpt.3388cf5f+onnx.7a39d2e5e566+fbank.v1"


def _token() -> str:
    return secrets.token_urlsafe(48)


def _fernet_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def _keep_or_create(values: dict[str, str], key: str, factory: Callable[[], str]) -> str:
    existing = values.get(key, "")
    return existing if existing.strip() else factory()


def _required(values: dict[str, str], key: str) -> str:
    value = values.get(key, "").strip()
    if not value:
        raise ValueError(f"missing required bootstrap value: {key}")
    return value


def _postgres_dsn(*, user: str, password: str) -> str:
    return f"postgresql://{user}:{quote(password, safe='')}@memoria-postgres:5432/memoria"


def _miniprogram_gateway_url(public_base_url: str) -> str:
    parsed = urlsplit(public_base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("PUBLIC_BASE_URL must be HTTPS to derive Mini Program gateway URL")
    prefix = parsed.path.rstrip("/")
    if prefix.endswith("/memoria-api"):
        prefix = prefix.removesuffix("/memoria-api")
    return f"wss://{parsed.netloc}{prefix}/memoria-mini-media/v1/mini-program/media"


def prepare(
    *,
    legacy: dict[str, str],
    postgres: dict[str, str],
    minio: dict[str, str],
    release_tag: str,
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    known = (
        _aliases(ControlSettings)
        | _aliases(AgentSettings)
        | set(_CONTROL_EXTRA_KEYS)
        | set(_AGENT_EXTRA_KEYS)
        | _aliases(MiniProgramGatewaySettings)
        | set(_GATEWAY_EXTRA_KEYS)
        | set(_MEDIA_EDGE_EXTRA_KEYS)
    )
    values = {key: value for key, value in legacy.items() if key in known}
    validate_doubao_auth(
        api_key=values.get("DOUBAO_TTS_API_KEY", ""),
        app_id=values.get("DOUBAO_TTS_APP_ID", ""),
        access_token=values.get("DOUBAO_TTS_ACCESS_TOKEN", ""),
        required=True,
    )
    dashscope_key = _required(values, "DASHSCOPE_API_KEY")
    _required(values, "WECHAT_MINIPROGRAM_APPID")
    _required(values, "WECHAT_MINIPROGRAM_APPSECRET")
    app_password = _required(postgres, "MEMORIA_DB_APP_PASSWORD")
    compiler_password = _required(postgres, "MEMORIA_DB_COMPILER_PASSWORD")
    archive_access = _required(minio, "MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY")
    archive_secret = _required(minio, "MEMORIA_ARCHIVE_OBJECT_SECRET_KEY")
    voice_access = _required(minio, "MEMORIA_VOICE_OBJECT_ACCESS_KEY")
    voice_secret = _required(minio, "MEMORIA_VOICE_OBJECT_SECRET_KEY")
    voice_clone_provider = (
        values.get("MEMORIA_VOICE_CLONE_PROVIDER", "alibaba_model_studio").strip()
        or "alibaba_model_studio"
    )
    default_voice_target = (
        "seed-icl-2.0" if voice_clone_provider == "volcengine_doubao" else "cosyvoice-v3.5-flash"
    )
    public_base_url = _required(values, "PUBLIC_BASE_URL")
    gateway_url = (
        values.get("MINIPROGRAM_MEDIA_GATEWAY_URL", "").strip()
        or _miniprogram_gateway_url(public_base_url)
    )
    streamcore_token_secret = _keep_or_create(values, "STREAMCORE_TOKEN_SECRET", _token)
    values.update(
        {
            "ENVIRONMENT": "production",
            "OFFLINE_MOCK": "false",
            "LLM_PROVIDER": "bailian_deepseek",
            "INTERRUPT_SEMANTIC_ENABLED": "true",
            "INTERRUPT_SEMANTIC_MODEL": "deepseek-v4-flash",
            "INTERRUPT_SEMANTIC_TIMEOUT_S": "1.2",
            "DASHSCOPE_SUMMARY_MODEL": "deepseek-v4-flash",
            "MEMORIA_MEMORY_EXTRACTION_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_FAST_MODEL": "deepseek-v4-flash",
            "DEEPSEEK_DEEP_MODEL": "deepseek-v4-flash",
            "MEMORIA_RELEASE_TAG": release_tag,
            # Control API signs StreamCore tokens; the Go edge verifies the
            # same short-lived credential in its own least-privilege env.
            "STREAMCORE_TOKEN_SECRET": streamcore_token_secret,
            "MEDIA_EDGE_CONTROL_URL": values.get(
                "MEDIA_EDGE_CONTROL_URL", "http://media-edge:8080"
            ),
            "MEDIA_EDGE_CONTROL_TIMEOUT_S": values.get(
                "MEDIA_EDGE_CONTROL_TIMEOUT_S", "2"
            ),
            "MEDIA_EDGE_JWT_SECRET": streamcore_token_secret,
            "MEDIA_EDGE_JWT_ISSUER": values.get("JWT_ISSUER", "voice-agent"),
            "MEDIA_EDGE_JWT_AUDIENCE": "memoria-media",
            "MEDIA_EDGE_HTTP_ADDR": ":8080",
            "MEDIA_EDGE_HEALTHCHECK_URL": "http://127.0.0.1:8080/readyz",
            "MEDIA_EDGE_VOICE_CORE_REQUIRED": "true",
            "MEDIA_EDGE_VOICE_CORE_ADDR": "voice-core-media-bridge:7001",
            "MEDIA_EDGE_VOICE_CORE_CA_FILE": "/etc/memoria-media-runtime/media-edge-ca.crt",
            "MEDIA_EDGE_VOICE_CORE_CLIENT_CERT_FILE": "/etc/memoria-media-runtime/media-edge-client.crt",
            "MEDIA_EDGE_VOICE_CORE_CLIENT_KEY_FILE": "/etc/memoria-media-runtime/media-edge-client.key",
            "MEDIA_EDGE_VOICE_CORE_SERVER_NAME": "voice-core-media-bridge",
            "MEDIA_EDGE_VOICE_CORE_ALLOW_INSECURE_DEVELOPMENT": "false",
            "MEDIA_EDGE_VOICE_CORE_CONNECT_TIMEOUT_MS": "5000",
            "MEMORIA_ARCHIVE_DATABASE_URL": _postgres_dsn(
                user="memoria_app", password=app_password
            ),
            "MEMORIA_ARCHIVE_COMPILER_DATABASE_URL": _postgres_dsn(
                user="memoria_compiler", password=compiler_password
            ),
            "MEMORIA_ARCHIVE_COMPILER_ROLE": "memoria_compiler",
            "MEMORIA_ARCHIVE_WRITE_TOKEN": _token(),
            "MEMORIA_MESSAGE_IDEMPOTENCY_SECRET": _keep_or_create(
                values, "MEMORIA_MESSAGE_IDEMPOTENCY_SECRET", _token
            ),
            "MEMORIA_WECHAT_IDENTITY_SECRET": _keep_or_create(
                values, "MEMORIA_WECHAT_IDENTITY_SECRET", _token
            ),
            "MEMORIA_AGENT_HEARTBEAT_TOKEN": _token(),
            "MEMORIA_MEMORY_READ_TOKEN": _token(),
            "MEMORIA_PERSONA_READ_TOKEN": _token(),
            "MEMORIA_VOICE_RESOLUTION_TOKEN": _token(),
            "MEMORIA_VOICE_CLEANUP_TOKEN": _token(),
            "MEMORIA_INTERACTION_POLICY_TOKEN": _token(),
            "MEMORIA_RESPONSE_PLAN_TOKEN": _token(),
            "MEMORIA_RESPONSE_PLAN_URL": "http://control-api:8000/v1/interaction/response-plan",
            "MEMORIA_RESPONSE_PLAN_TIMEOUT_S": "0.8",
            "MINIPROGRAM_MEDIA_GATEWAY_URL": gateway_url,
            "MINIPROGRAM_GATEWAY_TICKET_TTL_S": "90",
            "MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET": _keep_or_create(
                values,
                "MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET",
                _token,
            ),
            "MINIPROGRAM_GATEWAY_TICKET_MAX_TTL_S": "300",
            "MINIPROGRAM_GATEWAY_LIVEKIT_TOKEN_TTL_S": "300",
            "MEMORIA_SPEAKER_INTERNAL_TOKEN": _token(),
            "MEMORIA_SPEAKER_EMBEDDING_TOKEN": _token(),
            "MEMORIA_SPEAKER_TEMPLATE_KEY": _keep_or_create(
                values, "MEMORIA_SPEAKER_TEMPLATE_KEY", _fernet_key
            ),
            "MEMORIA_SPEAKER_EMBEDDING_URL": ("http://speaker-model:8001/v1/embeddings/speaker"),
            "MEMORIA_SPEAKER_EMBEDDING_MODEL": _MODEL_VERSION,
            "MEMORIA_SPEAKER_AUTHORITY_ENABLED": "true",
            "MEMORIA_SPEAKER_AUTHORITY_URL": ("http://control-api:8000/v1/speakers/classify"),
            "MEMORIA_SPEAKER_AUTHORITY_TIMEOUT_S": "0.4",
            "MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY": _keep_or_create(
                values, "MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY", _fernet_key
            ),
            "MEMORIA_VOICE_SAMPLE_KEY_VERSION": _keep_or_create(
                values, "MEMORIA_VOICE_SAMPLE_KEY_VERSION", lambda: "voice-sample-v1"
            ),
            "MEMORIA_VOICE_SAMPLE_URL_SECRET": _token(),
            "MEMORIA_VOICE_OBJECT_BUCKET": "memoria-voice",
            "MEMORIA_VOICE_OBJECT_ENDPOINT": "http://memoria-minio:9000",
            "MEMORIA_VOICE_OBJECT_REGION": "us-east-1",
            "MEMORIA_VOICE_OBJECT_ACCESS_KEY": voice_access,
            "MEMORIA_VOICE_OBJECT_SECRET_KEY": voice_secret,
            "MEMORIA_VOICE_OBJECT_PREFIX": "voice-clone",
            "MEMORIA_VOICE_CLONE_PROVIDER": voice_clone_provider,
            "MEMORIA_VOICE_TARGET_MODEL": values.get(
                "MEMORIA_VOICE_TARGET_MODEL", default_voice_target
            ).strip()
            or default_voice_target,
            "MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY": _keep_or_create(
                values, "MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY", _fernet_key
            ),
            "MEMORIA_ARCHIVE_OBJECT_KEY_VERSION": _keep_or_create(
                values, "MEMORIA_ARCHIVE_OBJECT_KEY_VERSION", lambda: "archive-object-v1"
            ),
            "MEMORIA_ARCHIVE_OBJECT_BUCKET": "memoria-archive",
            "MEMORIA_ARCHIVE_OBJECT_ENDPOINT": "http://memoria-minio:9000",
            "MEMORIA_ARCHIVE_OBJECT_REGION": "us-east-1",
            "MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY": archive_access,
            "MEMORIA_ARCHIVE_OBJECT_SECRET_KEY": archive_secret,
            "MEMORIA_ARCHIVE_OBJECT_PREFIX": "archive",
            "MEMORIA_MEMORY_EMBEDDING_URL": (
                "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"
            ),
            "MEMORIA_MEMORY_EMBEDDING_API_KEY": dashscope_key,
            "MEMORIA_MEMORY_EMBEDDING_MODEL": "text-embedding-v4",
            "MEMORIA_MEMORY_EMBEDDING_DIMENSIONS": "1024",
            "MEMORIA_ARCHIVE_SINK_ENABLED": "true",
            "MEMORIA_ARCHIVE_SESSION_EVENTS_URL": (
                "http://control-api:8000/v1/archive/session-events"
            ),
            "MEMORIA_ARCHIVE_SPOOL_KEY": _keep_or_create(
                values, "MEMORIA_ARCHIVE_SPOOL_KEY", _fernet_key
            ),
            "MEMORIA_ARCHIVE_SPOOL_PATH": "/data/archive-events.spool",
            "MEMORIA_ARCHIVE_SPOOL_MAX_BYTES": "8388608",
            "MEMORIA_PERSONA_ENABLED": "true",
            "MEMORIA_PERSONA_CAPSULE_URL": ("http://control-api:8000/v1/persona/session-capsule"),
            "MEMORIA_MEMORY_CONTEXT_ENABLED": "true",
            "MEMORIA_MEMORY_CONTEXT_URL": ("http://control-api:8000/v1/archive/session-context"),
            "MEMORIA_VOICE_PROFILE_ENABLED": "true",
            "MEMORIA_VOICE_PROFILE_URL": ("http://control-api:8000/v1/voices/session-resolution"),
            "LIVEKIT_ADAPTIVE_INTERRUPTION": "false",
            # coturn uses REST/HMAC credentials; the shared secret stays in
            # the control-api env and is never copied to H5 or the device.
            "COTURN_URLS": values.get(
                "COTURN_URLS",
                "turn:turn.example.com:3478,turns:turn.example.com:5349",
            ),
            "COTURN_REALM": values.get("COTURN_REALM", "memoria"),
            "COTURN_SHARED_SECRET": _keep_or_create(
                values, "COTURN_SHARED_SECRET", _token
            ),
            "COTURN_CREDENTIAL_TTL_S": values.get("COTURN_CREDENTIAL_TTL_S", "300"),
            "PREEMPTIVE_GENERATION": "false",
            "PREEMPTIVE_TTS": "false",
            "ENDPOINTING_MIN_DELAY_S": f"{SELF_HOSTED_ENDPOINTING_MIN_DELAY_S:.2f}",
            "ENDPOINTING_MAX_DELAY_S": f"{SELF_HOSTED_ENDPOINTING_MAX_DELAY_S:.2f}",
            "FALSE_INTERRUPTION_TIMEOUT_S": (
                f"{SELF_HOSTED_FALSE_INTERRUPTION_TIMEOUT_S:.2f}"
            ),
        }
    )
    values.pop("MEMORIA_ARCHIVE_INTERNAL_TOKEN", None)
    control, agent, speaker_model, gateway, media_edge = split_env(values)
    ControlSettings.model_validate(control).validate_production()
    AgentSettings.model_validate(agent)
    MiniProgramGatewaySettings.model_validate(gateway).validate_production()
    if not speaker_model:
        raise ValueError("speaker-model env must contain its scoped token")
    return control, agent, speaker_model, gateway, media_edge


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy", required=True, type=Path)
    parser.add_argument("--postgres", required=True, type=Path)
    parser.add_argument("--minio", required=True, type=Path)
    parser.add_argument("--release-tag", required=True)
    parser.add_argument("--control", required=True, type=Path)
    parser.add_argument("--agent", required=True, type=Path)
    parser.add_argument("--speaker-model", required=True, type=Path)
    parser.add_argument("--gateway", required=True, type=Path)
    parser.add_argument("--media-edge", required=True, type=Path)
    args = parser.parse_args()
    for path in (args.control, args.agent, args.speaker_model, args.gateway, args.media_edge):
        if path.exists():
            raise FileExistsError(f"refusing to replace existing candidate: {path}")
    control, agent, speaker_model, gateway, media_edge = prepare(
        legacy=_read_env(args.legacy),
        postgres=_read_env(args.postgres),
        minio=_read_env(args.minio),
        release_tag=args.release_tag,
    )
    _write_env(args.control, control)
    _write_env(args.agent, agent)
    _write_env(args.speaker_model, speaker_model)
    _write_env(args.gateway, gateway)
    _write_env(args.media_edge, media_edge)
    print(
        f"created validated env candidates: control={len(control)}, "
        f"agent={len(agent)}, speaker-model={len(speaker_model)}, gateway={len(gateway)}, "
        f"media-edge={len(media_edge)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
