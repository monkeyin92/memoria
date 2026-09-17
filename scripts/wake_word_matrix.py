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
from collections.abc import Callable
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
    latency_prompt: float | None = None
    latency_speech_onset: float | None = None
    speech_onset_monotonic: float | None = None
    speech_onset_iso: str | None = None
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
    effective_exposure_s: float = 0.0
    idle_s: float = 0.0
    paused: list[dict[str, Any]] = field(default_factory=list)
    invalid_reason: str | None = None
    detector_on_evidence: str | None = None


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

    def state_timeline(self) -> list[dict[str, Any]]:
        """Every StateMachine transition seen so far, oldest first.

        Each entry carries ``monotonic``/``iso``/``text`` plus the parsed
        ``from``/``to`` states; unparsable lines are skipped, never fatal.
        """
        timeline: list[dict[str, Any]] = []
        for line in self.snapshot():
            if "StateMachine: State:" not in line.text:
                continue
            try:
                segment = line.text.split("State:", 1)[1]
                before, _, after = segment.partition("->")
                from_state = before.strip().split()[0]
                to_state = after.strip().split()[0]
            except IndexError:
                continue
            timeline.append(
                {
                    "monotonic": line.monotonic,
                    "iso": line.iso,
                    "text": line.text,
                    "from": from_state,
                    "to": to_state,
                }
            )
        return timeline

    def current_state(self) -> str | None:
        """Newest known device state, or None before any transition is seen."""
        timeline = self.state_timeline()
        return timeline[-1]["to"] if timeline else None

    def is_detector_on(self) -> bool:
        """True once the firmware reports its wake-word detector configured."""
        return any(
            "MemoriaWakeWord: configured" in line.text for line in self.snapshot()
        )

    def detector_on_evidence(self) -> str | None:
        """Newest detector-configured line, i.e. the detector-on proof."""
        for line in reversed(self.snapshot()):
            if "MemoriaWakeWord: configured" in line.text:
                return line.text
        return None


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


def _parse_output_settings(text: str) -> tuple[bool, str]:
    """Split an ``osascript get volume settings`` line into (muted, volume)."""
    values: dict[str, str] = {}
    for part in text.split(","):
        key, _, value = part.partition(":")
        values[key.strip()] = value.strip()
    return values.get("output muted", "").lower() == "true", values.get("output volume", "")


def _is_output_muted(settings: str) -> bool:
    """True when the recorded output settings say the output is muted."""
    return _parse_output_settings(settings)[0]


def _settings_match(before: str, after: str) -> bool:
    """True when a re-read after restore agrees with the pre-run snapshot."""
    return _parse_output_settings(before) == _parse_output_settings(after)


def _restore_output_settings(before: str) -> bool:
    """Put the machine back the way it was found; True if every command ran."""
    muted, volume = _parse_output_settings(before)
    ok = True
    if volume.isdigit():
        completed = subprocess.run(
            ["osascript", "-e", f"set volume output volume {int(volume)}"],
            check=False,
            capture_output=True,
        )
        ok = ok and completed.returncode == 0
    completed = subprocess.run(
        [
            "osascript",
            "-e",
            "set volume with output muted" if muted else "set volume without output muted",
        ],
        check=False,
        capture_output=True,
    )
    return ok and completed.returncode == 0


def _verify_output_restored(before: str) -> tuple[bool, str]:
    """Re-read the output settings and compare them with the pre-run snapshot.

    Returns ``(restored, after)``; ``restored`` is True only when the re-read
    agrees with ``before``.  Never raises: a failed re-read counts as unrestored.
    """
    try:
        after = _output_settings()
    except Exception:  # noqa: BLE001 - the receipt records unverified, not a crash
        return False, ""
    return _settings_match(before, after), after


def _finalize_output_restore(before: str, invalid: list[str]) -> tuple[bool, str]:
    """Restore the output settings, verify by re-reading, record failures.

    Returns ``(restored, after)`` for the receipt; appends to ``invalid`` when
    the restore commands failed or the re-read disagrees with ``before``.
    """
    try:
        commands_ok = _restore_output_settings(before)
    except Exception as exc:  # noqa: BLE001 - restore runs in finally, must not raise
        invalid.append(f"system output restore command failed ({exc}); run marked invalid")
        return False, ""
    restored, after = _verify_output_restored(before)
    restored = commands_ok and restored
    if not restored:
        invalid.append(
            "system output was not restored "
            f"(before={before!r} after={after!r}); run marked invalid"
        )
    return restored, after


