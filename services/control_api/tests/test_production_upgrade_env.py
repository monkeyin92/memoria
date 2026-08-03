from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from scripts.prepare_production_upgrade_env import main, prepare


def _env_values(path: Path) -> dict[str, str]:
    return {
        key.strip(): value.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for key, separator, value in (line.partition("="),)
        if separator
    }


def _upgrade_inputs(
    auth: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    legacy = {
        "ENVIRONMENT": "production",
        "DEPLOYMENT_PROFILE": "cn_self_hosted",
        "LLM_PROVIDER": "bailian_deepseek",
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
        "ENDPOINTING_MIN_DELAY_S": "1.50",
        "ENDPOINTING_MAX_DELAY_S": "2.20",
        "FALSE_INTERRUPTION_TIMEOUT_S": "1.70",
        "TTS_PROVIDER": "doubao",
        "DOUBAO_TTS_RESOURCE_ID": "seed-tts-2.0",
        "DOUBAO_TTS_SAMPLE_RATE": "24000",
        "MEMORIA_AUTH_SECRET": "auth-secret-material-that-is-long-enough",
        "WECHAT_MINIPROGRAM_APPID": "wx-test",
        "WECHAT_MINIPROGRAM_APPSECRET": "wechat-secret",
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


def test_endpointing_defaults_match_operator_templates() -> None:
    root = Path(__file__).resolve().parents[3]
    expected = {
        "ENDPOINTING_MIN_DELAY_S": "1.50",
        "ENDPOINTING_MAX_DELAY_S": "2.20",
        "FALSE_INTERRUPTION_TIMEOUT_S": "1.70",
        "INTERRUPT_SEMANTIC_ENABLED": "true",
        "INTERRUPT_SEMANTIC_MODEL": "deepseek-v4-flash",
        "INTERRUPT_SEMANTIC_TIMEOUT_S": "1.2",
    }

    for relative in (".env.example", "infra/memoria.env.production.example"):
        values = _env_values(root / relative)
        assert {key: values[key] for key in expected} == expected


def test_upgrade_env_is_valid_split_and_does_not_expose_storage_secrets_to_agent() -> None:
    legacy, postgres, minio = _upgrade_inputs()
    legacy["DOUBAO_TTS_SECRET_KEY"] = "not-a-websocket-credential"
    archive_read_keys = {"archive-v1": Fernet.generate_key().decode("ascii")}
    voice_read_keys = {"voice-v1": Fernet.generate_key().decode("ascii")}
    legacy["MEMORIA_ARCHIVE_OBJECT_READ_KEYS"] = json.dumps(archive_read_keys)
    legacy["MEMORIA_VOICE_SAMPLE_READ_KEYS"] = json.dumps(voice_read_keys)

    control, agent, speaker_model, gateway, media_edge = prepare(
        legacy=legacy,
        postgres=postgres,
        minio=minio,
        release_tag="20260719-test",
    )

    assert control["MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY"] == "archive-access"
    assert control["WECHAT_MINIPROGRAM_APPID"] == "wx-test"
    assert control["WECHAT_MINIPROGRAM_APPSECRET"] == "wechat-secret"
    assert len(control["MEMORIA_WECHAT_IDENTITY_SECRET"]) >= 32
    assert control["MEMORIA_VOICE_OBJECT_ACCESS_KEY"] == "voice-access"
    assert control["MEMORIA_ARCHIVE_OBJECT_READ_KEYS"] == json.dumps(archive_read_keys)
    assert control["MEMORIA_VOICE_SAMPLE_READ_KEYS"] == json.dumps(voice_read_keys)
    assert control["TTS_PROVIDER"] == "doubao"
    assert agent["TTS_PROVIDER"] == "doubao"
    assert agent["MEMORIA_ARCHIVE_SINK_ENABLED"] == "true"
    assert agent["MEMORIA_MEMORY_CONTEXT_ENABLED"] == "true"
    assert agent["MEMORIA_PERSONA_ENABLED"] == "true"
    assert agent["MEMORIA_VOICE_PROFILE_ENABLED"] == "true"
    assert agent["ENDPOINTING_MIN_DELAY_S"] == "1.50"
    assert agent["ENDPOINTING_MAX_DELAY_S"] == "2.20"
    assert agent["FALSE_INTERRUPTION_TIMEOUT_S"] == "1.70"
    assert agent["INTERRUPT_SEMANTIC_ENABLED"] == "true"
    assert agent["LLM_PROVIDER"] == "bailian_deepseek"
    assert agent["INTERRUPT_SEMANTIC_MODEL"] == "deepseek-v4-flash"
    assert agent["INTERRUPT_SEMANTIC_TIMEOUT_S"] == "1.2"
    assert agent["DOUBAO_TTS_APP_ID"] == "doubao-app-id"
    assert agent["DOUBAO_TTS_ACCESS_TOKEN"] == "doubao-access-token"
    assert media_edge["MEDIA_EDGE_JWT_SECRET"] == control["STREAMCORE_TOKEN_SECRET"]
    assert media_edge["MEDIA_EDGE_JWT_ISSUER"] == "voice-agent"
    assert media_edge["MEDIA_EDGE_JWT_AUDIENCE"] == "memoria-media"
    assert "MEDIA_EDGE_JWT_SECRET" not in control
    assert "MEDIA_EDGE_JWT_SECRET" not in agent
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
        for service_env in (control, agent, speaker_model, gateway, media_edge)
    )
    assert "QWEN_OMNI_PLUS_VAD_THRESHOLD" not in control
    assert "QWEN_OMNI_PLUS_VAD_THRESHOLD" not in agent
    assert agent["MEMORIA_AGENT_HEARTBEAT_TOKEN"] == control["MEMORIA_AGENT_HEARTBEAT_TOKEN"]
    assert agent["MEMORIA_AGENT_HEARTBEAT_TOKEN"] != agent["MEMORIA_ARCHIVE_WRITE_TOKEN"]
    assert agent["MEMORIA_INTERACTION_POLICY_TOKEN"] == control["MEMORIA_INTERACTION_POLICY_TOKEN"]
    assert agent["MEMORIA_INTERACTION_POLICY_TOKEN"] != agent["MEMORIA_AGENT_HEARTBEAT_TOKEN"]
    assert len(control["MEMORIA_RESPONSE_PLAN_TOKEN"]) >= 32
    assert control["MEMORIA_RESPONSE_PLAN_TOKEN"] != control["MEMORIA_INTERACTION_POLICY_TOKEN"]
    assert len(control["MEMORIA_VOICE_CLEANUP_TOKEN"]) >= 32
    assert "MEMORIA_VOICE_CLEANUP_TOKEN" not in agent
    assert speaker_model == {
        "MEMORIA_SPEAKER_MODEL_TOKEN": control["MEMORIA_SPEAKER_EMBEDDING_TOKEN"]
    }
    assert gateway["LIVEKIT_API_KEY"] == "livekit-key"
    assert gateway["LIVEKIT_API_SECRET"] == "livekit-secret-material-that-is-long-enough"
    assert gateway["MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET"] == (
        control["MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET"]
    )
    assert gateway["MINIPROGRAM_GATEWAY_TICKET_MAX_TTL_S"] == "300"
    assert control["MINIPROGRAM_MEDIA_GATEWAY_URL"] == (
        "wss://voice.example.com/memoria-mini-media/v1/mini-program/media"
    )
    for forbidden in (
        "MEMORIA_AUTH_SECRET",
        "MEMORIA_ARCHIVE_DATABASE_URL",
        "MEMORIA_ARCHIVE_OBJECT_SECRET_KEY",
        "MEMORIA_VOICE_OBJECT_SECRET_KEY",
        "DASHSCOPE_API_KEY",
        "DOUBAO_TTS_API_KEY",
        "DOUBAO_TTS_ACCESS_TOKEN",
    ):
        assert forbidden not in gateway


def test_upgrade_env_preserves_existing_encryption_keys_versions_and_read_keyrings() -> None:
    legacy, postgres, minio = _upgrade_inputs()
    preserved = {
        "MEMORIA_MESSAGE_IDEMPOTENCY_SECRET": "preserved-message-idempotency-secret-material",
        "MEMORIA_WECHAT_IDENTITY_SECRET": "preserved-wechat-identity-secret-material",
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

    control, agent, _, _, _ = prepare(
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

    control, agent, _, _, _ = prepare(
        legacy=legacy,
        postgres=postgres,
        minio=minio,
        release_tag="20260722-generate-keys",
    )

    generated = (
        control["MEMORIA_MESSAGE_IDEMPOTENCY_SECRET"],
        control["MEMORIA_WECHAT_IDENTITY_SECRET"],
        control["MEMORIA_ARCHIVE_OBJECT_ENCRYPTION_KEY"],
        control["MEMORIA_VOICE_SAMPLE_ENCRYPTION_KEY"],
        control["MEMORIA_SPEAKER_TEMPLATE_KEY"],
        agent["MEMORIA_ARCHIVE_SPOOL_KEY"],
    )
    for key in generated:
        assert len(key) >= 32
    for key in generated[2:]:
        Fernet(key.encode("ascii"))
    assert len(set(generated)) == len(generated)
    assert control["MEMORIA_MESSAGE_IDEMPOTENCY_SECRET"] != control["MEMORIA_AUTH_SECRET"]
    assert control["MEMORIA_WECHAT_IDENTITY_SECRET"] != control["MEMORIA_AUTH_SECRET"]
    assert "MEMORIA_MESSAGE_IDEMPOTENCY_SECRET" not in agent
    assert "MEMORIA_WECHAT_IDENTITY_SECRET" not in agent
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
            "--gateway",
            str(tmp_path / "gateway.env"),
            "--media-edge",
            str(tmp_path / "media-edge.env"),
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

    control, agent, _, _, _ = prepare(
        legacy=legacy,
        postgres=postgres,
        minio=minio,
        release_tag="20260721-doubao-api-key",
    )

    assert agent["DOUBAO_TTS_API_KEY"] == "doubao-api-key"
    assert "DOUBAO_TTS_API_KEY" not in control
    assert "DOUBAO_TTS_APP_ID" not in agent
    assert "DOUBAO_TTS_ACCESS_TOKEN" not in agent


def test_upgrade_env_routes_independent_doubao_clone_key_to_control_only() -> None:
    legacy, postgres, minio = _upgrade_inputs()
    legacy.update(
        {
            "MEMORIA_VOICE_CLONE_PROVIDER": "volcengine_doubao",
            "MEMORIA_VOICE_TARGET_MODEL": "seed-icl-2.0",
            "MEMORIA_DOUBAO_VOICE_API_KEY": "control-only-clone-key",
            "MEMORIA_DOUBAO_VOICE_SYNTH_READY_ID_MODE": "custom_speaker_id",
            "MEMORIA_DOUBAO_VOICE_EXPIRES_AT_FIELD": "result.ExpireTime",
        }
    )

    control, agent, speaker_model, _, _ = prepare(
        legacy=legacy,
        postgres=postgres,
        minio=minio,
        release_tag="20260723-doubao-clone",
    )

    assert control["MEMORIA_DOUBAO_VOICE_API_KEY"] == "control-only-clone-key"
    assert control["MEMORIA_VOICE_CLONE_PROVIDER"] == "volcengine_doubao"
    assert control["MEMORIA_VOICE_TARGET_MODEL"] == "seed-icl-2.0"
    assert "MEMORIA_DOUBAO_VOICE_API_KEY" not in agent
    assert "MEMORIA_DOUBAO_VOICE_API_KEY" not in speaker_model


def test_upgrade_env_rejects_a_shared_doubao_clone_and_runtime_key() -> None:
    legacy, postgres, minio = _upgrade_inputs({"DOUBAO_TTS_API_KEY": "shared-key"})
    legacy.update(
        {
            "MEMORIA_VOICE_CLONE_PROVIDER": "volcengine_doubao",
            "MEMORIA_VOICE_TARGET_MODEL": "seed-icl-2.0",
            "MEMORIA_DOUBAO_VOICE_API_KEY": "shared-key",
            "MEMORIA_DOUBAO_VOICE_SYNTH_READY_ID_MODE": "custom_speaker_id",
            "MEMORIA_DOUBAO_VOICE_EXPIRES_AT_FIELD": "result.ExpireTime",
        }
    )

    with pytest.raises(ValueError, match="must be independent"):
        prepare(
            legacy=legacy,
            postgres=postgres,
            minio=minio,
            release_tag="20260723-doubao-shared-key",
        )


def test_upgrade_env_rejects_unverified_doubao_clone_synth_id_mapping() -> None:
    legacy, postgres, minio = _upgrade_inputs()
    legacy.update(
        {
            "MEMORIA_VOICE_CLONE_PROVIDER": "volcengine_doubao",
            "MEMORIA_VOICE_TARGET_MODEL": "seed-icl-2.0",
            "MEMORIA_DOUBAO_VOICE_API_KEY": "control-only-clone-key",
        }
    )

    with pytest.raises(ValueError, match="smoke-verified synth ID mapping"):
        prepare(
            legacy=legacy,
            postgres=postgres,
            minio=minio,
            release_tag="20260723-doubao-unverified",
        )
