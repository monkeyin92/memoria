"""Validate bounded client audio diagnostics before they reach logs."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

CLIENT_AUDIO_TRACE_NAMES = frozenset(
    {
        "audio_unlock",
        "session_created",
        "room_connected",
        "agent_ready",
        "track_subscribed",
        "audio_attached",
        "play_resolved",
        "play_rejected",
        "playing",
        "first_playback",
        "media_error",
        "webrtc_inbound_audio",
        "webrtc_microphone_settings",
        "webrtc_outbound_audio",
        "miniprogram_playback_underrun",
        "miniprogram_playback_hard_reset",
        "miniprogram_gap_concealed",
        "miniprogram_playback_lead_adjusted",
    }
)

CLIENT_AUDIO_METRIC_NAMES = frozenset(
    {
        "jitter",
        "packets_lost",
        "packets_received",
        "packets_discarded",
        "packets_lost_delta",
        "packets_received_delta",
        "packets_discarded_delta",
        "bytes_received",
        "nack_count",
        "concealed_samples",
        "concealed_samples_delta",
        "silent_concealed_samples",
        "total_samples_received",
        "total_samples_received_delta",
        "concealment_events",
        "concealment_ratio",
        "non_silent_concealment_ratio",
        "jitter_buffer_delay",
        "jitter_buffer_target_delay",
        "jitter_buffer_minimum_delay",
        "jitter_buffer_emitted_count",
        "total_samples_duration",
        "average_jitter_buffer_delay_ms",
        "average_jitter_buffer_target_delay_ms",
        "encoded_audio_bitrate_kbps",
        "inserted_samples_for_deceleration",
        "removed_samples_for_acceleration",
        "packets_sent",
        "packets_sent_delta",
        "bytes_sent",
        "bytes_sent_delta",
        "retransmitted_packets_sent",
        "retransmitted_packets_sent_delta",
        "retransmitted_bytes_sent",
        "retransmitted_bytes_sent_delta",
        "total_packet_send_delay",
        "round_trip_time",
        "fraction_lost",
        "audio_level",
        "total_audio_energy",
        "echo_return_loss",
        "echo_return_loss_enhancement",
        "queue_lead_ms",
        "pending_audio_ms",
        "missing_frames",
        "scheduled_sources",
        "clock_ahead_ms",
        "target_lead_ms",
        "underflow_count",
    }
)

CLIENT_MICROPHONE_NUMERIC_SETTINGS = frozenset(
    {
        "sample_rate",
        "sample_size",
        "channel_count",
        "latency_ms",
    }
)

CLIENT_MICROPHONE_BOOLEAN_SETTINGS = frozenset(
    {
        "auto_gain_control",
        "echo_cancellation",
        "noise_suppression",
    }
)


@dataclass(frozen=True, slots=True)
class ClientAudioTrace:
    name: str
    status: str
    metrics: dict[str, int | float | bool]


def parse_client_audio_trace(
    event: dict[str, Any],
    *,
    session_id: str,
) -> ClientAudioTrace | None:
    name = event.get("name")
    status = event.get("status")
    if (
        event.get("session_id") != session_id
        or name not in CLIENT_AUDIO_TRACE_NAMES
        or status not in {"ok", "error"}
    ):
        return None

    miniprogram_trace = event.get("source") == "miniprogram" and name in {
        "first_playback",
        "miniprogram_playback_underrun",
        "miniprogram_playback_hard_reset",
        "miniprogram_gap_concealed",
        "miniprogram_playback_lead_adjusted",
    }
    if name in {"webrtc_inbound_audio", "webrtc_outbound_audio"} or miniprogram_trace:
        metrics = _numeric_metrics(event.get("detail"))
        if metrics is None:
            return None
        return ClientAudioTrace(name=str(name), status=str(status), metrics=metrics)
    if name == "webrtc_microphone_settings":
        metrics = _microphone_settings(event.get("detail"))
        if metrics is None:
            return None
        return ClientAudioTrace(name=str(name), status=str(status), metrics=metrics)
    return ClientAudioTrace(name=str(name), status=str(status), metrics={})


def _numeric_metrics(detail: object) -> dict[str, int | float] | None:
    if not isinstance(detail, dict) or not detail or not set(detail).issubset(
        CLIENT_AUDIO_METRIC_NAMES
    ):
        return None
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in detail.values()
    ):
        return None
    return {str(key): value for key, value in detail.items()}


def _microphone_settings(detail: object) -> dict[str, int | float | bool] | None:
    if not isinstance(detail, dict) or not detail:
        return None
    allowed = CLIENT_MICROPHONE_NUMERIC_SETTINGS | CLIENT_MICROPHONE_BOOLEAN_SETTINGS
    if not set(detail).issubset(allowed):
        return None
    for key, value in detail.items():
        if key in CLIENT_MICROPHONE_BOOLEAN_SETTINGS:
            if not isinstance(value, bool):
                return None
        elif (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return None
    return {str(key): value for key, value in detail.items()}
