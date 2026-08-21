"""Fail-closed reporter for the Control API media SLO gate."""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)

SLOSnapshotProvider = Callable[[], Mapping[str, float] | Awaitable[Mapping[str, float]]]

_PROMETHEUS_LINE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[-+0-9.eE]+)$"
)
_PROMETHEUS_LABEL = re.compile(r'(?P<key>[a-zA-Z_][a-zA-Z0-9_]*)="(?P<value>(?:\\.|[^"])*)"')


@dataclass(frozen=True, slots=True)
class MediaSLOReporterConfig:
    endpoint: str
    token: str
    source: str
    interval_s: float = 30.0
    timeout_s: float = 2.0

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            raise ValueError("media SLO reporter endpoint must be an HTTP(S) URL")
        if parsed.scheme == "http" and parsed.hostname not in {
            "control-api",
            "agent",
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("plaintext media SLO reporter endpoint must be local control API")
        if not self.token.strip() or len(self.token) < 32:
            raise ValueError("media SLO reporter token must contain at least 32 characters")
        if not self.source.strip() or len(self.source) > 64:
            raise ValueError("media SLO reporter source must be a short non-empty string")
        if not math.isfinite(self.interval_s) or self.interval_s < 5.0:
            raise ValueError("media SLO reporter interval must be at least five seconds")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("media SLO reporter timeout must be positive")


class MediaSLOReporter:
    """Post only allowlisted aggregate metrics; never send audio or text."""

    def __init__(
        self,
        config: MediaSLOReporterConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config
        self._client = client
        self._owns_client = client is None

    async def report(self, metrics: Mapping[str, float]) -> None:
        payload = _clean_metrics(metrics)
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=self.config.timeout_s)
            self._client = client
        response = await client.post(
            self.config.endpoint,
            headers={
                "X-Media-SLO-Token": self.config.token,
                "Content-Type": "application/json",
            },
            json={"source": self.config.source, "metrics": payload},
        )
        response.raise_for_status()

    async def run(
        self,
        snapshot_provider: SLOSnapshotProvider,
        stop_event: asyncio.Event,
    ) -> None:
        """Report periodically until stopped; transient failure is fail-closed."""

        while not stop_event.is_set():
            try:
                snapshot = snapshot_provider()
                if hasattr(snapshot, "__await__"):
                    snapshot = await snapshot
                await self.report(snapshot)
            except (httpx.HTTPError, OSError, TypeError, ValueError) as exc:
                # The Control API TTL gate will roll back when reports stop;
                # this loop must not crash the audio process.
                logger.warning("media SLO report failed: %s", type(exc).__name__)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.config.interval_s)
            except TimeoutError:
                continue

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


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


def parse_prometheus_slo_snapshot(payload: str) -> dict[str, float]:
    """Extract only media SLO fields from an agent Prometheus text endpoint."""

    values: dict[str, float] = {}
    for raw_line in payload.splitlines():
        match = _PROMETHEUS_LINE.match(raw_line.strip())
        if match is None:
            continue
        name = match.group("name")
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        labels: dict[str, str] = {}
        for label in _PROMETHEUS_LABEL.finditer(match.group("labels") or ""):
            labels[label.group("key")] = label.group("value").replace('\\"', '"')
        if name == "stale_result_dropped_total":
            source = labels.get("source")
            if source == "media_generation":
                values["stale_generation_total"] = value
            elif source == "asr_final":
                values["stale_asr_final_total"] = value
        elif name == "voice_latency_seconds":
            if labels.get("quantile") != "p95":
                continue
            stage = labels.get("stage")
            if stage == "first_audio":
                values["first_audio_p95_ms"] = value * 1000.0
            elif stage == "tts_first_frame":
                values["tts_first_frame_p95_ms"] = value * 1000.0
            elif stage == "interrupt_stop":
                values["interrupt_stop_p95_ms"] = value * 1000.0
        elif name == "media_sessions_total":
            values["_media_sessions_total"] = value
        elif name == "media_sessions_failed_total":
            values["_media_sessions_failed_total"] = value
    sessions = values.pop("_media_sessions_total", 0.0)
    failures = values.pop("_media_sessions_failed_total", 0.0)
    if sessions > 0:
        values["session_failure_rate"] = min(1.0, max(0.0, failures / sessions))
    return values


__all__ = [
    "MediaSLOReporter",
    "MediaSLOReporterConfig",
    "parse_prometheus_slo_snapshot",
    "SLOSnapshotProvider",
]
