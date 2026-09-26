"""Process-local response-plan cache (moved out of routes/interaction.py).

A cached plan can hold private persona and memory context for one subject, so a
subject's deletion must drop every plan cached for that subject.
"""

from __future__ import annotations

import asyncio
import copy
import hmac
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request

_RESPONSE_PLAN_CACHE_MAX_ENTRIES = 256
#: (session_id, turn_id, generation_id, tool_epoch, evolution protocol, subject_id)
ResponsePlanCacheKey = tuple[str, int, int, int, str, str]
_ResponsePlanCacheKey = ResponsePlanCacheKey


@dataclass(frozen=True)
class _CachedResponsePlan:
    fingerprint: str
    payload: dict[str, Any]


@dataclass
class ResponsePlanCache:
    """Process-local first-write-wins snapshot keyed by the complete generation fence."""

    max_entries: int = _RESPONSE_PLAN_CACHE_MAX_ENTRIES
    _entries: OrderedDict[_ResponsePlanCacheKey, _CachedResponsePlan] = field(
        default_factory=OrderedDict
    )
    _key_locks: dict[_ResponsePlanCacheKey, asyncio.Lock] = field(default_factory=dict)
    _guard: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def lock_for(self, key: _ResponsePlanCacheKey) -> asyncio.Lock:
        async with self._guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._key_locks[key] = lock
            if len(self._key_locks) > self.max_entries * 2:
                for candidate in tuple(self._key_locks):
                    if candidate in self._entries:
                        continue
                    candidate_lock = self._key_locks[candidate]
                    if candidate_lock.locked():
                        continue
                    self._key_locks.pop(candidate, None)
                    if len(self._key_locks) <= self.max_entries:
                        break
            return lock

    async def get(
        self,
        key: _ResponsePlanCacheKey,
        fingerprint: str,
    ) -> dict[str, Any] | None:
        async with self._guard:
            cached = self._entries.get(key)
            if cached is None:
                return None
            if not hmac.compare_digest(cached.fingerprint, fingerprint):
                raise HTTPException(
                    status_code=409,
                    detail={"code": "response_plan_conflict"},
                )
            self._entries.move_to_end(key)
            return copy.deepcopy(cached.payload)

    async def invalidate(self, key: _ResponsePlanCacheKey) -> None:
        """Drop one entry so a withdrawn plan stops being served and retained.

        A cached payload can contain private persona/memory context.  Once the
        read path reports that the session's authorization is gone, the entry is
        not merely unusable, it is private data the process has no reason to keep
        holding.
        """

        async with self._guard:
            self._entries.pop(key, None)

    async def invalidate_subject(self, subject_id: str) -> int:
        """Drop every plan cached for one subject (their data is being erased)."""

        async with self._guard:
            doomed = [key for key in self._entries if key[5] == subject_id]
            for key in doomed:
                self._entries.pop(key, None)
            return len(doomed)

    async def put(
        self,
        key: _ResponsePlanCacheKey,
        fingerprint: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._guard:
            cached = self._entries.get(key)
            if cached is not None:
                if not hmac.compare_digest(cached.fingerprint, fingerprint):
                    raise HTTPException(
                        status_code=409,
                        detail={"code": "response_plan_conflict"},
                    )
                self._entries.move_to_end(key)
                return copy.deepcopy(cached.payload)
            snapshot = copy.deepcopy(payload)
            self._entries[key] = _CachedResponsePlan(
                fingerprint=fingerprint,
                payload=snapshot,
            )
            while len(self._entries) > self.max_entries:
                evicted_key, _ = self._entries.popitem(last=False)
                evicted_lock = self._key_locks.get(evicted_key)
                if evicted_lock is not None and not evicted_lock.locked():
                    self._key_locks.pop(evicted_key, None)
            return copy.deepcopy(snapshot)


def response_plan_cache(request: Request) -> ResponsePlanCache:
    cache = getattr(request.app.state, "response_plan_cache", None)
    if not isinstance(cache, ResponsePlanCache):
        cache = ResponsePlanCache()
        request.app.state.response_plan_cache = cache
    return cache


async def forget_subject_plans(state: Any, subject_id: str) -> int:
    """Drop a deleted subject's cached plans from this process, if any exist."""

    cache = getattr(state, "response_plan_cache", None)
    if not isinstance(cache, ResponsePlanCache):
        return 0
    return await cache.invalidate_subject(subject_id)
