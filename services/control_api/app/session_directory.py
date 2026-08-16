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


class SessionOwnershipConflict(SessionEpochConflict):
    """The caller no longer owns the route fencing lease."""


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
    owner_instance_id: str
    ownership_epoch: int
    media_runtime: Literal["livekit", "streamcore", "direct_voice_core"] = "livekit"
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

    @property
    def lease_expires_at(self) -> datetime:
        """Explicit ownership-lease spelling for shared-directory consumers."""

        return self.expires_at

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at <= now

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["expires_at"] = self.expires_at.isoformat()
        value["lease_expires_at"] = value["expires_at"]
        value["generation_id"] = self.generation
        return value


def _coerce_media_runtime(value: object) -> Literal["livekit", "streamcore", "direct_voice_core"]:
    if value == "direct_voice_core":
        return "direct_voice_core"
    return "streamcore" if value == "streamcore" else "livekit"


Clock = Callable[[], datetime]


class SessionDirectory:
    """A TTL session directory with an optional Redis backend.

    With no ``redis_url`` or ``redis_client`` this is an in-memory directory,
    useful for a single control-api process and tests.  Once a Redis backend is
    configured, failures raise :class:`SessionDirectoryUnavailable`; the class
    never silently falls back to local state.
    """

    _KEY_PREFIX = "memoria:session-directory:"
    _OWNERSHIP_COUNTER_PREFIX = "memoria:session-directory-ownership:"
    _REPLACE_SCRIPT = """
local raw = redis.call('GET', KEYS[1])
if not raw then return -1 end
local current = cjson.decode(raw)
if current.state == 'draining' then return -3 end
if tonumber(current.stream_epoch) ~= tonumber(ARGV[1]) then return -2 end
if tonumber(current.generation) ~= tonumber(ARGV[2]) then return -5 end
local current_owner = current.owner_instance_id or current.media_edge_id
local current_ownership_epoch = tonumber(current.ownership_epoch or 1)
if current_owner ~= ARGV[3] then return -6 end
if current_ownership_epoch ~= tonumber(ARGV[4]) then return -7 end
local replacement = cjson.decode(ARGV[5])
if replacement.__require_generation_advance == true
   and tonumber(current.generation) + 1 ~= tonumber(replacement.generation) then
  return -4
end
redis.call('SET', KEYS[1], ARGV[5], 'EX', ARGV[6])
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
        self._ownership_epochs: dict[str, int] = {}
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
        owner_instance_id: str,
        ownership_epoch: int,
        media_runtime: Literal["livekit", "streamcore", "direct_voice_core"] = "livekit",
        state: Literal["active", "draining"] = "active",
    ) -> SessionRoute:
        self._validate_ids(
            session_id=session_id,
            media_edge_id=media_edge_id,
            voice_core_id=voice_core_id,
        )
        if not device_id.strip() or not account_id.strip():
            raise ValueError("device_id and account_id are required")
        if not owner_instance_id.strip():
            raise ValueError("owner_instance_id is required")
        if stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if generation < 0:
            raise ValueError("generation must be non-negative")
        if ownership_epoch < 1:
            raise ValueError("ownership_epoch must be positive")
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
            owner_instance_id=owner_instance_id,
            ownership_epoch=ownership_epoch,
            media_runtime=media_runtime,
            state=state,
        )

    @classmethod
    def _key(cls, session_id: str) -> str:
        return f"{cls._KEY_PREFIX}{session_id}"

    @classmethod
    def _ownership_key(cls, session_id: str) -> str:
        return f"{cls._OWNERSHIP_COUNTER_PREFIX}{session_id}"

    @staticmethod
    def _from_dict(value: dict[str, Any]) -> SessionRoute:
        try:
            expires_raw = value.get("expires_at") or value["lease_expires_at"]
            expires_at = datetime.fromisoformat(str(expires_raw))
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
                owner_instance_id=str(value.get("owner_instance_id") or value["media_edge_id"]),
                ownership_epoch=max(1, int(value.get("ownership_epoch", 1))),
                media_runtime=_coerce_media_runtime(value.get("media_runtime")),
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
        media_runtime: Literal["livekit", "streamcore", "direct_voice_core"] = "livekit",
        owner_instance_id: str | None = None,
    ) -> SessionRoute:
        owner = (owner_instance_id or media_edge_id).strip()
        if self._redis is not None:
            try:
                existing = await self._redis_get(session_id)
                if existing is not None:
                    requested = self._route(
                        session_id=session_id,
                        media_edge_id=media_edge_id,
                        voice_core_id=voice_core_id,
                        stream_epoch=stream_epoch,
                        generation=generation,
                        device_id=device_id,
                        account_id=account_id,
                        ttl_s=ttl_s,
                        owner_instance_id=owner,
                        ownership_epoch=existing.ownership_epoch,
                        media_runtime=media_runtime,
                    )
                    if self._same_claim(existing, requested):
                        return existing
                    raise SessionEpochConflict(session_id)
                incr = getattr(self._redis, "incr", None)
                if not callable(incr):
                    raise SessionDirectoryUnavailable(
                        "Redis backend lacks ownership fencing counter"
                    )
                ownership_epoch = int(await incr(self._ownership_key(session_id)))
                route = self._route(
                    session_id=session_id,
                    media_edge_id=media_edge_id,
                    voice_core_id=voice_core_id,
                    stream_epoch=stream_epoch,
                    generation=generation,
                    device_id=device_id,
                    account_id=account_id,
                    ttl_s=ttl_s,
                    owner_instance_id=owner,
                    ownership_epoch=ownership_epoch,
                    media_runtime=media_runtime,
                )
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
                requested = self._route(
                    session_id=session_id,
                    media_edge_id=media_edge_id,
                    voice_core_id=voice_core_id,
                    stream_epoch=stream_epoch,
                    generation=generation,
                    device_id=device_id,
                    account_id=account_id,
                    ttl_s=ttl_s,
                    owner_instance_id=owner,
                    ownership_epoch=existing.ownership_epoch,
                    media_runtime=media_runtime,
                )
                if self._same_claim(existing, requested):
                    return existing
                raise SessionEpochConflict(session_id)
            ownership_epoch = self._ownership_epochs.get(session_id, 0) + 1
            self._ownership_epochs[session_id] = ownership_epoch
            route = self._route(
                session_id=session_id,
                media_edge_id=media_edge_id,
                voice_core_id=voice_core_id,
                stream_epoch=stream_epoch,
                generation=generation,
                device_id=device_id,
                account_id=account_id,
                ttl_s=ttl_s,
                owner_instance_id=owner,
                ownership_epoch=ownership_epoch,
                media_runtime=media_runtime,
            )
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
            and existing.owner_instance_id == requested.owner_instance_id
            and existing.media_runtime == requested.media_runtime
            and existing.state == requested.state == "active"
        )

    @staticmethod
    def _owner_fence(
        current: SessionRoute,
        *,
        owner_instance_id: str | None,
        expected_ownership_epoch: int | None,
    ) -> tuple[str, int]:
        owner = (owner_instance_id or current.owner_instance_id).strip()
        if owner != current.owner_instance_id:
            raise SessionOwnershipConflict(current.session_id)
        epoch = (
            current.ownership_epoch
            if expected_ownership_epoch is None
            else expected_ownership_epoch
        )
        if epoch != current.ownership_epoch:
            raise SessionOwnershipConflict(current.session_id)
        return owner, epoch

    async def _replace_local(
        self,
        current: SessionRoute,
        replacement: SessionRoute,
        *,
        owner_instance_id: str | None,
        expected_ownership_epoch: int | None,
        allow_draining: bool = False,
    ) -> SessionRoute:
        owner, ownership_epoch = self._owner_fence(
            current,
            owner_instance_id=owner_instance_id,
            expected_ownership_epoch=expected_ownership_epoch,
        )
        async with self._lock:
            latest = self._routes.get(current.session_id)
            if latest is None or latest.is_expired(self._timestamp()):
                self._routes.pop(current.session_id, None)
                raise SessionNotFound(current.session_id)
            if (
                latest.stream_epoch != current.stream_epoch
                or latest.generation != current.generation
                or latest.owner_instance_id != owner
                or latest.ownership_epoch != ownership_epoch
            ):
                raise SessionEpochConflict(current.session_id)
            if latest.state == "draining":
                if allow_draining:
                    return latest
                raise SessionDraining(current.session_id)
            self._routes[current.session_id] = replacement
        return replacement

    async def _redis_replace(
        self,
        current: SessionRoute,
        replacement: SessionRoute,
        *,
        owner_instance_id: str | None = None,
        expected_ownership_epoch: int | None = None,
    ) -> SessionRoute:
        """Atomically replace one route using its stream epoch as a CAS fence."""

        if self._redis is None:  # pragma: no cover - callers guard this branch
            raise SessionDirectoryUnavailable("Redis backend is not configured")
        owner, ownership_epoch = self._owner_fence(
            current,
            owner_instance_id=owner_instance_id,
            expected_ownership_epoch=expected_ownership_epoch,
        )
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
                    owner,
                    ownership_epoch,
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
        if result in {-6, -7}:
            raise SessionOwnershipConflict(current.session_id)
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
        owner_instance_id: str | None = None,
        expected_ownership_epoch: int | None = None,
    ) -> SessionRoute:
        current = await self.lookup(session_id)
        if current is None:
            raise SessionNotFound(session_id)
        if current.state == "draining":
            raise SessionDraining(session_id)
        owner, ownership_epoch = self._owner_fence(
            current,
            owner_instance_id=owner_instance_id,
            expected_ownership_epoch=expected_ownership_epoch,
        )
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
            owner_instance_id=owner,
            ownership_epoch=ownership_epoch,
            media_runtime=current.media_runtime,
        )
        if self._redis is not None:
            return await self._redis_replace(
                current,
                replacement,
                owner_instance_id=owner,
                expected_ownership_epoch=ownership_epoch,
            )
        return await self._replace_local(
            current,
            replacement,
            owner_instance_id=owner,
            expected_ownership_epoch=ownership_epoch,
        )

    async def fallback_to_livekit(
        self,
        session_id: str,
        *,
        expected_stream_epoch: int | None = None,
        ttl_s: int | None = None,
        owner_instance_id: str | None = None,
        expected_ownership_epoch: int | None = None,
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
        owner, ownership_epoch = self._owner_fence(
            current,
            owner_instance_id=owner_instance_id,
            expected_ownership_epoch=expected_ownership_epoch,
        )
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
            owner_instance_id=owner,
            ownership_epoch=ownership_epoch,
            media_runtime="livekit",
        )
        if self._redis is not None:
            return await self._redis_replace(
                current,
                replacement,
                owner_instance_id=owner,
                expected_ownership_epoch=ownership_epoch,
            )
        return await self._replace_local(
            current,
            replacement,
            owner_instance_id=owner,
            expected_ownership_epoch=ownership_epoch,
        )

    async def advance_generation(
        self,
        session_id: str,
        *,
        expected_stream_epoch: int | None = None,
        ttl_s: int | None = None,
        owner_instance_id: str | None = None,
        expected_ownership_epoch: int | None = None,
    ) -> SessionRoute:
        """Advance the authoritative cancellation generation with a CAS fence."""

        current = await self.lookup(session_id)
        if current is None:
            raise SessionNotFound(session_id)
        if current.state == "draining":
            raise SessionDraining(session_id)
        owner, ownership_epoch = self._owner_fence(
            current,
            owner_instance_id=owner_instance_id,
            expected_ownership_epoch=expected_ownership_epoch,
        )
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
            owner_instance_id=owner,
            ownership_epoch=ownership_epoch,
            media_runtime=current.media_runtime,
        )
        if self._redis is not None:
            return await self._redis_replace(
                current,
                replacement,
                owner_instance_id=owner,
                expected_ownership_epoch=ownership_epoch,
            )
        return await self._replace_local(
            current,
            replacement,
            owner_instance_id=owner,
            expected_ownership_epoch=ownership_epoch,
        )

    async def observe_generation(
        self,
        session_id: str,
        *,
        generation: int,
        expected_stream_epoch: int | None = None,
        ttl_s: int | None = None,
        owner_instance_id: str | None = None,
        expected_ownership_epoch: int | None = None,
    ) -> SessionRoute:
        """Record an Edge/Core-authoritative generation without inventing it.

        Normal Voice Core turns do not synchronously pass through the Control
        API, so the directory generation is only a routing observation. HTTP
        stop first obtains the complete fence from Media Edge, then uses this
        monotonic CAS to catch the directory up; it must never pre-advance this
        counter and present it as the media generation authority.
        """

        current = await self.lookup(session_id)
        if current is None:
            raise SessionNotFound(session_id)
        if current.state == "draining":
            raise SessionDraining(session_id)
        owner, ownership_epoch = self._owner_fence(
            current,
            owner_instance_id=owner_instance_id,
            expected_ownership_epoch=expected_ownership_epoch,
        )
        if (
            expected_stream_epoch is not None
            and current.stream_epoch != expected_stream_epoch
        ):
            raise SessionEpochConflict(session_id)
        if generation < current.generation:
            raise SessionEpochConflict(session_id)
        if generation == current.generation:
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
            generation=generation,
            device_id=current.device_id,
            account_id=current.account_id,
            ttl_s=lifetime,
            owner_instance_id=owner,
            ownership_epoch=ownership_epoch,
            media_runtime=current.media_runtime,
        )
        if self._redis is not None:
            return await self._redis_replace(
                current,
                replacement,
                owner_instance_id=owner,
                expected_ownership_epoch=ownership_epoch,
            )
        return await self._replace_local(
            current,
            replacement,
            owner_instance_id=owner,
            expected_ownership_epoch=ownership_epoch,
        )

    async def renew(
        self,
        session_id: str,
        *,
        expected_stream_epoch: int | None = None,
        ttl_s: int | None = None,
        owner_instance_id: str | None = None,
        expected_ownership_epoch: int | None = None,
    ) -> SessionRoute:
        """Extend TTL while preserving the owner and exact stream epoch."""

        current = await self.lookup(session_id)
        if current is None:
            raise SessionNotFound(session_id)
        if current.state == "draining":
            raise SessionDraining(session_id)
        owner, ownership_epoch = self._owner_fence(
            current,
            owner_instance_id=owner_instance_id,
            expected_ownership_epoch=expected_ownership_epoch,
        )
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
            owner_instance_id=owner,
            ownership_epoch=ownership_epoch,
            media_runtime=current.media_runtime,
        )
        if self._redis is not None:
            return await self._redis_replace(
                current,
                replacement,
                owner_instance_id=owner,
                expected_ownership_epoch=ownership_epoch,
            )
        return await self._replace_local(
            current,
            replacement,
            owner_instance_id=owner,
            expected_ownership_epoch=ownership_epoch,
        )

    async def drain(
        self,
        session_id: str,
        *,
        owner_instance_id: str | None = None,
        expected_ownership_epoch: int | None = None,
    ) -> SessionRoute | None:
        current = await self.lookup(session_id)
        if current is None:
            return None
        owner, ownership_epoch = self._owner_fence(
            current,
            owner_instance_id=owner_instance_id,
            expected_ownership_epoch=expected_ownership_epoch,
        )
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
            owner_instance_id=owner,
            ownership_epoch=ownership_epoch,
            media_runtime=current.media_runtime,
            state="draining",
        )
        if self._redis is not None:
            return await self._redis_replace(
                current,
                route,
                owner_instance_id=owner,
                expected_ownership_epoch=ownership_epoch,
            )
        return await self._replace_local(
            current,
            route,
            owner_instance_id=owner,
            expected_ownership_epoch=ownership_epoch,
            allow_draining=True,
        )

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
