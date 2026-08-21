"""TTL-backed media SLO snapshot used by server-owned runtime selection."""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from services.agent.src.voice_core.slo import SLOReport, evaluate_slo


class MediaSLOUnavailable(RuntimeError):
    """The shared SLO store cannot be reached."""


@dataclass(frozen=True, slots=True)
class MediaSLOSnapshot:
    metrics: dict[str, float]
    source: str
    observed_at: datetime
    expires_at: datetime
    report: SLOReport


class MediaSLOGate:
    """Keep the latest aggregate report in memory or Redis with a hard TTL."""

    _KEY = "memoria:media-runtime:slo"

    def __init__(
        self,
        *,
        ttl_s: int = 120,
        redis_url: str | None = None,
        redis_client: Any | None = None,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("SLO snapshot ttl must be positive")
        if redis_url and redis_client is not None:
            raise ValueError("configure redis_url or redis_client, not both")
        self.ttl_s = ttl_s
        self._redis = redis_client
        self._lock = asyncio.Lock()
        self._snapshot: MediaSLOSnapshot | None = None
        if redis_url:
            try:
                from redis import asyncio as redis_asyncio
            except ImportError as exc:  # pragma: no cover - optional deployment import
                raise MediaSLOUnavailable("redis.asyncio is required for shared SLO snapshots") from exc
            self._redis = redis_asyncio.from_url(  # type: ignore[no-untyped-call]
                redis_url,
                decode_responses=True,
            )

    async def publish(
        self,
        metrics: Mapping[str, float],
        *,
        source: str,
        ttl_s: int | None = None,
        observed_at: datetime | None = None,
    ) -> MediaSLOSnapshot:
        clean = _clean_metrics(metrics)
        report = evaluate_slo(clean)
        now = _utc(observed_at or datetime.now(UTC))
        lifetime = self.ttl_s if ttl_s is None else ttl_s
        if lifetime <= 0:
            raise ValueError("SLO snapshot ttl must be positive")
        expires_at = now + timedelta(seconds=lifetime)
        snapshot = MediaSLOSnapshot(
            metrics=clean,
            source=_source(source),
            observed_at=now,
            expires_at=expires_at,
            report=report,
        )
        if self._redis is not None:
            try:
                await self._redis.set(
                    self._KEY,
                    json.dumps(_snapshot_dict(snapshot), separators=(",", ":")),
                    ex=max(1, math.ceil((expires_at - datetime.now(UTC)).total_seconds())),
                )
            except Exception as exc:
                raise MediaSLOUnavailable("media SLO snapshot publish failed") from exc
        else:
            async with self._lock:
                self._snapshot = snapshot
        return snapshot

    async def current(self, *, now: datetime | None = None) -> MediaSLOSnapshot | None:
        current = _utc(now or datetime.now(UTC))
        if self._redis is not None:
            try:
                raw = await self._redis.get(self._KEY)
            except Exception as exc:
                raise MediaSLOUnavailable("media SLO snapshot lookup failed") from exc
            if raw is None:
                return None
            try:
                snapshot = _snapshot_from_dict(json.loads(raw))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise MediaSLOUnavailable("media SLO snapshot is invalid") from exc
            if snapshot.expires_at <= current:
                return None
            return snapshot
        async with self._lock:
            if self._snapshot is None or self._snapshot.expires_at <= current:
                self._snapshot = None
                return None
            return self._snapshot

    async def close(self) -> None:
        if self._redis is None:
            return
        closer = getattr(self._redis, "aclose", None) or getattr(self._redis, "close", None)
        if closer is not None:
            result = closer()
            if hasattr(result, "__await__"):
                await result


def _clean_metrics(metrics: Mapping[str, float]) -> dict[str, float]:
    allowed = {
        "first_audio_p95_ms",
        "tts_first_frame_p95_ms",
        "interrupt_stop_p95_ms",
        "session_failure_rate",
        "stale_generation_total",
        "stale_asr_final_total",
    }
    if not metrics or any(key not in allowed for key in metrics):
        raise ValueError("media SLO metrics are not allowlisted")
    clean: dict[str, float] = {}
    for key, value in metrics.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("media SLO metric must be finite")
        if value < 0:
            raise ValueError("media SLO metric must be non-negative")
        if key in {"stale_generation_total", "stale_asr_final_total"} and not float(value).is_integer():
            raise ValueError("stale media SLO metrics must be integers")
        clean[key] = float(value)
    return clean


def _source(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 64:
        raise ValueError("SLO source must be a short non-empty string")
    return value.strip()


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _snapshot_dict(snapshot: MediaSLOSnapshot) -> dict[str, Any]:
    return {
        "metrics": snapshot.metrics,
        "source": snapshot.source,
        "observed_at": snapshot.observed_at.isoformat(),
        "expires_at": snapshot.expires_at.isoformat(),
    }


def _snapshot_from_dict(value: object) -> MediaSLOSnapshot:
    if not isinstance(value, dict):
        raise ValueError("SLO snapshot must be an object")
    observed_at = _utc(datetime.fromisoformat(str(value["observed_at"])))
    expires_at = _utc(datetime.fromisoformat(str(value["expires_at"])))
    metrics = _clean_metrics(value["metrics"])
    return MediaSLOSnapshot(
        metrics=metrics,
        source=_source(str(value["source"])),
        observed_at=observed_at,
        expires_at=expires_at,
        report=evaluate_slo(metrics),
    )


__all__ = ["MediaSLOSnapshot", "MediaSLOUnavailable", "MediaSLOGate"]
