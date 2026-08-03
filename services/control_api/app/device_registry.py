"""Persistent device identity registry and single-use signed challenges."""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from services.agent.src.voice_core.device_security import (
    DeviceIdentity,
    SignedChallenge,
    verify_challenge,
)
from services.control_api.app.database import MemoryStore


class DeviceRegistryError(RuntimeError):
    """Base error for device provisioning/authentication failures."""


class DeviceNotFound(DeviceRegistryError):
    pass


class DeviceChallengeRejected(DeviceRegistryError):
    pass


class DeviceChallengeRateLimited(DeviceRegistryError):
    """Too many outstanding bootstrap challenges for one device."""


@dataclass(frozen=True, slots=True)
class DeviceChallenge:
    device_id: str
    nonce: str
    issued_at_ms: int
    expires_at_ms: int

    def to_dict(self) -> dict[str, int | str]:
        return {
            "device_id": self.device_id,
            "nonce": self.nonce,
            "issued_at_ms": self.issued_at_ms,
            "expires_at_ms": self.expires_at_ms,
        }


class DeviceRegistry:
    """Store public identities in the existing encrypted-data boundary.

    Private keys never enter Control API.  A challenge is persisted as a
    SHA-256 nonce hash and can be consumed exactly once, so reconnect/session
    claims cannot be replayed with the same signature.
    """

    def __init__(
        self,
        store: MemoryStore,
        *,
        challenge_ttl_ms: int = 120_000,
        max_pending_challenges: int = 3,
    ) -> None:
        if challenge_ttl_ms <= 0:
            raise ValueError("challenge_ttl_ms must be positive")
        if max_pending_challenges <= 0:
            raise ValueError("max_pending_challenges must be positive")
        self.store = store
        self.challenge_ttl_ms = challenge_ttl_ms
        self.max_pending_challenges = max_pending_challenges

    def register(
        self,
        *,
        device_id: str,
        account_id: str,
        public_key_b64: str,
        firmware_channel: str = "stable",
        now: str,
    ) -> DeviceIdentity:
        identity = DeviceIdentity(
            device_id=device_id,
            public_key_b64=public_key_b64,
            firmware_channel=firmware_channel,
        )
        self.store.register_device_identity(
            device_id=identity.device_id,
            account_id=account_id,
            public_key_b64=identity.public_key_b64,
            firmware_channel=identity.firmware_channel,
            now=now,
        )
        return identity

    def identity(self, device_id: str) -> tuple[DeviceIdentity, str] | None:
        record = self.store.get_device_identity(device_id=device_id)
        if record is None or record.get("revoked_at") is not None:
            return None
        return (
            DeviceIdentity(
                device_id=str(record["device_id"]),
                public_key_b64=str(record["public_key_b64"]),
                firmware_channel=str(record["firmware_channel"]),
            ),
            str(record["account_id"]),
        )

    def issue_challenge(
        self,
        device_id: str,
        *,
        now_ms: int | None = None,
    ) -> DeviceChallenge:
        resolved = self.identity(device_id)
        if resolved is None:
            raise DeviceNotFound(device_id)
        issued_at_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if issued_at_ms < 0:
            raise ValueError("now_ms must be non-negative")
        if self.store.count_pending_device_challenges(
            device_id=device_id,
            now_ms=issued_at_ms,
        ) >= self.max_pending_challenges:
            raise DeviceChallengeRateLimited(device_id)
        nonce = secrets.token_urlsafe(32)
        challenge = DeviceChallenge(
            device_id=device_id,
            nonce=nonce,
            issued_at_ms=issued_at_ms,
            expires_at_ms=issued_at_ms + self.challenge_ttl_ms,
        )
        self.store.issue_device_challenge(
            nonce_hash=_hash_nonce(nonce),
            device_id=device_id,
            issued_at_ms=challenge.issued_at_ms,
            expires_at_ms=challenge.expires_at_ms,
        )
        return challenge

    def authenticate(
        self,
        device_id: str,
        challenge: SignedChallenge,
        *,
        now_ms: int | None = None,
    ) -> str:
        resolved = self.identity(device_id)
        if resolved is None:
            raise DeviceNotFound(device_id)
        identity, account_id = resolved
        current_ms = int(time.time() * 1000) if now_ms is None else now_ms
        if (
            challenge.device_id != device_id
            or not verify_challenge(
                identity,
                challenge,
                now_ms=current_ms,
                max_age_ms=self.challenge_ttl_ms,
            )
            or not self.store.consume_device_challenge(
                nonce_hash=_hash_nonce(challenge.nonce),
                device_id=device_id,
                now_ms=current_ms,
            )
        ):
            raise DeviceChallengeRejected(device_id)
        return account_id


def _hash_nonce(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


__all__ = [
    "DeviceChallenge",
    "DeviceChallengeRejected",
    "DeviceChallengeRateLimited",
    "DeviceNotFound",
    "DeviceRegistry",
    "DeviceRegistryError",
]
