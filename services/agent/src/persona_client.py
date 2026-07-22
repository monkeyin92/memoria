"""Non-blocking session-scoped PersonaCapsule client with a short-lived cache."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal

import httpx

SpeakerClass = Literal["owner", "guest", "uncertain"]


@dataclass(frozen=True, slots=True)
class PersonaClientConfig:
    endpoint: str
    internal_token: str
    timeout_s: float = 0.3
    cache_ttl_s: float = 60.0

    def __post_init__(self) -> None:
        url = httpx.URL(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("persona endpoint must be HTTP(S)")
        if not self.internal_token.strip():
            raise ValueError("persona internal token must not be blank")
        if self.timeout_s <= 0 or self.cache_ttl_s <= 0:
            raise ValueError("persona timeout and cache TTL must be positive")


@dataclass(frozen=True, slots=True)
class PersonaCapsuleSnapshot:
    version_id: str | None
    version_number: int | None
    prompt_fragment: str


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    capsule: PersonaCapsuleSnapshot
    fetched_at: float


class PersonaClient:
    """Refresh in the background; realtime callers only read completed cache entries."""

    def __init__(
        self,
        config: PersonaClientConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_s)
        self._owns_client = client is None
        self._cache: dict[tuple[str, SpeakerClass], _CacheEntry] = {}
        self._epochs: dict[str, int] = {}

    async def refresh(
        self,
        *,
        session_id: str,
        speaker_class: SpeakerClass,
        topic: str,
    ) -> bool:
        if not session_id.strip() or len(topic) > 1000:
            raise ValueError("persona refresh requires session_id and topic <= 1000 characters")
        epoch = self._next_epoch(session_id)
        if speaker_class == "guest":
            self._clear_session(session_id)
            return False
        cache_key = (session_id, speaker_class)
        try:
            response = await self._client.post(
                self._config.endpoint,
                headers={"X-Memoria-Internal-Token": self._config.internal_token},
                json={
                    "session_id": session_id,
                    "speaker_class": speaker_class,
                    "topic": topic,
                },
                timeout=self._config.timeout_s,
            )
            response.raise_for_status()
            capsule = self._parse(response.json())
        except (httpx.HTTPError, TypeError, ValueError):
            if self._epochs.get(session_id) == epoch:
                self._cache.pop(cache_key, None)
            return False
        if self._epochs.get(session_id) != epoch:
            return False
        if capsule.prompt_fragment:
            self._cache[cache_key] = _CacheEntry(capsule, time.monotonic())
        else:
            self._cache.pop(cache_key, None)
        return True

    def cached(
        self,
        *,
        session_id: str,
        speaker_class: SpeakerClass,
    ) -> PersonaCapsuleSnapshot | None:
        if speaker_class == "guest":
            self._next_epoch(session_id)
            self._clear_session(session_id)
            return None
        cache_key = (session_id, speaker_class)
        entry = self._cache.get(cache_key)
        if entry is None:
            return None
        if time.monotonic() - entry.fetched_at > self._config.cache_ttl_s:
            self._cache.pop(cache_key, None)
            return None
        return entry.capsule

    def _clear_session(self, session_id: str) -> None:
        for key in tuple(self._cache):
            if key[0] == session_id:
                self._cache.pop(key, None)

    def _next_epoch(self, session_id: str) -> int:
        epoch = self._epochs.get(session_id, 0) + 1
        self._epochs[session_id] = epoch
        return epoch

    @staticmethod
    def _parse(payload: Any) -> PersonaCapsuleSnapshot:
        if not isinstance(payload, dict):
            raise ValueError("invalid persona response")
        prompt = payload.get("prompt_fragment")
        version_id = payload.get("version_id")
        version_number = payload.get("version_number")
        if not isinstance(prompt, str) or len(prompt) > 4000:
            raise ValueError("invalid persona response")
        if version_id is not None and (not isinstance(version_id, str) or not version_id):
            raise ValueError("invalid persona response")
        if version_number is not None and (
            not isinstance(version_number, int)
            or isinstance(version_number, bool)
            or version_number <= 0
        ):
            raise ValueError("invalid persona response")
        if bool(version_id) != (version_number is not None):
            raise ValueError("invalid persona response")
        return PersonaCapsuleSnapshot(
            version_id=version_id,
            version_number=version_number,
            prompt_fragment=prompt,
        )

    async def close(self) -> None:
        self._cache.clear()
        if self._owns_client:
            await self._client.aclose()
