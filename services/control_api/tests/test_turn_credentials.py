from __future__ import annotations

import base64
import hashlib
import hmac

import pytest
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
