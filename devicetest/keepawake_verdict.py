#!/usr/bin/env python3
"""Re-judge a BUILD=26 keep-awake soak.

Silence steps: agent log (bridge.log) says listening_closed inside the window; informational unless
--gate-silence. Tail: serial screen off / -> idle, or listening_closed.

    python3 devicetest/keepawake_verdict.py RUN_DIR [--serial FILE] [--summary summary.json]
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

STANDBY = re.compile(r"MemoriaMascot: screen off \(idle\)|wifi hold suppressed MEMORIA_KEEP_AWAKE")
SESSION_END = re.compile(r"StateMachine: State: \w+ -> idle|session\.close|session close")
LISTENING_CLOSED = re.compile(r"listening[ _]closed")
ISO_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z ")
LINE_TS = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d{3}) ")


def marker_times(serial: Path, day: datetime):
    hits = []
    for line in serial.read_text(encoding="utf-8", errors="replace").splitlines():
        m = LINE_TS.match(line)
        if m and (STANDBY.search(line) or SESSION_END.search(line)):
            h, mi, s, ms = map(int, m.groups())
            hits.append((day.replace(hour=h, minute=mi, second=s, microsecond=ms * 1000).timestamp(), line.strip()))
    return hits


def closed_times(bridge: Path):
    """Agent log lines (UTC ISO, e.g. 2026-10-29T12:13:14.328Z) that mention listening_closed."""

    hits = []
    for line in bridge.read_text(encoding="utf-8", errors="replace").splitlines():
        m = ISO_TS.match(line)
        if m and LISTENING_CLOSED.search(line):
            t = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=UTC
            ).timestamp()
            hits.append((t + float(m.group(2) or 0), line.strip()[:300]))
    return hits


def found(hits, start, end):
    return next((line for t, line in hits if start <= t <= end), None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run", type=Path)
    ap.add_argument("--serial", type=Path)
    ap.add_argument("--summary", type=Path)
    ap.add_argument("--bridge", type=Path)
    ap.add_argument("--gate-silence", action="store_true",
                    help="make silence-step listening_closed checks fail the run (default: informational)")
    ap.add_argument("--max-no-reply", default="0")
    ap.add_argument("--max-reply-latency-p90", default="2.0")
    ap.add_argument("--max-reply-latency-max", default="3.0")
    a = ap.parse_args()
    serial = a.serial or a.run / "serial.log"
    out = subprocess.run([sys.executable, "scripts/voice_soak_verdict.py", str(a.run),
                          "--max-no-reply", a.max_no_reply,
                          "--max-reply-latency-p90", a.max_reply_latency_p90,
                          "--max-reply-latency-max", a.max_reply_latency_max],
                         capture_output=True, text=True).stdout
    base = json.loads([line for line in out.splitlines() if line.startswith("{")][-1])
    rows = [
        json.loads(line)
        for line in (a.run / "timeline.jsonl").read_text().splitlines()
        if line.strip()
    ]
    day = datetime.fromtimestamp(rows[0]["ts"])
    hits = marker_times(serial, day) if serial.is_file() else None

    bridge = a.bridge or a.run / "bridge.log"
    closed = closed_times(bridge) if bridge.is_file() else None
    failures, checks, informational = [], [], []
    for f in base["failures"]:
        if not (f.startswith("silence_not_idle:") or f.startswith("tail_idle:")):
            failures.append(f)
    for r in rows:
        if r.get("kind") == "silence":
            tag = r.get("tag")
            win = (r["ts"] - r["after_s"], r["ts"])
            hit = None if closed is None else found(closed, *win)
            first = next((ln for t, ln in (closed or []) if t >= win[0]), None)
            c = {"step": tag, "window": [datetime.fromtimestamp(t).strftime("%H:%M:%S") for t in win],
                 "listening_closed": hit, "first_listening_closed_after_start": first}
            checks.append(c)
            if hit is None:
                msg = f"keepawake_no_listening_closed:{tag}" + (" (no bridge.log)" if closed is None else "")
                (failures if a.gate_silence else informational).append(msg)
        elif r.get("kind") == "post_scenario_observation":
            win = (r["ts"] - r["waited_s"], r["ts"])
            hit = None if hits is None else found(hits, *win)
            hit = hit or (None if closed is None else found(closed, *win))
            checks.append({"step": "tail", "window": [datetime.fromtimestamp(t).strftime("%H:%M:%S") for t in win],
                           "marker": hit})
            if hit is None:
                failures.append("keepawake_no_standby_or_end: tail" + (" (no serial log)" if hits is None else ""))
    if not any(c["step"] == "tail" for c in checks):
        failures.append("keepawake_no_standby_or_end: tail missing")

    result = {"passed": not failures, "result": "PASS" if not failures else "FAIL", "failures": failures,
              "informational": informational, "silence_gated": a.gate_silence, "keepawake_checks": checks, "serial_log": str(serial),
              "rules": {"standby": STANDBY.pattern, "session_end": SESSION_END.pattern, "listening_closed": "bridge.log line containing listening_closed"}, "base_verdict": base}
    print(json.dumps(result, ensure_ascii=False))
    if a.summary:
        s = json.loads(a.summary.read_text()) if a.summary.is_file() else {}
        s["keepawake_verdict"] = result
        s["final"] = result["result"]
        a.summary.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
