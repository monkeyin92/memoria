"""Thread-safe metrics registry with a real Prometheus collector/exporter."""

from __future__ import annotations

import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client import start_http_server as prometheus_start_http_server
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

_GAUGES = {
    "tool_tasks_active",
    "tts_pool_available",
    "voice_latency_seconds",
    "voice_sessions_active",
}


@dataclass
class MetricsRegistry:
    counters: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    labeled: dict[str, dict[tuple[tuple[str, str], ...], float]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(float))
    )
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

    def inc_state_transition(self, from_state: str, to_state: str, event: str) -> None:
        self._inc(
            "state_transition_total",
            {"from": from_state, "to": to_state, "event": event},
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

    def add_audio_input_seconds(self, seconds: float) -> None:
        self._inc("audio_input_seconds_total", amount=seconds)

    def inc_asr_request(self, status: str, model: str) -> None:
        self._inc("asr_requests_total", {"status": status, "model": model})

    def inc_asr_reconnect(self) -> None:
        self._inc("asr_reconnects_total")

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

    def snapshot(self) -> dict[str, float]:
        with self._lock:
            out = dict(self.counters)
            for name, series in self.labeled.items():
                for labels, val in series.items():
                    label_s = ",".join(f'{k}="{v}"' for k, v in labels)
                    out[f"{name}{{{label_s}}}"] = val
            return out

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
            labeled = {
                name: dict(series) for name, series in self._metrics.labeled.items()
            }
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
    def _family(
        name: str, label_names: tuple[str, ...]
    ) -> CounterMetricFamily | GaugeMetricFamily:
        family_type = GaugeMetricFamily if name in _GAUGES else CounterMetricFamily
        return family_type(name, f"Voice agent metric {name}.", labels=list(label_names))


GLOBAL_METRICS = MetricsRegistry()
