from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from scripts.prepare_production_upgrade_env import main, prepare


def _upgrade_inputs(
    auth: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    legacy = {
        "ENVIRONMENT": "production",
        "DEPLOYMENT_PROFILE": "cn_self_hosted",
        "LLM_PROVIDER": "qwen",
        "PUBLIC_BASE_URL": "https://voice.example.com/memoria-api",
        "ALLOWED_ORIGINS": "https://voice.example.com",
        "LIVEKIT_URL": "wss://voice.example.com",
        "LIVEKIT_API_KEY": "livekit-key",
        "LIVEKIT_API_SECRET": "livekit-secret-material-that-is-long-enough",
        "DASHSCOPE_API_KEY": "dashscope-secret",
        "DASHSCOPE_WS_URL": "wss://dashscope.example/ws",
        "DASHSCOPE_COMPATIBLE_BASE_URL": "https://dashscope.example/v1",
        "DASHSCOPE_BASE_URL": "https://dashscope.example/v1",
        "FUNASR_MODEL": "fun-asr-realtime",
        "FUNASR_SAMPLE_RATE": "16000",
        "TTS_PROVIDER": "doubao",
        "DOUBAO_TTS_RESOURCE_ID": "seed-tts-2.0",
        "DOUBAO_TTS_SAMPLE_RATE": "24000",
        "MEMORIA_AUTH_SECRET": "auth-secret-material-that-is-long-enough",
        "QWEN_OMNI_PLUS_VAD_THRESHOLD": "ignored-legacy-key",
    }
    legacy.update(
        auth
        if auth is not None
        else {
            "DOUBAO_TTS_APP_ID": "doubao-app-id",
            "DOUBAO_TTS_ACCESS_TOKEN": "doubao-access-token",
        }
    )
    postgres = {
        "MEMORIA_DB_APP_PASSWORD": "app-pass",
        "MEMORIA_DB_COMPILER_PASSWORD": "compiler-pass",
    }
    minio = {
        "MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY": "archive-access",
        "MEMORIA_ARCHIVE_OBJECT_SECRET_KEY": "archive-secret",
        "MEMORIA_VOICE_OBJECT_ACCESS_KEY": "voice-access",
        "MEMORIA_VOICE_OBJECT_SECRET_KEY": "voice-secret",
    }
    return legacy, postgres, minio


def test_upgrade_env_is_valid_split_and_does_not_expose_storage_secrets_to_agent() -> None:
    legacy, postgres, minio = _upgrade_inputs()
    legacy["DOUBAO_TTS_SECRET_KEY"] = "not-a-websocket-credential"
    archive_read_keys = {"archive-v1": Fernet.generate_key().decode("ascii")}
    voice_read_keys = {"voice-v1": Fernet.generate_key().decode("ascii")}
    legacy["MEMORIA_ARCHIVE_OBJECT_READ_KEYS"] = json.dumps(archive_read_keys)
    legacy["MEMORIA_VOICE_SAMPLE_READ_KEYS"] = json.dumps(voice_read_keys)

    control, agent, speaker_model = prepare(
        legacy=legacy,
        postgres=postgres,
        minio=minio,
        release_tag="20260719-test",
    )

    assert control["MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY"] == "archive-access"
    assert control["MEMORIA_VOICE_OBJECT_ACCESS_KEY"] == "voice-access"
    assert control["MEMORIA_ARCHIVE_OBJECT_READ_KEYS"] == json.dumps(archive_read_keys)
    assert control["MEMORIA_VOICE_SAMPLE_READ_KEYS"] == json.dumps(voice_read_keys)
    assert control["TTS_PROVIDER"] == "doubao"
    assert agent["TTS_PROVIDER"] == "doubao"
    assert agent["MEMORIA_ARCHIVE_SINK_ENABLED"] == "true"
    assert agent["MEMORIA_MEMORY_CONTEXT_ENABLED"] == "true"
    assert agent["MEMORIA_PERSONA_ENABLED"] == "true"
    assert agent["MEMORIA_VOICE_PROFILE_ENABLED"] == "true"
    assert agent["DOUBAO_TTS_APP_ID"] == "doubao-app-id"
    assert agent["DOUBAO_TTS_ACCESS_TOKEN"] == "doubao-access-token"
    assert "DOUBAO_TTS_APP_ID" not in control
    assert "DOUBAO_TTS_ACCESS_TOKEN" not in control
    assert "MEMORIA_ARCHIVE_OBJECT_SECRET_KEY" not in agent
    assert "MEMORIA_VOICE_OBJECT_SECRET_KEY" not in agent
    assert "MEMORIA_ARCHIVE_OBJECT_READ_KEYS" not in agent
    assert "MEMORIA_VOICE_SAMPLE_READ_KEYS" not in agent
    assert "MEMORIA_ARCHIVE_OBJECT_READ_KEYS" not in speaker_model
    assert "MEMORIA_VOICE_SAMPLE_READ_KEYS" not in speaker_model
    assert all(
        "DOUBAO_TTS_SECRET_KEY" not in service_env
        for service_env in (control, agent, speaker_model)
    )
    assert "QWEN_OMNI_PLUS_VAD_THRESHOLD" not in control
    assert "QWEN_OMNI_PLUS_VAD_THRESHOLD" not in agent
    assert agent["MEMORIA_AGENT_HEARTBEAT_TOKEN"] == control[
        "MEMORIA_AGENT_HEARTBEAT_TOKEN"
    ]
    assert agent["MEMORIA_AGENT_HEARTBEAT_TOKEN"] != agent[
        "MEMORIA_ARCHIVE_WRITE_TOKEN"
    ]
    assert agent["MEMORIA_INTERACTION_POLICY_TOKEN"] == control[
        "MEMORIA_INTERACTION_POLICY_TOKEN"
    ]
    assert agent["MEMORIA_INTERACTION_POLICY_TOKEN"] != agent[
        "MEMORIA_AGENT_HEARTBEAT_TOKEN"
    ]
    assert speaker_model == {
        "MEMORIA_SPEAKER_MODEL_TOKEN": control["MEMORIA_SPEAKER_EMBEDDING_TOKEN"]
    }


def test_upgrade_env_preserves_existing_encryption_keys_versions_and_read_keyrings() -> None:
    legacy, postgres, minio = _upgrade_inputs()
    preserved = {
        "MEMORIA_MESSAGE_IDEMPOTENCY_SECRET": "preserved-message-idempotency-secret-material",
        "MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "MEMORIA_ARCHIVE_OBJECT_KEY_VERSION": "archive-object-v7",
        "MEMORIA_ARCHIVE_OBJECT_READ_KEYS": json.dumps(
            {"archive-object-v6": Fernet.generate_key().decode("ascii")}
        ),
        "MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
        "MEMORIA_VOICE_SAMPLE_KEY_VERSION": "voice-sample-v4",
        "MEMORIA_VOICE_SAMPLE_READ_KEYS": json.dumps(
            {"voice-sample-v3": Fernet.generate_key().decode("ascii")}
        ),
        "MEMORIA_SPEAKER_TEMPLATE_KEY": Fernet.generate_key().decode("ascii"),
        "MEMORIA_ARCHIVE_SPOOL_KEY": Fernet.generate_key().decode("ascii"),
    }
    legacy.update(preserved)

    control, agent, _ = prepare(
        legacy=legacy,
        postgres=postgres,
        minio=minio,
        release_tag="20260722-preserve-keys",
    )

    for key in preserved.keys() - {"MEMORIA_ARCHIVE_SPOOL_KEY"}:
        assert control[key] == preserved[key]
    assert agent["MEMORIA_ARCHIVE_SPOOL_KEY"] == preserved["MEMORIA_ARCHIVE_SPOOL_KEY"]


def test_upgrade_env_generates_only_missing_encryption_keys() -> None:
    legacy, postgres, minio = _upgrade_inputs()

    control, agent, _ = prepare(
        legacy=legacy,
        postgres=postgres,
        minio=minio,
        release_tag="20260722-generate-keys",
    )

    generated = (
        control["MEMORIA_MESSAGE_IDEMPOTENCY_SECRET"],
        control["MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY"],
        control["MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY"],
        control["MEMORIA_SPEAKER_TEMPLATE_KEY"],
        agent["MEMORIA_ARCHIVE_SPOOL_KEY"],
    )
    for key in generated:
        assert len(key) >= 32
    for key in generated[1:]:
        Fernet(key.encode("ascii"))
    assert len(set(generated)) == len(generated)
    assert control["MEMORIA_MESSAGE_IDEMPOTENCY_SECRET"] != control["MEMORIA_AUTH_SECRET"]
    assert "MEMORIA_MESSAGE_IDEMPOTENCY_SECRET" not in agent
    assert control["MEMORIA_ARCHIVE_OBJECT_KEY_VERSION"] == "archive-object-v1"
    assert control["MEMORIA_VOICE_SAMPLE_KEY_VERSION"] == "voice-sample-v1"


def test_upgrade_env_cli_does_not_print_preserved_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    legacy, postgres, minio = _upgrade_inputs()
    secret = Fernet.generate_key().decode("ascii")
    legacy["MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY"] = secret
    legacy_path = tmp_path / "legacy.env"
    postgres_path = tmp_path / "postgres.env"
    minio_path = tmp_path / "minio.env"
    for path, values in (
        (legacy_path, legacy),
        (postgres_path, postgres),
        (minio_path, minio),
    ):
        path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prepare_production_upgrade_env.py",
            "--legacy",
            str(legacy_path),
            "--postgres",
            str(postgres_path),
            "--minio",
            str(minio_path),
            "--release-tag",
            "20260722-no-secret-output",
            "--control",
            str(tmp_path / "control.env"),
            "--agent",
            str(tmp_path / "agent.env"),
            "--speaker-model",
            str(tmp_path / "speaker.env"),
        ],
    )

    assert main() == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_upgrade_env_rejects_missing_doubao_authentication() -> None:
    legacy, postgres, minio = _upgrade_inputs({})

    with pytest.raises(ValueError, match="exactly one complete authentication mode"):
        prepare(
            legacy=legacy,
            postgres=postgres,
            minio=minio,
            release_tag="20260721-missing-doubao-auth",
        )


