"""coturn REST (HMAC) short-lived credential minting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class TurnCredentials:
    """Credentials safe to return to a client for one short TURN allocation."""

    username: str
    password: str
    expires_at: int

    @property
    def credential(self) -> str:
        """The ``credential`` spelling used by WebRTC ICE-server JSON."""

        return self.password

    def as_dict(self) -> dict[str, object]:
        return {
            "username": self.username,
            "credential": self.password,
            "expires_at": self.expires_at,
        }


def _epoch_seconds(now: datetime | int | float | None) -> int:
    if now is None:
        return int(time.time())
    if isinstance(now, datetime):
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return int(now.timestamp())
    return int(now)


def mint_turn_credentials(
    identity: str,
    shared_secret: str,
    *,
    ttl_s: int = 300,
    now: datetime | int | float | None = None,
) -> TurnCredentials:
    """Mint coturn's time-limited REST credential for ``identity``.

    coturn expects ``username = <unix-expiry>:<opaque-id>`` and an
    base64 HMAC-SHA1 digest as the password.  The shared secret stays
    server-side; only this short-lived pair is returned.
    """

    if not identity or ":" in identity:
        raise ValueError("identity must be non-empty and must not contain ':'")
    if not shared_secret:
        raise ValueError("coturn shared secret is required")
    if ttl_s <= 0:
        raise ValueError("ttl_s must be positive")
    expires_at = _epoch_seconds(now) + ttl_s
    username = f"{expires_at}:{identity}"
    digest = hmac.new(
        shared_secret.encode("utf-8"),
        username.encode("utf-8"),
        hashlib.sha1,
    ).digest()
    password = base64.b64encode(digest).decode("ascii")
    return TurnCredentials(username=username, password=password, expires_at=expires_at)


class TurnCredentialMinter:
    """Configured minting helper for dependency injection in API routes."""

    def __init__(self, shared_secret: str, *, ttl_s: int = 300) -> None:
        if not shared_secret:
            raise ValueError("coturn shared secret is required")
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self._shared_secret = shared_secret
        self.ttl_s = ttl_s

    def mint(
        self,
        identity: str,
        *,
        now: datetime | int | float | None = None,
    ) -> TurnCredentials:
        return mint_turn_credentials(
            identity,
            self._shared_secret,
            ttl_s=self.ttl_s,
            now=now,
        )


mint_credentials = mint_turn_credentials
