from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.x509.oid import NameOID
from scripts.bootstrap_production_data_env import main as bootstrap_data_env_main
from scripts.prepare_production_upgrade_env import main, prepare
from scripts.production_postgres_roles import PRODUCTION_POSTGRES_ROLES

EXPECTED_CONTROL_DATABASE_ROLES = {
    "MEMORIA_ARCHIVE_DATABASE_URL": "memoria_app",
    "MEMORIA_ARCHIVE_COMPILER_DATABASE_URL": "memoria_compiler",
    "MEMORIA_EVOLUTION_DATABASE_URL": "memoria_evolution",
    "MEMORIA_GUARDIAN_DATABASE_URL": "memoria_guardian",
    "MEMORIA_GUARDIAN_MAINTENANCE_DATABASE_URL": "memoria_guardian_maintenance",
    "MEMORIA_GUARDIAN_WORKER_DATABASE_URL": "memoria_guardian_worker",
    "MEMORIA_IDENTITY_DATABASE_URL": "memoria_identity",
    "MEMORIA_IDENTITY_REGISTRATION_DATABASE_URL": "memoria_identity_registration",
    "MEMORIA_CONSENT_DATABASE_URL": "memoria_consent",
    "MEMORIA_DEVICE_ONBOARDING_DATABASE_URL": "memoria_device_onboarding_api",
    "MEMORIA_SESSION_RUNTIME_DATABASE_URL": "memoria_session_api",
    "MEMORIA_ACTION_EXECUTOR_DATABASE_URL": "memoria_action_executor",
    "MEMORIA_SESSION_RUNTIME_PROJECTOR_DATABASE_URL": "memoria_session_projector",
    "MEMORIA_SESSION_RUNTIME_WORKER_DATABASE_URL": "memoria_session_worker",
    "MEMORIA_SESSION_RUNTIME_MAINTENANCE_DATABASE_URL": "memoria_session_maintenance",
    "MEMORIA_MEMORY_API_DATABASE_URL": "memoria_memory_api",
    "MEMORIA_MEMORY_WORKER_DATABASE_URL": "memoria_memory_worker",
}

EXPECTED_PASSWORD_ROLES = {
    "MEMORIA_DB_APP_PASSWORD": "memoria_app",
    "MEMORIA_DB_COMPILER_PASSWORD": "memoria_compiler",
    "MEMORIA_DB_EVOLUTION_PASSWORD": "memoria_evolution",
    "MEMORIA_DB_GUARDIAN_PASSWORD": "memoria_guardian",
    "MEMORIA_DB_GUARDIAN_MAINTENANCE_PASSWORD": "memoria_guardian_maintenance",
    "MEMORIA_DB_GUARDIAN_WORKER_PASSWORD": "memoria_guardian_worker",
    "MEMORIA_DB_IDENTITY_PASSWORD": "memoria_identity",
    "MEMORIA_DB_IDENTITY_REGISTRATION_PASSWORD": "memoria_identity_registration",
    "MEMORIA_DB_CONSENT_PASSWORD": "memoria_consent",
    "MEMORIA_DB_DEVICE_ONBOARDING_API_PASSWORD": "memoria_device_onboarding_api",
    "MEMORIA_DB_DEVICE_ONBOARDING_MAINTENANCE_PASSWORD": (
        "memoria_device_onboarding_maintenance"
    ),
    "MEMORIA_DB_SESSION_API_PASSWORD": "memoria_session_api",
    "MEMORIA_DB_ACTION_EXECUTOR_PASSWORD": "memoria_action_executor",
    "MEMORIA_DB_SESSION_PROJECTOR_PASSWORD": "memoria_session_projector",
    "MEMORIA_DB_SESSION_WORKER_PASSWORD": "memoria_session_worker",
    "MEMORIA_DB_SESSION_MAINTENANCE_PASSWORD": "memoria_session_maintenance",
    "MEMORIA_DB_MEMORY_API_PASSWORD": "memoria_memory_api",
    "MEMORIA_DB_MEMORY_WORKER_PASSWORD": "memoria_memory_worker",
}


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
        "MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256": "a" * 64,
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
        password_env: f"{role}-pass" for password_env, role in EXPECTED_PASSWORD_ROLES.items()
    }
    minio = {
        "MEMORIA_ARCHIVE_OBJECT_ACCESS_KEY": "archive-access",
        "MEMORIA_ARCHIVE_OBJECT_SECRET_KEY": "archive-secret",
        "MEMORIA_VOICE_OBJECT_ACCESS_KEY": "voice-access",
        "MEMORIA_VOICE_OBJECT_SECRET_KEY": "voice-secret",
    }
    return legacy, postgres, minio


