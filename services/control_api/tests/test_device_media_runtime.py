from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr
from services.common.miniprogram_gateway_ticket import (
    GatewayTicketError,
    issue_device_gateway_ticket,
    verify_device_gateway_ticket,
)
from services.control_api.app.config import ControlSettings
from services.control_api.app.media_runtime import (
    DEVICE_MEDIA_AUDIENCE,
    DEVICE_MEDIA_TOKEN_TYPE,
    DEVICE_STREAM_EPOCH_MAX,
    DeviceDirectMediaTicket,
    mint_device_direct_media_ticket,
    select_device_media_runtime,
)


def settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "device_media_runtime": "direct_voice_core",
        "device_media_direct_rollout_mode": "allowlist",
        "device_media_direct_canary_device_ids": "dev_test_01",
        "device_direct_media_wss_url": "wss://edge.example/v1/device/media",
        "streamcore_token_secret": SecretStr(""),
        "streamcore_token_private_key_pem": SecretStr(""),
        "streamcore_token_key_id": "media-2026-08",
        "streamcore_token_ttl_s": 120,
        "environment": "development",
        "jwt_issuer": "memoria-control-api",
        "memoria_auth_secret": SecretStr("auth-secret-material-that-is-long-enough"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def mint(**overrides: object) -> tuple[Ed25519PrivateKey, DeviceDirectMediaTicket]:
    private_key = Ed25519PrivateKey.generate()
    pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    issued_at = datetime.now(UTC)
    claims: dict[str, object] = {
        "session_id": "session-1",
        "user_id": "person_a",
        "device_id": "dev_test_01",
        "client_id": "esp-installation-1",
        "binding_id": "binding-7",
        "binding_version": 3,
        "subject_id": "person_subject",
        "runtime_profile_version": 27,
        "stream_epoch": 1,
    }
    claims.update(overrides)
    return private_key, mint_device_direct_media_ticket(
        settings(streamcore_token_private_key_pem=SecretStr(pem)),
        now=issued_at,
        **claims,
    )


def test_select_device_media_runtime_is_server_owned_and_fails_closed() -> None:
    assert select_device_media_runtime(settings(), device_id="dev_test_01") == "direct_voice_core"
    assert select_device_media_runtime(settings(), device_id="dev_other") == "livekit_compat"
    assert select_device_media_runtime(settings()) == "livekit_compat"
    assert (
        select_device_media_runtime(
            settings(device_media_direct_rollout_mode="all"), device_id="dev_other"
        )
        == "direct_voice_core"
    )
    assert (
        select_device_media_runtime(
            settings(device_media_runtime="livekit_compat"), device_id="dev_test_01"
        )
        == "livekit_compat"
    )
    assert (
        select_device_media_runtime(settings(device_media_runtime=""), device_id="dev_test_01")
        == "livekit_compat"
    )
    assert (
        select_device_media_runtime(settings(device_media_runtime="typo"), device_id="dev_test_01")
        == "livekit_compat"
    )
    assert (
        select_device_media_runtime(
            settings(device_media_direct_canary_device_ids="dev_test_01,,dev_other"),
            device_id="dev_test_01",
        )
        == "livekit_compat"
    )


def test_direct_ticket_is_eddsa_with_full_binding_claims() -> None:
    private_key, ticket = mint()
    assert ticket.token.count(".") == 2
    assert ticket.jti
    assert ticket.expires_at > datetime.now(UTC)
    header = jwt.get_unverified_header(ticket.token)
    assert header == {"alg": "EdDSA", "kid": "media-2026-08", "typ": "JWT"}
    claims = jwt.decode(
        ticket.token,
        private_key.public_key(),
        algorithms=["EdDSA"],
        audience=DEVICE_MEDIA_AUDIENCE,
        issuer="memoria-control-api",
        options={
            "require": [
                "exp",
                "iat",
                "nbf",
                "aud",
                "iss",
                "jti",
                "sub",
                "session_id",
                "device_id",
                "client_id",
                "binding_id",
                "binding_version",
                "subject_id",
                "client_type",
                "stream_epoch",
                "runtime_profile_version",
                "device_settings",
            ]
        },
    )
    assert claims["typ"] == DEVICE_MEDIA_TOKEN_TYPE
    assert claims["session_id"] == "session-1"
    assert claims["sub"] == "person_a"
    assert claims["device_id"] == "dev_test_01"
    assert claims["client_id"] == "esp-installation-1"
    assert claims["binding_id"] == "binding-7"
    assert claims["binding_version"] == 3
    assert claims["subject_id"] == "person_subject"
    assert claims["client_type"] == "device"
    assert claims["stream_epoch"] == 1
    assert claims["runtime_profile_version"] == 27
    assert claims["device_settings"]["audio_mode"] == "half_duplex_safe"
    assert claims["device_settings"]["volume_limit"] == 72
    assert claims["iat"] == claims["nbf"]


def test_direct_ticket_encodes_missing_runtime_subject_as_empty_claim() -> None:
    private_key, ticket = mint(subject_id=None)

    claims = jwt.decode(
        ticket.token,
        private_key.public_key(),
        algorithms=["EdDSA"],
        audience=DEVICE_MEDIA_AUDIENCE,
        issuer="memoria-control-api",
    )

    assert claims["subject_id"] == ""


def test_direct_ticket_never_falls_back_to_hs256() -> None:
    with pytest.raises(ValueError, match="Ed25519 private key"):
        mint_device_direct_media_ticket(
            settings(streamcore_token_secret=SecretStr("a-long-hs256-secret-material")),
            session_id="s",
            user_id="u",
            device_id="d",
            client_id="c",
            binding_id="b",
            binding_version=1,
            subject_id="subject",
            runtime_profile_version=1,
            stream_epoch=1,
        )
    with pytest.raises(ValueError, match="Ed25519 private key"):
        mint_device_direct_media_ticket(
            settings(
                environment="production",
                streamcore_token_secret=SecretStr("short"),
            ),
            session_id="s",
            user_id="u",
            device_id="d",
            client_id="c",
            binding_id="b",
            binding_version=1,
            subject_id="subject",
            runtime_profile_version=1,
            stream_epoch=1,
        )


def test_direct_ticket_rejects_invalid_binding_facts() -> None:
    with pytest.raises(ValueError, match="binding_version"):
        mint(binding_version=0)
    with pytest.raises(ValueError, match="stream_epoch"):
        mint(stream_epoch=0)
    with pytest.raises(ValueError, match="stream_epoch"):
        mint(stream_epoch=DEVICE_STREAM_EPOCH_MAX + 1)
    with pytest.raises(ValueError, match="runtime_profile_version"):
        mint(runtime_profile_version=0)
    with pytest.raises(ValueError, match="runtime_profile_version"):
        mint(runtime_profile_version=DEVICE_STREAM_EPOCH_MAX + 1)
    with pytest.raises(ValueError, match="settings_version"):
        mint(
            device_settings={
                "settings_version": DEVICE_STREAM_EPOCH_MAX + 1,
                "volume_limit": 72,
                "screen_brightness": 80,
                "night_mode": False,
                "do_not_disturb": False,
                "learning_mode": "off",
                "audio_mode": "half_duplex_safe",
                "wake_mode": "button_or_keyword",
                "allowed_barge_in": ["button"],
            }
        )
    with pytest.raises(ValueError, match="subject_id"):
        mint(subject_id=" ")
    with pytest.raises(ValueError, match="device_id"):
        mint(device_id=" ")


def test_ticket_families_are_not_interchangeable() -> None:
    _, direct = mint()
    with pytest.raises(GatewayTicketError):
        verify_device_gateway_ticket(
            direct.token,
            secret="device-ticket-secret-material-that-is-long-enough",
        )
    compat, _ = issue_device_gateway_ticket(
        secret="device-ticket-secret-material-that-is-long-enough",
        session_id="session-1",
        user_id="person_a",
        device_id="dev_test_01",
        client_id="esp-installation-1",
        binding_id="binding-7",
        binding_version=3,
        subject_id="person_subject",
        runtime_profile_version=27,
        room_name="voice-session-1",
        identity="user-person_a",
        agent_name="duplex-zh-agent",
        stream_epoch=1,
        ttl_s=120,
    )
    assert jwt.get_unverified_header(compat)["alg"] == "HS256"
    private_key = Ed25519PrivateKey.generate()
    with pytest.raises(jwt.PyJWTError):
        jwt.decode(compat, private_key.public_key(), algorithms=["EdDSA"])


def _pem() -> str:
    private_key = Ed25519PrivateKey.generate()
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _write_mtls_bundle(tmp_path: Path) -> dict[str, str]:
    """Write a real Control-to-Edge CA plus client certificate/key bundle."""
    ca_key = Ed25519PrivateKey.generate()
    ca_name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "memoria-control-test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(ca_key, None)
    )
    client_key = Ed25519PrivateKey.generate()
    client_name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "control-edge-client")])
    client_cert = (
        x509.CertificateBuilder()
        .subject_name(client_name)
        .issuer_name(ca_name)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=30))
        .add_extension(x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, None)
    )

    def _write(name: str, data: bytes) -> str:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    return {
        "ca": _write("control-edge-ca.crt", ca_cert.public_bytes(serialization.Encoding.PEM)),
        "cert": _write(
            "control-edge-client.crt", client_cert.public_bytes(serialization.Encoding.PEM)
        ),
        "key": _write(
            "control-edge-client.key",
            client_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        ),
    }


