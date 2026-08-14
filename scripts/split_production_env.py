#!/usr/bin/env python3
"""Split the operator env into least-privilege service files."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from services.agent.src.config import AgentSettings, validate_doubao_auth
from services.control_api.app.config import ControlSettings
from services.device_media_gateway.config import DeviceMediaGatewaySettings
from services.miniprogram_gateway.config import MiniProgramGatewaySettings

_AGENT_EXTRA_KEYS = frozenset(
    {
        "BACKCHANNEL_BOUNDARY_END_S",
        "BACKCHANNEL_BOUNDARY_START_S",
        "DASHSCOPE_REGION",
        "DEEPSEEK_DEEP_TOTAL_TIMEOUT_S",
        "DEEPSEEK_FAST_FIRST_TOKEN_TIMEOUT_S",
        "DEEPSEEK_FAST_MAX_TOKENS",
        "DEEPSEEK_FAST_TEMPERATURE",
        "DEEPSEEK_FAST_TOTAL_TIMEOUT_S",
        "DOUBAO_TTS_CONNECT_TIMEOUT_S",
        "DOUBAO_TTS_FIRST_AUDIO_TIMEOUT_S",
        "DOUBAO_TTS_LOUDNESS_RATE",
        "DOUBAO_TTS_PITCH",
        "DOUBAO_TTS_SPEECH_RATE",
        "DOUBAO_TTS_STYLE_CONTROL_ENABLED",
        "DOUBAO_TTS_TOTAL_TIMEOUT_S",
        "DOUBAO_TTS_VOICE_REGISTRY",
        "ENDPOINTING_ALPHA",
        "ENDPOINTING_MAX_DELAY_S",
        "ENDPOINTING_MIN_DELAY_S",
        "ENDPOINTING_MODE",
        "FALSE_INTERRUPTION_TIMEOUT_S",
        "FUNASR_CHUNK_MS",
        "FUNASR_CONNECT_TIMEOUT_S",
        "FUNASR_HEARTBEAT",
        "FUNASR_LANGUAGE",
        "FUNASR_RECONNECT_AUDIO_MS",
        "FUNASR_RESULT_TIMEOUT_S",
        "FUNASR_SEMANTIC_PUNCTUATION",
        "INTERRUPTION_MIN_DURATION_S",
        "QWEN_EMOTION_CONNECT_TIMEOUT_S",
        "QWEN_EMOTION_MODEL",
        "QWEN_EMOTION_QUEUE_CHUNKS",
        "QWEN_EMOTION_RECONNECT_DELAY_S",
        "QWEN_EMOTION_SAMPLE_RATE",
        "QWEN_EMOTION_WS_URL",
        "MEMORIA_RUNTIME_PROFILE_VERIFY_KEY",
        "VAD_MIN_SPEECH_DURATION_S",
        "VAD_PREFIX_PADDING_DURATION_S",
    }
)

_CONTROL_EXTRA_KEYS = frozenset(
    {
        "AUDIO_RETENTION_DAYS",
        "AUDIO_RETENTION_ENABLED",
        "LOG_LEVEL",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "PII_REDACTION_ENABLED",
        "PROMETHEUS_PORT",
        "SENTRY_DSN",
    }
)

_GATEWAY_EXTRA_KEYS = frozenset({"LOG_LEVEL"})

# The Go edge is a separate trust boundary.  Keep its JWT and bridge
# connection material out of both Control API and Agent env files.
_MEDIA_EDGE_EXTRA_KEYS = frozenset(
    {
        "MEDIA_EDGE_ALLOW_INSECURE_DEVELOPMENT",
        "MEDIA_EDGE_DEVICE_ACOUSTIC_REGISTRY_FILE",
        "MEDIA_EDGE_DEVICE_ALLOW_HS256_DEVELOPMENT",
        "MEDIA_EDGE_DEVICE_CLOSE_REPORT_TIMEOUT_MS",
        "MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN",
        "MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL",
        "MEDIA_EDGE_DEVICE_JWT_AUDIENCE",
        "MEDIA_EDGE_DEVICE_JWT_ISSUER",
        "MEDIA_EDGE_DEVICE_REQUIRED",
        "MEDIA_EDGE_DEVICE_STATE_KEY_PREFIX",
        "MEDIA_EDGE_DEVICE_STATE_REDIS_CA_FILE",
        "MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_CERT_FILE",
        "MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_KEY_FILE",
        "MEDIA_EDGE_DEVICE_STATE_REDIS_SERVER_NAME",
        "MEDIA_EDGE_DEVICE_STATE_REDIS_URL",
        "MEDIA_EDGE_DEVICE_STATE_TIMEOUT_MS",
        "MEDIA_EDGE_DEVICE_LEASE_CHECK_INTERVAL_MS",
        "MEDIA_EDGE_DEVICE_LEASE_TTL_MS",
        "MEDIA_EDGE_DEVICE_WSS_ADDR",
        "MEDIA_EDGE_DEVICE_WSS_ENABLED",
        "MEDIA_EDGE_HEALTHCHECK_CA_FILE",
        "MEDIA_EDGE_HEALTHCHECK_CLIENT_CERT_FILE",
        "MEDIA_EDGE_HEALTHCHECK_CLIENT_KEY_FILE",
        "MEDIA_EDGE_HEALTHCHECK_URL",
        "MEDIA_EDGE_HTTP_ADDR",
        "MEDIA_EDGE_INTERNAL_HTTP_ADDR",
        "MEDIA_EDGE_INTERNAL_CONTROL_TOKEN",
        "MEDIA_EDGE_INTERNAL_TLS_CERT_FILE",
        "MEDIA_EDGE_INTERNAL_TLS_CLIENT_CA_FILE",
        "MEDIA_EDGE_INTERNAL_TLS_KEY_FILE",
        "MEDIA_EDGE_INSTANCE_ID",
        "MEDIA_EDGE_INTERACTION_AUTHORITY",
        "MEDIA_EDGE_JWT_AUDIENCE",
        "MEDIA_EDGE_JWT_CLOCK_SKEW_S",
        "MEDIA_EDGE_JWT_KEY_ID",
        "MEDIA_EDGE_JWT_MAX_TTL_S",
        "MEDIA_EDGE_JWT_PUBLIC_KEY_FILE",
        "MEDIA_EDGE_JWT_PUBLIC_KEY_PEM",
        "MEDIA_EDGE_JWT_ISSUER",
        "MEDIA_EDGE_JWT_SECRET",
        "MEDIA_EDGE_MAX_PENDING_FRAMES",
        "MEDIA_EDGE_VOICE_CORE_ADDR",
        "MEDIA_EDGE_VOICE_CORE_ALLOW_INSECURE_DEVELOPMENT",
        "MEDIA_EDGE_VOICE_CORE_CA_FILE",
        "MEDIA_EDGE_VOICE_CORE_CLIENT_CERT_FILE",
        "MEDIA_EDGE_VOICE_CORE_CLIENT_KEY_FILE",
        "MEDIA_EDGE_VOICE_CORE_CONNECT_TIMEOUT_MS",
        "MEDIA_EDGE_VOICE_CORE_REQUIRED",
        "MEDIA_EDGE_VOICE_CORE_SERVER_NAME",
        "MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON",
        "MEDIA_EDGE_WEBRTC_PUBLIC_IPS",
        "MEDIA_EDGE_WEBRTC_UDP_PORT_MAX",
        "MEDIA_EDGE_WEBRTC_UDP_PORT_MIN",
    }
)


def _aliases(
    settings_type: type[AgentSettings]
    | type[ControlSettings]
    | type[DeviceMediaGatewaySettings]
    | type[MiniProgramGatewaySettings],
) -> set[str]:
    return {
        str(field.alias)
        for field in settings_type.model_fields.values()
        if isinstance(field.alias, str)
    }


def split_env(
    values: dict[str, str],
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, str],
]:
    if "DOUBAO_TTS_SECRET_KEY" in values:
        raise ValueError("DOUBAO_TTS_SECRET_KEY is not used and must not be deployed")
    validate_doubao_auth(
        api_key=values.get("DOUBAO_TTS_API_KEY", ""),
        app_id=values.get("DOUBAO_TTS_APP_ID", ""),
        access_token=values.get("DOUBAO_TTS_ACCESS_TOKEN", ""),
        required=False,
    )
    clone_key = values.get("MEMORIA_DOUBAO_VOICE_API_KEY", "").strip()
    runtime_tts_secrets = {
        values.get("DOUBAO_TTS_API_KEY", "").strip(),
        values.get("DOUBAO_TTS_ACCESS_TOKEN", "").strip(),
    }
    runtime_tts_secrets.discard("")
    if clone_key and clone_key in runtime_tts_secrets:
        raise ValueError("Doubao voice clone and runtime TTS credentials must be independent")
    if values.get("ENVIRONMENT", "").strip().lower() == "production":
        if values.get("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "").strip():
            raise ValueError("production forbids the legacy all-access internal token")
        streamcore_secret = values.get("STREAMCORE_TOKEN_SECRET", "").strip()
        media_edge_secret = values.get("MEDIA_EDGE_JWT_SECRET", "").strip()
        private_key = values.get("STREAMCORE_TOKEN_PRIVATE_KEY_FILE", "").strip() or values.get(
            "STREAMCORE_TOKEN_PRIVATE_KEY_PEM", ""
        ).strip()
        public_key = values.get("MEDIA_EDGE_JWT_PUBLIC_KEY_FILE", "").strip() or values.get(
            "MEDIA_EDGE_JWT_PUBLIC_KEY_PEM", ""
        ).strip()
        if private_key and not public_key:
            raise ValueError("Ed25519 StreamCore signing requires a Media Edge public key")
        if public_key and not private_key:
            raise ValueError("Media Edge public key requires a StreamCore private key")
        if (streamcore_secret or media_edge_secret) and streamcore_secret != media_edge_secret:
            raise ValueError(
                "production StreamCore and Media Edge token secrets must both be set and match"
            )
        if private_key and (streamcore_secret or media_edge_secret):
            raise ValueError("do not deploy both Ed25519 and shared StreamCore token secrets")
        encryption_keys = [
            values.get(name, "").strip()
            for name in (
                "MEMORIA_ARCHIVE_SPOOL_KEY",
                "MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY",
                "MEMORIA_SPEAKER_TEMPLATE_KEY",
                "MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY",
            )
        ]
        configured_keys = [key for key in encryption_keys if key]
        if len(configured_keys) != len(set(configured_keys)):
            raise ValueError("production encryption keys must be independent")
        runtime_profile_signing = values.get(
            "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET", ""
        ).strip()
        runtime_profile_verify = values.get(
            "MEMORIA_RUNTIME_PROFILE_VERIFY_KEY", ""
        ).strip()
        if not runtime_profile_signing or not runtime_profile_verify:
            raise ValueError(
                "production Runtime Profile signing and verify keys must both be set"
            )
        if runtime_profile_signing != runtime_profile_verify:
            raise ValueError(
                "production Runtime Profile signing and verify keys must match"
            )
    control_keys = _aliases(ControlSettings) | set(_CONTROL_EXTRA_KEYS)
    agent_keys = _aliases(AgentSettings) | set(_AGENT_EXTRA_KEYS)
    gateway_keys = _aliases(MiniProgramGatewaySettings) | set(_GATEWAY_EXTRA_KEYS)
    device_gateway_keys = _aliases(DeviceMediaGatewaySettings) | set(_GATEWAY_EXTRA_KEYS)
    media_edge_keys = set(_MEDIA_EDGE_EXTRA_KEYS)
    known = control_keys | agent_keys | gateway_keys | device_gateway_keys | media_edge_keys
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(f"unrouted production env keys: {', '.join(unknown)}")
    control = {key: value for key, value in values.items() if key in control_keys}
    agent = {key: value for key, value in values.items() if key in agent_keys}
    gateway = {key: value for key, value in values.items() if key in gateway_keys}
    device_gateway = {
        key: value for key, value in values.items() if key in device_gateway_keys
    }
    device_gateway.pop("MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET", None)
    media_edge = {key: value for key, value in values.items() if key in media_edge_keys}
    capability_flags = (
        ("MEMORIA_ARCHIVE_WRITE_TOKEN", "MEMORIA_ARCHIVE_SINK_ENABLED", True),
        ("MEMORIA_MEMORY_READ_TOKEN", "MEMORIA_MEMORY_CONTEXT_ENABLED", False),
        ("MEMORIA_PERSONA_READ_TOKEN", "MEMORIA_PERSONA_ENABLED", False),
        ("MEMORIA_VOICE_RESOLUTION_TOKEN", "MEMORIA_VOICE_PROFILE_ENABLED", False),
    )
    for token, flag, default in capability_flags:
        enabled = values.get(flag, str(default)).strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            agent.pop(token, None)
    embedding_token = values.get("MEMORIA_SPEAKER_EMBEDDING_TOKEN", "").strip()
    speaker_model = {"MEMORIA_SPEAKER_MODEL_TOKEN": embedding_token} if embedding_token else {}
    return control, agent, speaker_model, gateway, device_gateway, media_edge


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if not separator or not key or not key.replace("_", "").isalnum() or not key.isupper():
            raise ValueError(f"invalid env assignment at {path}:{line_number}")
        if key in values:
            raise ValueError(f"duplicate env key at {path}:{line_number}: {key}")
        values[key] = value
    return values


def _write_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "# Generated by split_production_env.py; chmod 0600\n" + "".join(
        f"{key}={values[key]}\n" for key in sorted(values)
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--control", type=Path, default=Path("/etc/memoria-control-api.env"))
    parser.add_argument("--agent", type=Path, default=Path("/etc/memoria-agent.env"))
    parser.add_argument(
        "--speaker-model",
        type=Path,
        default=Path("/etc/memoria-speaker-model.env"),
    )
    parser.add_argument(
        "--gateway",
        type=Path,
        default=Path("/etc/memoria-miniprogram-gateway.env"),
    )
    parser.add_argument(
        "--device-gateway",
        type=Path,
        default=Path("/etc/memoria-device-media-gateway.env"),
    )
    parser.add_argument(
        "--media-edge",
        type=Path,
        default=Path("/etc/memoria-media-edge.env"),
    )
    args = parser.parse_args()
    control, agent, speaker_model, gateway, device_gateway, media_edge = split_env(
        _read_env(args.source)
    )
    _write_env(args.control, control)
    _write_env(args.agent, agent)
    _write_env(args.speaker_model, speaker_model)
    _write_env(args.gateway, gateway)
    _write_env(args.device_gateway, device_gateway)
    _write_env(args.media_edge, media_edge)
    print(
        f"wrote {len(control)} Control API keys, {len(agent)} Agent keys "
        f"{len(speaker_model)} Speaker Model keys, {len(gateway)} Gateway keys, "
        f"{len(device_gateway)} Device Gateway keys and {len(media_edge)} Media Edge keys"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
