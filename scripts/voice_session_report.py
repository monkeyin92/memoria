#!/usr/bin/env python3
"""Report raw per-turn timings from one captured Memoria voice session.

Read-only: parses ``serial.log`` and the streamed bridge/agent logs from a
capture directory produced by ``capture.py``.  It prints the device-side
"finished speaking -> first audible frame" gap and the server-side per-delivery
chain, so the 1.5s silence criterion is judged from raw per-turn numbers rather
than an average.  It deliberately does not restate the pass/fail gates; the
capture's own verifier keeps that job.

Usage: ``python scripts/voice_session_report.py <capture-dir>``
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime
from pathlib import Path

SERIAL_TS = re.compile(r"^\[(?P<ts>[^\]]+)\]")
VAD = re.compile(r"MemoriaProtocol: Device VAD (?P<edge>start|end) at sample=(?P<sample>\d+) rms=(?P<rms>[\d.]+)")
FRAME = re.compile(r"MemoriaProtocol: First playable downlink frame generation=(?P<gen>\d+) seq=(?P<seq>\d+)")
STATE = re.compile(r"StateMachine: State: (?P<src>\S+) -> (?P<dst>\S+)")
DELIVERY = re.compile(
    r"delivery_id=(?P<sid>[^/\s]+)/epoch-(?P<epoch>\d+)/turn-(?P<turn>\d+)"
    r"/generation-(?P<gen>\d+)/tool-(?P<tool>\d+) event=(?P<event>\w+)"
)
PHASE = re.compile(
    r"interaction_phase from=(?P<src>\S+) to=(?P<dst>\S+) cause=(?P<cause>\S+)"
    r" session_id=(?P<sid>\S+) turn_id=(?P<turn>\d+) generation_id=(?P<gen>\d+)"
)
# ``turn_committed`` carries further key=value pairs (tool_epoch, text_len) between
# generation_id and the end of the line, so only the two keys that matter are pinned.
TURN_COMMITTED = re.compile(r"turn_committed turn_id=(?P<turn>\d+) generation_id=(?P<gen>\d+)\b")


def _device_time(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def _server_time(raw: str) -> datetime:
    # Docker log timestamps carry nanoseconds; datetime keeps microsecond precision.
    head, _, rest = raw.partition(".")
    digits = re.match(r"\d+", rest)
    fraction = digits.group(0)[:6] if digits else ""
    return datetime.fromisoformat(f"{head}.{fraction or '0'}+00:00")


def _seconds(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


def _read(path: Path) -> str:
    return path.read_text(errors="replace") if path.exists() else ""


def device_events(run: Path) -> tuple[list[tuple[datetime, str, str]], list[str]]:
    timeline: list[tuple[datetime, str, str]] = []
    notes: list[str] = []
    for line in _read(run / "serial.log").splitlines():
        stamp = SERIAL_TS.match(line.strip())
        if not stamp:
            continue
        when = _device_time(stamp.group("ts"))
        if match := VAD.search(line):
            timeline.append((when, f"vad.{match.group('edge')}", f"sample={match.group('sample')} rms={match.group('rms')}"))
        elif match := FRAME.search(line):
            timeline.append((when, "frame", f"generation={match.group('gen')}"))
        elif match := STATE.search(line):
            timeline.append((when, "state", f"{match.group('src')} -> {match.group('dst')}"))
    if not timeline:
        notes.append("serial.log has no parseable events")
    return timeline, notes


def server_events(run: Path) -> tuple[dict[tuple[int, int], dict[str, datetime]], list[tuple[datetime, int, int, str, str]], list[str]]:
    chains: dict[tuple[int, int], dict[str, datetime]] = {}
    phases: list[tuple[datetime, int, int, str, str]] = []
    notes: list[str] = []
    for name in ("bridge.log", "agent.log"):
        for line in _read(run / name).splitlines():
            stamp = re.match(r"(?P<ts>\S+Z)\s", line)
            if not stamp:
                continue
            when = _server_time(stamp.group("ts"))
            if match := DELIVERY.search(line):
                key = (int(match.group("turn")), int(match.group("gen")))
                chains.setdefault(key, {})[match.group("event")] = when
            elif match := TURN_COMMITTED.search(line):
                key = (int(match.group("turn")), int(match.group("gen")))
                chains.setdefault(key, {}).setdefault("turn_committed", when)
            elif match := PHASE.search(line):
                phases.append((when, int(match.group("turn")), int(match.group("gen")), match.group("src"), f"{match.group('dst')} ({match.group('cause')})"))
    if not chains:
        notes.append("no media reply delivery events found (capture may lack --server-logs)")
    return chains, phases, notes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    args = parser.parse_args(argv)
    run: Path = args.run

    timeline, device_notes = device_events(run)
    chains, phases, server_notes = server_events(run)
    for note in (*device_notes, *server_notes):
        print(f"note: {note}")

    print("\n== device timeline (local clock, ms)")
    last_vad_end: datetime | None = None
    last_listening: datetime | None = None
    for when, kind, detail in timeline:
        if kind == "vad.end":
            last_vad_end = when
        if kind == "state" and detail.startswith("listening -> speaking"):
            last_listening = when
        gap = ""
        if kind == "frame":
            # A stale mark would silently turn an unrelated earlier event into a
            # flattering latency, so anything beyond a plausible handover is
            # reported as such instead of as a measurement.
            parts = []
            if last_vad_end is not None:
                gap_s = _seconds(when, last_vad_end)
                parts.append(f"said->audible={gap_s:.3f}s" if gap_s <= 3.0 else f"said->audible=stale({gap_s:.3f}s)")
            if last_listening is not None:
                gap_s = _seconds(when, last_listening)
                parts.append(f"listen->speak={gap_s:.3f}s" if gap_s <= 3.0 else f"listen->speak=stale({gap_s:.3f}s)")
            gap = "  " + " ".join(parts) if parts else ""
        print(f"  {when.strftime('%H:%M:%S.%f')[:-3]}  {kind:<9} {detail}{gap}")

    print("\n== server deliveries (UTC, per turn/generation)")
    for turn, gen in sorted(chains):
        marks = chains[(turn, gen)]
        flags = " ".join(name for name in ("turn_committed", "first_frame_sent", "provider_completed", "actual_heard", "playback_ended") if name in marks)
        first = marks.get("first_frame_sent")
        committed = marks.get("turn_committed")
        timing = ""
        if first and committed:
            timing = f"  commit->frame={_seconds(first, committed):.3f}s"
        elif first:
            timing = f"  first_frame={first.strftime('%H:%M:%S.%f')[:-3]}"
        print(f"  turn {turn} gen {gen}: {flags}{timing}")

    print("\n== silence gaps between consecutive generations in one turn (UTC)")
    for turn in sorted({turn for turn, _ in chains}):
        gens = sorted(gen for turn_, gen in chains if turn_ == turn)
        for previous, following in zip(gens, gens[1:], strict=False):
            end = chains[(turn, previous)].get("playback_ended")
            start = chains[(turn, following)].get("first_frame_sent")
            if end and start:
                print(f"  turn {turn} gen {previous}->{following}: {_seconds(start, end):.3f}s")
        print(f"  turn {turn}: {len(gens)} generation(s)")

    print("\n== deliveries that never reached actual_heard (UTC)")
    missing = [key for key, marks in chains.items() if "actual_heard" not in marks]
    print("  none" if not missing else "  " + ", ".join(f"turn {t} gen {g}" for t, g in sorted(missing)))

    last_played = max((marks["playback_ended"] for marks in chains.values() if "playback_ended" in marks), default=None)
    to_idle = [when for when, _turn, _gen, _src, dst in phases if dst.startswith("idle")]
    if last_played and to_idle:
        print("\n== farewell: last playback_ended -> idle")
        for when in to_idle:
            if when > last_played:
                print(f"  {_seconds(when, last_played):.3f}s")
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