def _production_settings(
    monkeypatch: pytest.MonkeyPatch,
    private_key_pem: str,
    tmp_path: Path,
    mtls: dict[str, str] | None = None,
    **overrides: str | None,
) -> ControlSettings:
    bundle = mtls or _write_mtls_bundle(tmp_path)
    base: dict[str, str | None] = {
        "ENVIRONMENT": "production",
        "DEVICE_MEDIA_RUNTIME": "direct_voice_core",
        "DEVICE_MEDIA_DIRECT_ROLLOUT_MODE": "all",
        "DEVICE_DIRECT_MEDIA_WSS_URL": "wss://edge.example/v1/device/media",
        "STREAMCORE_TOKEN_PRIVATE_KEY_PEM": private_key_pem,
        "STREAMCORE_TOKEN_KEY_ID": "media-2026-08",
        "MEDIA_EDGE_INTERNAL_CONTROL_URL": "https://edge-internal.example/control",
        "MEDIA_EDGE_INTERNAL_CONTROL_TOKEN": "edge-control-token-material-that-is-long-enough",
        "MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN": (
            "edge-close-report-token-material-that-is-independent"
        ),
        "MEDIA_EDGE_INTERNAL_CONTROL_CA_FILE": bundle["ca"],
        "MEDIA_EDGE_INTERNAL_CONTROL_CLIENT_CERT_FILE": bundle["cert"],
        "MEDIA_EDGE_INTERNAL_CONTROL_CLIENT_KEY_FILE": bundle["key"],
    }
    base.update(overrides)
    for key, value in base.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return ControlSettings()


