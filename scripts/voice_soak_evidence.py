"""Text-free, offline evidence for spoken stops and post-stop silence.

A state transition or an arbitrary cancellation is not an interruption. Correlate
the device's playable generation to one full delivery fence, then require a spoken
stop, its preempted terminal and the device's playout closure in the attempt window.
Clock skew or ambiguous/missing receipts fail closed as unverified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

FIELDS = re.compile(r"\b(\w+)=([^\s]*)")
DELIVERY = re.compile(
    r"(?P<session>[^/\s]+)/epoch-\d+/turn-(?P<turn>\d+)"
    r"/generation-(?P<generation>\d+)/tool-\d+"
)
START_TOLERANCE_S = 2.0
# Fastest observed spoken stop: ~1.4 s clip, final ~0.33 s later, then the stop.
# A clip that starts with less playback left cannot win the race against the end.
MIN_PLAYBACK_LEFT_FOR_STOP_S = 2.5
MIN_SILENCE_WINDOW_S = 5.0
UNVERIFIABLE_STOP_REASONS = frozenset(
    {"interrupt_started_after_playback", "interrupt_too_late_to_stop"}
)


def _bridge_rows(text: str) -> list[tuple[float, str, dict[str, str]]]:
    rows = []
    for line in text.splitlines():
        if "media reply delivery " in line:
            kind = "delivery"
        elif "media spoken stop interrupted reply " in line:
            kind = "stop"
        else:
            continue
        try:
            at = datetime.fromisoformat(line.split()[0])
        except ValueError:
            continue
        if at.utcoffset() is not None:
            rows.append((at.timestamp(), kind, dict(FIELDS.findall(line))))
    return rows


def _serial_rows(
    text: str, reference: float, utc_offset_s: int
) -> list[tuple[float, str, dict[str, str]]]:
    zone = timezone(timedelta(seconds=utc_offset_s))
    day = datetime.fromtimestamp(reference, zone).date()
    rows = []
    for line in text.splitlines():
        if "First playable downlink frame " in line:
            kind = "start"
        elif "media playback supply summary " in line:
            kind = "end"
        else:
            continue
        try:
            clock = datetime.strptime(line.split()[0], "%H:%M:%S.%f").time()
        except ValueError:
            continue
        at = datetime.combine(day, clock, zone)
        # Serial logs have no date; an attempt may straddle midnight.
        timestamp = min(
            ((at + timedelta(days=d)).timestamp() for d in (-1, 0, 1)),
            key=lambda t: abs(t - reference),
        )
        rows.append((timestamp, kind, dict(FIELDS.findall(line))))
    return rows


def _silence_fence(rows: list[dict[str, Any]], index: int) -> tuple[float | None, str]:
    for row in rows[index + 1 :]:
        boundary = {
            "no_reply": "no_reply_boundary",
            "step_failed": "step_failed_boundary",
            "wake_try": "wake_try_boundary",
        }.get(row.get("kind"))
        if boundary is not None:
            timestamp = row.get("ts")
            return (
                float(timestamp) if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool) else None,
                boundary,
            )
        if row.get("kind") == "said" and isinstance(row.get("ts"), (int, float)):
            return float(row["ts"]), "next_said_end"
        if row.get("kind") == "post_scenario_observation" and isinstance(row.get("ts"), (int, float)):
            return float(row["ts"]), "post_scenario_observation"
    return None, "none"


def _observed_until(rows: list[dict[str, Any]], events: list[tuple[float, str, dict[str, str]]]) -> float | None:
    timestamps = [
        float(row["ts"])
        for row in rows
        if isinstance(row.get("ts"), (int, float)) and not isinstance(row.get("ts"), bool)
    ]
    timestamps.extend(at for at, _, _ in events)
    return max(timestamps) if timestamps else None


def assess_post_stop_silence(
    bridge: str,
    serial: str,
    attempt: Mapping[str, Any],
    receipt: Mapping[str, Any],
    fence_ts: float | None,
    fence_source: str,
    observed_until_ts: float | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "silence_verdict": "unverified",
        "silence_reason": "no_next_clip_fence",
        "silence_fence_ts": fence_ts,
        "silence_fence_source": fence_source,
        "silence_window_s": None,
        "later_reply_count": 0,
        "later_reply_delivery_id": None,
        "later_reply_generation": None,
        "later_reply_started_ts": None,
        "later_reply_delay_s": None,
    }
    if receipt.get("stopped") is not True:
        result.update(silence_verdict="not_applicable", silence_reason="no_stop_receipt")
        return result
    if attempt.get("interrupt_kind") in {"backchannel", "crisis"}:
        result.update(silence_verdict="not_applicable", silence_reason="kind_not_a_stop")
        return result
    next_speak_ts = attempt.get("next_speak_ts")
    if isinstance(next_speak_ts, (int, float)) and not isinstance(next_speak_ts, bool):
        result.update(silence_verdict="not_applicable", silence_reason="answer_expected")
        return result

    returned = attempt.get("device_returned_ts")
    delivery_id = receipt.get("reply_delivery_id")
    if not isinstance(returned, (int, float)) or not isinstance(delivery_id, str):
        result.update(silence_verdict="unverified", silence_reason="missing_stop_return")
        return result
    identity = DELIVERY.fullmatch(delivery_id)
    if identity is None:
        result.update(silence_verdict="unverified", silence_reason="missing_stop_delivery")
        return result
    observation_fence = fence_source in {"next_said_end", "post_scenario_observation"}
    upper = fence_ts if fence_ts is not None else observed_until_ts
    if upper is not None:
        result["silence_window_s"] = round(upper - float(returned), 3)
    if upper is None or upper <= float(returned):
        result.update(
            silence_verdict="unverified",
            silence_reason="no_next_clip_fence" if fence_ts is None else "no_silence_observation",
        )
        return result

    events = _bridge_rows(bridge)
    device = _serial_rows(
        serial,
        float(returned),
        int(attempt.get("serial_utc_offset_s", 8 * 3600)),
    )
    try:
        current_generation = int(identity["generation"])
    except ValueError:
        result.update(silence_verdict="unverified", silence_reason="invalid_stop_generation")
        return result
    lower = float(attempt.get("interrupt_start_ts", returned))
    later_starts = sorted(
        (at, int(fields["generation"]))
        for at, kind, fields in device
        if kind == "start"
        and lower <= at < upper
        and fields.get("generation", "").isdigit()
        and int(fields["generation"]) > current_generation
    )
    later_deliveries = []
    for at, kind, fields in events:
        if kind != "delivery" or fields.get("event") != "first_frame_sent":
            continue
        if not lower <= at < upper:
            continue
        other = DELIVERY.fullmatch(fields.get("delivery_id", ""))
        if other is None or other["session"] != identity["session"]:
            continue
        try:
            generation = int(other["generation"])
        except ValueError:
            continue
        if generation > current_generation:
            later_deliveries.append((at, fields["delivery_id"], generation))
    later_deliveries.sort()
    if later_deliveries:
        at, later_id, generation = later_deliveries[0]
        result.update(
            later_reply_count=len(later_deliveries),
            later_reply_delivery_id=later_id,
            later_reply_generation=generation,
            later_reply_started_ts=at,
            later_reply_delay_s=round(at - float(returned), 3),
        )
        if any(
            started_generation == generation
            and abs(started_at - at) <= START_TOLERANCE_S
            for started_at, started_generation in later_starts
        ):
            result.update(silence_verdict="reply_resumed", silence_reason="later_reply_started")
        else:
            result.update(
                silence_verdict="unverified",
                silence_reason="later_delivery_without_device_receipt",
            )
        return result
    if later_starts:
        started_at, generation = later_starts[0]
        result.update(
            later_reply_count=len(later_starts),
            later_reply_generation=int(generation),
            later_reply_started_ts=started_at,
            later_reply_delay_s=round(started_at - float(returned), 3),
            silence_verdict="unverified",
            silence_reason="unattributed_device_start",
        )
        return result
    if not observation_fence:
        result.update(silence_verdict="unverified", silence_reason="no_silence_observation")
        return result
    if fence_ts is None:
        return result
    if result["silence_window_s"] < MIN_SILENCE_WINDOW_S:
        result.update(silence_verdict="unverified", silence_reason="silence_window_too_short")
        return result
    result.update(silence_verdict="silent", silence_reason="no_reply_before_fence")
    return result


def assess_interruption(bridge: str, serial: str, attempt: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "stop_evidence_version": 2,
        "stopped": None,
        "stop_verdict": "unverified",
        "stop_reason": "missing_reply_start",
        "stop_delay_s": None,
        "stop_delay_origin": "interrupt_start",
        "stop_delay_from_interrupt_start_s": None,
        "stop_delay_from_speech_end_s": None,
    }

    def finish(reason: str, stopped: bool | None = None) -> dict[str, Any]:
        result.update(
            stop_reason=reason,
            stopped=stopped,
            stop_verdict="unverified"
            if stopped is None
            else ("stopped" if stopped else "not_stopped"),
        )
        return result

    started = attempt.get("reply_started_ts")
    if not isinstance(started, (int, float)):
        return result
    device = _serial_rows(serial, started, int(attempt.get("serial_utc_offset_s", 8 * 3600)))
    generations = {
        fields.get("generation")
        for at, kind, fields in device
        if kind == "start" and abs(at - started) <= 0.5
    }
    if len(generations) != 1 or None in generations:
        return finish("missing_or_ambiguous_device_generation")
    generation = generations.pop()
    events = _bridge_rows(bridge)
    candidates = {}
    for at, kind, fields in events:
        identity = DELIVERY.fullmatch(fields.get("delivery_id", ""))
        if (
            kind == "delivery"
            and identity is not None
            and identity["generation"] == generation
            and identity["session"] == fields.get("session")
            and fields.get("event") == "first_frame_sent"
            and abs(at - started) <= START_TOLERANCE_S
        ):
            candidates[identity[0]] = (at, identity)
    if len(candidates) != 1:
        return finish("missing_or_ambiguous_delivery")
    delivery_id, (first_frame_at, identity) = next(iter(candidates.items()))
    result["reply_delivery_id"] = delivery_id
    terminals = [
        (at, fields)
        for at, kind, fields in events
        if kind == "delivery"
        and fields.get("delivery_id") == delivery_id
        and fields.get("session") == identity["session"]
        and fields.get("terminal")
        and at >= first_frame_at
    ]
    if not terminals:
        return finish("missing_delivery_terminal")
    if len({(fields["terminal"], fields.get("terminal_reason")) for _, fields in terminals}) != 1:
        return finish("conflicting_delivery_terminals")
    terminal_at, terminal = terminals[0]
    result["reply_terminal_reason"] = terminal.get("terminal_reason", "")
    if terminal.get("terminal_reason") == "playback_completed":
        interrupt_start = attempt.get("interrupt_start_ts")
        if isinstance(interrupt_start, (int, float)):
            remaining = terminal_at - interrupt_start
            result["remaining_playback_at_clip_start_s"] = round(remaining, 3)
            if remaining <= 0:
                return finish("interrupt_started_after_playback")
            if remaining < MIN_PLAYBACK_LEFT_FOR_STOP_S:
                return finish("interrupt_too_late_to_stop")
        return finish("playback_completed", False)
    if terminal["terminal"] != "preempted":
        return finish("non_interrupt_terminal", False)

    window_start, window_end = (
        attempt.get("interrupt_start_ts"),
        attempt.get("interrupt_deadline_ts"),
    )
    if not isinstance(window_start, (int, float)) or not isinstance(window_end, (int, float)):
        return finish("missing_interrupt_window")
    if not started <= window_start < window_end or not window_start <= terminal_at <= window_end:
        return finish("terminal_outside_interrupt_window", False)
    stops = [
        (at, fields)
        for at, kind, fields in events
        if kind == "stop"
        and fields.get("session") == identity["session"]
        and fields.get("turn") == identity["turn"]
        and fields.get("generation") == generation
        and fields.get("flush") == "True"
        and window_start <= at <= window_end
    ]
    if not stops:
        return finish("missing_matching_spoken_stop")
    if all(abs(at - terminal_at) > START_TOLERANCE_S for at, _ in stops):
        return finish("stop_and_terminal_timing_mismatch")
    # The stop log has a partial fence. Do not borrow it from another epoch/tool.
    if any(
        kind == "delivery"
        and window_start <= at <= window_end
        and (other := DELIVERY.fullmatch(fields.get("delivery_id", ""))) is not None
        and other["session"] == identity["session"]
        and other["turn"] == identity["turn"]
        and other["generation"] == generation
        and other[0] != delivery_id
        for at, kind, fields in events
    ):
        return finish("ambiguous_stop_fence")
    returned = attempt.get("device_returned_ts")
    if not isinstance(returned, (int, float)) or not window_start <= returned <= window_end:
        return finish("device_did_not_stop_in_window", False)
    if terminal_at > returned + START_TOLERANCE_S:
        return finish("device_return_precedes_server_stop")
    played = [
        fields
        for at, kind, fields in device
        if kind == "end"
        and fields.get("generation") == generation
        and fields.get("first_output") == "yes"
        and fields.get("output_frames", "").isdigit()
        and int(fields["output_frames"]) > 0
        and window_start <= at <= window_end
        and abs(at - returned) <= 1.0
    ]
    if not played:
        return finish("missing_device_playout_receipt")
    from_start = round(returned - window_start, 3)
    result["stop_delay_s"] = from_start
    result["stop_delay_origin"] = "interrupt_start"
    result["stop_delay_from_interrupt_start_s"] = from_start
    speech_end = attempt.get("interrupt_end_ts")
    if isinstance(speech_end, (int, float)) and not isinstance(speech_end, bool):
        result["stop_delay_from_speech_end_s"] = round(returned - speech_end, 3)
    else:
        result["stop_delay_from_speech_end_s"] = None
    return finish("spoken_stop_and_device_playout", True)


def audit_interruptions(
    rows: list[dict[str, Any]], bridge: str, serial: str
) -> dict[str, dict[str, Any]]:
    """Reassess without trusting or rewriting historical `stopped` fields.

    Legacy rows permit identifying natural completion but cannot prove a stop:
    their interruption timestamps were never recorded.
    """
    said = {row["tag"]: row for row in rows if row.get("kind") == "said"}
    assessed = {}
    events = _bridge_rows(bridge)
    observed_until_ts = _observed_until(rows, events)
    for index, row in enumerate(rows):
        if row.get("kind") != "reply" or not row.get("interrupted"):
            continue
        attempt = dict(row)
        speech_end = said.get(row["tag"], {}).get("ts")
        latency = row.get("latency_s")
        if (
            "reply_started_ts" not in attempt
            and isinstance(speech_end, (int, float))
            and isinstance(latency, (int, float))
        ):
            attempt["reply_started_ts"] = speech_end + latency
        assessment = assess_interruption(bridge, serial, attempt)
        fence_ts, fence_source = _silence_fence(rows, index)
        assessment.update(
            assess_post_stop_silence(
                bridge,
                serial,
                attempt,
                assessment,
                fence_ts,
                fence_source,
                observed_until_ts,
            )
        )
        assessed[row["tag"]] = assessment
    return assessed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reassess recorded stops offline; never contacts a device or server."
    )
    parser.add_argument("run", type=Path)
    args = parser.parse_args(argv)
    paths = {name: args.run / name for name in ("timeline.jsonl", "bridge.log", "serial.log")}
    if not paths["timeline.jsonl"].is_file():
        parser.error("run must contain timeline.jsonl")
    data = {name: path.read_bytes() if path.exists() else b"" for name, path in paths.items()}
    rows = [json.loads(line) for line in data["timeline.jsonl"].splitlines() if line.strip()]
    report = {
        "schema_version": "voice-soak-stop-v3",
        "run": str(args.run),
        "source_sha256": {
            name: hashlib.sha256(content).hexdigest() if content else None
            for name, content in data.items()
        },
        "interruptions": audit_interruptions(
            rows,
            data["bridge.log"].decode("utf-8", "replace"),
            data["serial.log"].decode("utf-8", "replace"),
        ),
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
