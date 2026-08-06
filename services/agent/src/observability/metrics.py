"""Thread-safe metrics registry with a real Prometheus collector/exporter."""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client import start_http_server as prometheus_start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

from services.agent.src.voice_core.telemetry import MEDIA_METRIC_NAMES

_GAUGES = {
    "tool_tasks_active",
    "tts_pool_available",
    "provider_ws_active",
    "voice_latency_seconds",
    "voice_sessions_active",
    "media_active_sessions",
    "media_pcm_queue_depth",
    "media_rtp_jitter_ms",
    "context_snapshot_build_seconds",
    "context_snapshot_size_chars",
    "asr_send_lag_ms",
    "asr_partial_age_ms",
    "tts_frame_age_ms",
}


@dataclass
class MetricsRegistry:
    counters: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    labeled: dict[str, dict[tuple[tuple[str, str], ...], float]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(float))
    )
    latency_samples: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    _lock: Any = field(default_factory=threading.RLock, init=False, repr=False)

    def _inc(self, name: str, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
        with self._lock:
            if not labels:
                self.counters[name] += amount
                return
            key = tuple(sorted(labels.items()))
            self.labeled[name][key] += amount

    def _set(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        with self._lock:
            if not labels:
                self.counters[name] = value
                return
            key = tuple(sorted(labels.items()))
            self.labeled[name][key] = value

    def get(self, name: str, labels: dict[str, str] | None = None) -> float:
        with self._lock:
            if not labels:
                return float(self.counters.get(name, 0.0))
            key = tuple(sorted(labels.items()))
            return float(self.labeled.get(name, {}).get(key, 0.0))

    def inc_stale_result_dropped(self, source: str) -> None:
        self._inc("stale_result_dropped_total", {"source": source})

    def observe_context_snapshot_build(self, seconds: float) -> None:
        if seconds < 0 or seconds != seconds or seconds in {float("inf"), float("-inf")}:
            raise ValueError("context snapshot build time must be finite and non-negative")
        self._set("context_snapshot_build_seconds", seconds)

    def set_context_snapshot_size(self, size_chars: int) -> None:
        if size_chars < 0:
            raise ValueError("context snapshot size must be non-negative")
        self._set("context_snapshot_size_chars", float(size_chars))

    def inc_context_snapshot_build_failed(self, reason: str) -> None:
        self._inc("context_snapshot_build_failed_total", {"reason": reason})

    def inc_state_transition(self, from_state: str, to_state: str, event: str) -> None:
        self._inc(
            "state_transition_total",
            {"from": from_state, "to": to_state, "event": event},
        )

    def inc_interaction_phase(self, from_phase: str, to_phase: str, cause: str) -> None:
        """P0-4: observable BACKCHANNEL / THINKING_SILENT / SPEAKING transitions."""
        self._inc(
            "interaction_phase_total",
            {
                "from": from_phase,
                "to": to_phase,
                "cause": (cause or "unspecified")[:48],
            },
        )

    def inc_interruptions_confirmed(self) -> None:
        self._inc("interruptions_confirmed_total")

    def inc_false_interruptions(self) -> None:
        self._inc("false_interruptions_total")

    def inc_interruption_candidate(self) -> None:
        self._inc("interruption_candidates_total")

    def inc_guarded_user_input(self, reason: str) -> None:
        self._inc("guarded_user_input_total", {"reason": reason})

    def inc_tts_connections_discarded(self, reason: str) -> None:
        self._inc("tts_connections_discarded_total", {"reason": reason})

    def set_sessions_active(self, n: int) -> None:
        self._set("voice_sessions_active", float(n))

    def set_tts_pool_available(self, n: int) -> None:
        self._set("tts_pool_available", float(n))

    def set_tool_tasks_active(self, tool: str, n: int) -> None:
        self._set("tool_tasks_active", float(n), {"tool": tool})

    def set_voice_latency(self, stage: str, quantile: str, seconds: float) -> None:
        self._set(
            "voice_latency_seconds",
            seconds,
            {"stage": stage, "quantile": quantile},
        )

    def observe_voice_latency(self, stage: str, seconds: float) -> None:
        """Keep a bounded latency reservoir for the media SLO projection."""

        if seconds < 0 or seconds != seconds or seconds in {float("inf"), float("-inf")}:
            raise ValueError("voice latency must be finite and non-negative")
        with self._lock:
            samples = self.latency_samples[stage]
            if len(samples) >= 256:
                samples.pop(0)
            samples.append(float(seconds))

    def inc_media_session_started(self) -> None:
        self._inc("media_sessions_total")

    def inc_media_session_failed(self) -> None:
        self._inc("media_sessions_failed_total")

    def set_media_active_sessions(self, count: int) -> None:
        if count < 0:
            raise ValueError("active media session count must be non-negative")
        self.set_media_metric("media_active_sessions", float(count))

    def inc_media_stale_generation(self) -> None:
        self.inc_stale_result_dropped("media_generation")

    def inc_media_stale_asr_final(self) -> None:
        self.inc_stale_result_dropped("asr_final")

    def add_audio_input_seconds(self, seconds: float) -> None:
        self._inc("audio_input_seconds_total", amount=seconds)

    def inc_asr_request(self, status: str, model: str) -> None:
        self._inc("asr_requests_total", {"status": status, "model": model})

    def inc_asr_reconnect(self) -> None:
        self._inc("asr_reconnects_total")
        self._inc("provider_ws_reconnect_total", {"provider": "asr"})

    def set_provider_ws_active(self, provider: str, n: int) -> None:
        if n < 0:
            raise ValueError("active provider connection count must be non-negative")
        self._set("provider_ws_active", float(n), {"provider": provider})

    def add_provider_ws_active(self, provider: str, delta: int) -> None:
        with self._lock:
            labels = {"provider": provider}
            active = self.get("provider_ws_active", labels) + delta
            if active < 0:
                raise ValueError("active provider connection count must be non-negative")
            self._set("provider_ws_active", active, labels)

    def inc_provider_ws_reconnect(self, provider: str) -> None:
        self._inc("provider_ws_reconnect_total", {"provider": provider})

    def inc_llm_request(self, model: str, status: str, *, thinking: bool) -> None:
        self._inc(
            "llm_requests_total",
            {"model": model, "status": status, "thinking": str(thinking).lower()},
        )

    def inc_tts_request(self, status: str, model: str, voice: str) -> None:
        self._inc(
            "tts_requests_total",
            {"status": status, "model": model, "voice": voice},
        )

    def inc_media_metric(
        self,
        name: str,
        *,
        amount: float = 1.0,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Record an allowlisted media metric for the Prometheus exporter."""

        if name not in MEDIA_METRIC_NAMES:
            raise ValueError(f"media metric is not allowlisted: {name}")
        if amount < 0:
            raise ValueError("counter increment must be non-negative")
        self._inc(name, labels, amount)

    def set_media_metric(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
    ) -> None:
        """Set a bounded media gauge without accepting arbitrary metric names."""

        if name not in MEDIA_METRIC_NAMES:
            raise ValueError(f"media metric is not allowlisted: {name}")
        self._set(name, value, labels)

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            out = dict(self.counters)
            for name, series in self.labeled.items():
                for labels, val in series.items():
                    label_s = ",".join(f'{k}="{v}"' for k, v in labels)
                    out[f"{name}{{{label_s}}}"] = val
            return out

    def media_slo_snapshot(self) -> dict[str, float]:
        """Project only aggregate SLO fields for the Control API reporter.

        Missing latency/session fields are intentionally omitted so the
        Control API evaluator fails closed instead of treating an uninstrumented
        process as healthy. Stale counters are present from zero because zero
        is an authoritative observation for a process that has started.
        """

        snapshot: dict[str, float] = {
            "stale_generation_total": self.get(
                "stale_result_dropped_total", {"source": "media_generation"}
            ),
            "stale_asr_final_total": self.get(
                "stale_result_dropped_total", {"source": "asr_final"}
            ),
        }
        for stage, key in (
            ("first_audio", "first_audio_p95_ms"),
            ("interrupt_stop", "interrupt_stop_p95_ms"),
        ):
            with self._lock:
                samples = list(self.latency_samples.get(stage, ()))
            if samples:
                ordered = sorted(samples)
                index = min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))
                snapshot[key] = ordered[index] * 1000.0
            else:
                configured = self.get(
                    "voice_latency_seconds",
                    {"stage": stage, "quantile": "p95"},
                )
                if configured > 0:
                    snapshot[key] = configured * 1000.0
        sessions = self.get("media_sessions_total")
        failures = self.get("media_sessions_failed_total")
        if sessions > 0:
            snapshot["session_failure_rate"] = min(1.0, failures / sessions)
        return snapshot

    def prometheus_registry(self) -> CollectorRegistry:
        registry = CollectorRegistry()
        registry.register(_MetricsCollector(self))
        return registry

    def render_prometheus(self) -> bytes:
        return generate_latest(self.prometheus_registry())

    def start_http_server(self, port: int, addr: str = "0.0.0.0") -> Any:
        return prometheus_start_http_server(
            port,
            addr=addr,
            registry=self.prometheus_registry(),
        )


class _MetricsCollector:
    def __init__(self, metrics: MetricsRegistry) -> None:
        self._metrics = metrics

    def collect(self) -> list[CounterMetricFamily | GaugeMetricFamily]:
        with self._metrics._lock:
            plain = dict(self._metrics.counters)
            labeled = {name: dict(series) for name, series in self._metrics.labeled.items()}
            for stage, samples in self._metrics.latency_samples.items():
                if not samples:
                    continue
                ordered = sorted(samples)
                index = min(len(ordered) - 1, int(0.95 * (len(ordered) - 1)))
                labeled.setdefault("voice_latency_seconds", {})[
                    (("quantile", "p95"), ("stage", stage))
                ] = ordered[index]
        families: list[CounterMetricFamily | GaugeMetricFamily] = []
        for name, value in sorted(plain.items()):
            family = self._family(name, ())
            family.add_metric([], value)
            families.append(family)
        for name, series in sorted(labeled.items()):
            first_labels = next(iter(series), ())
            label_names = tuple(key for key, _ in first_labels)
            family = self._family(name, label_names)
            for labels, value in sorted(series.items()):
                family.add_metric([val for _, val in labels], value)
            families.append(family)
        return families

    @staticmethod
    def _family(name: str, label_names: tuple[str, ...]) -> CounterMetricFamily | GaugeMetricFamily:
        family_type = GaugeMetricFamily if name in _GAUGES else CounterMetricFamily
        return family_type(name, f"Voice agent metric {name}.", labels=list(label_names))


GLOBAL_METRICS = MetricsRegistry()
