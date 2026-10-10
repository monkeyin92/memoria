"""Text-free, offline evidence for spoken stops in the robot soak driver.

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


def assess_interruption(bridge: str, serial: str, attempt: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "stop_evidence_version": 2,
        "stopped": None,
        "stop_verdict": "unverified",
        "stop_reason": "missing_reply_start",
        "stop_delay_s": None,
        "stop_delay_origin": "interrupt_start",
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
    result["stop_delay_s"] = round(returned - window_start, 3)
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
    for row in rows:
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
        assessed[row["tag"]] = assess_interruption(bridge, serial, attempt)
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
        "schema_version": "voice-soak-stop-v2",
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
