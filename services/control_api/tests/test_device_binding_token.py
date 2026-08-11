from datetime import UTC, datetime, timedelta

import pytest
from services.control_api.app.device_binding_token import (
    DeviceBindingTokenError,
    mint_device_binding_token,
    verify_device_binding_token,
)


def test_device_binding_token_is_signed_scoped_and_expires() -> None:
    now = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    token = mint_device_binding_token(
        device_id="device-1",
        secret=b"device-binding-token-test-secret",
        now=now,
        ttl=timedelta(minutes=5),
        nonce="nonce-1",
    )

    assert verify_device_binding_token(
        token,
        secret=b"device-binding-token-test-secret",
        now=now + timedelta(minutes=1),
    ) == "device-1"
    with pytest.raises(DeviceBindingTokenError, match="expired"):
        verify_device_binding_token(
            token,
            secret=b"device-binding-token-test-secret",
            now=now + timedelta(minutes=6),
        )
    with pytest.raises(DeviceBindingTokenError, match="signature"):
        verify_device_binding_token(
            f"{token[:-1]}x",
            secret=b"device-binding-token-test-secret",
            now=now + timedelta(minutes=1),
        )
