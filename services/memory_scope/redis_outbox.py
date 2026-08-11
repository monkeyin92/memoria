"""Idempotent Redis Stream dispatcher for the MemoryScope outbox."""

from __future__ import annotations

import json
import logging
from typing import Protocol

from redis.asyncio import Redis

from services.memory_scope.domain import MemoryOutboxEvent

LOGGER = logging.getLogger(__name__)

_DISPATCH_SCRIPT = """
local fresh = redis.call('SET', KEYS[2], '1', 'NX', 'EX', ARGV[1])
if fresh then
  redis.call(
    'XADD', KEYS[1], 'MAXLEN', '~', ARGV[2], '*',
    'outbox_id', ARGV[3],
    'event_id', ARGV[4],
    'topic', ARGV[5],
    'payload', ARGV[6],
    'created_at', ARGV[7]
  )
  return 1
end
return 0
"""


class _RedisPort(Protocol):
    async def eval(
        self,
        script: str,
        numkeys: int,
        *keys_and_args: object,
    ) -> object: ...

    async def aclose(self) -> None: ...


class RedisMemoryOutboxDispatcher:
    """Publish each event once and let duplicate retries count as success.

    The Lua script atomically creates a bounded-lifetime dedup marker and
    appends the stream event.  Therefore a worker crash after Redis accepted
    the event but before PostgreSQL acknowledgement is safe: the retry sees
    the marker and may acknowledge without emitting a duplicate.
    """

    def __init__(
        self,
        redis: _RedisPort,
        *,
        stream: str = "memoria:memory:events",
        dedup_ttl_seconds: int = 2_592_000,
        max_stream_length: int = 100_000,
    ) -> None:
        if not stream.strip():
            raise ValueError("memory outbox stream must not be blank")
        if dedup_ttl_seconds < 86_400:
            raise ValueError("memory outbox dedup TTL must be at least one day")
        if max_stream_length < 1_000:
            raise ValueError("memory outbox stream bound must be at least 1000")
        self._redis = redis
        self._stream = stream
        self._dedup_ttl_seconds = dedup_ttl_seconds
        self._max_stream_length = max_stream_length

    @classmethod
    def from_url(
        cls,
        redis_url: str,
        *,
        stream: str = "memoria:memory:events",
    ) -> RedisMemoryOutboxDispatcher:
        if not redis_url.strip():
            raise ValueError("memory outbox requires REDIS_URL")
        client = Redis.from_url(redis_url, decode_responses=True)
        return cls(client, stream=stream)

    async def dispatch(self, event: MemoryOutboxEvent) -> bool:
        try:
            payload = json.dumps(
                event.payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            await self._redis.eval(
                _DISPATCH_SCRIPT,
                2,
                self._stream,
                f"{self._stream}:dedup:{event.event_id}",
                self._dedup_ttl_seconds,
                self._max_stream_length,
                event.outbox_id,
                event.event_id,
                event.topic,
                payload,
                event.created_at.isoformat(),
            )
        except Exception:
            LOGGER.exception(
                "memory outbox dispatch failed event_id=%s topic=%s",
                event.event_id,
                event.topic,
            )
            return False
        return True

    async def close(self) -> None:
        await self._redis.aclose()


__all__ = ["RedisMemoryOutboxDispatcher"]
