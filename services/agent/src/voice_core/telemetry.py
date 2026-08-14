"""Bounded media telemetry contracts.

The media path needs useful Prometheus and OpenTelemetry signals without
putting audio, transcripts, JWTs, or unbounded user identifiers into a metric
label.  This module is deliberately small and self-owned: callers can connect
the two sinks to the process exporters they already run, while unit tests can
exercise the same redaction and fence rules without a collector.
"""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

MEDIA_METRIC_NAMES = frozenset(
    {
        "aec_double_talk_asr_error_rate",
        "aec_far_end_false_vad_total",
        "device_downlink_queue_ms",
        "device_media_connect_success_total",
        "device_media_reconnect_total",
        "device_playback_ack_lag_ms",
        "device_uplink_gap_samples_total",
        "interrupt_audible_stop_ms",
        "interrupt_candidate_total",
        "interrupt_confirmed_total",
        "interrupt_false_positive_total",
        "media_active_sessions",
        "media_peer_connection_state_total",
        "media_ice_connection_time_ms",
        "media_rtp_packets_received_total",
        "media_rtp_packets_lost_total",
        "media_rtp_jitter_ms",
        "runtime_profile_version_lag",
        "stale_generation_drop_total",
        "media_pcm_queue_depth",
        "media_pcm_overflow_total",
        "media_discontinuity_total",
        "media_loss_concealed_frames_total",
        "asr_send_lag_ms",
        "asr_partial_age_ms",
        "tts_frame_age_ms",
        "voice_vad_onset_ms",
        "voice_asr_partial_latency_ms",
        "voice_asr_final_latency_ms",
        "voice_turn_commit_latency_ms",
        "voice_llm_ttft_ms",
        "voice_tts_ttfb_ms",
        "voice_first_audio_ms",
        "voice_interrupt_duck_latency_ms",
        "voice_interrupt_stop_latency_ms",
        "voice_interrupt_success_total",
        "voice_false_interrupt_total",
        "voice_kws_hits_total",
        "voice_stale_asr_result_dropped_total",
        "voice_stale_audio_frame_dropped_total",
        "voice_old_epoch_event_dropped_total",
        "device_online_total",
        "device_reconnect_total",
        "device_command_latency_ms",
        "device_command_failure_total",
        "device_ota_result_total",
    }
)

MEDIA_SPAN_NAMES = frozenset(
    {
        "device.dac_started",
        "device.first_audio_received",
        "device.mic_first_sample",
        "device.playback_ended",
        "edge.audio_first",
        "edge.first_downlink_opus",
        "generation.cancelled",
        "interrupt.detected",
        "interrupt.flush",
        "interrupt.local_duck",
        "asr.partial_first",
        "turn.committed",
        "tts.first_pcm",
        "vad.end",
        "vad.start",
        "webrtc.connect",
        "ice.gather",
        "rtp.receive",
        "opus.decode",
        "vad.detect",
        "kws.detect",
        "asr.send",
        "asr.partial",
        "asr.final",
        "turn.commit",
        "llm.first_token",
        "tts.first_audio",
        "audio.encode",
        "rtp.send",
        "playback.ack",
        "interrupt.detect",
        "interrupt.cancel",
    }
)

GOLDEN_TRACE_BASE_EVENTS = (
    "device.mic_first_sample",
    "vad.start",
    "vad.end",
    "edge.audio_first",
    "asr.partial_first",
    "asr.final",
    "turn.committed",
    "llm.first_token",
    "tts.first_pcm",
    "edge.first_downlink_opus",
    "device.first_audio_received",
    "device.dac_started",
    "device.playback_ended",
)

GOLDEN_TRACE_INTERRUPT_EVENTS = (
    "interrupt.detected",
    "interrupt.local_duck",
    "interrupt.flush",
    "generation.cancelled",
)

_SAFE_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_ALLOWED_LABELS = frozenset({"runtime", "state", "source", "reason", "kind", "status"})
_ALLOWED_TRACE_EVENT_FIELDS = frozenset(
    {
        "approximate",
        "audio_mode",
        "capture_end_sample",
        "capture_start_sample",
        "device_sequence",
        "frame_samples",
        "playback_sample_end",
        "playback_sample_start",
        "queue_ms",
        "reason",
        "received_sequence",
        "sample_position",
        "source",
        "status",
        "voiced_end_sample",
    }
)
_SENSITIVE_TRACE_FIELD_FRAGMENTS = (
    "audio",
    "credential",
    "key",
    "password",
    "payload",
    "private",
    "secret",
    "text",
    "token",
    "transcript",
    "wifi",
)