class PlayFailed(Exception):
    """Raised when a stimulus playback exits non-zero (silent rig, no data)."""


def _play(path: Path, gain: float) -> float:
    started = time.monotonic()
    completed = subprocess.run(["afplay", "-v", f"{gain}", str(path)], check=False)
    if completed.returncode != 0:
        raise PlayFailed(f"afplay exited with {completed.returncode} for {path}")
    return started


def _wait_idle(reader: ConsoleReader, *, timeout: float, since: float) -> bool:
    # ``since`` is kept for caller compatibility: standby is a level (the last
    # transition is into idle, settled), not an edge that must occur after it.
    _ = since
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if reader.is_idle():
            return True
        time.sleep(0.5)
    return False


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


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
    human_confirm: bool = False,
) -> Trial | None:
    """Run one recall attempt; invalid runs are recorded, never a miss.

    Wake detection uses the wake-event definition (:func:`_is_wake_event`) so
    the lagging duplicate line (no ``(state:)``) can never count.  Human runs
    stamp the operator's speech onset (``--human-confirm``) separately from
    the prompt moment, so both ``latency_prompt`` and ``latency_speech_onset``
    are recorded.
    """
    attempt_start = time.monotonic()
    baseline_errors = reader.read_errors

    def _record(
        outcome: str,
        *,
        started: float,
        started_iso: str,
        onset: float | None,
        onset_iso: str | None,
        wake_line: ConsoleLine | None,
        detector: float | None,
        notes: list[str],
    ) -> Trial:
        prompt_latency = (wake_line.monotonic - started) if wake_line is not None else None
        onset_latency = (
            (wake_line.monotonic - onset)
            if (wake_line is not None and onset is not None)
            else None
        )
        trial = Trial(
            phase=phase,
            gain=gain,
            voice=stimulus.voice,
            started_monotonic=started,
            started_iso=started_iso,
            finished_monotonic=time.monotonic(),
            outcome=outcome,
            latency_s=prompt_latency,
            latency_prompt=prompt_latency,
            latency_speech_onset=onset_latency,
            speech_onset_monotonic=onset,
            speech_onset_iso=onset_iso,
            detector_prob=detector,
            wake_line=wake_line.text if wake_line is not None else None,
            notes=notes,
        )
        trials.append(trial)
        return trial

    if not reader.state_timeline():
        return _record(
            "invalid_state_unknown",
            started=attempt_start,
            started_iso=_iso_now(),
            onset=None,
            onset_iso=None,
            wake_line=None,
            detector=None,
            notes=["no StateMachine transitions seen; device state unknown"],
        )
    if not _wait_idle(reader, timeout=idle_timeout_s, since=attempt_start):
        return _record(
            "invalid_never_idle",
            started=attempt_start,
            started_iso=_iso_now(),
            onset=None,
            onset_iso=None,
            wake_line=None,
            detector=None,
            notes=["device did not reach standby before this trial"],
        )

    started_iso = _iso_now()
    notes: list[str] = []
    onset: float | None = None
    onset_iso: str | None = None
    if human_speaker:
        prompt_at = time.monotonic()
        started = prompt_at
        print(f"  >>> 请现在说「{_WAKE_PHRASE}」({phase}, 第 {len(trials) + 1} 次)", flush=True)
        if human_confirm:
            try:
                input("  >>> 在开口说出「茉莉」的瞬间按回车（记录开口时间戳）...")
            except EOFError:
                notes.append("human-confirm prompt got EOF; no speech-onset stamp")
            else:
                onset = time.monotonic()
                onset_iso = _iso_now()
        else:
            notes.append("no --human-confirm; speech onset not stamped, latency uses prompt")
    else:
        try:
            started = _play(Path(stimulus.path), gain)
        except PlayFailed as exc:
            return _record(
                "invalid_play_failed",
                started=attempt_start,
                started_iso=started_iso,
                onset=None,
                onset_iso=None,
                wake_line=None,
                detector=None,
                notes=[f"playback failed: {exc}"],
            )
    wake_line = _wait_for_wake_event(reader, since=started - 0.05, timeout=wake_timeout_s)
    detector = None
    if wake_line is not None:
        detector = _detector_prob_before(
            reader, since=started - 0.05, until=wake_line.monotonic
        )
    if reader.read_errors > baseline_errors:
        return _record(
            "invalid_serial_gap",
            started=started,
            started_iso=started_iso,
            onset=onset,
            onset_iso=onset_iso,
            wake_line=None,
            detector=None,
            notes=[
                f"{reader.read_errors - baseline_errors} console read error(s) "
                "during this trial; the stream may have gaps"
            ],
        )
    if wake_line is None:
        if not reader.state_timeline():
            return _record(
                "invalid_state_unknown",
                started=started,
                started_iso=started_iso,
                onset=onset,
                onset_iso=onset_iso,
                wake_line=None,
                detector=None,
                notes=["device state became unknown during this trial"],
            )
        return _record(
            "miss",
            started=started,
            started_iso=started_iso,
            onset=onset,
            onset_iso=onset_iso,
            wake_line=None,
            detector=None,
            notes=notes,
        )
    return _record(
        "wake",
        started=started,
        started_iso=started_iso,
        onset=onset,
        onset_iso=onset_iso,
        wake_line=wake_line,
        detector=detector,
        notes=notes,
    )


