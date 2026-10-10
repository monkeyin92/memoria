#!/usr/bin/env python3
"""Conversation soak driver: the Mac speaker plays the user, the robot answers.

One serial connection is held for the whole run. Device state comes from the serial `StateMachine`
lines; the production bridge and edge logs are followed over ssh for the post-run analysis. Nothing
here touches the device, its configuration or production data: it only talks to the robot through the room.

    # 1. the user's lines in production Doubao voices (macOS `say` is a poor stand-in for a child, and on the
    #    test Mac only the Tingting voice produces audio at all: Flo/Sandy/Shelley write empty files)
    python scripts/voice_soak_bank.py items --scenario scripts/voice_soak_scenarios/child.json --out items.json
    python scripts/voice_soak_bank.py render items.json --out outputs/voice-bank
    # 2. the resident serial logger (the ONLY process that may open the robot's USB port)
    uv run --no-project --with pyserial python scripts/voice_soak_serial_logger.py outputs/serial/robot.log
    # 3. the run
    uv run --no-project --with pyserial python scripts/voice_soak.py \
        --scenario scripts/voice_soak_scenarios/child.json --bank outputs/voice-bank \
        --serial-log outputs/serial/robot.log \
        --out outputs/acceptance/run-<stamp>-soak [--duration-min 35] [--volume 55] \
        [--wake-socket PATH] [--post-scenario-wait 90]

The robot is woken over the USB cable (firmware build 20): the driver sends `wake` through the logger's
command socket and the robot starts a conversation exactly like a tap on the round screen, under the same
gate (the phone's wake mode must include the screen). The wake word is never played, because it
false-triggers; if the command cannot be sent, or the robot refuses it, the step fails.

Without --serial-log the driver opens the port itself and writes `wake` into it. Opening the serial port may
reset the board (`rst:0x15 USB_UART_CHIP_RESET`, seen once in four opens); the driver notices the ROM banner and
waits for the wake word engine. A clip shorter than half a second is refused, because a silent "user" makes
every later number meaningless.

The Mac speaker must be near the robot; the output volume is raised for the run and restored.
Device sessions only start outside the guardian's quiet hours (04:00-07:00 at the time of writing)
and end at the signed session limit (30 minutes), so a long run re-wakes the robot over USB.
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from voice_soak_evidence import assess_interruption  # noqa: E402
from voice_soak_serial_command import SerialCommandError, send_command  # noqa: E402

STATE_RE = re.compile(r"StateMachine: State: (\w+) -> (\w+)")
USB_WAKE_RE = re.compile(r"usb wake (accepted|ignored)(?: reason=(\w+))?")
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
    state_source: str = "unknown"
    events: list[tuple[float, str, str]] = field(default_factory=list)  # (t, kind, payload)
    lines_seen: int = 0
    booted_at: float | None = None  # set when the ROM banner shows up: opening the port reset the board
    cond: threading.Condition = field(default_factory=threading.Condition)
    stop: threading.Event = field(default_factory=threading.Event)
    ser: serial.Serial | None = None  # the port this watcher opened itself (not in the log-tail mode)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        ser = serial.Serial(self.port, 115200, timeout=0.2, write_timeout=2.0)
        self.ser = ser
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
            self.lines_seen += 1
            if "ESP-ROM" in line:
                self.booted_at = t
            if match:
                self.state = match.group(2)
                self.state_since = t
                self.state_source = "serial_transition"
                self.events.append((t, "state", f"{match.group(1)}->{match.group(2)}"))
            elif (usb := USB_WAKE_RE.search(line)) is not None:
                self.events.append((t, "usb_wake", usb.group(1) + (f" {usb.group(2)}" if usb.group(2) else "")))
            elif "Wake word detected" in line or "screen tap" in line or "screen off" in line or "screen on" in line:
                self.events.append((t, "note", line.split(") ", 1)[-1][:120]))
            elif re.search(r"\b(E|W) \(\d+\)", line):
                self.events.append((t, "log", line[:160]))
            self.cond.notify_all()

    def alive(self) -> bool:
        with self.cond:
            return bool(self.lines_seen)

    def wake(self) -> None:
        """Wake the idle robot over USB, like a tap on the screen. Never by playing the wake word."""
        if self.ser is None:
            raise SerialCommandError("the serial port is not open yet")
        try:
            self.ser.write(b"wake\n")
            self.ser.flush()
        except (serial.SerialException, OSError) as exc:
            raise SerialCommandError(f"serial write failed: {exc}") from exc

    def wake_refusal(self, since: float, timeout: float = 0.0) -> str | None:
        """The reason the robot logged for refusing a USB wake after `since` (`not_idle`, `wake_mode`,
        `pairing`, `starting`), or None when it did not refuse (yet)."""
        deadline = now() + timeout
        with self.cond:
            while True:
                for t, kind, payload in self.events:
                    if kind == "usb_wake" and t >= since and payload.startswith("ignored"):
                        return payload.partition("ignored")[2].strip() or "ignored"
                remaining = deadline - now()
                if remaining <= 0:
                    return None
                self.cond.wait(min(remaining, 0.25))

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

    def wait_state_event(
        self, states: set[str], timeout: float, since: float
    ) -> float | None:
        """Return the first matching state transition after `since`, even if it has ended."""
        deadline = now() + timeout
        with self.cond:
            while True:
                for t, kind, payload in self.events:
                    if kind == "state" and t >= since and payload.rsplit("->", 1)[-1] in states:
                        return t
                remaining = deadline - now()
                if remaining <= 0:
                    return None
                self.cond.wait(min(remaining, 0.25))

    def observe_post_scenario(self, timeout: float) -> dict[str, object]:
        """Keep collecting serial events for a bounded tail and snapshot the final state."""
        started_at = now()
        started_monotonic = time.monotonic()
        deadline = started_monotonic + timeout
        with self.cond:
            while (remaining := deadline - time.monotonic()) > 0:
                self.cond.wait(min(remaining, 0.25))

            final_state = self.state
            final_state_since = self.state_since
            state_source = self.state_source

        idle_observed = (
            final_state == "idle"
            and state_source == "serial_transition"
            and final_state_since >= started_at
        )
        verdict = (
            "timeout"
            if final_state != "idle"
            else "verified_idle" if idle_observed else "evidence_insufficient"
        )

        return {
            "idle_observed": idle_observed,
            "final_state": final_state,
            "final_state_source": state_source,
            "final_state_since": stamp(final_state_since),
            "waited_s": round(time.monotonic() - started_monotonic, 2),
            "timeout_s": timeout,
            "verdict": verdict,
            "timed_out": verdict == "timeout",
        }


MIN_CLIP_SECONDS = 0.5
DEFAULT_POST_SCENARIO_WAIT_S = 90.0
MAX_POST_SCENARIO_WAIT_S = 180.0


class LogTailWatcher(SerialWatcher):
    """Follow the log of `voice_soak_serial_logger.py` instead of opening the port (an open resets the board)."""

    source: Path = Path("/dev/null")
    wake_socket: Path | None = None  # the logger's command socket; default derived from the port

    def wake(self) -> None:
        send_command("wake", socket_path=self.wake_socket, port=self.port)

    def _run(self) -> None:
        line_re = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d\d\d) (.*)$")
        with self.source.open("rb") as src, self.path.open("ab") as out:
            src.seek(0, 2)  # only what happens from now on
            buf = b""
            while not self.stop.is_set():
                chunk = src.read(65536)
                if not chunk:
                    time.sleep(0.1)
                    continue
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    text = raw.decode("utf-8", "replace")
                    match = line_re.match(text)
                    t = now()
                    if match:
                        hh, mm, ss, ms, text = int(match[1]), int(match[2]), int(match[3]), int(match[4]), match[5]
                        local = time.localtime(t)
                        t = time.mktime(local[:3] + (hh, mm, ss) + local[6:]) + ms / 1000
                    out.write(f"{stamp(t)} {text}\n".encode())
                    out.flush()
                    self._parse(t, text)


def clip_seconds(path: Path) -> float:
    info = subprocess.run(["afinfo", str(path)], capture_output=True, text=True).stdout
    match = re.search(r"estimated duration: ([0-9.]+)", info)
    return float(match.group(1)) if match else 0.0


def play_clip(path: Path) -> tuple[float, float, float]:
    """Play a prepared clip. A clip shorter than half a second is a failed synthesis, not a short word."""
    clip = clip_seconds(path)
    if clip < MIN_CLIP_SECONDS:
        raise RuntimeError(f"{path.name} is {clip:.2f}s long: refusing to play a silent clip")
    start = now()
    subprocess.run(["afplay", str(path)], check=True)
    return start, now(), clip


def say(text: str, voice: str, rate: int, workdir: Path, tag: str) -> tuple[float, float, float]:
    """Synthesize with macOS `say`, then play. Only some voices work: on the test Mac Tingting does, while
    Flo/Sandy/Shelley/Grandma/... write EMPTY files (0.01 s) with `-o`; play_clip refuses those."""
    path = workdir / f"{tag}.aiff"
    subprocess.run(["say", "-v", voice, "-r", str(rate), "-o", str(path), text], check=True)
    return play_clip(path)


def speak(text: str, rate: int, workdir: Path, tag: str, bank: Path | None, key: str | None) -> tuple[float, float, float]:
    """The user's line: a clip from the voice bank (production Doubao voices) when there is one for this key,
    otherwise macOS Tingting."""
    if bank is not None and key and (bank / f"{key}.wav").exists():
        return play_clip(bank / f"{key}.wav")
    return say(text, "Tingting", rate, workdir, tag)


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
    ap.add_argument("--bank", default=None, help="directory of <step tag>.wav clips (voice bank) for the user's lines")
    ap.add_argument("--loops", type=int, default=1, help="repeat the scenario list this many times")
    ap.add_argument(
        "--post-scenario-wait",
        type=float,
        default=DEFAULT_POST_SCENARIO_WAIT_S,
        help="seconds to keep collecting logs after the scenario (0-180; default 90)",
    )
    ap.add_argument("--port", default=DEFAULT_PORT, help="the robot's USB serial port")
    ap.add_argument("--serial-log", default=None, help="follow this log (voice_soak_serial_logger.py) instead of opening the port")
    ap.add_argument("--wake-socket", default=None, help="the serial logger's command socket (default: derived from --port)")
    ap.add_argument("--remote", default=DEFAULT_REMOTE, help="ssh host that runs the production containers")
    args = ap.parse_args()
    if not 0 <= args.post_scenario_wait <= MAX_POST_SCENARIO_WAIT_S:
        ap.error("--post-scenario-wait must be between 0 and 180 seconds")

    out = Path(args.out)
    bank = Path(args.bank) if args.bank else None
    out.mkdir(parents=True, exist_ok=False)
    steps = json.loads(Path(args.scenario).read_text(encoding="utf-8"))
    deadline = now() + args.duration_min * 60
    timeline = (out / "timeline.jsonl").open("w", encoding="utf-8")

    def record(**row: object) -> None:
        row["t"] = stamp(now())
        row["ts"] = round(now(), 3)  # epoch seconds: scripts/voice_soak_pause_split.py report joins on it
        timeline.write(json.dumps(row, ensure_ascii=False) + "\n")
        timeline.flush()
        print(row, flush=True)

    if args.serial_log:
        watcher: SerialWatcher = LogTailWatcher(out / "serial.log", port=args.port)
        watcher.source = Path(args.serial_log)
        if args.wake_socket:
            watcher.wake_socket = Path(args.wake_socket)
    else:
        watcher = SerialWatcher(out / "serial.log", port=args.port)
    watcher.start()
    log_procs = follow_logs(out, args.remote)
    previous_volume = set_volume(args.volume)
    record(kind="start", volume=args.volume, previous_volume=previous_volume, duration_min=args.duration_min)
    try:
        # Opening the port does not always reset the board. A board that was already idle prints no
        # state line, only its periodic logs, so after a short wait trust "serial is alive and the
        # display-profile poll is running" (it only runs while idle) instead of waiting for a line.
        if watcher.wait_state({"idle", "listening"}, 15) is None:
            if watcher.alive():
                with watcher.cond:
                    watcher.state = "idle"
                    watcher.state_since = now()
                    watcher.state_source = "assumed_idle"
                record(kind="assume_idle", reason="no state line after the port open, serial is alive")
            elif watcher.wait_state({"idle", "listening"}, 75) is None:
                record(kind="abort", reason="device never reached idle after the port open")
                return 2
        if watcher.booted_at is not None:
            # A board reset by the port open prints idle ~11 s after the ROM banner, but the wake word engine and
            # the first display-profile poll finish ~5 s later; speaking before that is lost.
            settle = watcher.booted_at + 20.0 - now()
            if settle > 0:
                record(kind="boot_settle", seconds=round(settle, 1))
                time.sleep(settle)
        time.sleep(2.0)

        def ensure_listening(tag: str) -> bool:
            for attempt in range(5):
                if watcher.state == "listening":
                    return True
                if watcher.state == "idle":
                    # Over the USB cable, like a tap on the screen. The wake word is never played: it
                    # false-triggers, so a failed or refused wake fails the step instead of falling back.
                    t0 = now()
                    try:
                        watcher.wake()
                    except SerialCommandError as exc:
                        record(kind="wake_failed", attempt=attempt, tag=tag, error=str(exc))
                        return False
                    record(kind="wake_try", attempt=attempt, tag=tag, via="usb")
                    got = watcher.wait_state({"connecting", "listening"}, 9, since=t0)
                    if got is None:
                        refused = watcher.wake_refusal(t0)
                        if refused is not None:
                            record(kind="wake_refused", attempt=attempt, tag=tag, reason=refused)
                            if refused in {"wake_mode", "pairing", "starting"}:
                                return False  # retrying cannot help: the phone's mode, pairing or start-up
                    if got is not None:
                        listening_at = watcher.wait_state_event(
                            {"listening"}, 12.0, since=t0
                        )
                        if listening_at is None:
                            continue
                        # The robot greets on wake ("我在。", or the goodnight phrase in quiet
                        # hours). State events are retained because the greeting may start after
                        # listening, and can even finish before this waiter is scheduled.
                        greeted = watcher.wait_state_event(
                            {"speaking"}, 8.0, since=listening_at
                        )
                        if greeted is not None:
                            record(kind="greeting_started", started=stamp(greeted))
                            finished = watcher.wait_state({"listening", "idle"}, 25, since=greeted)
                            record(
                                kind="greeting_done",
                                finished=stamp(finished) if finished is not None else None,
                                state=watcher.state,
                            )
                        else:
                            record(kind="greeting_absent", state=watcher.state)
                        time.sleep(0.8)
                        record(kind="woke", attempt=attempt, state=watcher.state)
                        return watcher.state == "listening"
                else:
                    watcher.wait_state({"listening", "idle"}, 30)
            return watcher.state == "listening"

        turn = 0
        deadline_reached = False
        for _loop in range(args.loops):
            for step in steps:
                if now() >= deadline:
                    record(kind="deadline")
                    deadline_reached = True
                    break
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
                t0, t1, clip = speak(step["say"], int(step.get("rate", 175)), out, tag, bank, step.get("tag"))
                record(kind="said", tag=tag, text=step["say"], voice="bank" if bank is not None and (bank / f"{step.get('tag')}.wav").exists() else "Tingting",
                       rate=step.get("rate", 175), clip_s=round(clip, 2), start=stamp(t0), end=stamp(t1))
                started = watcher.wait_state({"speaking"}, float(step.get("reply_timeout", 30)), since=t1 - 0.5)
                if started is None:
                    record(kind="no_reply", tag=tag, state=watcher.state)
                    continue
                latency = started - t1
                interrupt = step.get("interrupt")
                outcome = {"kind": "reply", "tag": tag, "latency_s": round(latency, 2),
                           "reply_started_ts": started,
                           "serial_utc_offset_s": -time.altzone if time.localtime().tm_isdst > 0 else -time.timezone}
                if interrupt:
                    time.sleep(float(interrupt.get("after", 5)))
                    if watcher.state == "speaking":
                        i0, i1, _ = speak(interrupt["say"], int(interrupt.get("rate", 175)), out, tag + "-stop",
                                          bank, step.get("tag", "") + "-stop")
                        settle = float(interrupt.get("settle", 8))
                        returned = watcher.wait_state({"listening", "idle"}, settle, since=i0)
                        outcome.update(interrupted=True, interrupt_text=interrupt["say"],
                                       interrupt_start_ts=i0, interrupt_end_ts=i1,
                                       interrupt_deadline_ts=i1 + settle, device_returned_ts=returned)
                    else:
                        outcome.update(interrupted=False, note="reply ended before the interrupt")
                ended = watcher.wait_state({"listening", "idle"}, float(step.get("end_timeout", 150)), since=started)
                outcome.update(reply_s=round(ended - started, 2) if ended else None, end_state=watcher.state)
                if outcome.get("interrupted"):
                    outcome.update(assess_interruption(
                        (out / "bridge.log").read_text(encoding="utf-8", errors="replace"),
                        (out / "serial.log").read_text(encoding="utf-8", errors="replace"),
                        outcome,
                    ))
                record(**outcome)
                time.sleep(float(step.get("gap", 2.0)))
            if deadline_reached:
                break
        record(kind="scenario_done", turns=turn, deadline_reached=deadline_reached)
        post_scenario = watcher.observe_post_scenario(args.post_scenario_wait)
        record(kind="post_scenario_observation", **post_scenario)
        if post_scenario["verdict"] == "timeout":
            return 3
        if post_scenario["verdict"] == "evidence_insufficient":
            return 4
        return 0
    finally:
        set_volume(previous_volume)
        watcher.stop.set()
        for proc in log_procs:
            proc.terminate()
        timeline.close()


if __name__ == "__main__":
    sys.exit(main())
