"""Short-lived HMAC URLs for provider-only access to decrypted clone samples."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import quote


class VoiceSampleURLSigner:
    def __init__(self, *, secret: str, public_base_url: str, ttl_s: int = 300) -> None:
        if len(secret) < 16:
            raise ValueError("voice sample URL secret must contain at least 16 characters")
        if not public_base_url.startswith(("https://", "http://")):
            raise ValueError("voice sample public base URL must be HTTP(S)")
        if not 30 <= ttl_s <= 1800:
            raise ValueError("voice sample URL TTL must be 30..1800 seconds")
        self._secret = secret.encode("utf-8")
        self._base = public_base_url.rstrip("/")
        self._ttl_s = ttl_s

    def url(self, sample_id: str) -> str:
        if not sample_id.strip():
            raise ValueError("sample_id must not be blank")
        expires = int(time.time()) + self._ttl_s
        token = self._token(sample_id, expires)
        return (
            f"{self._base}/v1/voices/provider-samples/{quote(sample_id, safe='')}"
            f"?token={quote(token, safe='')}"
        )

    def verify(self, *, sample_id: str, token: str) -> bool:
        try:
            expires_text, supplied = token.split(".", 1)
            expires = int(expires_text)
        except (TypeError, ValueError):
            return False
        if expires < int(time.time()):
            return False
        expected = self._signature(sample_id, expires)
        return hmac.compare_digest(supplied, expected)

    def _token(self, sample_id: str, expires: int) -> str:
        return f"{expires}.{self._signature(sample_id, expires)}"

    def _signature(self, sample_id: str, expires: int) -> str:
        digest = hmac.new(
            self._secret,
            f"{sample_id}.{expires}".encode(),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
