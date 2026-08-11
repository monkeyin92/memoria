from __future__ import annotations

import pytest
from services.common.miniprogram_gateway_ticket import (
    GatewayTicketError,
    issue_device_gateway_ticket,
    issue_gateway_ticket,
    verify_device_gateway_ticket,
    verify_gateway_ticket,
)

_SECRET = "ticket-secret-material-that-is-long-enough"


def _ticket(*, now_s: int = 1_700_000_000, ttl_s: int = 90) -> str:
    token, ttl = issue_gateway_ticket(
        secret=_SECRET,
        session_id="session-1",
        user_id="account-1",
        room_name="voice-session-1",
        identity="user-account-1-session",
        agent_name="duplex-zh-agent",
        ttl_s=ttl_s,
        now_s=now_s,
    )
    assert ttl == ttl_s
    return token


def test_gateway_ticket_is_short_lived_and_bound_to_cascade_session() -> None:
    claims = verify_gateway_ticket(
        _ticket(),
        secret=_SECRET,
        now_s=1_700_000_010,
    )

    assert claims.session_id == "session-1"
    assert claims.user_id == "account-1"
    assert claims.room_name == "voice-session-1"
    assert claims.identity == "user-account-1-session"
    assert claims.agent_name == "duplex-zh-agent"
    assert claims.voice_backend == "cascade"
    assert claims.ticket_id


@pytest.mark.parametrize("now_s", [1_699_999_999, 1_700_000_090])
def test_gateway_ticket_rejects_before_validity_or_after_expiry(now_s: int) -> None:
    with pytest.raises(GatewayTicketError, match="not currently valid"):
        verify_gateway_ticket(_ticket(), secret=_SECRET, now_s=now_s)


def test_gateway_ticket_rejects_tamper_and_long_lived_claim() -> None:
    token = _ticket()
    with pytest.raises(GatewayTicketError, match="verification failed"):
        verify_gateway_ticket(token + "x", secret=_SECRET, now_s=1_700_000_010)

    with pytest.raises(GatewayTicketError, match="not currently valid"):
        verify_gateway_ticket(
            _ticket(ttl_s=301),
            secret=_SECRET,
            now_s=1_700_000_010,
            max_ttl_s=300,
        )


def test_device_gateway_ticket_is_bound_to_device_binding_and_epoch() -> None:
    token, ttl = issue_device_gateway_ticket(
        secret=_SECRET,
        session_id="session-device-1",
        user_id="person-1",
        device_id="dev-1",
        client_id="installation-1",
        binding_id="binding-1",
        binding_version=3,
        room_name="room-device-1",
        identity="device-dev-1",
        agent_name="duplex-zh-agent",
        stream_epoch=2,
        ttl_s=90,
        now_s=1_700_000_000,
    )
    claims = verify_device_gateway_ticket(
        token,
        secret=_SECRET,
        now_s=1_700_000_010,
    )
    assert ttl == 90
    assert claims.device_id == "dev-1"
    assert claims.client_id == "installation-1"
    assert claims.binding_id == "binding-1"
    assert claims.binding_version == 3
    assert claims.stream_epoch == 2
    assert claims.session_id == "session-device-1"


def test_miniprogram_and_device_gateway_tickets_are_not_interchangeable() -> None:
    device_token, _ = issue_device_gateway_ticket(
        secret=_SECRET,
        session_id="session-device-1",
        user_id="person-1",
        device_id="dev-1",
        client_id="installation-1",
        binding_id="binding-1",
        binding_version=1,
        room_name="room-device-1",
        identity="device-dev-1",
        agent_name="duplex-zh-agent",
        stream_epoch=1,
        ttl_s=90,
        now_s=1_700_000_000,
    )
    with pytest.raises(GatewayTicketError):
        verify_gateway_ticket(device_token, secret=_SECRET, now_s=1_700_000_010)
    with pytest.raises(GatewayTicketError):
        verify_device_gateway_ticket(_ticket(), secret=_SECRET, now_s=1_700_000_010)