@pytest.mark.parametrize(
    "auth",
    [
        {
            "DOUBAO_TTS_API_KEY": "doubao-api-key",
            "DOUBAO_TTS_APP_ID": "doubao-app-id",
            "DOUBAO_TTS_ACCESS_TOKEN": "doubao-access-token",
        },
        {"DOUBAO_TTS_APP_ID": "doubao-app-id"},
        {"DOUBAO_TTS_ACCESS_TOKEN": "doubao-access-token"},
    ],
)
def test_upgrade_env_rejects_ambiguous_or_half_configured_doubao_authentication(
    auth: dict[str, str],
) -> None:
    legacy, postgres, minio = _upgrade_inputs(auth)

    with pytest.raises(ValueError, match="exactly one complete authentication mode"):
        prepare(
            legacy=legacy,
            postgres=postgres,
            minio=minio,
            release_tag="20260721-half-doubao-auth",
        )


def test_upgrade_env_accepts_doubao_api_key_authentication() -> None:
    legacy, postgres, minio = _upgrade_inputs({"DOUBAO_TTS_API_KEY": "doubao-api-key"})

    control, agent, _ = prepare(
        legacy=legacy,
        postgres=postgres,
        minio=minio,
        release_tag="20260721-doubao-api-key",
    )

    assert agent["DOUBAO_TTS_API_KEY"] == "doubao-api-key"
    assert "DOUBAO_TTS_API_KEY" not in control
    assert "DOUBAO_TTS_APP_ID" not in agent
    assert "DOUBAO_TTS_ACCESS_TOKEN" not in agent
