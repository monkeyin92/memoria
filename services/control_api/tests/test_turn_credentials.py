from __future__ import annotations

import base64
import hashlib
import hmac
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from services.control_api.app.routes.session import turn_ice_servers
from services.control_api.app.turn_credentials import (
    TurnCredentialMinter,
    mint_turn_credentials,
)


def test_coturn_credentials_match_rest_hmac_spec() -> None:
    result = mint_turn_credentials("session-1", "shared-secret", ttl_s=300, now=1_700_000_000)
    expected_username = "1700000300:session-1"
    expected_password = base64.b64encode(
        hmac.new(b"shared-secret", expected_username.encode(), hashlib.sha1).digest()
    ).decode()
    assert result.username == expected_username
    assert result.password == expected_password
    assert result.credential == result.password
    assert result.as_dict() == {
        "username": expected_username,
        "credential": expected_password,
        "expires_at": 1_700_000_300,
    }


def test_minter_reuses_ttl_without_exposing_secret() -> None:
    minter = TurnCredentialMinter("shared-secret", ttl_s=60)
    result = minter.mint("device-1", now=1_700_000_000)
    assert result.expires_at == 1_700_000_060
    assert "shared-secret" not in repr(result)


@pytest.mark.parametrize("identity", ["", "device:forged"])
def test_identity_cannot_break_coturn_username_format(identity: str) -> None:
    with pytest.raises(ValueError):
        mint_turn_credentials(identity, "shared-secret")


def test_device_turn_scope_is_opaque_and_coturn_safe() -> None:
    settings = SimpleNamespace(
        coturn_urls_list=lambda: ["turn:turn.example:3478"],
        coturn_shared_secret=SecretStr("shared-secret"),
        coturn_credential_ttl_s=300,
    )

    servers = turn_ice_servers(
        settings,
        session_id="session:with:separators",
        device_id="device:with:separators",
    )

    expiry, identity = servers[0]["username"].split(":", 1)
    assert expiry.isdigit()
    assert identity.startswith("media-")
    assert ":" not in identity
