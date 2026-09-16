"""Count device wake-word recall and false wakes with a reproducible receipt.

P2-05: the wake word runs on the device (WakeNet), so its recall/false-wake counts
are firmware-side evidence and do not depend on which server candidate is
deployed.  This harness drives the acoustic side (computer speaker), watches the
device console over serial for the authoritative wake markers, and records a
receipt with the stimulus timeline (ground truth), the tested input range, the
identities it actually read this run, and the counts.

Console markers (ESP-VoCat firmware, variable names come from the firmware logs):

* ``Application: Wake word detected: 茉莉 (state: N)``  -- a wake
* ``CustomWakeWord: Custom wake word detected: ... prob=...`` -- detector detail
* ``StateMachine: State: idle -> connecting``  -- wake started a session
* ``StateMachine: State: activating -> idle``  -- booted into standby
* ``StateMachine: State: listening -> idle``  -- session closed, standby again

Measured on 2026-09-16 (ESP-VoCat, app 2.4.2, `/dev/cu.usbmodem101`):

* synthetic TTS DOES wake this device once the stimulus is padded with silence
  (0.25 s lead / 0.4 s tail) and a working zh voice is used -- the earlier
  "TTS never wakes it" runs played an unpadded render;
* the first ~4 attempts after boot do not wake it even though the same audio is
  verified at the microphone: the device needs roughly 45-50 s after
  ``activating -> idle`` before WakeNet answers, so ``--warmup-s`` must be spent
  before a matrix is counted;
* each wake is logged twice (the wake line carries ``(state: N)``, a duplicate
  follows when the session connects), so window counts must use the wake line
  only or every wake is counted twice;
* ``say`` on this host silently renders ~11 ms of silence for Flo/Eddy/Grandma,
  so a stimulus is validated (duration/RMS) before it is ever played.

Honest boundaries, recorded in the receipt rather than glossed over:

* the stimulus is synthetic TTS played through the computer speaker, NOT a human
  speaker: a voice/timbre effect on recall is possible, so recall numbers are not
  comparable with human-operator counts;
* physical distance/angle is not varied or verified -- the "far" condition is a
  playback-gain ladder, and the microphone level is a co-located proxy, not what
  the device hears;
* the flash receipt is evidence about the flash that was performed; the on-board
  identity read this run is the boot banner (app version + ELF SHA prefix), which
  is recorded separately and is not the full app-image hash;
* no production logs are read, so session/epoch ids are not part of this receipt.

Run it through uv so no project dependency changes:

    uv run --no-project --with pyserial --with sounddevice --with numpy \
        scripts/wake_word_matrix.py --out /tmp/wake-matrix-1 \
        --firmware-receipt outputs/acceptance/run-.../postflash.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
import threading
import time
import wave
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_PORT = "/dev/cu.usbmodem101"
DEFAULT_BAUDRATE = 460800
WAKE_MARKER = "Application: Wake word detected"
#: The wake line carries the device state; the firmware logs the phrase again a
#: second time once the session connects.  Window counts must use this one, or
#: every single wake is counted twice.
WAKE_EVENT_MARKER = "Application: Wake word detected: 茉莉 (state:"
DETECTOR_MARKER = "CustomWakeWord: Custom wake word detected"
IDLE_MARKER = "StateMachine: State: activating -> idle"
SESSION_MARKER = "StateMachine: State: idle -> connecting"
STANDBY_MARKER = "StateMachine: State: listening -> idle"

_WAKE_PHRASE = "茉莉"
#: A TV-like passage and a second speaker: neither contains the wake phrase.
_TV_PASSAGE = (
    "今天的天气不错，我们一起看一下下午的新闻。下午三点有一场会议，"
    "请记得提前十分钟进入会议室。晚饭后可以去楼下走一走，顺便把快递取回来。"
)
_SMALL_TALK = (
    "我昨天在超市买了点水果，苹果和橘子都挺新鲜的。你那边最近忙不忙，"
    "周末要不要一起吃饭？我听说新开的那家店味道还可以。"
)


@dataclass
class ConsoleLine:
    monotonic: float
    iso: str
    text: str


@dataclass
class Stimulus:
    label: str
    voice: str
    text: str
    path: str
    sha256: str
    duration_s: float
    rms_dbfs: float
    peak_dbfs: float
    level_at_mic_dbfs: float | None = None


@dataclass
class Trial:
    phase: str
    gain: float
    voice: str
    started_monotonic: float
    started_iso: str
    finished_monotonic: float
    outcome: str
    latency_s: float | None = None
    detector_prob: float | None = None
    wake_line: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Window:
    label: str
    gain: float
    started_monotonic: float
    started_iso: str
    finished_monotonic: float
    wakes: list[dict[str, Any]]


class ConsoleReader(threading.Thread):
    """Timestamp every console line; the log is the wake evidence."""

    def __init__(self, port: Any, log_path: Path) -> None:
        super().__init__(name="wake-matrix-console", daemon=True)
        self._port = port
        self._log_path = log_path
        self._stopping = threading.Event()
        self.lines: list[ConsoleLine] = []
        self._lock = threading.Lock()
        self.read_errors = 0

    def run(self) -> None:
        with self._log_path.open("w", encoding="utf-8") as log:
            while not self._stopping.is_set():
                try:
                    raw = self._port.readline()
                except Exception:  # noqa: BLE001 - a serial fault is recorded, not fatal
                    self.read_errors += 1
                    time.sleep(0.2)
                    continue
                if not raw:
                    continue
                monotonic = time.monotonic()
                iso = datetime.now().astimezone().isoformat(timespec="milliseconds")
                text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                line = ConsoleLine(monotonic=monotonic, iso=iso, text=text)
                with self._lock:
                    self.lines.append(line)
                log.write(f"[{iso}] {text}\n")
                log.flush()

    def stop(self) -> None:
        self._stopping.set()
        self.join(timeout=5)

    def snapshot(self, since: float | None = None) -> list[ConsoleLine]:
        with self._lock:
            lines = list(self.lines)
        if since is None:
            return lines
        return [line for line in lines if line.monotonic >= since]

    def wait_for(
        self,
        needle: str,
        *,
        since: float,
        timeout: float,
    ) -> ConsoleLine | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for line in self.snapshot(since=since):
                if needle in line.text:
                    return line
            time.sleep(0.1)
        return None

    def is_idle(self, *, settle_s: float = 1.5) -> bool:
        """Standby means the last state transition was into idle.

        ``settle_s`` guards the race right after a wake: the ``idle -> connecting``
        line takes a moment to appear, so an immediate re-read would still see the
        previous ``-> idle`` and play into a session that is starting.
        """

        transitions = [line for line in self.snapshot() if "StateMachine: State:" in line.text]
        if not transitions:
            return False
        last = transitions[-1]
        if not last.text.rstrip().endswith("-> idle"):
            return False
        return (time.monotonic() - last.monotonic) >= settle_s


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _synth(text: str, voice: str, target: Path) -> None:
    """Synthesize and REFUSE an empty render.

    Some macOS voices silently produce an ~11 ms silent file (Flo/Eddy/Grandma on
    this host); a harness that trusts ``say`` would then report a bogus recall row.
    Tingting/Meijia have been the usable zh voices.
    """

    interim = target.with_suffix(".aiff")
    subprocess.run(
        ["say", "-v", voice, "-o", str(interim), text],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@22050", str(interim), str(target)],
        check=True,
        capture_output=True,
    )
    interim.unlink(missing_ok=True)
    _pad_wav(target)
    duration, rms, _ = _wav_levels(target)
    if duration < 0.25 or rms < -60.0:
        raise SystemExit(
            f"voice {voice!r} produced an unusable stimulus ({duration:.3f}s, {rms:.1f} dBFS)"
        )


def _pad_wav(path: Path, *, lead_s: float = 0.25, tail_s: float = 0.4) -> None:
    """Pad a stimulus with silence so the detector sees a clean onset and decay.

    TTS renders start and stop abruptly; a wake-word detector that needs a quiet
    frame before the phrase can miss an otherwise audible stimulus.
    """

    import numpy as np

    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        frames = handle.readframes(handle.getnframes())
    if width != 2:
        raise SystemExit("stimulus padding expects 16-bit PCM")
    samples = np.frombuffer(frames, dtype="<i2")
    lead = np.zeros(int(rate * lead_s), dtype="<i2")
    tail = np.zeros(int(rate * tail_s), dtype="<i2")
    padded = np.concatenate([lead, samples, tail])
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        handle.writeframes(padded.tobytes())


def _wav_levels(path: Path) -> tuple[float, float, float]:
    import numpy as np

    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        count = handle.getnframes()
    if width != 2 or channels != 1:
        raise SystemExit(f"unexpected stimulus format: width={width} channels={channels}")
    samples = np.frombuffer(frames, dtype="<i2").astype("float64") / 32768.0
    duration = count / rate if rate else 0.0
    rms = float(math.sqrt(float((samples**2).mean()))) if samples.size else 0.0
    peak = float(abs(samples).max()) if samples.size else 0.0
    return duration, _dbfs(rms), _dbfs(peak)


def _dbfs(amplitude: float) -> float:
    return 20.0 * math.log10(amplitude) if amplitude > 0 else -120.0


def _record_dbfs(seconds: float) -> tuple[float, float]:
    """Ambient floor as heard by the computer microphone (co-located proxy)."""

    import numpy as np
    import sounddevice as sd

    frames = int(48000 * seconds)
    captured = sd.rec(frames, samplerate=48000, channels=1, dtype="float32")
    sd.wait()
    samples = np.asarray(captured, dtype="float64").reshape(-1)
    rms = float(math.sqrt(float((samples**2).mean()))) if samples.size else 0.0
    peak = float(abs(samples).max()) if samples.size else 0.0
    return _dbfs(rms), _dbfs(peak)


def _measure_level_at_mic(stimulus: Path, gain: float, *, delay_s: float = 0.4) -> float:
    """Play the stimulus while recording: a proxy for what a co-located mic hears."""

    import numpy as np
    import sounddevice as sd

    seconds = delay_s + 3.0
    frames = int(48000 * seconds)
    captured = sd.rec(frames, samplerate=48000, channels=1, dtype="float32")
    time.sleep(delay_s)
    subprocess.run(["afplay", "-v", f"{gain}", str(stimulus)], check=False)
    sd.wait()
    samples = np.asarray(captured, dtype="float64").reshape(-1)
    rms = float(math.sqrt(float((samples**2).mean()))) if samples.size else 0.0
    return _dbfs(rms)


def _output_settings() -> str:
    completed = subprocess.run(
        ["osascript", "-e", "get volume settings"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip()


def _apply_output_settings(*, unmute: bool, volume: int | None) -> None:
    if unmute:
        subprocess.run(
            ["osascript", "-e", "set volume without output muted"],
            check=False,
            capture_output=True,
        )
    if volume is not None:
        subprocess.run(
            ["osascript", "-e", f"set volume output volume {int(volume)}"],
            check=False,
            capture_output=True,
        )


def _restore_output_settings(before: str) -> None:
    """Put the machine back the way it was found (mute state and volume)."""

    values: dict[str, str] = {}
    for part in before.split(","):
        key, _, value = part.partition(":")
        values[key.strip()] = value.strip()
    muted = values.get("output muted", "false").lower() == "true"
    volume = values.get("output volume", "")
    if volume.isdigit():
        subprocess.run(
            ["osascript", "-e", f"set volume output volume {int(volume)}"],
            check=False,
            capture_output=True,
        )
    subprocess.run(
        [
            "osascript",
            "-e",
            "set volume with output muted" if muted else "set volume without output muted",
        ],
        check=False,
        capture_output=True,
    )


def _play(path: Path, gain: float) -> float:
    started = time.monotonic()
    subprocess.run(["afplay", "-v", f"{gain}", str(path)], check=False)
    return started


def _wait_idle(reader: ConsoleReader, *, timeout: float, since: float) -> bool:
    del since
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reader.is_idle():
            return True
        time.sleep(0.5)
    return False


def _run_trial(
    reader: ConsoleReader,
    stimulus: Stimulus,
    *,
    phase: str,
    gain: float,
    wake_timeout_s: float,
    idle_timeout_s: float,
    trials: list[Trial],
    human_speaker: bool = False,
) -> Trial | None:
    attempt_start = time.monotonic()
    if not _wait_idle(reader, timeout=idle_timeout_s, since=attempt_start):
        trials.append(
            Trial(
                phase=phase,
                gain=gain,
                voice=stimulus.voice,
                started_monotonic=attempt_start,
                started_iso=datetime.now().astimezone().isoformat(timespec="milliseconds"),
                finished_monotonic=time.monotonic(),
                outcome="invalid_never_idle",
                notes=["device did not reach standby before this trial"],
            )
        )
        return None

    started_iso = datetime.now().astimezone().isoformat(timespec="milliseconds")
    if human_speaker:
        print(f"  >>> 请现在说「茉莉」({phase}, 第 {len(trials) + 1} 次)", flush=True)
        started = time.monotonic()
        finished = started
    else:
        started = _play(Path(stimulus.path), gain)
        finished = time.monotonic()
    wake_line = reader.wait_for(WAKE_MARKER, since=started - 0.05, timeout=wake_timeout_s)
    detector = None
    if wake_line is not None:
        detector_line = reader.wait_for(DETECTOR_MARKER, since=started - 0.05, timeout=0.5)
        if detector_line is not None:
            marker = "prob="
            if marker in detector_line.text:
                try:
                    detector = float(detector_line.text.split(marker, 1)[1].split()[0])
                except ValueError:
                    detector = None
    trial = Trial(
        phase=phase,
        gain=gain,
        voice=stimulus.voice,
        started_monotonic=started,
        started_iso=started_iso,
        finished_monotonic=finished,
        outcome="wake" if wake_line is not None else "miss",
        latency_s=(wake_line.monotonic - started) if wake_line is not None else None,
        detector_prob=detector,
        wake_line=wake_line.text if wake_line is not None else None,
    )
    trials.append(trial)
    return trial


def _is_wake_event(text: str) -> bool:
    return WAKE_EVENT_MARKER in text or (WAKE_MARKER in text and "(state:" in text)


def _wake_lines(reader: ConsoleReader, *, since: float, until: float) -> list[dict[str, Any]]:
    return [
        {"monotonic": line.monotonic, "iso": line.iso, "text": line.text}
        for line in reader.snapshot()
        if line.monotonic >= since and line.monotonic <= until and _is_wake_event(line.text)
    ]


def _parse_board_identity(reader: ConsoleReader) -> list[str]:
    markers = (
        "app_init: Project name:",
        "app_init: App version:",
        "app_init: Compile time:",
        "app_init: ELF file SHA256:",
        "app_init: ESP-IDF:",
        "MemoriaWakeWord: configured wake word",
    )
    return [
        line.text for line in reader.snapshot() if any(marker in line.text for marker in markers)
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="receipt directory; must not exist yet")
    parser.add_argument("--firmware-receipt", required=True)
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baudrate", type=int, default=DEFAULT_BAUDRATE)
    parser.add_argument("--trials", type=int, default=8, help="trials per gain level")
    parser.add_argument("--gain-near", type=float, default=1.0)
    parser.add_argument("--gain-far", type=float, default=0.3)
    parser.add_argument(
        "--probe-voices",
        default="Tingting,Meijia",
        help="installed zh voices to probe (Flo/Eddy/Grandma render silence on this host)",
    )
    parser.add_argument(
        "--probe-phrases",
        default="茉莉,梅莫里亚",
        help="wake phrases to probe; the firmware whitelist carries both",
    )
    parser.add_argument("--run-voice", default="Tingting")
    parser.add_argument("--wake-timeout", type=float, default=8.0)
    parser.add_argument("--idle-timeout", type=float, default=180.0)
    parser.add_argument("--boot-timeout", type=float, default=180.0)
    parser.add_argument("--distractor-seconds", type=float, default=60.0)
    parser.add_argument("--quiet-seconds", type=float, default=120.0)
    parser.add_argument("--ambient-seconds", type=float, default=3.0)
    parser.add_argument(
        "--skip-probe",
        action="store_true",
        help="assume the run voice triggers the detector already",
    )
    parser.add_argument(
        "--warmup-s",
        type=float,
        default=60.0,
        help="seconds to wait after the device reaches standby before the first stimulus",
    )
    parser.add_argument(
        "--human-speaker",
        action="store_true",
        help=(
            "the operator says the wake phrase when prompted instead of playing a TTS "
            "file; required for recall, because synthetic speech never woke this device "
            "in prior bounded runs"
        ),
    )
    parser.add_argument(
        "--unmute",
        action="store_true",
        help="unmute the system output for this run and restore the previous setting",
    )
    parser.add_argument(
        "--output-volume",
        type=int,
        default=None,
        help="system output volume (0-100) to use with --unmute",
    )
    args = parser.parse_args(argv)

    out = Path(args.out)
    if out.exists():
        raise SystemExit(f"refusing to overwrite existing receipt directory: {out}")
    out.mkdir(parents=True)
    stimuli_dir = out / "stimuli"
    stimuli_dir.mkdir()

    receipt_path = Path(args.firmware_receipt)
    receipt_content = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt_record = {
        "path": str(receipt_path),
        "sha256": _sha256(receipt_path),
        "content": receipt_content,
        # Same semantics as voice_session_capture.py: this file is evidence about the
        # flash that was performed, not a live read of the chip.
        "read_from_board_this_run": False,
    }

    import serial  # noqa: PLC0415 - uv-provided dependency

    stimuli: dict[str, Stimulus] = {}
    for label, (voice, text) in {
        "wake": (args.run_voice, _WAKE_PHRASE),
        # Only Tingting/Meijia render usable audio on this host; the two
        # distractors are therefore two installed voices, not more.
        "tv": ("Tingting", _TV_PASSAGE),
        "small_talk": ("Meijia", _SMALL_TALK),
    }.items():
        target = stimuli_dir / f"{label}.wav"
        _synth(text, voice, target)
        duration, rms, peak = _wav_levels(target)
        stimuli[label] = Stimulus(
            label=label,
            voice=voice,
            text=text,
            path=str(target),
            sha256=_sha256(target),
            duration_s=round(duration, 3),
            rms_dbfs=round(rms, 2),
            peak_dbfs=round(peak, 2),
        )

    output_before = _output_settings()
    _apply_output_settings(unmute=args.unmute, volume=args.output_volume)
    output_during = _output_settings()
    ambient_rms, ambient_peak = _record_dbfs(args.ambient_seconds)

    port = serial.Serial(port=None, baudrate=args.baudrate, timeout=0.5, exclusive=True)
    port.dtr = False
    port.rts = False
    port.port = args.port
    port.open()
    reader = ConsoleReader(port, out / "console.log")
    reader.start()

    trials: list[Trial] = []
    windows: list[Window] = []
    invalid: list[str] = []
    try:
        boot_idle = reader.wait_for(IDLE_MARKER, since=time.monotonic(), timeout=args.boot_timeout)
        if boot_idle is None:
            invalid.append("device never reported activating -> idle after the port opened")
        elif args.warmup_s > 0:
            # Measured: the same verified audio that wakes the device later does not
            # wake it in the first ~45 s after standby, so those attempts are not
            # recall failures and must not be counted as trials.
            print(f"warming up {args.warmup_s:.0f}s after standby", flush=True)
            time.sleep(args.warmup_s)
        # Levels are measured with the same speaker/mic pair used for the matrix.
        if not args.human_speaker:
            for gain in sorted({args.gain_near, args.gain_far}, reverse=True):
                stimuli["wake"].level_at_mic_dbfs = _measure_level_at_mic(
                    Path(stimuli["wake"].path), gain
                )

        trigger_voice = args.run_voice
        if not args.skip_probe and not args.human_speaker and boot_idle is not None:
            # Positive control: if synthetic TTS cannot trigger the detector at all,
            # recall is not measurable with this rig and must be reported as such.
            for phrase in [item.strip() for item in args.probe_phrases.split(",") if item.strip()]:
                for voice in [
                    item.strip() for item in args.probe_voices.split(",") if item.strip()
                ]:
                    target = stimuli_dir / f"probe-{voice}-{len(trials)}.wav"
                    _synth(phrase, voice, target)
                    duration, rms, peak = _wav_levels(target)
                    probe = Stimulus(
                        label=f"probe-{voice}",
                        voice=voice,
                        text=phrase,
                        path=str(target),
                        sha256=_sha256(target),
                        duration_s=round(duration, 3),
                        rms_dbfs=round(rms, 2),
                        peak_dbfs=round(peak, 2),
                    )
                    trial = _run_trial(
                        reader,
                        probe,
                        phase=f"probe:{phrase}",
                        gain=args.gain_near,
                        wake_timeout_s=args.wake_timeout,
                        idle_timeout_s=args.idle_timeout,
                        trials=trials,
                        human_speaker=args.human_speaker,
                    )
                    if trial is not None and trial.outcome == "wake":
                        trigger_voice = voice
                        break
                if any(
                    trial.phase.startswith("probe") and trial.outcome == "wake" for trial in trials
                ):
                    break
            if not any(
                trial.phase.startswith("probe") and trial.outcome == "wake" for trial in trials
            ):
                invalid.append(
                    "no synthetic voice triggered the wake detector; recall is not "
                    "measurable with TTS stimuli (false-wake phases still ran)"
                )
            if trigger_voice != args.run_voice:
                target = stimuli_dir / "wake.wav"
                _synth(_WAKE_PHRASE, trigger_voice, target)
                duration, rms, peak = _wav_levels(target)
                stimuli["wake"] = Stimulus(
                    label="wake",
                    voice=trigger_voice,
                    text=_WAKE_PHRASE,
                    path=str(target),
                    sha256=_sha256(target),
                    duration_s=round(duration, 3),
                    rms_dbfs=round(rms, 2),
                    peak_dbfs=round(peak, 2),
                )
        measurable = boot_idle is not None and not any(
            "not measurable" in reason for reason in invalid
        )

        if measurable:
            for phase, gain in (("recall_near", args.gain_near), ("recall_far", args.gain_far)):
                for _ in range(args.trials):
                    _run_trial(
                        reader,
                        stimuli["wake"],
                        phase=phase,
                        gain=gain,
                        wake_timeout_s=args.wake_timeout,
                        idle_timeout_s=args.idle_timeout,
                        trials=trials,
                        human_speaker=args.human_speaker,
                    )

        # False wakes: distractors first, then an ambient-only window.  Neither
        # plays the wake phrase, so any wake line here is a false wake.
        for label in ("tv", "small_talk"):
            window_start = time.monotonic()
            iso_start = datetime.now().astimezone().isoformat(timespec="milliseconds")
            deadline = window_start + args.distractor_seconds
            while time.monotonic() < deadline:
                _play(Path(stimuli[label].path), args.gain_far)
            window_end = time.monotonic()
            windows.append(
                Window(
                    label=label,
                    gain=args.gain_far,
                    started_monotonic=window_start,
                    started_iso=iso_start,
                    finished_monotonic=window_end,
                    wakes=_wake_lines(reader, since=window_start, until=window_end),
                )
            )

        quiet_start = time.monotonic()
        iso_quiet = datetime.now().astimezone().isoformat(timespec="milliseconds")
        time.sleep(args.quiet_seconds)
        quiet_end = time.monotonic()
        windows.append(
            Window(
                label="quiet",
                gain=0.0,
                started_monotonic=quiet_start,
                started_iso=iso_quiet,
                finished_monotonic=quiet_end,
                wakes=_wake_lines(reader, since=quiet_start, until=quiet_end),
            )
        )
    finally:
        reader.stop()
        port.close()
        _restore_output_settings(output_before)

    def _counts(phase: str) -> dict[str, int]:
        selected = [trial for trial in trials if trial.phase == phase]
        return {
            "trials": len(selected),
            "wakes": sum(1 for trial in selected if trial.outcome == "wake"),
            "misses": sum(1 for trial in selected if trial.outcome == "miss"),
            "invalid": sum(1 for trial in selected if trial.outcome.startswith("invalid")),
        }

    receipt = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": "scripts/wake_word_matrix.py",
        "device": {
            "port": args.port,
            "baudrate": args.baudrate,
            "session_device_id_last_recorded": "dev_atk_a4cb8fd6095c",
            "board_identity_read_this_run": _parse_board_identity(reader),
            "firmware_receipt": receipt_record,
        },
        "stimulus": {
            "kind": (
                "live_human_voice"
                if args.human_speaker
                else "synthetic_tts_through_computer_speaker"
            ),
            "human_speaker": bool(args.human_speaker),
            "wake_phrase": _WAKE_PHRASE,
            "files": {label: asdict(item) for label, item in stimuli.items()},
        },
        "input_range": {
            "system_output_settings_before_run": output_before,
            "system_output_settings_during_run": output_during,
            "system_output_restored_after_run": True,
            "gain_near": args.gain_near,
            "gain_far": args.gain_far,
            "gain_units": "afplay -v linear amplitude multiplier",
            "physical_distance_verified": False,
            "distance_note": (
                "distance/angle were not varied or measured; the 'far' condition is a "
                "playback-gain ladder only"
            ),
            "ambient_floor_dbfs_at_mic": round(ambient_rms, 2),
            "ambient_peak_dbfs_at_mic": round(ambient_peak, 2),
        },
        "truth": {
            "wake_stimulus_windows": [
                {
                    "phase": trial.phase,
                    "voice": trial.voice,
                    "gain": trial.gain,
                    "started_iso": trial.started_iso,
                    "started_monotonic": trial.started_monotonic,
                }
                for trial in trials
            ]
        },
        "protocol": {
            "warmup_s": args.warmup_s,
            "wake_timeout_s": args.wake_timeout,
            "idle_timeout_s": args.idle_timeout,
            "distractor_seconds": args.distractor_seconds,
            "quiet_seconds": args.quiet_seconds,
            "trials_per_gain": args.trials,
        },
        "counts": {
            "probe": {
                "trials": len([t for t in trials if t.phase.startswith("probe")]),
                "wakes": len(
                    [t for t in trials if t.phase.startswith("probe") and t.outcome == "wake"]
                ),
            },
            "probe_by_phrase": {
                phrase: {
                    "trials": len([t for t in trials if t.phase == f"probe:{phrase}"]),
                    "wakes": len(
                        [t for t in trials if t.phase == f"probe:{phrase}" and t.outcome == "wake"]
                    ),
                }
                for phrase in [
                    item.strip() for item in args.probe_phrases.split(",") if item.strip()
                ]
            },
            "recall_near": _counts("recall_near"),
            "recall_far": _counts("recall_far"),
            "false_wakes_distractor": sum(
                len(window.wakes) for window in windows if window.label != "quiet"
            ),
            "false_wakes_quiet": sum(
                len(window.wakes) for window in windows if window.label == "quiet"
            ),
        },
        "trials": [asdict(trial) for trial in trials],
        "windows": [asdict(window) for window in windows],
        "invalid_reasons": invalid,
        "console_lines": len(reader.snapshot()),
        "console_read_errors": reader.read_errors,
    }
    (out / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    near = receipt["counts"]["recall_near"]
    far = receipt["counts"]["recall_far"]
    lines = [
        "# 唤醒矩阵 receipt（设备端）",
        "",
        f"- 生成时间：{receipt['created_at']}",
        f"- 刺激：合成 TTS（{stimuli['wake'].voice}）经电脑扬声器播放；真人说话者=否",
        f"- 输入范围：gain 近={args.gain_near} / 远={args.gain_far}（线性幅度系数）；物理距离未变化、未测量",
        f"- 系统输出：运行中 {output_during}；运行前 {output_before}（已恢复）",
        f"- 环境底噪（麦克风代理）：{round(ambient_rms, 2)} dBFS（峰值 {round(ambient_peak, 2)}）",
        f"- 唤醒刺激电平（麦克风代理）：{stimuli['wake'].level_at_mic_dbfs} dBFS",
        "",
        "## 唤醒召回（真唤醒）",
        "",
        f"- 近距档：{near['wakes']}/{near['trials']} 唤醒，漏唤醒 {near['misses']}，无效 {near['invalid']}",
        f"- 远距档：{far['wakes']}/{far['trials']} 唤醒，漏唤醒 {far['misses']}，无效 {far['invalid']}",
        "",
        "## 误唤醒",
        "",
    ]
    for window in windows:
        lines.append(f"- {window.label}：{len(window.wakes)} 次（gain={window.gain}）")
    if invalid:
        lines += ["", "## 无效项", ""]
        lines += [f"- {reason}" for reason in invalid]
    lines += [
        "",
        "## 边界",
        "",
        "- 计数来自设备终端日志（`Application: Wake word detected`），不读生产日志，因此不含 session/epoch。",
        "- 板卡身份本次只读到启动横幅（app 版本 + ELF SHA256 前缀）；flash receipt 标记为 read_from_board_this_run=false。",
        "- 该 receipt 不改写 `advertised_duplex_level` 或 `aec_reference_verified`。",
    ]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    if invalid and any("never reported" in reason for reason in invalid):
        print("wake matrix invalid: device never reached standby", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
