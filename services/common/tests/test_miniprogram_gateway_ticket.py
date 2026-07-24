from __future__ import annotations

import pytest
from services.common.miniprogram_gateway_ticket import (
    GatewayTicketError,
    issue_gateway_ticket,
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