def _is_wake_event(text: str) -> bool:
    return WAKE_EVENT_MARKER in text or (WAKE_MARKER in text and "(state:" in text)


def _wait_for_wake_event(
    reader: ConsoleReader, *, since: float, timeout: float
) -> ConsoleLine | None:
    """First wake *event* at/after ``since``; lagging duplicates never match."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for line in reader.snapshot(since=since):
            if _is_wake_event(line.text):
                return line
        time.sleep(0.1)
    return None


def _detector_prob_before(reader: ConsoleReader, *, since: float, until: float) -> float | None:
    """Newest detector probability strictly inside the started..wake interval."""
    prob: float | None = None
    for line in reader.snapshot():
        if since <= line.monotonic <= until and DETECTOR_MARKER in line.text:
            marker = "prob="
            if marker in line.text:
                try:
                    prob = float(line.text.split(marker, 1)[1].split()[0])
                except ValueError:
                    continue
    return prob


def _wake_lines(reader: ConsoleReader, *, since: float, until: float) -> list[dict[str, Any]]:
    """Wake events in ``[since, until)``: left-closed, right-open.

    Adjacent windows share an edge timestamp, so a closed right edge would let
    one console line belong to two windows; the half-open interval keeps every
    wake line attributable to exactly one window.
    """
    return [
        {"monotonic": line.monotonic, "iso": line.iso, "text": line.text}
        for line in reader.snapshot()
        if since <= line.monotonic < until and _is_wake_event(line.text)
    ]


def _run_window(
    reader: ConsoleReader,
    *,
    label: str,
    gain: float,
    wall_seconds: float,
    idle_timeout_s: float,
    invalid: list[str],
    play_fn: Callable[[], float] | None = None,
    poll_s: float = 0.2,
) -> Window:
    """Run one false-wake window; the denominator is idle exposure, not wall time.

    The window opens only behind two gates -- settled standby (``_wait_idle``)
    and a confirmed detector (``is_detector_on``); otherwise it is returned
    invalid with no wakes.  Inside, the device state is polled: any stretch
    away from idle pauses the exposure clock and is recorded in ``paused``
    with its reason, so a window that spends 133 s wall with 0 s idle reports
    0 s of exposure instead of a full denominator.

    Speaking time never counts as exposure: while the device itself is
    playing, its KWS pipeline is off, so audio played then proves nothing
    about false wakes.  That pause must NOT be "fixed" by enabling KWS during
    playback just to pad exposure -- the pause is the honest denominator.
    ``play_fn`` is None for the quiet window (no playback, idle slices only).
    """
    wall_start = time.monotonic()
    iso_start = _iso_now()
    baseline_errors = reader.read_errors
    evidence = reader.detector_on_evidence()

    def _invalid(reason: str, note: str) -> Window:
        invalid.append(f"window {label} invalid: {note}")
        return Window(
            label=label,
            gain=gain,
            started_monotonic=wall_start,
            started_iso=iso_start,
            finished_monotonic=time.monotonic(),
            wakes=[],
            effective_exposure_s=0.0,
            idle_s=0.0,
            paused=[],
            invalid_reason=reason,
            detector_on_evidence=evidence,
        )

    if not reader.state_timeline():
        return _invalid("invalid_state_unknown", "no StateMachine transitions; state unknown")
    if not _wait_idle(reader, timeout=idle_timeout_s, since=wall_start):
        return _invalid("invalid_never_idle", "device did not reach standby before the window")
    if not reader.is_detector_on():
        return _invalid("invalid_detector_off", "detector never reported configured")
    evidence = reader.detector_on_evidence()

    wall_end = wall_start + wall_seconds
    exposure = 0.0
    paused: list[dict[str, Any]] = []
    pause_start: float | None = None
    pause_reason = ""
    invalid_reason: str | None = None

    def _close_pause(now: float) -> None:
        nonlocal pause_start, pause_reason
        if pause_start is not None:
            paused.append({"from": pause_start, "to": now, "reason": pause_reason})
            pause_start = None
            pause_reason = ""

    while time.monotonic() < wall_end and invalid_reason is None:
        state = reader.current_state()
        if state == "idle":
            _close_pause(time.monotonic())
            if play_fn is not None:
                # Playback happens only while idle (see docstring): the assert
                # pins that down next to the call it guards.
                assert reader.current_state() == "idle", "playback only while idle"
                seg_start = time.monotonic()
                try:
                    play_fn()
                except PlayFailed as exc:
                    invalid_reason = "invalid_play_failed"
                    invalid.append(f"window {label} invalid: playback failed ({exc})")
                    break
                seg_end = time.monotonic()
                landed = reader.current_state()
                if landed == "idle":
                    exposure += seg_end - seg_start
                else:
                    pause_start = seg_start
                    pause_reason = (
                        f"left idle during playback (state={landed}); exposure paused"
                    )
            else:
                seg_start = time.monotonic()
                time.sleep(max(0.0, min(poll_s, wall_end - seg_start)))
                seg_end = time.monotonic()
                if reader.current_state() == "idle":
                    exposure += seg_end - seg_start
                else:
                    pause_start = seg_start
                    pause_reason = (
                        "left idle during quiet window "
                        f"(state={reader.current_state()}); exposure paused"
                    )
        else:
            detail = f"state={state}" if state else "state unknown"
            if state == "speaking":
                detail += " (device playback; KWS off)"
            if pause_start is None:
                pause_start = time.monotonic()
                pause_reason = f"left idle ({detail}); exposure paused"
            time.sleep(max(0.0, min(poll_s, wall_end - time.monotonic())))
    _close_pause(time.monotonic())
    window_end = time.monotonic()
    if invalid_reason is None and reader.read_errors > baseline_errors:
        invalid_reason = "invalid_serial_gap"
        invalid.append(
            f"window {label} invalid: "
            f"{reader.read_errors - baseline_errors} console read error(s); "
            "the stream may have gaps"
        )
    wakes = (
        [] if invalid_reason is not None else _wake_lines(reader, since=wall_start, until=window_end)
    )
    paused_total = sum(entry["to"] - entry["from"] for entry in paused)
    return Window(
        label=label,
        gain=gain,
        started_monotonic=wall_start,
        started_iso=iso_start,
        finished_monotonic=window_end,
        wakes=wakes,
        effective_exposure_s=round(exposure, 3),
        idle_s=round(max(0.0, (window_end - wall_start) - paused_total), 3),
        paused=[
            {"from": entry["from"], "to": entry["to"], "reason": entry["reason"]}
            for entry in paused
        ],
        invalid_reason=invalid_reason,
        detector_on_evidence=evidence,
    )


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
        "--human-confirm",
        action="store_true",
        help=(
            "with --human-speaker, wait for the operator to press Enter at speech "
            "onset so the receipt records latency_speech_onset as well as latency_prompt"
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

    trials: list[Trial] = []
    windows: list[Window] = []
    invalid: list[str] = []
    output_before = ""
    output_during = ""
    output_after = ""
    output_restored = False
    output_changed = False
    playback_blocked = False
    ambient_rms: float | None = None
    ambient_peak: float | None = None
    boot_idle: ConsoleLine | None = None
    boot_idle_iso: str | None = None
    warmup_actual_s = 0.0
    levels_at_mic: dict[str, float | None] = {}
    level_note = ""
    trigger_voice = args.run_voice
    port: Any = None
    reader: ConsoleReader | None = None
    reader_started = False
    port_opened = False
    try:
        output_before = _output_settings()
        _apply_output_settings(unmute=args.unmute, volume=args.output_volume)
        output_changed = True
        output_during = _output_settings()
        if _is_output_muted(output_during) and not args.human_speaker:
            playback_blocked = True
            invalid.append(
                "system output is muted during the run; playback stimuli would be "
                "silent, so playback phases are refused"
            )
        ambient_rms, ambient_peak = _record_dbfs(args.ambient_seconds)

        port = serial.Serial(port=None, baudrate=args.baudrate, timeout=0.5, exclusive=True)
        port.dtr = False
        port.rts = False
        port.port = args.port
        port.open()
        port_opened = True
        reader = ConsoleReader(port, out / "console.log")
        reader.start()
        reader_started = True

        boot_idle = reader.wait_for(IDLE_MARKER, since=time.monotonic(), timeout=args.boot_timeout)
        if boot_idle is None:
            invalid.append("device never reported activating -> idle after the port opened")
        else:
            boot_idle_iso = boot_idle.iso
            if args.warmup_s > 0:
                # Measured: the same verified audio that wakes the device later does not
                # wake it in the first ~45 s after standby, so those attempts are not
                # recall failures and must not be counted as trials.
                print(f"warming up {args.warmup_s:.0f}s after standby", flush=True)
                warmup_mark = time.monotonic()
                time.sleep(args.warmup_s)
                warmup_actual_s = round(time.monotonic() - warmup_mark, 3)
        # Levels are measured with the same speaker/mic pair used for the matrix,
        # one entry per tier; a human speaker has no playback level (null).
        if args.human_speaker:
            levels_at_mic = {"near": None, "far": None, "tv": None, "small_talk": None}
            level_note = "human speaker: no playback, levels not measured (null)"
        elif playback_blocked:
            levels_at_mic = {"near": None, "far": None, "tv": None, "small_talk": None}
            level_note = "output muted: playback refused, levels not measured (null)"
        else:
            levels_at_mic["near"] = _measure_level_at_mic(
                Path(stimuli["wake"].path), args.gain_near
            )
            levels_at_mic["far"] = _measure_level_at_mic(
                Path(stimuli["wake"].path), args.gain_far
            )
            stimuli["wake"].level_at_mic_dbfs = levels_at_mic["far"]
            levels_at_mic["tv"] = _measure_level_at_mic(
                Path(stimuli["tv"].path), args.gain_far
            )
            stimuli["tv"].level_at_mic_dbfs = levels_at_mic["tv"]
            levels_at_mic["small_talk"] = _measure_level_at_mic(
                Path(stimuli["small_talk"].path), args.gain_far
            )
            stimuli["small_talk"].level_at_mic_dbfs = levels_at_mic["small_talk"]
            level_note = "co-located computer-mic proxy, not what the device hears"

        if (
            not args.skip_probe
            and not args.human_speaker
            and not playback_blocked
            and boot_idle is not None
        ):
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
                        human_confirm=args.human_confirm,
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
                if not playback_blocked:
                    levels_at_mic["near"] = _measure_level_at_mic(target, args.gain_near)
                    levels_at_mic["far"] = _measure_level_at_mic(target, args.gain_far)
                    stimuli["wake"].level_at_mic_dbfs = levels_at_mic["far"]
        measurable = (
            boot_idle is not None
            and not playback_blocked
            and not any("not measurable" in reason for reason in invalid)
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
                        human_confirm=args.human_confirm,
                    )

        # False wakes: distractors first, then an ambient-only window.  Neither
        # plays the wake phrase, so any wake line here is a false wake.
        assert reader is not None  # the reader was started above
        for label in ("tv", "small_talk"):
            if playback_blocked:
                moment = time.monotonic()
                windows.append(
                    Window(
                        label=label,
                        gain=args.gain_far,
                        started_monotonic=moment,
                        started_iso=_iso_now(),
                        finished_monotonic=moment,
                        wakes=[],
                        invalid_reason="invalid_output_muted",
                    )
                )
                invalid.append(f"window {label} invalid: output muted, playback refused")
            else:
                stimulus_path = Path(stimuli[label].path)
                windows.append(
                    _run_window(
                        reader,
                        label=label,
                        gain=args.gain_far,
                        wall_seconds=args.distractor_seconds,
                        idle_timeout_s=args.idle_timeout,
                        invalid=invalid,
                        play_fn=lambda p=stimulus_path, g=args.gain_far: _play(p, g),
                    )
                )

        windows.append(
            _run_window(
                reader,
                label="quiet",
                gain=0.0,
                wall_seconds=args.quiet_seconds,
                idle_timeout_s=args.idle_timeout,
                invalid=invalid,
            )
        )
        if reader.read_errors > 0:
            invalid.append(
                f"invalid_serial_gap: {reader.read_errors} console read error(s) this run; "
                "affected trials/windows are invalid, not misses"
            )
    finally:
        if reader_started and reader is not None:
            try:
                reader.stop()
            except Exception as exc:  # noqa: BLE001 - teardown must not mask run errors
                invalid.append(f"console reader stop failed ({exc})")
        if port_opened and port is not None:
            try:
                port.close()
            except Exception as exc:  # noqa: BLE001 - teardown must not mask run errors
                invalid.append(f"serial port close failed ({exc})")
        if output_changed:
            output_restored, output_after = _finalize_output_restore(output_before, invalid)
        else:
            # The output was never touched, so there is nothing to restore.
            output_restored, output_after = True, output_before

    def _counts(phase: str) -> dict[str, int]:
        selected = [trial for trial in trials if trial.phase == phase]
        return {
            "trials": len(selected),
            "wakes": sum(1 for trial in selected if trial.outcome == "wake"),
            "misses": sum(1 for trial in selected if trial.outcome == "miss"),
            "invalid": sum(1 for trial in selected if trial.outcome.startswith("invalid")),
        }

    board_identity = _parse_board_identity(reader) if reader is not None else []
    console_total = len(reader.snapshot()) if reader is not None else 0
    console_errors = reader.read_errors if reader is not None else 0
    valid_windows = [window for window in windows if window.invalid_reason is None]
    receipt = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tool": "scripts/wake_word_matrix.py",
        "device": {
            "port": args.port,
            "baudrate": args.baudrate,
            "session_device_id_last_recorded": "dev_atk_a4cb8fd6095c",
            "board_identity_read_this_run": board_identity,
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
            "system_output_settings_after_run": output_after,
            "system_output_restored_after_run": output_restored,
            "gain_near": args.gain_near,
            "gain_far": args.gain_far,
            "gain_units": "afplay -v linear amplitude multiplier",
            "physical_distance_verified": False,
            "distance_note": (
                "distance/angle were not varied or measured; the 'far' condition is a "
                "playback-gain ladder only"
            ),
            "levels_at_mic_dbfs": dict(levels_at_mic),
            "level_note": level_note,
            "ambient_floor_dbfs_at_mic": round(ambient_rms, 2) if ambient_rms is not None else None,
            "ambient_peak_dbfs_at_mic": round(ambient_peak, 2) if ambient_peak is not None else None,
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
            "warmup_actual_s": warmup_actual_s,
            "boot_idle_iso": boot_idle_iso,
            "human_confirm": bool(args.human_confirm),
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
                len(window.wakes) for window in valid_windows if window.label != "quiet"
            ),
            "false_wakes_quiet": sum(
                len(window.wakes) for window in valid_windows if window.label == "quiet"
            ),
            "windows_invalid": len(windows) - len(valid_windows),
        },
        "trials": [asdict(trial) for trial in trials],
        "windows": [asdict(window) for window in windows],
        "invalid_reasons": invalid,
        "console_lines": console_total,
        "console_read_errors": console_errors,
    }
    (out / "receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    report = _build_report(receipt)
    (out / "report.md").write_text(report, encoding="utf-8")

    print(report, end="")
    if invalid and any("never reported" in reason for reason in invalid):
        print("wake matrix invalid: device never reached standby", file=sys.stderr)
        return 2
    return 0


def _format_level(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "未测"


def _build_report(receipt: dict[str, Any]) -> str:
    """Render report.md purely from the receipt (unit-testable, no hardware)."""
    stimulus = receipt.get("stimulus", {})
    human = bool(stimulus.get("human_speaker", False))
    protocol = receipt.get("protocol", {})
    input_range = receipt.get("input_range", {})
    counts = receipt.get("counts", {})
    near = counts.get("recall_near", {})
    far = counts.get("recall_far", {})
    windows = receipt.get("windows", [])
    trials = receipt.get("trials", [])
    invalid = receipt.get("invalid_reasons", [])
    files = stimulus.get("files", {})
    wake_voice = files.get("wake", {}).get("voice", "?")
    if human:
        confirmed = "，开口时刻经回车确认" if protocol.get("human_confirm") else ""
        stim_line = f"- 刺激：真人说话者=是（操作员按提示说「茉莉」{confirmed}）；合成 TTS=否"
    else:
        stim_line = f"- 刺激：合成 TTS（{wake_voice}）经电脑扬声器播放；真人说话者=否"
    restored = bool(input_range.get("system_output_restored_after_run", False))
    restore_note = "已恢复" if restored else "恢复失败"
    ambient = input_range.get("ambient_floor_dbfs_at_mic")
    ambient_peak = input_range.get("ambient_peak_dbfs_at_mic")
    levels = input_range.get("levels_at_mic_dbfs", {}) or {}
    if human:
        level_line = "- 唤醒刺激电平（麦克风代理）：真人发声，未测量"
    else:
        level_line = (
            "- 唤醒刺激电平（麦克风代理）："
            f"近 {_format_level(levels.get('near'))} / 远 {_format_level(levels.get('far'))} dBFS；"
            f"tv {_format_level(levels.get('tv'))} / small_talk "
            f"{_format_level(levels.get('small_talk'))} dBFS"
        )
    lines = [
        "# 唤醒矩阵 receipt（设备端）",
        "",
        f"- 生成时间：{receipt.get('created_at', '')}",
        stim_line,
        (
            f"- 输入范围：gain 近={input_range.get('gain_near')} / "
            f"远={input_range.get('gain_far')}（线性幅度系数）；物理距离未变化、未测量"
        ),
        (
            f"- 系统输出：运行中 {input_range.get('system_output_settings_during_run')}；"
            f"运行前 {input_range.get('system_output_settings_before_run')}（{restore_note}）"
        ),
        f"- 环境底噪（麦克风代理）：{ambient} dBFS（峰值 {ambient_peak}）",
        level_line,
        "",
        "## 唤醒召回（真唤醒）",
        "",
        (
            f"- 近距档：{near.get('wakes')}/{near.get('trials')} 唤醒，"
            f"漏唤醒 {near.get('misses')}，无效 {near.get('invalid')}"
        ),
        (
            f"- 远距档：{far.get('wakes')}/{far.get('trials')} 唤醒，"
            f"漏唤醒 {far.get('misses')}，无效 {far.get('invalid')}"
        ),
    ]
    if human:
        onset_latencies = [
            trial["latency_speech_onset"]
            for trial in trials
            if trial.get("outcome") == "wake" and trial.get("latency_speech_onset") is not None
        ]
        if onset_latencies:
            mean_onset = sum(onset_latencies) / len(onset_latencies)
            lines.append(
                f"- 平均延迟（开口起算）：{mean_onset:.2f} s（{len(onset_latencies)} 次真唤醒）"
            )
        else:
            lines.append("- 延迟：本次无开口戳（未用 --human-confirm），仅记录提示时刻延迟")
    lines += ["", "## 误唤醒", ""]
    for window in windows:
        wakes = window.get("wakes", [])
        wall = window.get("finished_monotonic", 0.0) - window.get("started_monotonic", 0.0)
        exposure = window.get("effective_exposure_s")
        exposure_note = f"，有效曝光 {exposure:.1f}s/{wall:.1f}s" if exposure is not None else ""
        state_note = (
            f"；无效：{window['invalid_reason']}" if window.get("invalid_reason") else ""
        )
        lines.append(
            f"- {window.get('label')}：{len(wakes)} 次（gain={window.get('gain')}{exposure_note}）"
            f"{state_note}"
        )
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
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
