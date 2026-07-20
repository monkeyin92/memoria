"""Non-blocking session-scoped long-term memory client."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx

SpeakerClass = Literal["owner", "guest", "uncertain"]
MemoryCategory = Literal[
    "life_story",
    "work_experience",
    "family_principle",
    "parenting_principle",
    "life_wisdom",
    "daily_life",
]
_MEMORY_CATEGORIES = frozenset(
    {
        "life_story",
        "work_experience",
        "family_principle",
        "parenting_principle",
        "life_wisdom",
        "daily_life",
    }
)


@dataclass(frozen=True, slots=True)
class MemoryContextClientConfig:
    endpoint: str
    internal_token: str
    timeout_s: float = 0.3
    cache_ttl_s: float = 60.0
    limit: int = 8

    def __post_init__(self) -> None:
        url = httpx.URL(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("memory context endpoint must be HTTP(S)")
        if not self.internal_token.strip():
            raise ValueError("memory context internal token must not be blank")
        if self.timeout_s <= 0 or self.cache_ttl_s <= 0:
            raise ValueError("memory context timeout and cache TTL must be positive")
        if not 1 <= self.limit <= 20:
            raise ValueError("memory context limit must be between 1 and 20")


@dataclass(frozen=True, slots=True)
class MemoryContextItem:
    kind: str
    title: str
    snippet: str
    category: MemoryCategory
    status: Literal["confirmed"]
    source_event_id: str
    occurred_at: str


@dataclass(frozen=True, slots=True)
class MemoryContextSnapshot:
    items: tuple[MemoryContextItem, ...]


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    snapshot: MemoryContextSnapshot
    fetched_at: float


class MemoryContextClient:
    """Refresh in the background; realtime callers only read completed cache entries."""

    def __init__(
        self,
        config: MemoryContextClientConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_s)
        self._owns_client = client is None
        self._cache: dict[str, _CacheEntry] = {}
        self._epochs: dict[str, int] = {}

    async def refresh(
        self,
        *,
        session_id: str,
        speaker_class: SpeakerClass,
        topic: str,
    ) -> bool:
        if not session_id.strip() or len(topic) > 1000:
            raise ValueError(
                "memory context refresh requires session_id and topic <= 1000 characters"
            )
        epoch = self._next_epoch(session_id)
        if speaker_class != "owner":
            self._cache.pop(session_id, None)
            return False
        try:
            response = await self._client.post(
                self._config.endpoint,
                headers={"X-Memoria-Internal-Token": self._config.internal_token},
                json={
                    "session_id": session_id,
                    "speaker_class": speaker_class,
                    "topic": topic,
                    "limit": self._config.limit,
                },
                timeout=self._config.timeout_s,
            )
            response.raise_for_status()
            snapshot = self._parse(response.json())
        except (httpx.HTTPError, TypeError, ValueError):
            if self._epochs.get(session_id) == epoch:
                self._cache.pop(session_id, None)
            return False
        if self._epochs.get(session_id) != epoch:
            return False
        if snapshot.items:
            self._cache[session_id] = _CacheEntry(snapshot, time.monotonic())
        else:
            self._cache.pop(session_id, None)
        return True

    def cached(
        self,
        *,
        session_id: str,
        speaker_class: SpeakerClass,
    ) -> MemoryContextSnapshot | None:
        if speaker_class != "owner":
            self._next_epoch(session_id)
            self._cache.pop(session_id, None)
            return None
        entry = self._cache.get(session_id)
        if entry is None:
            return None
        if time.monotonic() - entry.fetched_at > self._config.cache_ttl_s:
            self._cache.pop(session_id, None)
            return None
        return entry.snapshot

    def _next_epoch(self, session_id: str) -> int:
        epoch = self._epochs.get(session_id, 0) + 1
        self._epochs[session_id] = epoch
        return epoch

    def _parse(self, payload: Any) -> MemoryContextSnapshot:
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise ValueError("invalid memory context response")
        raw_items = payload["items"]
        if len(raw_items) > self._config.limit:
            raise ValueError("invalid memory context response")
        items: list[MemoryContextItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict) or raw.get("status") != "confirmed":
                raise ValueError("invalid memory context response")
            kind = self._text(raw, "kind", 64)
            title = self._text(raw, "title", 1000)
            snippet = self._text(raw, "snippet", 4000)
            category = self._text(raw, "category", 64)
            source_event_id = self._text(raw, "source_event_id", 128)
            occurred_at = self._text(raw, "occurred_at", 64)
            timestamp = datetime.fromisoformat(occurred_at)
            if timestamp.tzinfo is None or category not in _MEMORY_CATEGORIES:
                raise ValueError("invalid memory context response")
            items.append(
                MemoryContextItem(
                    kind=kind,
                    title=title,
                    snippet=snippet,
                    category=category,  # type: ignore[arg-type]
                    status="confirmed",
                    source_event_id=source_event_id,
                    occurred_at=occurred_at,
                )
            )
        return MemoryContextSnapshot(items=tuple(items))

    @staticmethod
    def _text(payload: dict[str, Any], key: str, max_length: int) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > max_length:
            raise ValueError("invalid memory context response")
        return value

    async def close(self) -> None:
        self._cache.clear()
        if self._owns_client:
            await self._client.aclose()