def test_production_direct_media_requires_secure_wss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_value = _production_settings(
        monkeypatch,
        _pem(),
        tmp_path,
        DEVICE_DIRECT_MEDIA_WSS_URL="ws://edge.example/v1/device/media",
    )
    with pytest.raises(ValueError, match="DEVICE_DIRECT_MEDIA_WSS_URL"):
        settings_value.validate_device_direct_media()


def test_production_direct_canary_requires_valid_nonempty_device_allowlist(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    missing = _production_settings(
        monkeypatch,
        _pem(),
        tmp_path,
        DEVICE_MEDIA_DIRECT_ROLLOUT_MODE="allowlist",
        DEVICE_MEDIA_DIRECT_CANARY_DEVICE_IDS="",
    )
    with pytest.raises(ValueError, match="DEVICE_MEDIA_DIRECT_CANARY_DEVICE_IDS"):
        missing.validate_device_direct_media()

    malformed = _production_settings(
        monkeypatch,
        _pem(),
        tmp_path,
        DEVICE_MEDIA_DIRECT_ROLLOUT_MODE="allowlist",
        DEVICE_MEDIA_DIRECT_CANARY_DEVICE_IDS="dev_test_01,,dev_other",
    )
    with pytest.raises(ValueError, match="invalid DEVICE_MEDIA_DIRECT_CANARY_DEVICE_IDS"):
        malformed.validate_device_direct_media()


def test_production_direct_media_rejects_hs256_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_value = _production_settings(
        monkeypatch,
        _pem(),
        tmp_path,
        STREAMCORE_TOKEN_PRIVATE_KEY_PEM=None,
        STREAMCORE_TOKEN_SECRET="a-long-hs256-secret-material",
    )
    with pytest.raises(ValueError, match="Ed25519 private key"):
        settings_value.validate_device_direct_media()


def test_production_direct_media_requires_key_id_and_edge_control(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_value = _production_settings(
        monkeypatch,
        _pem(),
        tmp_path,
        STREAMCORE_TOKEN_KEY_ID="",
    )
    with pytest.raises(ValueError, match="STREAMCORE_TOKEN_KEY_ID"):
        settings_value.validate_device_direct_media()
    settings_value = _production_settings(
        monkeypatch,
        _pem(),
        tmp_path,
        STREAMCORE_TOKEN_KEY_ID="media-2026-08",
        MEDIA_EDGE_INTERNAL_CONTROL_URL="http://edge.example/control",
    )
    with pytest.raises(ValueError, match="MEDIA_EDGE_INTERNAL_CONTROL_URL"):
        settings_value.validate_device_direct_media()
    settings_value = _production_settings(
        monkeypatch,
        _pem(),
        tmp_path,
        MEDIA_EDGE_INTERNAL_CONTROL_URL="https://edge-internal.example/control",
        MEDIA_EDGE_INTERNAL_CONTROL_TOKEN="short",
    )
    with pytest.raises(ValueError, match="MEDIA_EDGE_INTERNAL_CONTROL_TOKEN"):
        settings_value.validate_device_direct_media()
    settings_value = _production_settings(
        monkeypatch,
        _pem(),
        tmp_path,
        MEDIA_EDGE_INTERNAL_CONTROL_CLIENT_KEY_FILE="",
    )
    with pytest.raises(ValueError, match="Control-to-Edge mTLS"):
        settings_value.validate_device_direct_media()


def test_production_direct_media_validation_passes_when_complete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_value = _production_settings(monkeypatch, _pem(), tmp_path)
    settings_value.validate_device_direct_media()


def test_production_direct_media_rejects_unparseable_private_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_value = _production_settings(monkeypatch, "not-a-private-key", tmp_path)
    with pytest.raises(ValueError, match="parseable unencrypted Ed25519 private key"):
        settings_value.validate_device_direct_media()


def _rsa_pem() -> str:
    from cryptography.hazmat.primitives.asymmetric import rsa

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return rsa_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def test_production_direct_media_rejects_non_ed25519_private_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_value = _production_settings(monkeypatch, _rsa_pem(), tmp_path)
    with pytest.raises(ValueError, match="requires an Ed25519 private key"):
        settings_value.validate_device_direct_media()


def test_production_direct_media_rejects_mismatched_client_cert_and_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first = _write_mtls_bundle(tmp_path / "first")
    second = _write_mtls_bundle(tmp_path / "second")
    mismatched = {"ca": first["ca"], "cert": first["cert"], "key": second["key"]}
    settings_value = _production_settings(monkeypatch, _pem(), tmp_path, mtls=mismatched)
    with pytest.raises(ValueError, match="certificate and key do not match"):
        settings_value.validate_device_direct_media()


def test_production_direct_media_rejects_foreign_ca_signature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Same CA subject name, different key: the issuer comparison alone must
    # not be enough; the signature has to verify against the configured CA.
    ca_key = Ed25519PrivateKey.generate()
    ca_name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "memoria-control-test-ca")])
    foreign_client_key = Ed25519PrivateKey.generate()
    foreign_client = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "control-edge-client")]))
        .issuer_name(ca_name)
        .public_key(foreign_client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=30))
        .add_extension(x509.ExtendedKeyUsage([x509.ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, None)
    )
    real_bundle = _write_mtls_bundle(tmp_path)
    foreign_cert_path = tmp_path / "foreign-client.crt"
    foreign_key_path = tmp_path / "foreign-client.key"
    foreign_cert_path.write_bytes(foreign_client.public_bytes(serialization.Encoding.PEM))
    foreign_key_path.write_bytes(
        foreign_client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    bundle = {
        "ca": real_bundle["ca"],
        "cert": str(foreign_cert_path),
        "key": str(foreign_key_path),
    }
    settings_value = _production_settings(monkeypatch, _pem(), tmp_path, mtls=bundle)
    with pytest.raises(ValueError, match="not signed by the configured CA"):
        settings_value.validate_device_direct_media()


def test_production_direct_media_rejects_unreadable_mtls_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings_value = _production_settings(
        monkeypatch,
        _pem(),
        tmp_path,
        MEDIA_EDGE_INTERNAL_CONTROL_CA_FILE=str(tmp_path / "missing-ca.crt"),
    )
    with pytest.raises(ValueError, match="readable, parseable"):
        settings_value.validate_device_direct_media()


def test_direct_device_audience_is_dedicated() -> None:
    assert DEVICE_MEDIA_AUDIENCE == "memoria-media-edge"
    assert DEVICE_MEDIA_AUDIENCE != "memoria-media"
