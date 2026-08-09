"""Time-bounded, consent-bound storage for an authorized child speech corpus."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from services.archive.object_store import ObjectRef, ObjectStore

logger = logging.getLogger(__name__)
MAX_CORPUS_RETENTION = timedelta(days=30)
MAX_ACTIVE_CORPUS_SAMPLES_PER_MINOR = 20


class CorpusConsentInactiveError(RuntimeError):
    """The corpus grant was revoked, expired, or detached during persistence."""


class CorpusSampleLimitError(RuntimeError):
    """The minor already has the maximum number of active corpus samples."""


def _utc(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CorpusSample:
    sample_id: str
    minor_user_id: str
    consent_id: str
    source_event_id: str
    reference: ObjectRef
    created_at: datetime
    expires_at: datetime
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        for field in ("sample_id", "minor_user_id", "consent_id", "source_event_id"):
            value = str(getattr(self, field)).strip()
            if not value or len(value) > 128:
                raise ValueError(f"{field} must be a bounded non-empty string")
            object.__setattr__(self, field, value)
        if self.reference.account_id != self.minor_user_id:
            raise ValueError("corpus object must belong to the minor account")
        object.__setattr__(self, "created_at", _utc(self.created_at, field="created_at"))
        object.__setattr__(self, "expires_at", _utc(self.expires_at, field="expires_at"))
        if not self.created_at < self.expires_at <= self.created_at + MAX_CORPUS_RETENTION:
            raise ValueError("corpus sample retention must be between zero and 30 days")
        if self.deleted_at is not None:
            object.__setattr__(self, "deleted_at", _utc(self.deleted_at, field="deleted_at"))


class CorpusSampleStorePort(Protocol):
    async def corpus_sample_by_event(
        self,
        *,
        minor_user_id: str,
        source_event_id: str,
    ) -> CorpusSample | None: ...

    async def record_corpus_sample(self, sample: CorpusSample) -> CorpusSample: ...

    async def corpus_samples(
        self,
        *,
        minor_user_id: str,
        include_deleted: bool = False,
    ) -> tuple[CorpusSample, ...]: ...

    async def expired_corpus_samples(
        self,
        *,
        now: datetime,
        limit: int = 100,
    ) -> tuple[CorpusSample, ...]: ...

    async def mark_corpus_sample_deleted(
        self,
        *,
        sample_id: str,
        deleted_at: datetime,
    ) -> CorpusSample: ...


class CorpusRetentionService:
    def __init__(self, store: CorpusSampleStorePort, objects: ObjectStore) -> None:
        self._store = store
        self._objects = objects

    async def purge_expired(self, *, now: datetime | None = None, limit: int = 100) -> int:
        timestamp = _utc(now or datetime.now(UTC), field="now")
        samples = await self._store.expired_corpus_samples(now=timestamp, limit=limit)
        for sample in samples:
            await self._objects.delete(sample.reference)
            await self._store.mark_corpus_sample_deleted(
                sample_id=sample.sample_id,
                deleted_at=timestamp,
            )
        return len(samples)

    async def purge_minor(self, *, minor_user_id: str, now: datetime | None = None) -> int:
        timestamp = _utc(now or datetime.now(UTC), field="now")
        samples = await self._store.corpus_samples(
            minor_user_id=minor_user_id,
            include_deleted=False,
        )
        for sample in samples:
            await self._objects.delete(sample.reference)
            await self._store.mark_corpus_sample_deleted(
                sample_id=sample.sample_id,
                deleted_at=timestamp,
            )
        return len(samples)


class CorpusRetentionWorker:
    def __init__(self, service: CorpusRetentionService, *, interval_s: float = 300.0) -> None:
        if interval_s <= 0:
            raise ValueError("corpus retention interval must be positive")
        self._service = service
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="guardian-corpus-retention")

    async def stop(self) -> None:
        self._closed.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        while not self._closed.is_set():
            try:
                await self._service.purge_expired()
            except Exception:
                logger.exception("authorized child corpus purge failed")
            try:
                await asyncio.wait_for(self._closed.wait(), timeout=self._interval_s)
            except TimeoutError:
                continue


__all__ = [
    "CorpusConsentInactiveError",
    "CorpusRetentionService",
    "CorpusRetentionWorker",
    "CorpusSample",
    "CorpusSampleLimitError",
    "CorpusSampleStorePort",
    "MAX_CORPUS_RETENTION",
    "MAX_ACTIVE_CORPUS_SAMPLES_PER_MINOR",
]
