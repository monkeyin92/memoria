#!/usr/bin/env python3
"""Conversation soak driver: the Mac speaker plays the user, the robot answers.

One serial connection is held for the whole run (opening the port resets the board once, at
the start). Device state comes from the serial `StateMachine` lines; the production bridge and
edge logs are followed over ssh for the post-run analysis. Nothing here touches the device,
its configuration or production data: it only talks to the robot through the room.

    uv run --no-project --with pyserial python scripts/voice_soak.py \
        --scenario scripts/voice_soak_scenarios/child.json --out outputs/acceptance/run-<stamp>-soak \
        [--duration-min 35] [--volume 55] [--port /dev/cu.usbmodem2101]

The Mac speaker must be near the robot; the output volume is raised for the run and restored.
Device sessions only start outside the guardian's quiet hours (04:00-07:00 at the time of writing)
and end at the signed session limit (30 minutes), so a long run re-wakes the robot by voice.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import serial  # pyserial, from the ESP-IDF python env

STATE_RE = re.compile(r"StateMachine: State: (\w+) -> (\w+)")
DEFAULT_PORT = "/dev/cu.usbmodem2101"
DEFAULT_REMOTE = "memoria-prod"


def now() -> float:
    return time.time()


def stamp(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


@dataclass
class SerialWatcher:
    path: Path
    port: str = DEFAULT_PORT
    state: str = "unknown"
    state_since: float = field(default_factory=now)
    events: list[tuple[float, str, str]] = field(default_factory=list)  # (t, kind, payload)
    cond: threading.Condition = field(default_factory=threading.Condition)
    stop: threading.Event = field(default_factory=threading.Event)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        ser = serial.Serial(self.port, 115200, timeout=0.2)
        buf = b""
        with self.path.open("ab") as fh:
            while not self.stop.is_set():
                chunk = ser.read(4096)
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    t = now()
                    line = raw.decode("utf-8", "replace").rstrip("\r")
                    fh.write(f"{stamp(t)} {line}\n".encode())
                    fh.flush()
                    self._parse(t, line)
        ser.close()

    def _parse(self, t: float, line: str) -> None:
        match = STATE_RE.search(line)
        with self.cond:
            if match:
                self.state = match.group(2)
                self.state_since = t
                self.events.append((t, "state", f"{match.group(1)}->{match.group(2)}"))
            elif "Wake word detected" in line or "screen tap" in line or "screen off" in line or "screen on" in line:
                self.events.append((t, "note", line.split(") ", 1)[-1][:120]))
            elif re.search(r"\b(E|W) \(\d+\)", line):
                self.events.append((t, "log", line[:160]))
            self.cond.notify_all()

    def wait_state(self, states: set[str], timeout: float, since: float | None = None) -> float | None:
        """Return the time we were in one of `states` (after `since`), or None on timeout."""
        deadline = now() + timeout
        with self.cond:
            while True:
                if self.state in states and (since is None or self.state_since >= since):
                    return self.state_since
                remaining = deadline - now()
                if remaining <= 0:
                    return None
                self.cond.wait(min(remaining, 0.25))


def say(text: str, voice: str, rate: int, workdir: Path, tag: str) -> tuple[float, float, float]:
    """Synthesize then play. Returns (start, end, clip_seconds)."""
    path = workdir / f"{tag}.aiff"
    subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", str(path), text], check=True)
    info = subprocess.run(["afinfo", str(path)], capture_output=True, text=True).stdout
    match = re.search(r"estimated duration: ([0-9.]+)", info)
    clip = float(match.group(1)) if match else 0.0
    start = now()
    subprocess.run(["afplay", str(path)], check=True)
    return start, now(), clip


def set_volume(level: int) -> int:
    previous = int(subprocess.run(["osascript", "-e", "output volume of (get volume settings)"],
                                  capture_output=True, text=True).stdout.strip() or 0)
    subprocess.run(["osascript", "-e", f"set volume output volume {level}"], check=True)
    return previous


def follow_logs(out: Path, remote: str) -> list[subprocess.Popen[bytes]]:
    procs = []
    for name, container in (("bridge", "memoria-voice-core-media-bridge-1"),
                            ("edge", "memoria-media-edge-1"),
                            ("control", "memoria-control-api-1")):
        fh = (out / f"{name}.log").open("wb")
        procs.append(subprocess.Popen(
            ["ssh", remote, f"docker logs -f -t --since 0s {container} 2>&1"],
            stdout=fh, stderr=subprocess.STDOUT))
    return procs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--duration-min", type=float, default=35.0)
    ap.add_argument("--volume", type=int, default=55)
    ap.add_argument("--wake-voice", default="Tingting")
    ap.add_argument("--loops", type=int, default=1, help="repeat the scenario list this many times")
    ap.add_argument("--port", default=DEFAULT_PORT, help="the robot's USB serial port")
    ap.add_argument("--remote", default=DEFAULT_REMOTE, help="ssh host that runs the production containers")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=False)
    steps = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    deadline = now() + args.duration_min * 60
    timeline = (out / "timeline.jsonl").open("w", encoding="utf-8")

    def record(**row: object) -> None:
        row["t"] = stamp(now())
        timeline.write(json.dumps(row, ensure_ascii=False) + "\n")
        timeline.flush()
        print(row, flush=True)

    watcher = SerialWatcher(out / "serial.log", port=args.port)
    watcher.start()
    log_procs = follow_logs(out, args.remote)
    previous_volume = set_volume(args.volume)
    record(kind="start", volume=args.volume, previous_volume=previous_volume, duration_min=args.duration_min)
    try:
        # The open reset the board: wait for it to settle into idle.
        if watcher.wait_state({"idle", "listening"}, 90) is None:
            record(kind="abort", reason="device never reached idle after the port open")
            return 2
        time.sleep(2.0)

        def ensure_listening(tag: str) -> bool:
            for attempt in range(5):
                if watcher.state == "listening":
                    return True
                if watcher.state == "idle":
                    t0, t1, _ = say("茉莉", args.wake_voice, 150 + 10 * attempt, out, f"{tag}-wake{attempt}")
                    record(kind="wake_try", attempt=attempt, tag=tag)
                    got = watcher.wait_state({"connecting", "listening"}, 9, since=t0)
                    if got is not None:
                        watcher.wait_state({"listening"}, 12, since=t0)
                        time.sleep(0.8)
                        record(kind="woke", attempt=attempt, state=watcher.state)
                        return watcher.state == "listening"
                else:
                    watcher.wait_state({"listening", "idle"}, 30)
            return watcher.state == "listening"

        turn = 0
        for _loop in range(args.loops):
            for step in steps:
                if now() >= deadline:
                    record(kind="deadline")
                    return 0
                turn += 1
                tag = f"t{turn:03d}-{step.get('tag', 'x')}"
                if step.get("pause"):
                    time.sleep(float(step["pause"]))
                    continue
                if step.get("expect_idle_wait"):
                    # e.g. stay silent and watch the owner-silence timeout / standby
                    t0 = now()
                    got = watcher.wait_state({"idle"}, float(step["expect_idle_wait"]))
                    record(kind="silence", tag=tag, went_idle=got is not None, after_s=round(now() - t0, 1))
                    continue
                if not ensure_listening(tag):
                    record(kind="step_failed", tag=tag, reason="could not wake the device", state=watcher.state)
                    continue
                # keep a gap after the last playback so the room echo is gone
                quiet_for = now() - watcher.state_since
                if quiet_for < 1.5:
                    time.sleep(1.5 - quiet_for)
                t0, t1, clip = say(step["say"], step.get("voice", "Tingting"), int(step.get("rate", 175)), out, tag)
                record(kind="said", tag=tag, text=step["say"], voice=step.get("voice", "Tingting"),
                       rate=step.get("rate", 175), clip_s=round(clip, 2), start=stamp(t0), end=stamp(t1))
                started = watcher.wait_state({"speaking"}, float(step.get("reply_timeout", 30)), since=t1 - 0.5)
                if started is None:
                    record(kind="no_reply", tag=tag, state=watcher.state)
                    continue
                latency = started - t1
                interrupt = step.get("interrupt")
                outcome = {"kind": "reply", "tag": tag, "latency_s": round(latency, 2)}
                if interrupt:
                    time.sleep(float(interrupt.get("after", 5)))
                    if watcher.state == "speaking":
                        i0, i1, _ = say(interrupt["say"], interrupt.get("voice", "Tingting"),
                                        int(interrupt.get("rate", 175)), out, tag + "-stop")
                        stopped = watcher.wait_state({"listening", "idle"}, float(interrupt.get("settle", 8)), since=i0)
                        outcome.update(interrupted=True, interrupt_text=interrupt["say"],
                                       stopped=stopped is not None,
                                       stop_delay_s=round(stopped - i1, 2) if stopped else None)
                    else:
                        outcome.update(interrupted=False, note="reply ended before the interrupt")
                ended = watcher.wait_state({"listening", "idle"}, float(step.get("end_timeout", 150)), since=started)
                outcome.update(reply_s=round(ended - started, 2) if ended else None, end_state=watcher.state)
                record(**outcome)
                time.sleep(float(step.get("gap", 2.0)))
        record(kind="scenario_done", turns=turn)
        return 0
    finally:
        set_volume(previous_volume)
        watcher.stop.set()
        for proc in log_procs:
            proc.terminate()
        timeline.close()


if __name__ == "__main__":
    sys.exit(main())