def _ed25519_pem_pair() -> tuple[str, str]:
    private = ed25519.Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_pem, public_pem


def _control_edge_mtls_bundle(tmp_path: Path) -> dict[str, str]:
    """CA plus Control client identity that ControlSettings verifies on disk."""
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "memoria-test-control-edge-ca")]
    )
    now = datetime.datetime.now(datetime.UTC)
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    client_key = ec.generate_private_key(ec.SECP256R1())
    client_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "memoria-test-control-edge-client")]
    )
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(client_name)
        .issuer_name(ca_name)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(ca_key, hashes.SHA256())
    )
    ca_file = tmp_path / "control-edge-ca.crt"
    cert_file = tmp_path / "control-edge-client.crt"
    key_file = tmp_path / "control-edge-client.key"
    ca_file.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_file.write_bytes(client_cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return {
        "MEDIA_EDGE_INTERNAL_CONTROL_CA_FILE": str(ca_file),
        "MEDIA_EDGE_INTERNAL_CONTROL_CLIENT_CERT_FILE": str(cert_file),
        "MEDIA_EDGE_INTERNAL_CONTROL_CLIENT_KEY_FILE": str(key_file),
    }


def _direct_upgrade_inputs(
    tmp_path: Path,
) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Complete direct_voice_core bundle as an operator would leave it in the
    source env after a direct generation run."""
    legacy, postgres, minio = _upgrade_inputs()
    private_pem, public_pem = _ed25519_pem_pair()
    legacy.update(
        {
            "DEVICE_MEDIA_RUNTIME": "direct_voice_core",
            "DEVICE_MEDIA_DIRECT_ROLLOUT_MODE": "allowlist",
            "DEVICE_MEDIA_DIRECT_CANARY_DEVICE_IDS": "dev_test_01",
            "STREAMCORE_TOKEN_PRIVATE_KEY_PEM": private_pem,
            "MEDIA_EDGE_JWT_PUBLIC_KEY_PEM": public_pem,
            "MEDIA_EDGE_INTERNAL_CONTROL_URL": "https://media-edge:8081",
            "MEDIA_EDGE_INTERNAL_CONTROL_TOKEN": "direct-control-token-material-32+chars",
            "MEDIA_EDGE_INTERNAL_TLS_CERT_FILE": (
                "/etc/memoria-media-runtime/edge-internal-server.crt"
            ),
            "MEDIA_EDGE_INTERNAL_TLS_KEY_FILE": (
                "/etc/memoria-media-runtime/edge-internal-server.key"
            ),
            "MEDIA_EDGE_INTERNAL_TLS_CLIENT_CA_FILE": (
                "/etc/memoria-media-runtime/control-edge-ca.crt"
            ),
            "MEDIA_EDGE_HEALTHCHECK_CA_FILE": "/etc/memoria-media-runtime/control-edge-ca.crt",
            "MEDIA_EDGE_HEALTHCHECK_CLIENT_CERT_FILE": (
                "/etc/memoria-media-runtime/edge-healthcheck-client.crt"
            ),
            "MEDIA_EDGE_HEALTHCHECK_CLIENT_KEY_FILE": (
                "/etc/memoria-media-runtime/edge-healthcheck-client.key"
            ),
            "MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL": (
                "http://control-api:8000/v1/internal/device-close"
            ),
            "MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN": (
                "direct-close-report-token-material-32+"
            ),
            "MEDIA_EDGE_DEVICE_CLOSE_REPORT_TIMEOUT_MS": "2500",
            "MEDIA_EDGE_DEVICE_JWT_ISSUER": "voice-agent",
            "MEDIA_EDGE_DEVICE_JWT_AUDIENCE": "memoria-media-edge",
            "MEDIA_EDGE_DEVICE_WSS_ADDR": ":8082",
            "MEDIA_EDGE_DEVICE_STATE_REDIS_URL": "rediss://device-state.example:6379/4",
            "MEDIA_EDGE_DEVICE_STATE_REDIS_CA_FILE": (
                "/etc/memoria-media-runtime/device-state-redis-ca.crt"
            ),
            "MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_CERT_FILE": (
                "/etc/memoria-media-runtime/device-state-redis-client.crt"
            ),
            "MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_KEY_FILE": (
                "/etc/memoria-media-runtime/device-state-redis-client.key"
            ),
            "MEDIA_EDGE_DEVICE_STATE_REDIS_SERVER_NAME": "device-state-redis",
            "DEVICE_DIRECT_MEDIA_WSS_URL": (
                "wss://voice.example.com/memoria-device-edge/v1/device/media"
            ),
            **_control_edge_mtls_bundle(tmp_path),
        }
    )
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

    control, agent, speaker_model, gateway, device_gateway, media_edge = prepare(
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
    assert control["CRISIS_SEMANTIC_ENABLED"] == "true"
    assert control["CRISIS_SEMANTIC_MODEL"] == "deepseek-v4-flash"
    assert control["CRISIS_SEMANTIC_TIMEOUT_S"] == "0.8"
    assert "CRISIS_SEMANTIC_ENABLED" not in agent
    assert agent["DOUBAO_TTS_APP_ID"] == "doubao-app-id"
    assert agent["DOUBAO_TTS_ACCESS_TOKEN"] == "doubao-access-token"
    assert media_edge["MEDIA_EDGE_JWT_SECRET"] == control["STREAMCORE_TOKEN_SECRET"]
    assert media_edge["MEDIA_EDGE_JWT_ISSUER"] == "voice-agent"
    assert media_edge["MEDIA_EDGE_JWT_AUDIENCE"] == "memoria-media"
    assert control["DEVICE_MEDIA_RUNTIME"] == "livekit_compat"
    assert media_edge["MEDIA_EDGE_DEVICE_WSS_ENABLED"] == "false"
    assert media_edge["MEDIA_EDGE_DEVICE_REQUIRED"] == "false"
    assert media_edge["MEDIA_EDGE_INTERNAL_CONTROL_TOKEN"] == control[
        "MEDIA_EDGE_INTERNAL_CONTROL_TOKEN"
    ]
    assert device_gateway["MEMORIA_DEVICE_GATEWAY_TICKET_SECRET"] == control[
        "MEMORIA_DEVICE_GATEWAY_TICKET_SECRET"
    ]
    assert "MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET" not in device_gateway
    assert device_gateway["LIVEKIT_API_SECRET"] == gateway["LIVEKIT_API_SECRET"]
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
    assert agent["MEMORIA_RUNTIME_PROFILE_VERIFY_KEY"] == control[
        "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET"
    ]
    assert "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET" not in agent
    assert len(control["MEMORIA_RESPONSE_PLAN_TOKEN"]) >= 32
    assert control["MEMORIA_RESPONSE_PLAN_TOKEN"] != control["MEMORIA_INTERACTION_POLICY_TOKEN"]
    assert len(control["MEMORIA_EVOLUTION_CONTROL_TOKEN"]) >= 32
    assert len(control["MEMORIA_EVOLUTION_VALIDATOR_TOKEN"]) >= 32
    assert control["MEMORIA_EVOLUTION_RUNTIME_PROMPT_FAMILIES"] == "weather"
    assert control["MEMORIA_GUARDIAN_DATABASE_URL"].startswith("postgresql://memoria_guardian:")
    generated_database_urls = {
        field_name: control[field_name] for field_name in EXPECTED_CONTROL_DATABASE_ROLES
    }
    assert {
        field_name: urlsplit(database_url).username
        for field_name, database_url in generated_database_urls.items()
    } == EXPECTED_CONTROL_DATABASE_ROLES
    assert len(set(generated_database_urls.values())) == len(generated_database_urls)
    assert control["MEMORIA_SESSION_RUNTIME_SCHEMA_MANAGED_EXTERNALLY"] == "true"
    assert control["MEMORIA_MEMORY_SCHEMA_MANAGED_EXTERNALLY"] == "true"
    assert "MEMORIA_SESSION_RUNTIME_BOOTSTRAP_DATABASE_URL" not in control
    assert "MEMORIA_MEMORY_BOOTSTRAP_DATABASE_URL" not in control
    assert "MEMORIA_EVOLUTION_CONTROL_TOKEN" not in agent
    assert "MEMORIA_EVOLUTION_VALIDATOR_TOKEN" not in agent
    assert len(control["MEMORIA_VOICE_CLEANUP_TOKEN"]) >= 32
    assert "MEMORIA_VOICE_CLEANUP_TOKEN" not in agent
    assert speaker_model == {
        "MEMORIA_SPEAKER_MODEL_TOKEN": control["MEMORIA_SPEAKER_EMBEDDING_TOKEN"]
    }
    assert gateway["LIVEKIT_API_KEY"] == "livekit-key"
    assert gateway["LIVEKIT_API_SECRET"] == "livekit-secret-material-that-is-long-enough"
    assert (
        gateway["MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET"]
        == (control["MEMORIA_MINIPROGRAM_GATEWAY_TICKET_SECRET"])
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
        "MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET": (
            "preserved-runtime-profile-signing-secret-material"
        ),
        "MEMORIA_DEVICE_BINDING_TOKEN_SECRET": ("preserved-device-binding-token-secret-material"),
        "MEMORIA_TRANSFER_EVIDENCE_SECRET": ("preserved-transfer-evidence-secret-material"),
    }
    legacy.update(preserved)

    control, agent, _, _, _, _ = prepare(
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

    control, agent, _, _, _, _ = prepare(
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
    generated_signing_secrets = (
        control["MEMORIA_RUNTIME_PROFILE_SIGNING_SECRET"],
        control["MEMORIA_DEVICE_BINDING_TOKEN_SECRET"],
        control["MEMORIA_TRANSFER_EVIDENCE_SECRET"],
    )
    assert all(len(secret) >= 32 for secret in generated_signing_secrets)
    assert len(set(generated_signing_secrets)) == len(generated_signing_secrets)
    assert agent["MEMORIA_RUNTIME_PROFILE_VERIFY_KEY"] == generated_signing_secrets[0]
    assert control["MEMORIA_ARCHIVE_OBJECT_KEY_VERSION"] == "archive-object-v1"
    assert control["MEMORIA_VOICE_SAMPLE_KEY_VERSION"] == "voice-sample-v1"


def test_production_postgres_role_registry_is_complete_and_generates_root_only_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    assert {
        role.password_env: role.role for role in PRODUCTION_POSTGRES_ROLES
    } == EXPECTED_PASSWORD_ROLES
    assert {
        role.control_dsn_env: role.role
        for role in PRODUCTION_POSTGRES_ROLES
        if role.control_dsn_env is not None
    } == EXPECTED_CONTROL_DATABASE_ROLES

    postgres_path = tmp_path / "postgres.env"
    minio_path = tmp_path / "minio.env"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap_production_data_env.py",
            "--postgres",
            str(postgres_path),
            "--minio",
            str(minio_path),
        ],
    )

    assert bootstrap_data_env_main() == 0
    postgres = _env_values(postgres_path)
    assert set(postgres) == {"POSTGRES_PASSWORD", *EXPECTED_PASSWORD_ROLES}
    assert all(len(postgres[key]) >= 32 for key in postgres)
    assert len(set(postgres.values())) == len(postgres)
    assert postgres_path.stat().st_mode & 0o777 == 0o600
    assert minio_path.stat().st_mode & 0o777 == 0o600


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
            "--evolution-trusted-root",
            "b" * 64,
            "--control",
            str(tmp_path / "control.env"),
            "--agent",
            str(tmp_path / "agent.env"),
            "--speaker-model",
            str(tmp_path / "speaker.env"),
            "--gateway",
            str(tmp_path / "gateway.env"),
            "--device-gateway",
            str(tmp_path / "device-gateway.env"),
            "--media-edge",
            str(tmp_path / "media-edge.env"),
        ],
    )

    assert main() == 0
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err
    assert (
        _env_values(tmp_path / "control.env")["MEMORIA_EVOLUTION_TRUSTED_ROOT_SHA256"] == "b" * 64
    )


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

    control, agent, _, _, _, _ = prepare(
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

    control, agent, speaker_model, _, _, _ = prepare(
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


def test_upgrade_env_direct_voice_core_generates_complete_direct_bundle(
    tmp_path: Path,
) -> None:
    legacy, postgres, minio = _direct_upgrade_inputs(tmp_path)

    control, agent, _, _, _, media_edge = prepare(
        legacy=legacy,
        postgres=postgres,
        minio=minio,
        release_tag="20260813-direct",
    )

    assert control["DEVICE_MEDIA_RUNTIME"] == "direct_voice_core"
    assert control["DEVICE_MEDIA_DIRECT_ROLLOUT_MODE"] == "allowlist"
    assert control["DEVICE_MEDIA_DIRECT_CANARY_DEVICE_IDS"] == "dev_test_01"
    assert control["DEVICE_DIRECT_MEDIA_WSS_URL"] == (
        "wss://voice.example.com/memoria-device-edge/v1/device/media"
    )
    assert control["MEDIA_EDGE_INTERNAL_CONTROL_URL"] == "https://media-edge:8081"
    assert control["MEDIA_EDGE_INTERNAL_CONTROL_TOKEN"] == (
        "direct-control-token-material-32+chars"
    )
    assert control["MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN"] == (
        "direct-close-report-token-material-32+"
    )
    assert control["STREAMCORE_TOKEN_SECRET"] == ""
    assert control["STREAMCORE_TOKEN_PRIVATE_KEY_PEM"] == (
        legacy["STREAMCORE_TOKEN_PRIVATE_KEY_PEM"]
    )
    assert media_edge["MEDIA_EDGE_DEVICE_WSS_ENABLED"] == "true"
    assert media_edge["MEDIA_EDGE_DEVICE_REQUIRED"] == "true"
    assert media_edge["MEDIA_EDGE_DEVICE_WSS_ADDR"] == ":8082"
    assert media_edge["MEDIA_EDGE_DEVICE_JWT_ISSUER"] == "voice-agent"
    assert media_edge["MEDIA_EDGE_DEVICE_JWT_AUDIENCE"] == "memoria-media-edge"
    assert media_edge["MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL"] == (
        "http://control-api:8000/v1/internal/device-close"
    )
    assert media_edge["MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN"] == (
        "direct-close-report-token-material-32+"
    )
    assert media_edge["MEDIA_EDGE_DEVICE_CLOSE_REPORT_TIMEOUT_MS"] == "2500"
    assert media_edge["MEDIA_EDGE_DEVICE_STATE_REDIS_URL"] == (
        "rediss://device-state.example:6379/4"
    )
    assert media_edge["MEDIA_EDGE_DEVICE_STATE_REDIS_SERVER_NAME"] == "device-state-redis"
    for key in (
        "MEDIA_EDGE_DEVICE_STATE_REDIS_CA_FILE",
        "MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_CERT_FILE",
        "MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_KEY_FILE",
    ):
        assert media_edge[key] == legacy[key]
    assert media_edge["MEDIA_EDGE_DEVICE_STATE_KEY_PREFIX"] == (
        "memoria:device-media:v2"
    )
    assert media_edge["MEDIA_EDGE_DEVICE_STATE_TIMEOUT_MS"] == "500"
    assert media_edge["MEDIA_EDGE_DEVICE_LEASE_TTL_MS"] == "30000"
    assert media_edge["MEDIA_EDGE_DEVICE_LEASE_CHECK_INTERVAL_MS"] == "5000"
    for key in (
        "MEDIA_EDGE_INTERNAL_TLS_CERT_FILE",
        "MEDIA_EDGE_INTERNAL_TLS_KEY_FILE",
        "MEDIA_EDGE_INTERNAL_TLS_CLIENT_CA_FILE",
        "MEDIA_EDGE_HEALTHCHECK_CA_FILE",
        "MEDIA_EDGE_HEALTHCHECK_CLIENT_CERT_FILE",
        "MEDIA_EDGE_HEALTHCHECK_CLIENT_KEY_FILE",
    ):
        assert media_edge[key] == legacy[key]
    assert media_edge["MEDIA_EDGE_HEALTHCHECK_URL"] == "https://127.0.0.1:8081/readyz"
    assert media_edge["MEDIA_EDGE_JWT_PUBLIC_KEY_PEM"] == (
        legacy["MEDIA_EDGE_JWT_PUBLIC_KEY_PEM"]
    )
    assert media_edge["MEDIA_EDGE_JWT_SECRET"] == ""
    assert "DEVICE_DIRECT_MEDIA_WSS_URL" not in media_edge
    assert "MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN" not in agent


def test_upgrade_env_direct_voice_core_requires_shared_device_state_redis(
    tmp_path: Path,
) -> None:
    legacy, postgres, minio = _direct_upgrade_inputs(tmp_path)
    legacy.pop("MEDIA_EDGE_DEVICE_STATE_REDIS_URL")

    with pytest.raises(
        ValueError,
        match="MEDIA_EDGE_DEVICE_STATE_REDIS_URL",
    ):
        prepare(
            legacy=legacy,
            postgres=postgres,
            minio=minio,
            release_tag="20260814-direct-no-shared-state",
        )


def test_upgrade_env_direct_voice_core_rejects_plaintext_device_state_redis(
    tmp_path: Path,
) -> None:
    legacy, postgres, minio = _direct_upgrade_inputs(tmp_path)
    legacy["MEDIA_EDGE_DEVICE_STATE_REDIS_URL"] = "redis://device-state.example:6379/4"

    with pytest.raises(ValueError, match="must use rediss://"):
        prepare(
            legacy=legacy,
            postgres=postgres,
            minio=minio,
            release_tag="20260814-direct-plaintext-state",
        )


def test_upgrade_env_direct_canary_requires_a_nonempty_device_allowlist(
    tmp_path: Path,
) -> None:
    legacy, postgres, minio = _direct_upgrade_inputs(tmp_path)
    legacy.pop("DEVICE_MEDIA_DIRECT_CANARY_DEVICE_IDS")

    with pytest.raises(ValueError, match="DEVICE_MEDIA_DIRECT_CANARY_DEVICE_IDS"):
        prepare(
            legacy=legacy,
            postgres=postgres,
            minio=minio,
            release_tag="20260814-direct-empty-canary",
        )


@pytest.mark.parametrize(
    "runtime_value",
    [None, "livekit_compat"],
    ids=["runtime-removed", "runtime-livekit-compat"],
)
def test_upgrade_env_rollback_removes_direct_only_edge_config(
    tmp_path: Path,
    runtime_value: str | None,
) -> None:
    legacy, postgres, minio = _direct_upgrade_inputs(tmp_path)
    if runtime_value is None:
        legacy.pop("DEVICE_MEDIA_RUNTIME")
    else:
        legacy["DEVICE_MEDIA_RUNTIME"] = runtime_value
    direct_only_keys = {
        "DEVICE_MEDIA_DIRECT_CANARY_DEVICE_IDS",
        "DEVICE_MEDIA_DIRECT_ROLLOUT_MODE",
        "DEVICE_DIRECT_MEDIA_WSS_URL",
        "MEDIA_EDGE_DEVICE_WSS_ADDR",
        "MEDIA_EDGE_DEVICE_JWT_ISSUER",
        "MEDIA_EDGE_DEVICE_JWT_AUDIENCE",
        "MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL",
        "MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN",
        "MEDIA_EDGE_DEVICE_CLOSE_REPORT_TIMEOUT_MS",
        "MEDIA_EDGE_DEVICE_STATE_REDIS_URL",
        "MEDIA_EDGE_DEVICE_STATE_REDIS_CA_FILE",
        "MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_CERT_FILE",
        "MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_KEY_FILE",
        "MEDIA_EDGE_DEVICE_STATE_REDIS_SERVER_NAME",
        "MEDIA_EDGE_DEVICE_STATE_KEY_PREFIX",
        "MEDIA_EDGE_DEVICE_STATE_TIMEOUT_MS",
        "MEDIA_EDGE_DEVICE_LEASE_TTL_MS",
        "MEDIA_EDGE_DEVICE_LEASE_CHECK_INTERVAL_MS",
        "MEDIA_EDGE_INSTANCE_ID",
        "MEDIA_EDGE_INTERNAL_TLS_CERT_FILE",
        "MEDIA_EDGE_INTERNAL_TLS_KEY_FILE",
        "MEDIA_EDGE_INTERNAL_TLS_CLIENT_CA_FILE",
        "MEDIA_EDGE_HEALTHCHECK_CA_FILE",
        "MEDIA_EDGE_HEALTHCHECK_CLIENT_CERT_FILE",
        "MEDIA_EDGE_HEALTHCHECK_CLIENT_KEY_FILE",
    }

    control, agent, _, gateway, device_gateway, media_edge = prepare(
        legacy=legacy,
        postgres=postgres,
        minio=minio,
        release_tag="20260813-rollback",
    )

    assert control["DEVICE_MEDIA_RUNTIME"] == "livekit_compat"
    for service_env in (control, agent, gateway, device_gateway, media_edge):
        for key in direct_only_keys:
            assert key not in service_env
    assert media_edge["MEDIA_EDGE_DEVICE_WSS_ENABLED"] == "false"
    assert media_edge["MEDIA_EDGE_DEVICE_REQUIRED"] == "false"
    assert media_edge["MEDIA_EDGE_HEALTHCHECK_URL"] == "http://127.0.0.1:8081/readyz"
    # The Go edge must boot the plaintext compatibility shape: JWT identity
    # for H5/StreamCore stays, internal control token stays, and the device
    # listener is off.
    assert media_edge["MEDIA_EDGE_HTTP_ADDR"] == ":8080"
    assert media_edge["MEDIA_EDGE_JWT_ISSUER"] == "voice-agent"
    assert media_edge["MEDIA_EDGE_JWT_AUDIENCE"] == "memoria-media"
    assert media_edge["MEDIA_EDGE_JWT_SECRET"] == control["STREAMCORE_TOKEN_SECRET"]
    assert media_edge["MEDIA_EDGE_JWT_PUBLIC_KEY_PEM"] == (
        legacy["MEDIA_EDGE_JWT_PUBLIC_KEY_PEM"]
    )
    assert media_edge["MEDIA_EDGE_INTERNAL_CONTROL_TOKEN"] == (
        control["MEDIA_EDGE_INTERNAL_CONTROL_TOKEN"]
    )
    # General keys survive rollback untouched: the internal control endpoint
    # is documented for both runtimes (direct mode only additionally requires
    # HTTPS/mTLS), so it must never be stripped for legacy H5.
    assert control["MEDIA_EDGE_INTERNAL_CONTROL_URL"] == "https://media-edge:8081"
    assert control["MEDIA_EDGE_INTERNAL_CONTROL_CA_FILE"] == (
        legacy["MEDIA_EDGE_INTERNAL_CONTROL_CA_FILE"]
    )
    assert control["MEDIA_EDGE_INTERNAL_CONTROL_CLIENT_CERT_FILE"] == (
        legacy["MEDIA_EDGE_INTERNAL_CONTROL_CLIENT_CERT_FILE"]
    )
    assert control["MEDIA_EDGE_INTERNAL_CONTROL_CLIENT_KEY_FILE"] == (
        legacy["MEDIA_EDGE_INTERNAL_CONTROL_CLIENT_KEY_FILE"]
    )
