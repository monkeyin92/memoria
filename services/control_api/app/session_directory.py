"""Short-lived routing authority for media sessions.

The directory deliberately stores routing and fencing metadata only.  It is not
a source of truth for account data or conversation history.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal


class SessionDirectoryError(RuntimeError):
    """Base error raised by the session directory."""


class SessionDirectoryUnavailable(SessionDirectoryError):
    """The configured shared directory could not be reached."""


class SessionNotFound(SessionDirectoryError):
    """A session is absent or has expired."""


class SessionDraining(SessionDirectoryError):
    """A draining session cannot be reconnected or claimed implicitly."""


class SessionEpochConflict(SessionDirectoryError):
    """The caller tried to mutate a route from an older stream epoch."""


@dataclass(frozen=True, slots=True)
class SessionRoute:
    session_id: str
    media_edge_id: str
    voice_core_id: str
    stream_epoch: int
    generation: int
    device_id: str
    account_id: str
    expires_at: datetime
    media_runtime: Literal["livekit", "streamcore"] = "livekit"
    state: Literal["active", "draining"] = "active"

    @property
    def generation_id(self) -> int:
        """Wire-format spelling used by the media contracts."""

        return self.generation

    @property
    def media_edge(self) -> str:
        return self.media_edge_id

    @property
    def voice_core(self) -> str:
        return self.voice_core_id

    @property
    def status(self) -> Literal["active", "draining"]:
        return self.state

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= now

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["expires_at"] = self.expires_at.isoformat()
        value["generation_id"] = self.generation
        return value


Clock = Callable[[], datetime]


class SessionDirectory:
    """A TTL session directory with an optional Redis backend.

    With no ``redis_url`` or ``redis_client`` this is an in-memory directory,
    useful for a single control-api process and tests.  Once a Redis backend is
    configured, failures raise :class:`SessionDirectoryUnavailable`; the class
    never silently falls back to local state.
    """

    _KEY_PREFIX = "memoria:session-directory:"
    _REPLACE_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return -1 end
local current = cjson.decode(raw)
if current.state == 'draining' then return -3 end
if tonumber(current.stream_epoch) ~= tonumber(ARGV[1]) then return -2 end
if tonumber(current.generation) ~= tonumber(ARGV[2]) then return -5 end
local replacement = cjson.decode(ARGV[3])
if replacement.__require_generation_advance == true
   and tonumber(current.generation) + 1 ~= tonumber(replacement.generation) then
  return -4
end
redis.call('SET', KEYS[1], ARGV[3], 'EX', ARGV[4])
return 1
"""

    def __init__(
        self,
        *,
        ttl_s: int = 300,
        redis_url: str | None = None,
        redis_client: Any | None = None,
        now: Clock | None = None,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        if redis_url and redis_client is not None:
            raise ValueError("configure redis_url or redis_client, not both")
        self.ttl_s = ttl_s
        self._now = now or (lambda: datetime.now(UTC))
        self._routes: dict[str, SessionRoute] = {}
        self._lock = asyncio.Lock()
        self._redis = redis_client
        if redis_url:
            try:
                from redis import asyncio as redis_asyncio
            except ImportError as exc:  # pragma: no cover - dependency is optional at runtime
                raise SessionDirectoryUnavailable(
                    "redis.asyncio is required when REDIS_URL is configured"
                ) from exc
            self._redis = redis_asyncio.from_url(  # type: ignore[no-untyped-call]
                redis_url, decode_responses=True
            )

    @property
    def is_shared(self) -> bool:
        return self._redis is not None

    def _timestamp(self) -> datetime:
        current = self._now()
        if current.tzinfo is None or current.utcoffset() != timedelta(0):
            return (
                current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
            )
        return current

    @staticmethod
    def _validate_ids(*, session_id: str, media_edge_id: str, voice_core_id: str) -> None:
        if not session_id.strip() or not media_edge_id.strip() or not voice_core_id.strip():
            raise ValueError("session_id, media_edge_id and voice_core_id are required")

    def _route(
        self,
        *,
        session_id: str,
        media_edge_id: str,
        voice_core_id: str,
        stream_epoch: int,
        generation: int,
        device_id: str,
        account_id: str,
        ttl_s: int | None,
        media_runtime: Literal["livekit", "streamcore"] = "livekit",
        state: Literal["active", "draining"] = "active",
    ) -> SessionRoute:
        self._validate_ids(
            session_id=session_id,
            media_edge_id=media_edge_id,
            voice_core_id=voice_core_id,
        )
        if not device_id.strip() or not account_id.strip():
            raise ValueError("device_id and account_id are required")
        if stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if generation < 0:
            raise ValueError("generation must be non-negative")
        lifetime = self.ttl_s if ttl_s is None else ttl_s
        if lifetime <= 0:
            raise ValueError("ttl_s must be positive")
        return SessionRoute(
            session_id=session_id,
            media_edge_id=media_edge_id,
            voice_core_id=voice_core_id,
            stream_epoch=stream_epoch,
            generation=generation,
            device_id=device_id,
            account_id=account_id,
            expires_at=self._timestamp() + timedelta(seconds=lifetime),
            media_runtime=media_runtime,
            state=state,
        )

    @classmethod
    def _key(cls, session_id: str) -> str:
        return f"{cls._KEY_PREFIX}{session_id}"

    @staticmethod
    def _from_dict(value: dict[str, Any]) -> SessionRoute:
        try:
            expires_at = datetime.fromisoformat(str(value["expires_at"]))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            return SessionRoute(
                session_id=str(value["session_id"]),
                media_edge_id=str(value["media_edge_id"]),
                voice_core_id=str(value["voice_core_id"]),
                stream_epoch=int(value["stream_epoch"]),
                generation=int(value["generation"]),
                device_id=str(value["device_id"]),
                account_id=str(value["account_id"]),
                expires_at=expires_at.astimezone(UTC),
                media_runtime=(
                    "streamcore" if value.get("media_runtime") == "streamcore" else "livekit"
                ),
                state="draining" if value.get("state") == "draining" else "active",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionDirectoryError("invalid session directory record") from exc

    async def _redis_get(self, session_id: str) -> SessionRoute | None:
        assert self._redis is not None
        try:
            raw = await self._redis.get(self._key(session_id))
        except Exception as exc:  # Redis errors must fail closed.
            raise SessionDirectoryUnavailable("session directory lookup failed") from exc
        if raw is None:
            return None
        try:
            value = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
            if not isinstance(value, dict):
                raise TypeError("session directory record must be an object")
            route = self._from_dict(value)
        except (TypeError, json.JSONDecodeError, SessionDirectoryError) as exc:
            raise SessionDirectoryError("invalid session directory record") from exc
        if route.is_expired(self._timestamp()):
            await self.expire(session_id)
            return None
        return route

    async def claim(
        self,
        session_id: str,
        *,
        media_edge_id: str,
        voice_core_id: str,
        device_id: str,
        account_id: str,
        stream_epoch: int = 1,
        generation: int = 0,
        ttl_s: int | None = None,
        media_runtime: Literal["livekit", "streamcore"] = "livekit",
    ) -> SessionRoute:
        route = self._route(
            session_id=session_id,
            media_edge_id=media_edge_id,
            voice_core_id=voice_core_id,
            stream_epoch=stream_epoch,
            generation=generation,
            device_id=device_id,
            account_id=account_id,
            ttl_s=ttl_s,
            media_runtime=media_runtime,
        )
        if self._redis is not None:
            try:
                seconds = max(1, math.ceil((route.expires_at - self._timestamp()).total_seconds()))
                result = await self._redis.set(
                    self._key(session_id),
                    json.dumps(route.as_dict()),
                    ex=seconds,
                    nx=True,
                )
                if not result:
                    existing = await self._redis_get(session_id)
                    if existing is not None and self._same_claim(existing, route):
                        return existing
                    raise SessionEpochConflict(session_id)
            except Exception as exc:
                if isinstance(exc, SessionEpochConflict):
                    raise
                raise SessionDirectoryUnavailable("session directory claim failed") from exc
            return route
        async with self._lock:
            existing = self._routes.get(session_id)
            if existing is not None and not existing.is_expired(self._timestamp()):
                if self._same_claim(existing, route):
                    return existing
                raise SessionEpochConflict(session_id)
            self._routes[session_id] = route
        return route

    @staticmethod
    def _same_claim(existing: SessionRoute, requested: SessionRoute) -> bool:
        return (
            existing.session_id == requested.session_id
            and existing.media_edge_id == requested.media_edge_id
            and existing.voice_core_id == requested.voice_core_id
            and existing.stream_epoch == requested.stream_epoch
            and existing.generation == requested.generation
            and existing.device_id == requested.device_id
            and existing.account_id == requested.account_id
            and existing.media_runtime == requested.media_runtime
            and existing.state == requested.state == "active"
        )

    async def _redis_replace(
        self,
        current: SessionRoute,
        replacement: SessionRoute,
    ) -> SessionRoute:
        """Atomically replace one route using its stream epoch as a CAS fence."""

        if self._redis is None:  # pragma: no cover - callers guard this branch
            raise SessionDirectoryUnavailable("Redis backend is not configured")
        eval_script = getattr(self._redis, "eval", None)
        if not callable(eval_script):
            raise SessionDirectoryUnavailable("Redis backend lacks atomic route replacement")
        seconds = max(1, math.ceil((replacement.expires_at - self._timestamp()).total_seconds()))
        replacement_dict = replacement.as_dict()
        if replacement.generation == current.generation + 1:
            # Preserve the operation intent in the JSON argument so the Lua
            # CAS can distinguish an intentional generation bump from a
            # stale retry of a renew/reconnect replacement.
            replacement_dict["__require_generation_advance"] = True
        try:
            result = int(
                await eval_script(
                    self._REPLACE_SCRIPT,
                    1,
                    self._key(current.session_id),
                    current.stream_epoch,
                    current.generation,
                    json.dumps(replacement_dict),
                    seconds,
                )
            )
        except Exception as exc:
            raise SessionDirectoryUnavailable("session directory atomic update failed") from exc
        if result == -1:
            raise SessionNotFound(current.session_id)
        if result == -2:
            raise SessionEpochConflict(current.session_id)
        if result == -3:
            raise SessionDraining(current.session_id)
        if result == -4:
            raise SessionEpochConflict(current.session_id)
        if result == -5:
            raise SessionEpochConflict(current.session_id)
        if result != 1:
            raise SessionDirectoryError("unexpected session directory CAS result")
        return replacement

    async def lookup(self, session_id: str) -> SessionRoute | None:
        if self._redis is not None:
            return await self._redis_get(session_id)
        async with self._lock:
            route = self._routes.get(session_id)
            if route is not None and route.is_expired(self._timestamp()):
                self._routes.pop(session_id, None)
                return None
            return route

    async def reconnect(
        self,
        session_id: str,
        *,
        expected_stream_epoch: int | None = None,
        media_edge_id: str | None = None,
        voice_core_id: str | None = None,
        device_id: str | None = None,
        ttl_s: int | None = None,
    ) -> SessionRoute:
        current = await self.lookup(session_id)
        if current is None:
            raise SessionNotFound(session_id)
        if current.state == "draining":
            raise SessionDraining(session_id)
        if (
            expected_stream_epoch is not None
            and current.stream_epoch != expected_stream_epoch
        ):
            raise SessionEpochConflict(session_id)
        replacement = self._route(
            session_id=session_id,
            media_edge_id=media_edge_id or current.media_edge_id,
            voice_core_id=voice_core_id or current.voice_core_id,
            device_id=device_id or current.device_id,
            account_id=current.account_id,
            stream_epoch=current.stream_epoch + 1,
            generation=current.generation,
            ttl_s=ttl_s,
            media_runtime=current.media_runtime,
        )
        if self._redis is not None:
            return await self._redis_replace(current, replacement)
        async with self._lock:
            latest = self._routes.get(session_id)
            if latest is None or latest.is_expired(self._timestamp()):
                self._routes.pop(session_id, None)
                raise SessionNotFound(session_id)
            if (
                latest.stream_epoch != current.stream_epoch
                or latest.generation != current.generation
            ):
                raise SessionEpochConflict(session_id)
            if latest.state == "draining":
                raise SessionDraining(session_id)
            self._routes[session_id] = replacement
        return replacement

    async def fallback_to_livekit(
        self,
        session_id: str,
        *,
        expected_stream_epoch: int | None = None,
        ttl_s: int | None = None,
    ) -> SessionRoute:
        """Atomically move a failed experimental route back to LiveKit.

        The stream epoch is intentionally preserved: this is a control-plane
        runtime transition, not a new media connection.  A stale browser may
        not silently change the route that a newer reconnect already owns.
        """

        current = await self.lookup(session_id)
        if current is None:
            raise SessionNotFound(session_id)
        if current.state == "draining":
            raise SessionDraining(session_id)
        if (
            expected_stream_epoch is not None
            and current.stream_epoch != expected_stream_epoch
        ):
            raise SessionEpochConflict(session_id)
        if current.media_runtime == "livekit":
            return current
        lifetime = ttl_s
        if lifetime is None:
            lifetime = max(
                1,
                math.ceil((current.expires_at - self._timestamp()).total_seconds()),
            )
        replacement = self._route(
            session_id=current.session_id,
            media_edge_id=current.media_edge_id,
            voice_core_id=current.voice_core_id,
            stream_epoch=current.stream_epoch,
            generation=current.generation,
            device_id=current.device_id,
            account_id=current.account_id,
            ttl_s=lifetime,
            media_runtime="livekit",
        )
        if self._redis is not None:
            return await self._redis_replace(current, replacement)
        async with self._lock:
            latest = self._routes.get(session_id)
            if latest is None or latest.is_expired(self._timestamp()):
                self._routes.pop(session_id, None)
                raise SessionNotFound(session_id)
            if (
                latest.stream_epoch != current.stream_epoch
                or latest.generation != current.generation
            ):
                raise SessionEpochConflict(session_id)
            if latest.state == "draining":
                raise SessionDraining(session_id)
            self._routes[session_id] = replacement
        return replacement

    async def advance_generation(
        self,
        session_id: str,
        *,
        expected_stream_epoch: int | None = None,
        ttl_s: int | None = None,
    ) -> SessionRoute:
        """Advance the authoritative cancellation generation with a CAS fence."""

        current = await self.lookup(session_id)
        if current is None:
            raise SessionNotFound(session_id)
        if current.state == "draining":
            raise SessionDraining(session_id)
        if (
            expected_stream_epoch is not None
            and current.stream_epoch != expected_stream_epoch
        ):
            raise SessionEpochConflict(session_id)
        lifetime = ttl_s
        if lifetime is None:
            lifetime = max(
                1,
                math.ceil((current.expires_at - self._timestamp()).total_seconds()),
            )
        replacement = self._route(
            session_id=current.session_id,
            media_edge_id=current.media_edge_id,
            voice_core_id=current.voice_core_id,
            stream_epoch=current.stream_epoch,
            generation=current.generation + 1,
            device_id=current.device_id,
            account_id=current.account_id,
            ttl_s=lifetime,
            media_runtime=current.media_runtime,
        )
        if self._redis is not None:
            return await self._redis_replace(current, replacement)
        async with self._lock:
            latest = self._routes.get(session_id)
            if latest is None or latest.is_expired(self._timestamp()):
                self._routes.pop(session_id, None)
                raise SessionNotFound(session_id)
            if (
                latest.stream_epoch != current.stream_epoch
                or latest.generation != current.generation
            ):
                raise SessionEpochConflict(session_id)
            if latest.state == "draining":
                raise SessionDraining(session_id)
            self._routes[session_id] = replacement
        return replacement

    async def renew(
        self,
        session_id: str,
        *,
        expected_stream_epoch: int | None = None,
        ttl_s: int | None = None,
    ) -> SessionRoute:
        """Extend TTL while preserving the owner and exact stream epoch."""

        current = await self.lookup(session_id)
        if current is None:
            raise SessionNotFound(session_id)
        if current.state == "draining":
            raise SessionDraining(session_id)
        if (
            expected_stream_epoch is not None
            and current.stream_epoch != expected_stream_epoch
        ):
            raise SessionEpochConflict(session_id)
        replacement = self._route(
            session_id=session_id,
            media_edge_id=current.media_edge_id,
            voice_core_id=current.voice_core_id,
            device_id=current.device_id,
            account_id=current.account_id,
            stream_epoch=current.stream_epoch,
            generation=current.generation,
            ttl_s=ttl_s,
            media_runtime=current.media_runtime,
        )
        if self._redis is not None:
            return await self._redis_replace(current, replacement)
        async with self._lock:
            latest = self._routes.get(session_id)
            if latest is None or latest.is_expired(self._timestamp()):
                self._routes.pop(session_id, None)
                raise SessionNotFound(session_id)
            if (
                latest.stream_epoch != current.stream_epoch
                or latest.generation != current.generation
            ):
                raise SessionEpochConflict(session_id)
            if latest.state == "draining":
                raise SessionDraining(session_id)
            self._routes[session_id] = replacement
        return replacement

    async def drain(self, session_id: str) -> SessionRoute | None:
        current = await self.lookup(session_id)
        if current is None:
            return None
        if current.state == "draining":
            return current
        route = SessionRoute(
            session_id=current.session_id,
            media_edge_id=current.media_edge_id,
            voice_core_id=current.voice_core_id,
            stream_epoch=current.stream_epoch,
            generation=current.generation,
            device_id=current.device_id,
            account_id=current.account_id,
            expires_at=current.expires_at,
            media_runtime=current.media_runtime,
            state="draining",
        )
        if self._redis is not None:
            return await self._redis_replace(current, route)
        else:
            async with self._lock:
                latest = self._routes.get(session_id)
                if latest is None or latest.is_expired(self._timestamp()):
                    self._routes.pop(session_id, None)
                    raise SessionNotFound(session_id)
                if (
                    latest.stream_epoch != current.stream_epoch
                    or latest.generation != current.generation
                ):
                    raise SessionEpochConflict(session_id)
                if latest.state == "draining":
                    return latest
                self._routes[session_id] = route
        return route

    async def expire(self, session_id: str) -> bool:
        if self._redis is not None:
            try:
                return bool(await self._redis.delete(self._key(session_id)))
            except Exception as exc:
                raise SessionDirectoryUnavailable("session directory expire failed") from exc
        async with self._lock:
            return self._routes.pop(session_id, None) is not None

    async def close(self) -> None:
        """Close a Redis pool; the in-memory implementation is a no-op."""

        if self._redis is None:
            return
        closer = getattr(self._redis, "aclose", None) or getattr(self._redis, "close", None)
        if closer is None:
            return
        result = closer()
        if hasattr(result, "__await__"):
            await result


class InMemorySessionDirectory(SessionDirectory):
    """Explicit name for tests and single-process deployments."""

    def __init__(self, *, ttl_s: int = 300, now: Clock | None = None) -> None:
        super().__init__(ttl_s=ttl_s, now=now)


class RedisSessionDirectory(SessionDirectory):
    """Explicit Redis-backed directory; never falls back to local memory."""

    def __init__(
        self,
        redis_url: str,
        *,
        ttl_s: int = 300,
        redis_client: Any | None = None,
        now: Clock | None = None,
    ) -> None:
        super().__init__(
            ttl_s=ttl_s,
            redis_url=redis_url if redis_client is None else None,
            redis_client=redis_client,
            now=now,
        )