def _labels(labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    clean: list[tuple[str, str]] = []
    for key, value in labels.items():
        if key not in _ALLOWED_LABELS or not _SAFE_LABEL.fullmatch(key):
            raise ValueError(f"telemetry label is not allowlisted: {key}")
        if not isinstance(value, str) or not value or len(value) > 64:
            raise ValueError("telemetry label value must be a short string")
        clean.append((key, value))
    return tuple(sorted(clean))


@dataclass(frozen=True, slots=True)
class TraceContext:
    """The small propagation context shared by Edge, Core, and clients."""

    trace_id: str
    session_id: str
    stream_epoch: int
    turn_id: int = 0
    generation_id: int = 0
    tool_epoch: int = 0
    device_id: str | None = None
    provider_task_epoch: int = 0
    runtime_profile_version: int = 0

    def __post_init__(self) -> None:
        for field_name, text_value in (
            ("trace_id", self.trace_id),
            ("session_id", self.session_id),
        ):
            if not isinstance(text_value, str) or not text_value.strip() or len(text_value) > 128:
                raise ValueError(f"{field_name} must be a short non-empty string")
        for field_name, number_value in (
            ("stream_epoch", self.stream_epoch),
            ("turn_id", self.turn_id),
            ("generation_id", self.generation_id),
            ("tool_epoch", self.tool_epoch),
            ("provider_task_epoch", self.provider_task_epoch),
            ("runtime_profile_version", self.runtime_profile_version),
        ):
            if (
                isinstance(number_value, bool)
                or not isinstance(number_value, int)
                or number_value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if self.device_id is not None and (not self.device_id.strip() or len(self.device_id) > 128):
            raise ValueError("device_id must be a short non-empty string")

    def child(
        self, *, turn_id: int | None = None, generation_id: int | None = None
    ) -> TraceContext:
        return TraceContext(
            trace_id=self.trace_id,
            session_id=self.session_id,
            stream_epoch=self.stream_epoch,
            turn_id=self.turn_id if turn_id is None else turn_id,
            generation_id=self.generation_id if generation_id is None else generation_id,
            tool_epoch=self.tool_epoch,
            device_id=self.device_id,
            provider_task_epoch=self.provider_task_epoch,
            runtime_profile_version=self.runtime_profile_version,
        )

    def fields(self) -> dict[str, str | int]:
        result: dict[str, str | int] = {
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "stream_epoch": self.stream_epoch,
            "turn_id": self.turn_id,
            "generation_id": self.generation_id,
            "tool_epoch": self.tool_epoch,
            "provider_task_epoch": self.provider_task_epoch,
            "runtime_profile_version": self.runtime_profile_version,
        }
        if self.device_id is not None:
            result["device_id"] = self.device_id
        return result


@dataclass(frozen=True, slots=True)
class TraceEvent:
    name: str
    at_monotonic_ns: int
    fields: Mapping[str, str | int | float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.name not in MEDIA_SPAN_NAMES:
            raise ValueError("trace span name is not allowlisted")
        if self.at_monotonic_ns < 0:
            raise ValueError("trace timestamp must be non-negative")
        for key, value in self.fields.items():
            normalized = key.lower()
            if any(fragment in normalized for fragment in _SENSITIVE_TRACE_FIELD_FRAGMENTS):
                raise ValueError("sensitive trace event field is forbidden")
            if key not in _ALLOWED_TRACE_EVENT_FIELDS:
                raise ValueError("trace event field is not allowlisted")
            if not isinstance(value, (str, int, float, bool)):
                raise ValueError("trace event field value must be scalar")
            if isinstance(value, str) and (not value or len(value) > 64):
                raise ValueError("trace event string must be short and non-empty")


class TurnTimeline:
    """A bounded, redacted timeline for one turn."""

    def __init__(self, context: TraceContext, *, max_events: int = 64) -> None:
        if max_events <= 0:
            raise ValueError("max_events must be positive")
        self.context = context
        self._events: deque[TraceEvent] = deque(maxlen=max_events)
        self._max_events = max_events
        self._lock = threading.RLock()

    def add(
        self,
        name: str,
        *,
        at_monotonic_ns: int | None = None,
        fields: Mapping[str, str | int | float] | None = None,
    ) -> None:
        event = TraceEvent(
            name=name,
            at_monotonic_ns=time.monotonic_ns() if at_monotonic_ns is None else at_monotonic_ns,
            fields=dict(fields or {}),
        )
        with self._lock:
            self._events.append(event)

    def events(self) -> tuple[TraceEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def completion_gaps(self, *, require_interrupt: bool = False) -> tuple[str, ...]:
        """Return missing Golden Trace anchors without fabricating runtime proof."""

        with self._lock:
            observed = {event.name for event in self._events}
        required: tuple[str, ...] = GOLDEN_TRACE_BASE_EVENTS
        if require_interrupt:
            required += GOLDEN_TRACE_INTERRUPT_EVENTS
        return tuple(name for name in required if name not in observed)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            events = tuple(self._events)
        origin = events[0].at_monotonic_ns if events else None
        return {
            **self.context.fields(),
            "events": [
                {
                    "name": event.name,
                    "t_ms": (
                        round((event.at_monotonic_ns - origin) / 1_000_000, 3)
                        if origin is not None
                        else 0.0
                    ),
                    "fields": dict(event.fields),
                }
                for event in events
            ],
        }


class MediaTelemetry:
    """Thread-safe counters/gauges/histograms with bounded label cardinality."""

    def __init__(self, *, max_series: int = 2_048) -> None:
        if max_series <= 0:
            raise ValueError("max_series must be positive")
        self._max_series = max_series
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._observations: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = (
            defaultdict(list)
        )
        self._lock = threading.RLock()

    def _key(
        self, name: str, labels: Mapping[str, str] | None
    ) -> tuple[str, tuple[tuple[str, str], ...]]:
        if name not in MEDIA_METRIC_NAMES:
            raise ValueError(f"media metric is not allowlisted: {name}")
        key = (name, _labels(labels))
        with self._lock:
            if (
                key not in self._counters
                and key not in self._gauges
                and key not in self._observations
            ):
                series_count = len(
                    set(self._counters) | set(self._gauges) | set(self._observations)
                )
                if series_count >= self._max_series:
                    raise OverflowError("media telemetry series limit reached")
        return key

    def inc(
        self, name: str, *, amount: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        if amount < 0:
            raise ValueError("counter increment must be non-negative")
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += amount

    def set_gauge(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        if (
            not isinstance(value, (int, float))
            or value != value
            or value in {float("inf"), float("-inf")}
        ):
            raise ValueError("gauge value must be finite")
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = float(value)

    def observe_ms(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        if value < 0 or value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("latency observation must be finite and non-negative")
        key = self._key(name, labels)
        with self._lock:
            samples = self._observations[key]
            if len(samples) >= 256:
                samples.pop(0)
            samples.append(float(value))

    def get(self, name: str, *, labels: Mapping[str, str] | None = None) -> float:
        key = self._key(name, labels)
        with self._lock:
            if key in self._gauges:
                return self._gauges[key]
            if key in self._counters:
                return self._counters[key]
            samples = self._observations.get(key, [])
            return sum(samples) / len(samples) if samples else 0.0

    def render_prometheus(self) -> str:
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            observations = {key: tuple(values) for key, values in self._observations.items()}
        lines: list[str] = []
        for (name, labels), value in sorted(counters.items()):
            lines.append(f"{name}{_format_labels(labels)} {value:g}")
        for (name, labels), value in sorted(gauges.items()):
            lines.append(f"{name}{_format_labels(labels)} {value:g}")
        for (name, labels), values in sorted(observations.items()):
            if not values:
                continue
            for quantile, index in (("0.5", 0.5), ("0.95", 0.95), ("0.99", 0.99)):
                ordered = sorted(values)
                position = min(len(ordered) - 1, int(index * (len(ordered) - 1)))
                quantile_labels = _format_labels((*labels, ("quantile", quantile)))
                lines.append(f"{name}{quantile_labels} {ordered[position]:g}")
            lines.append(f"{name}_count{_format_labels(labels)} {len(values)}")
        return "\n".join(lines) + ("\n" if lines else "")


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    escaped = [(key, value.replace("\\", "\\\\").replace('"', '\\"')) for key, value in labels]
    return "{" + ",".join(f'{key}="{value}"' for key, value in escaped) + "}"


__all__ = [
    "GOLDEN_TRACE_BASE_EVENTS",
    "GOLDEN_TRACE_INTERRUPT_EVENTS",
    "MEDIA_METRIC_NAMES",
    "MEDIA_SPAN_NAMES",
    "MediaTelemetry",
    "TraceContext",
    "TraceEvent",
    "TurnTimeline",
]
