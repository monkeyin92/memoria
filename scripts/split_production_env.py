#!/usr/bin/env python3
"""Split the operator env into least-privilege service files."""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from services.agent.src.config import AgentSettings
from services.control_api.app.config import ControlSettings

_AGENT_EXTRA_KEYS = frozenset(
    {
        "BACKCHANNEL_BOUNDARY_END_S",
        "BACKCHANNEL_BOUNDARY_START_S",
        "COSYVOICE_CONNECT_TIMEOUT_S",
        "COSYVOICE_FIRST_AUDIO_TIMEOUT_S",
        "COSYVOICE_FORMAT",
        "COSYVOICE_LANGUAGE",
        "COSYVOICE_PITCH",
        "COSYVOICE_RATE",
        "COSYVOICE_TOTAL_TIMEOUT_S",
        "COSYVOICE_VOICE_REGISTRY",
        "COSYVOICE_VOLUME",
        "DASHSCOPE_REGION",
        "DEEPSEEK_DEEP_TOTAL_TIMEOUT_S",
        "DEEPSEEK_FAST_FIRST_TOKEN_TIMEOUT_S",
        "DEEPSEEK_FAST_MAX_TOKENS",
        "DEEPSEEK_FAST_TEMPERATURE",
        "DEEPSEEK_FAST_TOTAL_TIMEOUT_S",
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


def _aliases(settings_type: type[AgentSettings] | type[ControlSettings]) -> set[str]:
    return {
        str(field.alias)
        for field in settings_type.model_fields.values()
        if isinstance(field.alias, str)
    }


def split_env(
    values: dict[str, str],
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    if values.get("ENVIRONMENT", "").strip().lower() == "production":
        if values.get("MEMORIA_ARCHIVE_INTERNAL_TOKEN", "").strip():
            raise ValueError("production forbids the legacy all-access internal token")
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
    control_keys = _aliases(ControlSettings) | set(_CONTROL_EXTRA_KEYS)
    agent_keys = _aliases(AgentSettings) | set(_AGENT_EXTRA_KEYS)
    known = control_keys | agent_keys
    unknown = sorted(set(values) - known)
    if unknown:
        raise ValueError(f"unrouted production env keys: {', '.join(unknown)}")
    control = {key: value for key, value in values.items() if key in control_keys}
    agent = {key: value for key, value in values.items() if key in agent_keys}
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
    speaker_model = (
        {"MEMORIA_SPEAKER_MODEL_TOKEN": embedding_token} if embedding_token else {}
    )
    return control, agent, speaker_model


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
    args = parser.parse_args()
    control, agent, speaker_model = split_env(_read_env(args.source))
    _write_env(args.control, control)
    _write_env(args.agent, agent)
    _write_env(args.speaker_model, speaker_model)
    print(
        f"wrote {len(control)} Control API keys, {len(agent)} Agent keys "
        f"and {len(speaker_model)} Speaker Model keys"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
