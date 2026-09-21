#!/usr/bin/env python3
"""Bounded automated audio acceptance driver (run live; machine-side evidence only).

Plays a Memoria wake word and questions through this Mac's speakers and records the
robot with this Mac's microphone, while `scripts/voice_session_capture.py` owns the
serial port and follows the production log streams.

What the evidence is, and is not
--------------------------------
* The self-test is a pass only when the prompt that was played is located in the take by
  the analyzer's reference match (`selftest-prompt.aiff`, compared through ffmpeg/numpy).
  A reference that is missing or cannot be decoded is reported as an analysis dependency,
  never as a broken microphone or speaker, and no run continues without that pass.
* Each turn's content `speaking` span is bound to the completed generation's
  session/turn/generation/tool fence, and only a span that contains that generation's
  `first_frame_sent`..`playback_ended` delivery window (150 ms clock allowance) is handed
  to the audio side as the answer.  An acknowledgement, a span that never closed, a
  leftover from an earlier wake or an ambiguous pair stays diagnostic.
* The recording's own clock is never treated as an affine function of wall time.  The
  spawn and stop times are recorded for the log, alignment comes from prompt anchors, and
  the audio verdict stays `unverified_alignment` when those anchors do not bound a window.
  A microphone-side observation therefore never becomes `Actual Heard` or P0 acceptance.

Turn-completion rule (the reason this script is not trivial)
-----------------------------------------------------------
A tool turn emits at least two generations: an acknowledgement ("let me check") and the
content answer.  The acknowledgement's `playback_ended` and the device's
`speaking -> listening` transition are therefore NOT the end of the turn.  A turn counts
as finished only when all of these hold for one generation:

1. the runtime reports the turn is back to listening rather than waiting on a tool:
   `interaction_phase from=speaking to=listening|idle cause=media_playback_ack
   turn_id=T generation_id=G`;
2. that same generation G has both `first_frame_sent` and
   `playback_ended terminal_reason=playback_completed`;
3. if the turn performed a weather lookup, G started after that lookup completed;
4. the device's current serial state is `listening` or `idle`.

Every bridge marker is bound to the device session observed for this run: a line whose
`session_id`/`session` or `stream_epoch` differs from ours is dropped before any waiter
can see it, so a concurrent session on another device cannot satisfy a wait.

Red lines: no PCM injection, no device or config edits, no owner/enrollment forgery, no
voiceprint, wake or VAD threshold changes, and no system-setting edits.  Every wait is
clamped to one global deadline, and cleanup always stops the capture process and every
ffmpeg recorder, including the self-test one.

    python3 auto_audio_session.py --dry-run --run-dir ... --firmware-receipt ...
    python3 auto_audio_session.py --run     --run-dir ... --firmware-receipt ...
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from auto_audio_analyze import (  # noqa: E402
    ACTIVE_DBFS,
    ALIGNMENT_TOLERANCE_MS,
    SELFTEST_MIN_ABOVE_FLOOR_DB,
    SELFTEST_MIN_ACTIVE_MS,
    SELFTEST_MIN_ACTIVE_RATIO,
    SELFTEST_MIN_LEVEL_RANGE_DB,
    SELFTEST_MIN_RMS_DBFS,
    analyse_selftest,
    selftest_verdict,
)

CAPTURE_SCRIPT = "scripts/voice_session_capture.py"
SAMPLE_RATE = 16000
# ffmpeg writes a 44-byte WAV header and then 32 kB/s of PCM, so this many bytes prove
# that samples are really flowing rather than a permission dialog blocking the process.
RECORDER_MIN_BYTES = 4096
# The WAV file alone is a slow liveness signal: ffmpeg holds ~256 KiB in the muxer's buffer
# before flushing, so the file stays at its header until ~8 s of 16 kHz PCM exist.  Measured
# on this Mac on 2026-09-21 with the recorder's own argv, a 12 s take first showed bytes at
# 10.09 s, which is why the old 10 s default raced the flush and refused a working
# microphone (the same ~10 s flush is what the 2026-09-20 device window observed).  The
# `-progress` stream reports the muxer's own byte counter inside that buffer, and this
# fallback bound is kept at roughly 2.5x the measured flush time for builds that do not
# report progress at all.
FFMPEG_START_TIMEOUT_S = 25.0
# `-progress pipe:2` writes one block per ~0.5 s to stderr, which the recorder already
# redirects into its own log file.
PROGRESS_TOTAL_SIZE = re.compile(r"^total_size=(?P<total_size>\d+)\s*$", re.MULTILINE)
PROGRESS_OUT_TIME = re.compile(r"^out_time_us=(?P<out_time_us>-?\d+)\s*$", re.MULTILINE)
# A completed generation's delivery window (`first_frame_sent` .. `playback_ended`) is
# matched against the board's serial `speaking` spans.  The board stamps serial lines with
# its own local clock while the server stamps delivery lines in UTC, so the two clocks are
# allowed to differ by this much on either edge before a span stops counting as "contains
# the delivery".  A looser allowance would let the acknowledgement's span swallow the
# content generation's first frame and be credited as the answer.
DELIVERY_WINDOW_TOLERANCE_MS = 150.0
# A wake line is only accepted when the board's own stamp is not clearly older than the
# prompt this attempt played: anything older is a leftover (an ambient wake, or a line
# replayed from an earlier attempt) and must not open a session.
WAKE_FRESHNESS_TOLERANCE_S = 1.5
# A stamp further back than this cannot be a stale capture of our window, because only
# lines that arrived after the mark are scanned.  The board clock is then simply not
# comparable to this Mac's, so the line is accepted on arrival order and flagged instead
# of being silently refused.
WAKE_CLOCK_SANITY_S = 120.0

SERIAL_TS = re.compile(r"^\[(?P<ts>[^\]]+)\]")
STATE = re.compile(r"StateMachine: State: (?P<src>\S+) -> (?P<dst>\S+)")
WAKE = re.compile(r"Custom wake word detected")
LOG_TS = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T[0-9:.]+Z)\s")
PHASE = re.compile(
    r"interaction_phase from=(?P<src>\S+) to=(?P<dst>\S+) cause=(?P<cause>\S+)"
)
DELIVERY = re.compile(
    r"epoch-(?P<session_epoch>\d+)/turn-(?P<turn_id>\d+)/generation-(?P<generation_id>\d+)"
    r"/tool-(?P<tool_epoch>\d+)"
)
SESSION_ID = re.compile(r"session(?:_id)?=(?P<session_id>[0-9a-fA-F-]{8,})")
SESSION_EPOCH = re.compile(r"session_epoch=(?P<session_epoch>\d+)")
STREAM_EPOCH = re.compile(r"stream_epoch=(?P<stream_epoch>\d+)")
TURN_ID = re.compile(r"turn_id=(?P<turn_id>\d+)")
GEN_ID = re.compile(r"generation_id=(?P<generation_id>\d+)")
TEXT_LEN = re.compile(r"text_len=(?P<text_len>\d+)")
# The scoped line is "media turn committed ..." (with the session/fence); the agent-side one
# is "turn_committed" and carries no session at all.
COMMIT = re.compile(r"turn_committed|media turn committed")
PLAYBACK_END = re.compile(r"event=playback_ended .*terminal_reason=(?P<reason>\S+)")
FIRST_FRAME = re.compile(r"event=first_frame_sent")
SPEAKER_REJECT = re.compile(r"speaker_reject context=(?P<context>\S+) reason=(?P<reason>\S+)")
SPEAKER_ALLOW = re.compile(r"speaker_gate_allow reason=(?P<reason>\S+)")
TURN_IGNORED = re.compile(r"user_turn_ignored reason=(?P<reason>\S+)")
WEATHER = re.compile(r"open meteo weather lookup completed .*elapsed_ms=(?P<ms>\d+)")
SLOW_WEATHER = re.compile(r"slow weather lookup detected .*elapsed_ms=(?P<ms>\d+)")
GEOCODE = re.compile(
    r"GET https://geocoding-api\.open-meteo\.com/v1/search\?name=(?P<name>[^&\s]+)"
)
TOOL_EPOCH = re.compile(r"tool_epoch=(?P<tool_epoch>\d+)")

# Markers that identify a control or completion event.  Without a session id they cannot
# be attributed to this conversation, so they are refused rather than guessed at.
CONTROL_MARKERS = (
    "turn_committed", "first_frame_sent", "playback_ended", "interaction_phase",
    "speaker_reject", "speaker_gate_allow", "turn_ignored",
)
# Provider/HTTP lines carry no session; they are correlation evidence only, never identity.
CORRELATION_KINDS = ("weather_lookup", "slow_weather_lookup", "geocode_request")
RUNTIME_STATUS_CODE = (
    "import json, sys, httpx\n"
    "from datetime import datetime, timezone\n"
    "from services.control_api.app.config import ControlSettings\n"
    "from services.control_api.app.database import MemoryStore\n"
    "from services.control_api.app.device_control import DeviceSettingsAuthority\n"
    "from services.control_api.app.routes.device_control import _edge_internal_client_config\n"
    "settings = ControlSettings()\n"
    "base, token, timeout, verify, cert = _edge_internal_client_config(settings)\n"
    "with httpx.Client(timeout=timeout, verify=verify, cert=cert) as client:\n"
    "    r = client.get(base + '/v1/internal/device-runtime/status',\n"
    "                   headers={'X-Memoria-Edge-Control-Token': token},\n"
    "                   params={'device_id': sys.argv[1]})\n"
    "    r.raise_for_status()\n"
    "    d = r.json()\n"
    "keys = ['device_id', 'connected', 'connected_at', 'session_id', 'stream_epoch',\n"
    "        'conversation_state', 'audio_mode']\n"
    "out = {k: d[k] for k in keys if k in d}\n"
    "# A barge can only be judged if the sources this device is configured for are known, so\n"
    "# read them from the same store the runtime claims are built from.  The authority falls\n"
    "# back to built-in defaults when the device has no settings row, so the origin is\n"
    "# reported alongside the value and a defaults answer is not a permission.\n"
    "try:\n"
    "    store = MemoryStore(settings.memoria_db_path)\n"
    "    current = DeviceSettingsAuthority(store).current(sys.argv[1],\n"
    "                                                     now=datetime.now(timezone.utc))\n"
    "    out['allowed_barge_in'] = list(current.allowed_barge_in)\n"
    "    out['allowed_barge_in_origin'] = ('defaults' if current.update_reason == 'defaults'\n"
    "                                      else 'configured_row')\n"
    "    out['settings_version'] = current.settings_version\n"
    "except Exception as exc:\n"
    "    out['barge_settings_error'] = type(exc).__name__ + ': ' + str(exc)[:200]\n"
    "print(json.dumps(out))\n"
)

DEFAULT_QUESTIONS = (
    "明天南京天气怎么样？",
    "未来三天南京天气怎么样？",
    "今天南京天气怎么样？",
)
# The acceptance grid for a follow-up question: how long after the reply's own playback end
# the next question is played.  The first question follows the welcome, so it consumes no
# delay; the last value repeats when there are more questions than delays.
DEFAULT_FOLLOW_UP_DELAYS = "3,5,8"
# A follow-up cell counts as exercised only when the prompt reached its planned delay this
# closely.  Anything later was a wake, a replayed greeting or scheduling the acceptance grid
# never asked for, and it is reported as late instead of being credited to the planned cell.
FOLLOW_UP_TOLERANCE_S = 0.5
DEFAULT_WAKE_TEXT = "茉莉"
ADDRESSABLE_STATES = ("listening",)


def now_local() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def to_local(utc_stamp: str) -> str:
    return (
        datetime.fromisoformat(utc_stamp.replace("Z", "+00:00"))
        .astimezone()
        .isoformat(timespec="milliseconds")
    )


def parse_local(value: str) -> datetime:
    return datetime.fromisoformat(value)


def repo_root() -> Path:
    for parent in (HERE, *HERE.parents):
        if (parent / CAPTURE_SCRIPT).exists():
            return parent
    raise SystemExit(f"could not find {CAPTURE_SCRIPT} above {HERE}")


class Deadline:
    """One global budget; every wait in the run is clamped to what is left of it."""

    def __init__(self, seconds: float) -> None:
        self.end = time.monotonic() + seconds

    def remaining(self) -> float:
        return self.end - time.monotonic()

    def clamp(self, timeout: float) -> float:
        return max(0.0, min(timeout, self.remaining()))


class Stopped(Exception):
    """Raised from the signal handler so a stop still runs the cleanup path."""


class BudgetExpired(Exception):
    """Raised instead of starting work the global deadline no longer covers."""


def _raise_stopped(signum: int, _frame: object) -> None:
    raise Stopped(f"signal {signal.Signals(signum).name}")


def install_stop_handlers() -> None:
    """Make SIGINT/SIGTERM unwind through the finally block instead of killing the run."""

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, _raise_stopped)
        except (OSError, ValueError):
            pass


def ignore_stop_handlers() -> None:
    """Let cleanup finish even if more stop signals arrive while it unwinds."""

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, signal.SIG_IGN)
        except (OSError, ValueError):
            pass


def run_capture_output(argv: list[str], *, timeout: float = 30.0) -> tuple[int, str]:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def make_probe(deadline: Deadline, budget: float = 45.0):
    """One bounded probe callable shared by the read-only preflight helpers.

    The remaining budget is read at every call instead of being frozen when the probe is
    created, and a probe whose budget is already spent is never started at all (exit 125).
    """

    def probe(argv: list[str]) -> tuple[int, str]:
        allowed = deadline.clamp(budget)
        if allowed <= 0:
            return 125, ""
        try:
            return run_capture_output(argv, timeout=max(1.0, allowed))
        except subprocess.TimeoutExpired:
            return 124, ""
        except OSError as error:
            return 1, str(error)

    return probe


def read_volume_settings(probe) -> dict[str, object]:
    code, text = probe(["osascript", "-e", "get volume settings"])
    settings: dict[str, object] = {"raw": text.strip(), "exit_code": code}
    match = re.search(r"output volume:(\d+)", text)
    settings["output_volume"] = int(match.group(1)) if match else None
    match = re.search(r"output muted:(true|false)", text)
    settings["output_muted"] = (match.group(1) == "true") if match else None
    return settings


def list_avfoundation_audio_devices(probe) -> list[tuple[int, str]]:
    """Enumerate avfoundation devices; this captures no audio."""

    _, text = probe(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""]
    )
    devices: list[tuple[int, str]] = []
    in_audio = False
    for line in text.splitlines():
        if "AVFoundation audio devices:" in line:
            in_audio = True
            continue
        if "AVFoundation video devices:" in line:
            in_audio = False
            continue
        match = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line) if in_audio else None
        if match:
            devices.append((int(match.group(1)), match.group(2)))
    return devices


def default_input_device_name(probe) -> str | None:
    try:
        code, text = probe(["system_profiler", "SPAudioDataType", "-json"])
        payload = json.loads(text) if code == 0 else {}
    except json.JSONDecodeError:
        return None
    for section in payload.get("SPAudioDataType") or []:
        for item in section.get("_items", []) or []:
            flag = str(item.get("coreaudio_default_audio_input_device", "")).lower()
            if flag == "spaudio_yes":
                return item.get("_name")
    return None


def pick_input_device(probe) -> tuple[int | None, str, list[tuple[int, str]]]:
    devices = list_avfoundation_audio_devices(probe)
    if not devices:
        return None, "no avfoundation audio device was enumerated", devices
    wanted = default_input_device_name(probe)
    if wanted:
        for index, name in devices:
            if name == wanted:
                return index, f"matched the default input device {name!r}", devices
        for index, name in devices:
            if wanted in name or name in wanted:
                return index, f"fuzzy-matched the default input device {name!r}", devices
    return devices[0][0], f"fell back to the first audio device {devices[0][1]!r}", devices


def _last_json_object(text: str) -> dict[str, object] | None:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    return None


def fetch_device_runtime(args, deadline: Deadline) -> tuple[dict[str, object] | None, str | None]:
    """Read this board's authoritative session/stream straight from the device runtime.

    The Edge control token is resolved *inside* the control-api container by the snippet
    below, so no secret ever reaches this process or its argv.  The ssh call and the query
    behind it are both bounded, and the snapshot is read-only.
    """

    budget = deadline.clamp(args.runtime_status_timeout_s)
    if budget <= 0:
        return None, "the global deadline expired before the device runtime snapshot"
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2",
        args.ssh_host, "docker", "exec", "-w", "/app", "memoria-control-api-1",
        "/app/.venv/bin/python", "-c", _remote_status_program(args.device_id),
    ]
    try:
        code, text = run_capture_output(argv, timeout=budget)
    except subprocess.TimeoutExpired:
        return None, f"device runtime snapshot did not answer within {budget:.1f}s"
    except OSError as error:
        return None, f"device runtime snapshot could not run: {error}"
    if code != 0:
        return None, f"device runtime snapshot exited {code}: {text.strip()[:200]}"
    payload = _last_json_object(text)
    if payload is None:
        return None, f"device runtime snapshot returned no JSON object: {text.strip()[:200]}"
    return payload, None


def _remote_status_program(device_id: str) -> str:
    """Wrap the status snippet as one shell-safe line.

    ssh hands the command to the remote shell, which would split a multi-line `-c`
    program on its newlines and every line after the first would fail.  Base64 keeps the
    payload free of quotes, `$`, backticks and newlines, so exactly one quoted argument
    reaches python.  The device id is embedded, not passed as another argument.
    """

    code = RUNTIME_STATUS_CODE.replace("sys.argv[1]", repr(device_id))
    payload = base64.b64encode(code.encode("utf-8")).decode("ascii")
    # Double quotes keep it a single remote-shell word; the program itself has no spaces,
    # `$`, backticks or double quotes, so nothing else can be re-split or expanded.
    single = chr(39)
    double = chr(34)
    program = (
        "exec(__import__(" + single + "base64" + single + ").b64decode("
        + single + payload + single + ").decode())"
    )
    return double + program + double


class FileTail:
    """Read only the bytes appended to a file since the previous poll."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._buffer = ""

    def poll(self) -> list[str]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
                self._offset = handle.tell()
        except OSError:
            return []
        if not chunk:
            return []
        self._buffer += chunk.decode("utf-8", errors="replace")
        lines: list[str] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            lines.append(line)
        return lines


class EventLog:
    """Append-only JSONL of prompts, windows and markers for the analyzer."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = path.open("a", encoding="utf-8")

    def add(self, kind: str, **fields: object) -> None:
        payload = {"kind": kind, "t_local": now_local(), **fields}
        self.handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.handle.flush()

    def close(self) -> None:
        try:
            self.handle.close()
        except OSError:
            pass


@dataclass
class LogLine:
    source: str
    t_local: str
    kind: str
    arrived: float = 0.0
    session_id: str | None = None
    session_epoch: int | None = None
    stream_epoch: int | None = None
    turn_id: int | None = None
    generation_id: int | None = None
    tool_epoch: int | None = None
    fields: dict[str, object] = field(default_factory=dict)
    raw: str = ""


def _int(match: re.Match[str] | None) -> int | None:
    return int(match.group(1)) if match else None


def classify(source: str, raw: str, arrived: float) -> LogLine | None:
    """Turn one raw log line into a typed marker, or None when it is not interesting."""

    if source == "serial":
        stamp = SERIAL_TS.match(raw)
        t_local = stamp.group("ts") if stamp else now_local()
        match = STATE.search(raw)
        if match:
            return LogLine(
                source, t_local, "device_state", arrived,
                fields={"src": match.group("src"), "dst": match.group("dst")}, raw=raw,
            )
        if WAKE.search(raw):
            return LogLine(source, t_local, "wake_detected", arrived, raw=raw)
        return None

    stamp = LOG_TS.match(raw)
    if not stamp:
        return None
    arrival = LogLine(source, to_local(stamp.group("ts")), "", arrived, raw=raw)
    delivery = DELIVERY.search(raw)
    session_id = SESSION_ID.search(raw)
    arrival.session_id = session_id.group("session_id") if session_id else None
    # Explicit None checks: an epoch or generation of 0 is a real value, not a miss.
    arrival.session_epoch = _int(SESSION_EPOCH.search(raw))
    if arrival.session_epoch is None and delivery:
        arrival.session_epoch = int(delivery.group("session_epoch"))
    arrival.stream_epoch = _int(STREAM_EPOCH.search(raw))
    arrival.turn_id = _int(TURN_ID.search(raw))
    if arrival.turn_id is None and delivery:
        arrival.turn_id = int(delivery.group("turn_id"))
    arrival.generation_id = _int(GEN_ID.search(raw))
    if arrival.generation_id is None and delivery:
        arrival.generation_id = int(delivery.group("generation_id"))
    arrival.tool_epoch = _int(TOOL_EPOCH.search(raw))
    if arrival.tool_epoch is None and delivery:
        arrival.tool_epoch = int(delivery.group("tool_epoch"))

    if COMMIT.search(raw):
        arrival.kind = "turn_committed"
        arrival.fields = {"text_len": _int(TEXT_LEN.search(raw))}
        return arrival
    match = PLAYBACK_END.search(raw)
    if match:
        arrival.kind = "playback_ended"
        arrival.fields = {"reason": match.group("reason")}
        return arrival
    if FIRST_FRAME.search(raw):
        arrival.kind = "first_frame_sent"
        return arrival
    match = PHASE.search(raw)
    if match:
        arrival.kind = "interaction_phase"
        arrival.fields = {"src": match.group("src"), "dst": match.group("dst"),
                          "cause": match.group("cause")}
        return arrival
    match = SPEAKER_REJECT.search(raw)
    if match:
        arrival.kind = "speaker_reject"
        arrival.fields = {"context": match.group("context"), "reason": match.group("reason")}
        return arrival
    match = SPEAKER_ALLOW.search(raw)
    if match:
        arrival.kind = "speaker_gate_allow"
        arrival.fields = {"reason": match.group("reason")}
        return arrival
    match = TURN_IGNORED.search(raw)
    if match:
        arrival.kind = "turn_ignored"
        arrival.fields = {"reason": match.group("reason")}
        return arrival
    match = WEATHER.search(raw)
    if match:
        arrival.kind = "weather_lookup"
        arrival.fields = {"elapsed_ms": int(match.group("ms"))}
        return arrival
    match = SLOW_WEATHER.search(raw)
    if match:
        arrival.kind = "slow_weather_lookup"
        arrival.fields = {"elapsed_ms": int(match.group("ms"))}
        return arrival
    match = GEOCODE.search(raw)
    if match:
        arrival.kind = "geocode_request"
        arrival.fields = {"name": match.group("name")}
        return arrival
    return None


class LogSession:
    """Bound view of the capture's serial and bridge logs.

    The binding is authoritative and comes from this board's device runtime snapshot, not
    from the first session id that happens to appear in a log line.  A bridge line is
    admitted only when it carries that session, and that stream epoch when one is known.
    A session-less line is either kept as correlation-only evidence (provider/HTTP
    lookups) or refused outright when it is a control or completion marker, because such a
    marker cannot be attributed to this conversation.
    """

    def __init__(self, tails: dict[str, FileTail]) -> None:
        self.tails = tails
        self.lines: list[LogLine] = []
        self.unscoped: list[LogLine] = []
        self.foreign: list[dict[str, object]] = []
        self.unattributed: list[dict[str, object]] = []
        self.binding: str | None = None
        self.stream_epoch: int | None = None
        self.binding_source = ""
        self.enforce_stream_epoch = True
        self.unbind_reason = ""
        self._held: list[LogLine] = []

    @property
    def bound(self) -> bool:
        return self.binding is not None

    def unbind(self, reason: str) -> None:
        """Drop the identity before a fresh wake, so the new session binds from scratch.

        Without this, a re-wake would still be bound to the previous session and the new
        greeting could be rejected as foreign.
        """

        self.binding = None
        self.stream_epoch = None
        self.binding_source = ""
        self._held = []
        self.unbind_reason = reason

    def bind(self, session_id: str, *, stream_epoch: int | None = None,
             source: str = "device_runtime", enforce_stream_epoch: bool = True) -> dict[str, object]:
        """Set this board's authoritative identity and replay the held lines against it.

        Nothing is ever bound from a log line: only the device runtime snapshot decides
        which conversation this capture belongs to.  Held lines from before the snapshot
        are replayed here, and any that do not match are rejected rather than adopted.
        """

        previous = self.binding
        self.binding = session_id
        self.binding_source = source
        self.enforce_stream_epoch = enforce_stream_epoch
        # Always overwrite, including with None: an inherited epoch from a previous wake
        # would silently accept this session's lines for the wrong stream.
        self.stream_epoch = stream_epoch
        held, self._held = self._held, []
        for earlier in held:
            self._admit(earlier)
        return {
            "session_id": session_id,
            "stream_epoch": self.stream_epoch,
            "source": source,
            "previous_session_id": previous,
            "rebound": bool(previous and previous != session_id),
        }

    def lookups(self) -> list[LogLine]:
        """Session-less provider lines, kept for correlation only and never for identity."""

        return list(self.unscoped)

    def poll(self) -> None:
        for source, tail in self.tails.items():
            for raw in tail.poll():
                self.feed(source, raw)

    def feed(self, source: str, raw: str, arrived: float | None = None) -> LogLine | None:
        """Classify and admit one raw line; the seam the offline fixtures drive."""

        line = classify(source, raw, time.monotonic() if arrived is None else arrived)
        if line is not None:
            self._admit(line)
        return line

    def _admit(self, line: LogLine) -> None:
        if line.source == "serial":
            self.lines.append(line)
            return
        if not self.bound:
            self._held.append(line)
            return
        if not line.session_id:
            if line.kind in CORRELATION_KINDS:
                self.unscoped.append(line)
            else:
                self.unattributed.append({"reason": "no_session_id", "kind": line.kind, "raw": line.raw})
            return
        if line.session_id != self.binding:
            self.foreign.append({"reason": "session_id", "session_id": line.session_id,
                                 "kind": line.kind, "raw": line.raw})
            return
        if (
            self.enforce_stream_epoch
            and line.stream_epoch is not None
            and self.stream_epoch is not None
            and line.stream_epoch != self.stream_epoch
        ):
            self.foreign.append({"reason": "stream_epoch", "stream_epoch": line.stream_epoch,
                                 "expected": self.stream_epoch, "kind": line.kind, "raw": line.raw})
            return
        self.lines.append(line)

    def device_state(self) -> str | None:
        for line in reversed(self.lines):
            if line.kind == "device_state":
                return str(line.fields["dst"])
        return None

    def mark(self) -> int:
        return len(self.lines)

    def since(self, mark: int) -> list[LogLine]:
        return self.lines[mark:]

    def first(self, mark: int, kinds: tuple[str, ...]) -> LogLine | None:
        for line in self.since(mark):
            if line.kind in kinds:
                return line
        return None


class TurnTracker:
    """Decide when one question's turn is really finished (see the module docstring)."""

    def __init__(self) -> None:
        self.turn_id: int | None = None
        self.session_epoch: int | None = None
        self.tool_epoch: int | None = None
        self.generations: dict[int, dict[str, object]] = {}
        self.listening_generation: int | None = None
        self.listening_src: str | None = None
        self.device_state: str | None = None
        self.last_weather_t: str | None = None
        self.rejections: list[dict[str, object]] = []
        self.allows: list[str] = []
        self.playback_reasons: list[str] = []
        self.ignored_commits: list[dict[str, object]] = []
        self.fence_rejected: list[dict[str, object]] = []
        self.incomplete_markers: list[dict[str, object]] = []
        # Generations whose media playback ended into a tool wait, i.e. the spoken
        # acknowledgement ("let me check").  They are never the content, so a window
        # covering one may not be credited as the answer even if it is the only window.
        self.tool_waiting_acks: set[int] = set()

    def _incomplete(self, line: LogLine, fields: tuple[str, ...]) -> bool:
        """Control/completion evidence is only usable with its whole fence present."""

        missing = [name for name in fields if getattr(line, name) is None]
        if missing:
            self.incomplete_markers.append({"kind": line.kind, "missing": missing, "raw": line.raw})
            return True
        return False

    def _fence_ok(self, line: LogLine) -> bool:
        """The whole fence has to agree: turn, session epoch and tool epoch.

        Called only for lines that already passed the completeness check, so every bound
        value is compared rather than skipped when a field happens to be missing.
        """

        # A missing field is a mismatch for evidence markers, so a caller that forgot the
        # completeness check still cannot get a free pass.
        return self._compare(line, require_present=True, scope="evidence")

    def _aux_matches(self, line: LogLine) -> bool:
        """Auxiliary comparison for lines that legitimately carry only part of the fence.

        `interaction_phase` names a turn and generation but carries no session or tool
        epoch in the real logs.  It may only associate a generation that already has a
        complete fence of its own; it can never establish completion by itself, so a
        missing field is not a mismatch here.
        """

        return self._compare(line, require_present=False, scope="auxiliary")

    def _compare(self, line: LogLine, *, require_present: bool, scope: str) -> bool:
        for name, bound, actual in (
            ("turn_id", self.turn_id, line.turn_id),
            ("session_epoch", self.session_epoch, line.session_epoch),
            ("tool_epoch", self.tool_epoch, line.tool_epoch),
        ):
            if actual is None and not require_present:
                continue
            if actual != bound:
                self.fence_rejected.append(
                    {"reason": name, "bound": bound, "observed": actual, "kind": line.kind,
                     "scope": scope, "raw": line.raw}
                )
                return False
        return True

    def observe(self, line: LogLine) -> None:
        if line.kind == "device_state":
            self.device_state = str(line.fields["dst"])
            return
        if line.kind == "turn_committed":
            if self._incomplete(line, ("turn_id", "generation_id", "session_epoch", "tool_epoch")):
                return
            if self.turn_id is None:
                # The first commit seen after the prompt fixes the fence for this turn; a
                # later commit never silently re-points the tracker at another turn.
                self.turn_id = line.turn_id
                self.session_epoch = line.session_epoch
                self.tool_epoch = line.tool_epoch
            elif line.turn_id is not None and line.turn_id != self.turn_id:
                self.ignored_commits.append(
                    {"turn_id": line.turn_id, "generation_id": line.generation_id, "raw": line.raw}
                )
            return
        if line.kind == "first_frame_sent" and line.generation_id is not None:
            if self._incomplete(line, ("turn_id", "generation_id", "session_epoch", "tool_epoch")):
                return
            if not self._fence_ok(line):
                return
            entry = self.generations.setdefault(line.generation_id, {})
            entry["first_frame"] = True
            entry["first_frame_t"] = line.t_local
            entry.setdefault("started_t", line.t_local)
            return
        if line.kind == "playback_ended" and line.generation_id is not None:
            if self._incomplete(line, ("turn_id", "generation_id", "session_epoch", "tool_epoch")):
                return
            if not self._fence_ok(line):
                return
            entry = self.generations.setdefault(line.generation_id, {})
            entry.setdefault("started_t", line.t_local)
            entry["playback_ended_t"] = line.t_local
            entry["playback_reason"] = line.fields.get("reason")
            self.playback_reasons.append(str(line.fields.get("reason")))
            return
        if line.kind == "weather_lookup":
            self.last_weather_t = line.t_local
            return
        if line.kind in ("speaker_reject", "turn_ignored"):
            self.rejections.append({"kind": line.kind, **line.fields})
            return
        if line.kind == "speaker_gate_allow":
            self.allows.append(str(line.fields.get("reason")))
            return
        if line.kind == "interaction_phase" and line.generation_id is not None:
            # Auxiliary: this line only says which generation the runtime is moving
            # between.  It carries no session/tool epoch, so it is matched on what it has
            # and never used on its own to decide the turn is finished.
            if not self._aux_matches(line):
                return
            entry = self.generations.setdefault(line.generation_id, {})
            if line.fields.get("dst") == "speaking":
                entry["started_t"] = line.t_local
            elif (
                line.fields.get("dst") == "tool_waiting"
                and line.fields.get("cause") == "media_playback_ack"
            ):
                self.tool_waiting_acks.add(line.generation_id)
            elif (
                line.fields.get("dst") in ("listening", "idle")
                and line.fields.get("cause") == "media_playback_ack"
            ):
                self.listening_generation = line.generation_id
                self.listening_src = str(line.fields.get("src"))

    def completion(self) -> dict[str, object] | None:
        """All four conditions from the module docstring, or None while still pending."""

        if self.turn_id is None:
            return None
        generation = self.listening_generation
        if generation is None or self.listening_src != "speaking":
            return None
        entry = self.generations.get(generation) or {}
        if not entry.get("first_frame") or entry.get("playback_reason") != "playback_completed":
            return None
        if self.device_state not in ("listening", "idle"):
            return None
        content_after_weather: bool | None = None
        if self.last_weather_t is not None:
            started = entry.get("started_t")
            content_after_weather = bool(
                started and parse_local(str(started)) > parse_local(self.last_weather_t)
            )
            if not content_after_weather:
                return None
        return {
            "turn_id": self.turn_id,
            "session_epoch": self.session_epoch,
            "tool_epoch": self.tool_epoch,
            "generation_id": generation,
            "generations_seen": sorted(self.generations),
            "acknowledgement_generations": sorted(g for g in self.generations if g != generation),
            "content_after_weather": content_after_weather,
            "weather_correlation": "unscoped_provider_log" if self.last_weather_t else None,
            # The completed generation's own delivery window, so the audio side can bind a
            # serial `speaking` span to this exact generation instead of to the last one.
            "first_frame_t": entry.get("first_frame_t"),
            "playback_ended_t": entry.get("playback_ended_t"),
            "acknowledgement_marker": bool(
                generation in self.tool_waiting_acks
            ),
        }

    def server_evidence(self) -> dict[str, object]:
        return {
            "turn_id": self.turn_id,
            "session_epoch": self.session_epoch,
            "tool_epoch": self.tool_epoch,
            "generations_seen": sorted(self.generations),
            "generation_detail": {str(key): value for key, value in sorted(self.generations.items())},
            "playback_reasons": self.playback_reasons,
            "speaker_rejections": self.rejections,
            "speaker_allows": self.allows,
            "tool_waiting_ack_generations": sorted(self.tool_waiting_acks),
            "ignored_commits": self.ignored_commits,
            "fence_rejected": self.fence_rejected,
            "incomplete_markers": self.incomplete_markers,
        }


class Recorder:
    """One bounded ffmpeg avfoundation capture to a single 16 kHz mono WAV."""

    def __init__(self, log_path: Path, device_index: int, out_path: Path, max_seconds: int) -> None:
        self.log_path = log_path
        self.device_index = device_index
        self.out_path = out_path
        self.max_seconds = max_seconds
        self.proc: subprocess.Popen[bytes] | None = None
        self._handle = None
        # Process spawn clock, not a sample clock.  The measured gap between this stamp
        # and the take's own timeline is not a constant offset (live takes showed a
        # multi-second startup gap and a rate difference between wall and sample time), so
        # it is recorded for the log only: window alignment is derived from prompt anchors
        # by the analyzer rather than from this timestamp.
        self.spawn_local = ""
        self.ended_local = ""
        self.stopped = False
        # Set by emit_recorder_stop: whichever caller stops this recorder first owns the
        # single recording_stop event for it.
        self.stop_event_written = False

    def start(self) -> None:
        self.spawn_local = now_local()
        argv = [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning",
            # Machine-readable progress on stderr (which is this recorder's log file) so
            # liveness no longer depends on the muxer flushing its buffer to the WAV.
            "-nostats", "-progress", "pipe:2",
            "-f", "avfoundation", "-i", f":{self.device_index}",
            "-ac", "1", "-ar", str(SAMPLE_RATE), "-c:a", "pcm_s16le",
            "-t", str(self.max_seconds), "-y", str(self.out_path),
        ]
        # Keep the handle referenced: dropping it would close the parent's copy of the
        # descriptor while ffmpeg is still writing to it.
        self._handle = self.log_path.open("ab", buffering=0)
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=self._handle, stderr=subprocess.STDOUT
        )

    def file_bytes(self) -> int:
        """Bytes on disk; the muxer's buffer makes this lag by up to ~256 KiB."""
        try:
            return self.out_path.stat().st_size
        except OSError:
            return 0

    def progress(self) -> dict[str, int]:
        """The latest `-progress` block ffmpeg wrote into this recorder's log.

        Empty when the build writes no progress at all, which leaves the file-size path in
        charge - the caller only ever treats a reported counter as *additional* evidence.
        """
        try:
            text = self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {}
        found: dict[str, int] = {}
        for match in PROGRESS_TOTAL_SIZE.finditer(text):
            found["total_size"] = int(match.group("total_size"))
        for match in PROGRESS_OUT_TIME.finditer(text):
            found["out_time_us"] = int(match.group("out_time_us"))
        return found

    def bytes_written(self) -> int:
        """The best of what the file shows and what ffmpeg reports it has muxed."""
        reported = self.progress().get("total_size", 0)
        return max(self.file_bytes(), reported)

    def live(self) -> bool:
        return self.bytes_written() > RECORDER_MIN_BYTES

    def stop(self, grace: float = 10.0) -> None:
        """Stop ffmpeg once; a later call must not move the recorded stop time.

        Cleanup always runs, including on the paths where the self-test has already stopped
        this recorder, so a second stop has to be a no-op: re-dating `ended_local` there
        would silently move a stop that the run's other evidence is already anchored to.
        """

        if self.proc is None or self.stopped:
            return
        if self.proc.poll() is None:
            # SIGINT makes ffmpeg finalise the WAV header instead of truncating it.
            try:
                self.proc.send_signal(signal.SIGINT)
            except OSError:
                pass
        try:
            self.proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.stopped = True
        # First write wins: the stop time of a recorder is evidence, not a scratch value.
        if not self.ended_local:
            self.ended_local = now_local()
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    @property
    def returncode(self) -> int | None:
        return None if self.proc is None else self.proc.returncode


def emit_recorder_stop(events: EventLog, recorder) -> bool:
    """Write this recorder's stop event exactly once, whoever stopped it first.

    Cleanup runs on every exit path, including paths where the self-test already stopped
    its recorder, so the event is written by the first caller and the later one is a no-op.
    The payload keeps its original shape (`recording` + `start_local`) for the existing
    fixtures and reports.  Returns True only when this call wrote the event.
    """

    if getattr(recorder, "stop_event_written", False):
        return False
    ended = getattr(recorder, "ended_local", "")
    if not ended:
        return False
    events.add("recording_stop", recording=recorder.out_path.stem, start_local=ended)
    recorder.stop_event_written = True
    return True


def wait_for(session: LogSession, predicate, timeout: float, deadline: Deadline, *, poll: float = 0.2):
    limit = deadline.clamp(timeout)
    if limit <= 0:
        return None
    end = time.monotonic() + limit
    while True:
        session.poll()
        found = predicate(session)
        if found:
            return found
        now = time.monotonic()
        if now >= end:
            return None
        time.sleep(min(poll, max(0.0, end - now)))


def wait_recorder_live(recorder: Recorder, timeout: float, deadline: Deadline) -> bool:
    """Bound the moment ffmpeg starts producing samples, or proves it will not.

    A blocked microphone permission shows up as an ffmpeg that never writes, so this is
    what keeps a TCC dialog or a dead input device from hanging the run.  Two signals count
    as live (see `Recorder.bytes_written`): ffmpeg's own `-progress` byte counter, which
    answers inside a second, and the WAV file, which only moves when the muxer flushes.
    `timeout` has to cover the slower of the two, so it is sized on the flushed file.
    """

    end = time.monotonic() + deadline.clamp(timeout)
    while time.monotonic() < end:
        if recorder.proc is not None and recorder.proc.poll() is not None:
            return False
        if recorder.live():
            return True
        time.sleep(0.2)
    return False


def bounded_run(argv: list[str], deadline: Deadline, budget: float, *, label: str) -> str:
    """Run a subprocess with no more than the smaller of its budget and the run budget."""

    allowed = deadline.clamp(budget)
    if allowed <= 0:
        raise BudgetExpired(f"the global deadline left no budget for {label}")
    try:
        code, text = run_capture_output(argv, timeout=allowed)
    except subprocess.TimeoutExpired:
        raise BudgetExpired(f"{label} used more than the {allowed:.1f}s it had left") from None
    if code != 0:
        raise SystemExit(f"{label} failed: {text.strip()}")
    return text


def render_prompt(voice: str, text: str, path: Path, deadline: Deadline) -> float:
    # Never spend the remaining budget on synthesising a prompt that cannot be played.
    bounded_run(["say", "-v", voice, "-o", str(path), text], deadline, 60.0,
                label=f"say {text!r}")
    if not path.exists():
        raise SystemExit(f"say produced no file for {text!r}")
    expected = 0.0
    try:
        info = bounded_run(["afinfo", str(path)], deadline, 20.0, label="afinfo")
        match = re.search(r"estimated duration:\s*([0-9.]+)", info)
        expected = float(match.group(1)) if match else 0.0
    except (OSError, SystemExit):
        expected = 0.0
    return expected


def play(path: Path, deadline: Deadline) -> tuple[str, str, float]:
    started = now_local()
    began = time.monotonic()
    bounded_run(["afplay", str(path)], deadline, 60.0, label=f"afplay {path.name}")
    return started, now_local(), time.monotonic() - began


def speaking_windows(lines: list[LogLine]) -> list[dict[str, str]]:
    """Closed `speaking` spans only; kept as the compatibility entry point."""

    return speaking_spans(lines)[0]


def speaking_spans(lines: list[LogLine]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Split the serial state transitions into closed and never-closed `speaking` spans.

    A span that never closes means the leave transition was lost, so it has no end: it is
    reported on its own instead of being dropped or stretched to the end of the recording,
    which would let a lost line look like a long, loud answer.
    """

    closed: list[dict[str, str]] = []
    opened: str | None = None
    for line in lines:
        if line.kind != "device_state":
            continue
        if line.fields["dst"] == "speaking" and opened is None:
            opened = line.t_local
        elif line.fields["src"] == "speaking" and opened is not None:
            closed.append({"start_local": opened, "end_local": line.t_local})
            opened = None
    unclosed = [{"start_local": opened}] if opened is not None else []
    return closed, unclosed


def content_window_match(
    windows: list[dict[str, object]],
    *,
    first_frame_local: object = None,
    playback_ended_local: object = None,
    allowance_ms: float = DELIVERY_WINDOW_TOLERANCE_MS,
) -> dict[str, object]:
    """Find the one serial span that really covers this generation's delivery window.

    The comparison stays inside the log clock domain (the board's serial stamp against the
    server's delivery lines), so it does not inherit the recorder's spawn-to-first-sample
    skew.  Exactly one span has to contain `first_frame_sent` .. `playback_ended` within
    `allowance_ms`; zero spans, or two spans that both cover it, produce no content match
    rather than a guess.

    Both delivery stamps are taken as they arrive: a missing, non-string, malformed or
    inverted window is refused with its own reason instead of being stringified into a
    value that later fails to parse, and a span whose own edges do not parse is dropped as
    a candidate rather than aborting the run.
    """

    match: dict[str, object] = {
        "matched": False,
        "reason": "",
        "basis": None,
        "clock_tolerance_ms": allowance_ms,
        "delivery_first_frame_local": (
            first_frame_local if isinstance(first_frame_local, str) else None
        ),
        "delivery_playback_ended_local": (
            playback_ended_local if isinstance(playback_ended_local, str) else None
        ),
        "candidate_windows": len(windows),
        "unusable_windows": 0,
        "containing_windows": 0,
        "window": None,
    }
    if not windows:
        match["reason"] = "no_speaking_window"
        return match
    if not isinstance(first_frame_local, str) or not isinstance(playback_ended_local, str):
        match["reason"] = "delivery_window_missing"
        return match
    if not first_frame_local or not playback_ended_local:
        match["reason"] = "delivery_window_missing"
        return match
    try:
        first = parse_local(first_frame_local)
        last = parse_local(playback_ended_local)
    except ValueError:
        match["reason"] = "delivery_window_unparsable"
        return match
    if last < first:
        match["reason"] = "delivery_window_inverted"
        return match
    allowance = timedelta(milliseconds=max(0.0, float(allowance_ms)))
    containing: list[dict[str, object]] = []
    for window in windows:
        start_local = window.get("start_local")
        end_local = window.get("end_local")
        # A span needs both edges to be bounded at all; one edge, no edge or a stamp that
        # does not parse can never be the content window, so it is dropped as a candidate.
        if not isinstance(start_local, str) or not isinstance(end_local, str):
            continue
        try:
            span_start = parse_local(start_local)
            span_end = parse_local(end_local)
        except ValueError:
            match["unusable_windows"] = int(match["unusable_windows"]) + 1
            continue
        if span_start > span_end:
            match["unusable_windows"] = int(match["unusable_windows"]) + 1
            continue
        if span_start <= first + allowance and span_end >= last - allowance:
            containing.append(window)
    match["containing_windows"] = len(containing)
    if not containing:
        match["reason"] = "no_window_contains_delivery"
        return match
    if len(containing) > 1:
        # Two spans covering the same delivery window cannot both be the answer, so the
        # evidence is refused instead of resolved by picking the longest or the last one.
        match["reason"] = "ambiguous_containing_windows"
        return match
    match.update(
        {
            "matched": True,
            "reason": "one_window_contains_delivery",
            "basis": "serial_speaking_span_contains_first_frame_sent_to_playback_ended",
            "window": containing[0],
        }
    )
    return match


def fence_fields(session: LogSession, completion: dict[str, object] | None) -> dict[str, object]:
    """The session/turn/generation/tool fence a content window is attributed to."""

    return {
        "session_id": session.binding,
        "session_epoch": completion.get("session_epoch") if completion else None,
        "stream_epoch": session.stream_epoch,
        "turn_id": completion.get("turn_id") if completion else None,
        "generation_id": completion.get("generation_id") if completion else None,
        "tool_epoch": completion.get("tool_epoch") if completion else None,
    }


def record_windows(events: EventLog, session: LogSession, mark: int, index: int | None,
                   *, completion: dict[str, object] | None = None) -> dict[str, object]:
    """Write this turn's `speaking` spans, binding the content one to its generation.

    Only the span that contains the completed generation's `first_frame_sent` ..
    `playback_ended` delivery window is marked `content_matched` and carries the full
    session/turn/generation/tool fence.  Every other span (acknowledgement, greeting, one
    left over from an earlier wake) and every span that never closed stays diagnostic: a
    missing, leftover or ambiguous span is recorded as such and is never credited with the
    answer being audible.  Returns the matching decision for the run's results.
    """

    closed, unclosed = speaking_spans(session.since(mark))
    fence = fence_fields(session, completion)
    # Both delivery stamps are passed through untouched: stringifying a missing value would
    # turn an absent timestamp into the literal "None" and crash the parse below instead of
    # failing closed with a reason.
    match = content_window_match(
        closed,
        first_frame_local=completion.get("first_frame_t") if completion else None,
        playback_ended_local=completion.get("playback_ended_t") if completion else None,
    )
    if completion is None:
        match = {**match, "matched": False, "reason": "no_completed_generation", "window": None}
    elif completion.get("acknowledgement_marker"):
        # A generation whose own media playback ended into a tool wait is an
        # acknowledgement; it is never the content, even when a span covers its delivery.
        match = {**match, "matched": False,
                 "reason": "generation_is_acknowledgement", "window": None}
    matched_window = match["window"] if match["matched"] else None
    other_reason = "other_speaking_span" if match["matched"] else str(match["reason"])
    for window in closed:
        is_content = window is matched_window
        events.add(
            "device_speaking",
            recording="microphone",
            name="device_speaking",
            turn_index=index,
            **window,
            content_matched=is_content,
            content_reason="content_window" if is_content else other_reason,
            content_basis=match["basis"] if is_content else None,
            clock_tolerance_ms=match["clock_tolerance_ms"],
            delivery_first_frame_local=match["delivery_first_frame_local"],
            delivery_playback_ended_local=match["delivery_playback_ended_local"],
            **(fence if is_content else {}),
        )
    for window in unclosed:
        # No end means no usable level window, so it stays a diagnostic: `end_local` is not
        # invented, because the analyzer reads a missing end as "to the end of the take".
        events.add(
            "device_speaking_unclosed",
            recording="microphone",
            name="device_speaking",
            turn_index=index,
            **window,
            content_matched=False,
            content_reason="window_never_closed",
            clock_tolerance_ms=match["clock_tolerance_ms"],
        )
    summary: dict[str, object] = {
        **match,
        "closed_windows": len(closed),
        "unclosed_windows": len(unclosed),
        "fence": fence if matched_window is not None else None,
        # The fence is also spread flat, so a reader of the record can take the completed
        # generation's identity without unpacking the nested copy.
        **(fence if matched_window is not None else {}),
        "window": matched_window,
    }
    events.add(
        "turn_content_window",
        recording="microphone",
        name="turn_content_window",
        turn_index=index,
        **{key: value for key, value in summary.items() if key != "window"},
        window_start_local=(matched_window or {}).get("start_local"),
        window_end_local=(matched_window or {}).get("end_local"),
    )
    return summary


def preflight(args: argparse.Namespace, root: Path, deadline: Deadline) -> tuple[dict[str, object], list[str]]:
    report: dict[str, object] = {}
    blockers: list[str] = []
    # Preflight probes are read-only, but they still may not outrun the global deadline.
    probe = make_probe(deadline)
    if deadline.remaining() <= 0:
        return report, ["the global deadline expired before preflight could run"]
    for tool in ("say", "afplay", "afinfo", "ffmpeg", "uv"):
        path = shutil.which(tool)
        report[f"tool_{tool}"] = path
        if path is None:
            blockers.append(f"{tool} is not on PATH")
    receipt = Path(args.firmware_receipt)
    report["firmware_receipt"] = str(receipt)
    if not receipt.is_file():
        blockers.append(f"firmware receipt {receipt} does not exist")
    run_dir = Path(args.run_dir)
    report["run_dir"] = str(run_dir)
    if run_dir.exists():
        blockers.append(f"run dir {run_dir} already exists; pick a new one")
    port = Path(args.port)
    report["port_exists"] = port.exists()
    if not port.exists():
        blockers.append(f"serial port {port} does not exist")
    elif shutil.which("lsof"):
        _, holders = probe(["lsof", "-t", str(port)])
        report["port_holders"] = holders.split()
        if holders.strip():
            blockers.append(f"serial port {port} is already held by {holders.split()}")
    volume = read_volume_settings(probe)
    report["volume"] = volume
    if volume.get("output_muted") is True:
        blockers.append(
            "the Mac's output is muted, so a prompt would be silent and this run would be read "
            "as 'the robot did not answer'.  The operator unmutes (and restores the setting "
            "afterwards); this script never changes a system setting"
        )
    if isinstance(volume.get("output_volume"), int) and int(volume["output_volume"]) < 20:
        report["volume_warning"] = "output volume below 20%; far-field wake is unlikely"
    index, reason, devices = pick_input_device(probe)
    report["avfoundation_devices"] = devices
    report["input_device_index"] = index
    report["input_device_reason"] = reason
    if index is None:
        blockers.append("no avfoundation audio input device is available")
    if args.voice not in probe(["say", "-v", "?"])[1]:
        blockers.append(f"voice {args.voice!r} is not installed")
        report["voice_present"] = False
    else:
        report["voice_present"] = True
    report["capture_script"] = str(root / CAPTURE_SCRIPT)
    # The 150 ms is analysis padding, not a measured bound on the spawn-to-first-sample skew.
    report["alignment_uncertainty"] = "unknown"
    report["alignment_padding_ms"] = ALIGNMENT_TOLERANCE_MS
    report["alignment_note"] = (
        "started_local is the ffmpeg spawn clock; the first sample lands an unmeasured time "
        "later.  The padding is analysis-only, so no precise audible gap can be claimed."
    )
    return report, blockers


def _welcome_done(session: LogSession, mark: int):
    """The runtime has finished the greeting and is back to listening.

    Only lines seen since `mark` count: the greeting of *this* wake, never a matching
    transition left over from an earlier turn in the same capture.
    """

    if session.device_state() not in ADDRESSABLE_STATES:
        return None
    for line in session.since(mark):
        if line.kind != "interaction_phase":
            continue
        if (
            line.fields.get("src") == "speaking"
            and line.fields.get("dst") in ("listening", "idle")
            and line.fields.get("cause") == "media_playback_ack"
        ):
            return line
    return None


def fresh_wake_line(
    session: LogSession,
    mark: int,
    prompt_start_local: str,
    *,
    tolerance_s: float = WAKE_FRESHNESS_TOLERANCE_S,
    clock_sanity_s: float = WAKE_CLOCK_SANITY_S,
) -> tuple[LogLine | None, list[dict[str, object]], str]:
    """The first wake line that can belong to *this* prompt, plus the stale ones refused.

    The serial line carries the board's own local clock, so a line stamped clearly before
    the prompt is a leftover - an ambient wake, or a line replayed from an earlier attempt -
    and must not open a session.  A stamp further back than `clock_sanity_s` cannot be a
    stale capture of this window (only lines that arrived after the mark are scanned), so
    the board clock is simply not comparable with this Mac's: the line is then accepted on
    arrival order and reported as `board_clock_out_of_range` instead of being dropped.

    Returns `(line, stale, reason)`; `line` is None while no fresh wake has arrived, which
    is what keeps the caller waiting inside its own bounded wait.
    """

    try:
        prompt_start = parse_local(prompt_start_local)
        earliest = prompt_start - timedelta(seconds=max(0.0, tolerance_s))
    except ValueError:
        prompt_start = None
        earliest = None
    stale: list[dict[str, object]] = []
    for line in session.since(mark):
        if line.kind != "wake_detected":
            continue
        try:
            stamp = parse_local(line.t_local)
        except ValueError:
            # An unparsed stamp means classify() already fell back to this Mac's clock, so
            # the line is judged on arrival order, which the mark has already constrained.
            return line, stale, "unparsed_stamp_accepted_on_arrival"
        if earliest is None or stamp >= earliest:
            return line, stale, "stamped_after_prompt"
        behind_s = round((prompt_start - stamp).total_seconds(), 3)
        if behind_s > clock_sanity_s:
            return line, stale, "board_clock_out_of_range"
        stale.append({
            "stamp_local": line.t_local,
            "prompt_start_local": prompt_start_local,
            "behind_prompt_s": behind_s,
            "raw": line.raw,
        })
    return None, stale, "no_fresh_wake"


def ensure_listening(args, session: LogSession, events: EventLog, run_dir: Path,
                     deadline: Deadline, bind_device) -> dict[str, object]:
    """Bring the device to an addressable listening state; wake word first if needed.

    The wake word and the question are separate plays on purpose: while the device is
    still connecting, an utterance that carries the question too is spent on the
    connection and never becomes a turn.  The board's own `wake_detected` line stays the
    only gate that opens a conversation - this Mac's audio is never trusted as a proxy -
    but the line also has to be *this* attempt's wake: a stamped-before-the-prompt line is
    a leftover (an ambient wake, or a replay of an earlier attempt) and does not bind.
    Binding therefore still happens only after a real wake, and a re-wake either refreshes
    the authoritative identity or fails explicitly.
    """

    if session.device_state() in ADDRESSABLE_STATES and session.bound:
        return {"already_listening": True, "wake_detected": True, "welcome_completed": True}
    prompt = run_dir / "wake.aiff"
    render_prompt(args.voice, args.wake_text, prompt, deadline)
    # A wake starts a new conversation: drop the previous identity first, so the greeting
    # that follows binds from the fresh snapshot instead of being judged against the old
    # session.  Lines that arrive before the snapshot are held and replayed on bind.
    session.unbind("re-wake")
    mark = session.mark()
    attempts = 0
    woken = None
    stale_wakes: list[dict[str, object]] = []
    wake_evidence: dict[str, object] = {}
    for attempt in range(1, args.max_plays + 1):
        attempts = attempt
        start, end, elapsed = play(prompt, deadline)
        events.add("prompt", recording="microphone", name=f"wake_{attempt}", turn_index=None,
                   start_local=start, end_local=end, text=args.wake_text,
                   played_s=round(elapsed, 3))

        def wake_since_prompt(
            s: LogSession, _start: str = start, _attempt: int = attempts
        ) -> LogLine | None:
            """Accept only a wake line that can belong to this attempt's prompt."""

            line, stale, reason = fresh_wake_line(s, mark, _start)
            for item in stale:
                if item not in stale_wakes:
                    stale_wakes.append(item)
            if line is not None:
                wake_evidence.update({
                    "attempt": _attempt,
                    "prompt_start_local": _start,
                    "wake_stamp_local": line.t_local,
                    "freshness": reason,
                    "freshness_tolerance_s": WAKE_FRESHNESS_TOLERANCE_S,
                })
            return line

        woken = wait_for(session, wake_since_prompt, args.wake_wait_s, deadline)
        if woken:
            break
        wait_for(session, lambda s: s.device_state() == "idle", 5.0, deadline)
    if not woken:
        return {"wake_detected": False, "attempts": attempts,
                "device_state": session.device_state(),
                "stale_wake_lines": stale_wakes,
                "wake_freshness_tolerance_s": WAKE_FRESHNESS_TOLERANCE_S}
    binding = bind_device()
    if not binding.get("ok"):
        return {"wake_detected": True, "attempts": attempts, "welcome_completed": False,
                "binding_failed": binding.get("error"), "device_state": session.device_state(),
                "wake_evidence": wake_evidence, "stale_wake_lines": stale_wakes}
    welcome = wait_for(session, lambda s: _welcome_done(s, mark), args.welcome_wait_s, deadline)
    return {
        "wake_detected": True,
        "attempts": attempts,
        "welcome_completed": welcome is not None,
        "binding": binding,
        "device_state": session.device_state(),
        "wake_evidence": wake_evidence,
        "stale_wake_lines": stale_wakes,
    }


def wait_turn_complete(session: LogSession, tracker: TurnTracker, mark: int,
                       unscoped_mark: int, timeout: float, deadline: Deadline, *, poll: float = 0.2):
    fed = mark
    fed_unscoped = unscoped_mark
    end = time.monotonic() + deadline.clamp(timeout)
    if end <= time.monotonic():
        return None
    while True:
        session.poll()
        for line in session.lines[fed:]:
            tracker.observe(line)
        fed = len(session.lines)
        # Session-less provider lines are correlation evidence only: they inform the
        # "content came after the tool result" check and never confer identity.
        for line in session.unscoped[fed_unscoped:]:
            tracker.observe(line)
        fed_unscoped = len(session.unscoped)
        done = tracker.completion()
        if done:
            return done
        now = time.monotonic()
        if now >= end:
            return None
        time.sleep(min(poll, max(0.0, end - now)))


def parse_follow_up_delays(text: str) -> list[float]:
    """Parse `--follow-up-delays`; each value is seconds after the previous playback end."""

    delays: list[float] = []
    for item in str(text).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            value = float(item)
        except ValueError as error:
            raise ValueError(f"delay {item!r} is not a number") from error
        if not 0.0 <= value <= 60.0:
            raise ValueError(f"delay {value} is outside 0..60 s")
        delays.append(value)
    if not delays:
        raise ValueError("at least one delay is required")
    return delays


def planned_follow_up_delay(delays: list[float], position: int) -> float | None:
    """The delay this question is planned at, or None when it has no anchor to time from.

    Question 1 follows the welcome rather than a reply, so it has no playback end to count
    from and takes no delay; the last value repeats for questions past the end of the grid.
    """

    if position <= 1 or not delays:
        return None
    return delays[min(position - 2, len(delays) - 1)]


def follow_up_gate_verdict(position: int, gate: dict[str, object]) -> str | None:
    """Why this question must not be played after its gate, or None when it may.

    A follow-up only measures the 3 s/5 s/8 s grid if it belongs to the *same* conversation:
    when the device has gone to standby, `ensure_listening` wakes it (and may replay the
    greeting), so the question would open a new session and its delay would say nothing about
    continuing the previous answer.  Only an explicit "already listening" marker counts as a
    continuation, so a missing marker is refused rather than assumed.
    """

    if position <= 1:
        return None
    if gate.get("already_listening") is True:
        return None
    return "follow_up_requires_continuation"


def seconds_between(earlier: object, later: object) -> float | None:
    """Local-stamp difference, or None when either stamp is absent or unparsable."""

    if not isinstance(earlier, str) or not isinstance(later, str) or not earlier or not later:
        return None
    try:
        return round((parse_local(later) - parse_local(earlier)).total_seconds(), 3)
    except ValueError:
        return None


def follow_up_anchor_local(previous_turn: dict[str, object] | None) -> str | None:
    """The previous question's `playback_ended` stamp: the only honest delay anchor.

    A turn that never reached a completed playback exposes no anchor, and an unanchored
    delay is not invented from the question's own start or from the wall clock.
    """

    if not previous_turn:
        return None
    completion = previous_turn.get("completion")
    if not isinstance(completion, dict):
        return None
    anchor = completion.get("playback_ended_t")
    return anchor if isinstance(anchor, str) and anchor else None


def wait_for_follow_up(anchor_local: str, delay_s: float, deadline: Deadline, *,
                       now: datetime | None = None) -> dict[str, object]:
    """Wait until `delay_s` after the reply's playback end, bounded by the global deadline.

    The wait is clamped like every other wait in this driver, so a delay the budget cannot
    cover is reported as cut instead of being silently served short.  What the turn really
    waited is measured later, from the prompt's own start stamp.
    """

    plan: dict[str, object] = {
        "planned_s": delay_s, "anchor_local": anchor_local, "target_local": None,
        "waited_s": 0.0, "cut_by_deadline": False,
    }
    try:
        target = parse_local(anchor_local) + timedelta(seconds=max(0.0, delay_s))
    except ValueError:
        return {**plan, "reason": "anchor_unparsable"}
    plan["target_local"] = target.isoformat(timespec="milliseconds")
    remaining = (target - (now or datetime.now().astimezone())).total_seconds()
    if remaining <= 0.0:
        return {**plan, "reason": "already_past_target"}
    allowed = deadline.clamp(remaining)
    plan["cut_by_deadline"] = allowed < remaining
    if allowed > 0.0:
        time.sleep(allowed)
    return {**plan, "waited_s": round(allowed, 3), "reason": "waited"}


def follow_up_grid(delays: list[float], turns: list[dict[str, object]], *,
                   tolerance_s: float = FOLLOW_UP_TOLERANCE_S) -> dict[str, object]:
    """Which planned delays this run really exercised, so the grid is never assumed.

    A cell counts only when the prompt was played *after* its planned delay and no later
    than `tolerance_s` beyond it.  An early play is a clock or anchor anomaly and a late one
    was a wake, a replayed greeting or scheduling the grid never asked for; neither may read
    as the planned cell, and the first question has no reply to count from at all.
    """

    cells: list[dict[str, object]] = []
    covered: list[float] = []
    early: list[float] = []
    late: list[float] = []
    for turn in turns:
        follow_up = turn.get("follow_up")
        if not isinstance(follow_up, dict):
            continue
        planned = follow_up.get("planned_s")
        if not isinstance(planned, (int, float)):
            continue
        actual = follow_up.get("actual_s")
        cell: dict[str, object] = {
            "planned_s": float(planned), "actual_s": actual,
            "late_by_s": follow_up.get("late_by_s"), "counted": False,
        }
        if isinstance(actual, (int, float)):
            delta = float(actual) - float(planned)
            if delta < 0.0:
                cell["disposition"] = "early"
                early.append(float(planned))
            elif delta <= tolerance_s:
                cell["disposition"] = "covered"
                cell["counted"] = True
                covered.append(float(planned))
            else:
                cell["disposition"] = "late"
                late.append(float(planned))
        else:
            cell["disposition"] = "not_played"
        cells.append(cell)
    planned_grid = list(dict.fromkeys(float(value) for value in delays))
    return {
        "planned_delays_s": planned_grid,
        "tolerance_s": tolerance_s,
        "covered_delays_s": sorted(set(covered)),
        "early_delays_s": sorted(set(early)),
        "late_delays_s": sorted(set(late)),
        "uncovered_delays_s": [value for value in planned_grid if value not in set(covered)],
        "cells": cells,
    }


def barge_source_evidence(snapshot: dict[str, object]) -> dict[str, object]:
    """The barge sources the control plane has *configured*, or an explicit unknown.

    This is the control-plane value a claim would be built from, not the claim the Edge
    validated, and not a substitute for the device's own signed settings: the settings
    authority answers with the built-in defaults when the device has no settings row, so a
    defaults answer is reported as unknown rather than as a permission.  F2 therefore cannot
    be closed from this field - it only tells the operator which sources are worth trying.
    """

    kinds = snapshot.get("allowed_barge_in")
    origin = str(snapshot.get("allowed_barge_in_origin") or "unknown")
    if origin == "configured_row" and isinstance(kinds, (list, tuple)) and kinds:
        return {
            "configured_allowed_barge_in": [str(kind) for kind in kinds],
            "configured_allowed_barge_in_source": "control_api_device_settings_row",
            "settings_version": snapshot.get("settings_version"),
        }
    return {
        "configured_allowed_barge_in": None,
        "configured_allowed_barge_in_source": "unknown",
        "configured_allowed_barge_in_defaults_unverified": (
            [str(kind) for kind in kinds] if isinstance(kinds, (list, tuple)) else None
        ),
        "allowed_barge_in_origin": origin,
        "settings_version": snapshot.get("settings_version"),
        "barge_settings_error": snapshot.get("barge_settings_error"),
    }


def run_question(args, session: LogSession, events: EventLog, run_dir: Path,
                 index: int, question: str, deadline: Deadline,
                 *, follow_up: dict[str, object] | None = None) -> dict[str, object]:
    """Play one question and record the turn's own verdict plus its audio binding.

    `verdict` is this run's verdict and `verdict_log` mirrors it, so the audio analyzer
    keeps the turn state instead of replacing it with the microphone's level verdict.  The
    window-binding decision is kept under `content_window` for the report.
    """

    turn: dict[str, object] = {"index": index, "question": question}
    prompt = run_dir / f"question-{index}.aiff"
    turn["prompt_expected_s"] = round(render_prompt(args.voice, question, prompt, deadline), 3)
    mark = session.mark()
    unscoped_mark = len(session.unscoped)
    start, end, elapsed = play(prompt, deadline)
    events.add("prompt", recording="microphone", name=f"question_{index}", turn_index=index,
               start_local=start, end_local=end, text=question, played_s=round(elapsed, 3))
    if follow_up is not None:
        # `actual_s` is measured to the moment afplay was spawned, which is the only point
        # this process can date: the audible gap is a little longer and is not claimed here.
        # `late_by_s` is what the grid counts, so a wait a wake ate cannot read as the delay.
        actual = seconds_between(follow_up.get("anchor_local"), start)
        planned = follow_up.get("planned_s")
        turn["follow_up"] = {
            **follow_up,
            "started_local": start,
            "actual_s": actual,
            "late_by_s": (round(actual - float(planned), 3)
                          if isinstance(planned, (int, float)) and actual is not None else None),
            "measure_point": "afplay_spawn_not_audible_start",
        }

    tracker = TurnTracker()
    commit = wait_for(session, lambda s: s.first(mark, ("turn_committed",)),
                      args.commit_wait_s, deadline)
    if commit is None:
        session.poll()
        rejection = session.first(mark, ("speaker_reject", "turn_ignored"))
        turn["verdict"] = "turn_rejected_by_speaker_gate" if rejection else "no_turn_commit"
        turn["verdict_log"] = turn["verdict"]
        if rejection is not None:
            turn["rejection"] = {"kind": rejection.kind, **rejection.fields}
        turn["server_evidence"] = tracker.server_evidence()
        turn["content_window"] = record_windows(events, session, mark, index)
        return turn
    turn["commit"] = {"turn_id": commit.turn_id, "generation_id": commit.generation_id,
                      "text_len": commit.fields.get("text_len")}

    completion = wait_turn_complete(session, tracker, mark, unscoped_mark,
        args.turn_deadline_s, deadline)
    turn["verdict"] = "turn_completed" if completion else "turn_incomplete"
    turn["verdict_log"] = turn["verdict"]
    turn["completion"] = completion
    turn["server_evidence"] = tracker.server_evidence()
    turn["content_window"] = record_windows(events, session, mark, index, completion=completion)
    return turn


def abort_reason(results: dict[str, object]) -> str | None:
    """Why this run must exit non-zero, or None when it finished its questions cleanly.

    A run that never woke the device, never reached listening, or left a turn incomplete
    has not produced acceptance evidence, so it must not exit 0.
    """

    for key in ("aborted_by", "abort_reason"):
        value = results.get(key)
        if value:
            return str(value)
    if results.get("aborted_after_turn"):
        return "turn_incomplete"
    if results.get("aborted_before_turn"):
        return "wake_or_welcome_not_reached"
    return None


# Reference reasons that mean the analysis tool could not read or compare the prompt at
# all, as opposed to the audio path failing a measurement.  Keeping them apart stops a
# missing reference file, ffmpeg or numpy from reading like a broken microphone/speaker.
SELFTEST_REFERENCE_TOOL_REASONS = (
    "reference_missing",
    "reference_decode_unavailable",
    "reference_or_recording_length_invalid",
)


def reference_readout(reference: dict[str, object]) -> str:
    """One line from the matcher's own candidate numbers, for a refused prompt.

    `reference_not_detected` alone cannot say whether a chunk was weak or the chunks simply
    disagreed, and that distinction is what decides the next step - a weak chunk is the
    audio path, a disagreement is what the lattice bound exists for.  Diagnostics only: the
    verdict is already decided by the matcher.
    """

    candidate = reference.get("candidate")
    if not isinstance(candidate, dict) or not candidate:
        return ""
    parts = []
    for key, label in (("chunk_ncc", "chunk_ncc"), ("chunk_residual_s", "residual_s")):
        value = candidate.get(key)
        if isinstance(value, list):
            parts.append(f"{label}={value}")
    if candidate.get("spread_s") is not None:
        parts.append(f"spread_s={candidate['spread_s']}")
    if candidate.get("rejection"):
        parts.append(f"rejection={candidate['rejection']}")
    if not parts:
        return ""
    return (" [strongest candidate: " + " ".join(parts) + "]").replace("'", "")


def selftest_blocker(verdict: str, evidence: dict[str, object]) -> str:
    """Word a failed self-test so a tool dependency never reads as an audio fault.

    The reference evidence carries why the prompt could not be located, so a missing
    reference (or a decoder/library that is unavailable) is reported as an analysis
    dependency and every other verdict keeps the audio-path wording.
    """

    reference = evidence.get("reference_evidence")
    reference = reference if isinstance(reference, dict) else {}
    reason = str(reference.get("reason") or "unknown")
    path = str(reference.get("reference_path") or "")
    tool_side = (
        verdict == "blocked_reference_unavailable"
        or reason in SELFTEST_REFERENCE_TOOL_REASONS
    )
    if tool_side:
        return (
            f"self-test could not be judged ({verdict}): the prompt that was played could not "
            f"be compared with its reference ({reason}"
            + (f" for {path}" if path else "")
            + ").  That is an analysis dependency, not a measurement of the audio path, so "
            "neither the Mac's output nor the microphone has been shown to be broken; the "
            "serial port was never opened"
        )
    return (
        f"self-test failed ({verdict}); the audio path is not usable and the serial port "
        f"was never opened (reference evidence: {reason}"
        + (f" for {path}" if path else "")
        + ")"
        + reference_readout(reference)
    )


def cleanup(capture_proc, recorders: list[Recorder], events: EventLog,
            results: dict[str, object], run_dir: Path, capture_log=None,
            budget_s: float = 60.0) -> None:
    """Stop everything this run started; called on every exit path including failures.

    Cleanup has its own budget, so it finishes even when the run's global deadline is
    already spent: every wait takes the smaller of its own limit and what is left.
    """

    budget = Deadline(budget_s)
    if capture_proc is not None and capture_proc.poll() is None:
        try:
            capture_proc.send_signal(signal.SIGTERM)
            capture_proc.wait(timeout=max(1.0, budget.clamp(45.0)))
        except subprocess.TimeoutExpired:
            capture_proc.kill()
        except OSError:
            pass
    if capture_log is not None:
        try:
            capture_log.close()
        except OSError:
            pass
    for recorder in recorders:
        if recorder.proc is None:
            continue
        try:
            recorder.stop(grace=max(1.0, budget.clamp(10.0)))
        except Exception as error:  # one broken recorder must not skip the rest
            results.setdefault("cleanup_errors", []).append(
                f"stopping {recorder.out_path.name}: {type(error).__name__}: {error}"
            )
            continue
        # One stop event per recorder, written by whoever stopped it first: the self-test
        # path stops its recorder before cleanup runs, so a second event here would move
        # the recorded stop time of an already-stopped take.
        emit_recorder_stop(events, recorder)
    events.close()
    results["cleanup"] = {
        "budget_s": budget_s,
        "capture_stopped": capture_proc is None or capture_proc.poll() is not None,
        "budget_remaining_s": round(budget.remaining(), 1),
        "recorders_stopped": [recorder.out_path.name for recorder in recorders
                              if recorder.proc is not None],
        "recorder_returncodes": {recorder.out_path.name: recorder.returncode
                                 for recorder in recorders if recorder.proc is not None},
    }
    try:
        (run_dir / "results.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as error:
        print(f"warning: could not write results.json ({error})", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="validate and print the plan only")
    mode.add_argument("--run", action="store_true", help="perform playback, recording and capture")
    parser.add_argument("--run-dir", required=True, type=Path)
    parser.add_argument("--firmware-receipt", required=True)
    parser.add_argument("--voice", default="Tingting")
    parser.add_argument("--port", default="/dev/cu.usbmodem101")
    parser.add_argument("--ssh-host", default="memoria-prod",
                        help="host that runs the control-api container holding the device runtime")
    parser.add_argument("--device-id", default="dev_atk_a4cb8fd6095c",
                        help="this board's device id, used for the authoritative session snapshot")
    parser.add_argument("--runtime-status-timeout-s", type=float, default=25.0)
    parser.add_argument("--enforce-stream-epoch", action=argparse.BooleanOptionalAction, default=True,
                        help="refuse bridge lines whose stream_epoch differs from the snapshot")
    parser.add_argument("--duration", type=int, default=300, help="capture limit in seconds (max 900)")
    parser.add_argument("--record-extra-seconds", type=int, default=40)
    parser.add_argument("--max-total-s", type=float, default=540.0, help="hard global deadline")
    parser.add_argument("--cleanup-budget-s", type=float, default=60.0,
                        help="independent budget for stopping the capture and every recorder")
    parser.add_argument("--idle-wait-s", type=float, default=90.0)
    parser.add_argument("--wake-wait-s", type=float, default=12.0)
    parser.add_argument("--welcome-wait-s", type=float, default=30.0)
    parser.add_argument("--commit-wait-s", type=float, default=15.0)
    parser.add_argument("--turn-deadline-s", type=float, default=90.0)
    parser.add_argument("--max-plays", type=int, default=3, help="wake plays: 1 initial + 2 replays")
    parser.add_argument("--ffmpeg-start-timeout-s", type=float, default=FFMPEG_START_TIMEOUT_S,
                        help="bound for the file-only liveness fallback; ffmpeg's buffered WAV "
                             "needs about 10 s of 16 kHz PCM before its bytes show up")
    parser.add_argument("--follow-up-delays", default=DEFAULT_FOLLOW_UP_DELAYS,
                        help="comma-separated seconds to wait after the previous reply's "
                             "playback end before each follow-up question; the last repeats")
    parser.add_argument("--selftest-seconds", type=float, default=9.0)
    parser.add_argument("--selftest-offset-s", type=float, default=3.0)
    parser.add_argument("--selftest-text", default="测试，一二三四五")
    parser.add_argument("--wake-text", default=DEFAULT_WAKE_TEXT)
    parser.add_argument("--questions", nargs="*", default=list(DEFAULT_QUESTIONS))
    args = parser.parse_args(argv)

    if not 0 < args.duration <= 900:
        print("error: --duration must be 1..900", file=sys.stderr)
        return 2
    try:
        follow_up_delays = parse_follow_up_delays(args.follow_up_delays)
    except ValueError as error:
        print(f"error: --follow-up-delays {error}", file=sys.stderr)
        return 2
    root = repo_root()
    # One global budget starts now, so even the read-only preflight probes are clamped.
    deadline = Deadline(args.max_total_s)
    report, blockers = preflight(args, root, deadline)
    print(json.dumps({"preflight": report, "blockers": blockers}, ensure_ascii=False, indent=2))
    if blockers:
        print("refusing to run:", file=sys.stderr)
        for blocker in blockers:
            print(f"  - {blocker}", file=sys.stderr)
        return 1
    if args.dry_run:
        print("dry run: nothing was played, recorded or opened")
        return 0

    run_dir = Path(args.run_dir)
    run_dir.mkdir(parents=True, exist_ok=False)
    events = EventLog(run_dir / "events.jsonl")
    results: dict[str, object] = {
        "tool": "scripts/auto_audio/auto_audio_session.py",
        "run_dir": str(run_dir),
        "voice": args.voice,
        "preflight": report,
        "turns": [],
        "wake_gates": [],
        "bindings": [],
        "blockers": [],
        "machine_analyzed_only": True,
        "human_listened": False,
        "max_total_s": args.max_total_s,
        "follow_up_delays_s": follow_up_delays,
        "alignment_uncertainty": "unknown",
        "alignment_padding_ms": ALIGNMENT_TOLERANCE_MS,
    }
    recorders: list[Recorder] = []
    capture_proc = None
    capture_log = None
    aborted: str | None = None
    try:
        # Installed inside the guarded block so a stop can never bypass the finally below.
        install_stop_handlers()
        index = int(report["input_device_index"])
        selftest = Recorder(run_dir / "selftest.log", index, run_dir / "selftest.wav",
                            int(args.selftest_seconds + args.ffmpeg_start_timeout_s) + 2)
        recorders.append(selftest)
        selftest.start()
        if not wait_recorder_live(selftest, args.ffmpeg_start_timeout_s, deadline):
            results["blockers"].append(
                "the self-test recorder never produced samples (microphone permission or a busy "
                f"input device) within {args.ffmpeg_start_timeout_s:.1f}s "
                f"[file={selftest.file_bytes()}B progress={selftest.progress() or 'none'}]; "
                "nothing was sent to the robot"
            )
            return 1
        # Measured from *samples live*, not from the spawn: the spawn-to-first-sample delay is
        # unmeasured, and starting the clock at the spawn would silently shorten the take
        # below the length gate and be misread as a bad microphone.
        take_began = time.monotonic()
        events.add("recording_start", recording="selftest", start_local=selftest.spawn_local)
        time.sleep(deadline.clamp(args.selftest_offset_s))
        baseline_end = now_local()
        events.add("baseline", recording="selftest", name="baseline",
                   start_local=selftest.spawn_local, end_local=baseline_end)
        test_prompt = run_dir / "selftest-prompt.aiff"
        render_prompt(args.voice, args.selftest_text, test_prompt, deadline)
        test_start, test_end, test_played = play(test_prompt, deadline)
        events.add("selftest_play", recording="selftest", name="selftest_play",
                   start_local=test_start, end_local=test_end, text=args.selftest_text,
                   played_s=round(test_played, 3))
        time.sleep(deadline.clamp(2.0))
        # Keep taking until the file really is as long as the length gate below expects: a
        # take that stops early would be misread as a broken microphone.
        remaining = args.selftest_seconds - (time.monotonic() - take_began)
        if remaining > 0:
            allowed = deadline.clamp(remaining)
            if allowed < remaining:
                selftest.stop()
                raise BudgetExpired(
                    f"the global deadline cut the self-test take to "
                    f"{time.monotonic() - take_began:.1f}s of the {args.selftest_seconds:.1f}s "
                    "it needs"
                )
            time.sleep(allowed)
        selftest.stop()
        # The one stop event for this recorder; cleanup must not write a second one later.
        emit_recorder_stop(events, selftest)
        selftest_report = analyse_selftest(
            run_dir / "selftest.wav",
            selftest.spawn_local,
            active_dbfs=ACTIVE_DBFS,
            min_rms_dbfs=SELFTEST_MIN_RMS_DBFS,
            min_above_floor_db=SELFTEST_MIN_ABOVE_FLOOR_DB,
            # The prompt that was actually played is handed over explicitly instead of
            # being guessed from the WAV's directory, so the self-test can compare what it
            # found against the reference it rendered.
            reference_path=test_prompt,
        )
        verdict, evidence = selftest_verdict(
            selftest_report["duration_s"], selftest_report["windows"],
            min_recording_s=args.selftest_seconds - 0.5,
            min_rms_dbfs=SELFTEST_MIN_RMS_DBFS,
            min_above_floor_db=SELFTEST_MIN_ABOVE_FLOOR_DB,
            min_active_ms=SELFTEST_MIN_ACTIVE_MS,
            min_active_ratio=SELFTEST_MIN_ACTIVE_RATIO,
            min_level_range_db=SELFTEST_MIN_LEVEL_RANGE_DB,
        )
        results["selftest"] = {
            "verdict": verdict,
            "evidence": evidence,
            "ffmpeg_returncode": selftest.returncode,
            "recording_s": selftest_report["duration_s"],
            "alignment_method": selftest_report["alignment_method"],
            "alignment_uncertainty": selftest_report["alignment_uncertainty"],
            "baseline_search_s": selftest_report["baseline_search_s"],
            "playback_search_s": selftest_report["playback_search_s"],
        }
        events.add("selftest_result", verdict=verdict, **evidence)
        if verdict != "selftest_passed":
            # The wording comes from the reference evidence, so an unavailable analysis
            # dependency is never reported as a broken microphone or speaker.
            results["blockers"].append(selftest_blocker(verdict, evidence))
            return 1

        session_rec = Recorder(run_dir / "microphone.log", index, run_dir / "microphone.wav",
                               args.duration + args.record_extra_seconds)
        recorders.append(session_rec)
        session_rec.start()
        if not wait_recorder_live(session_rec, args.ffmpeg_start_timeout_s, deadline):
            results["blockers"].append(
                "the session recorder never produced samples "
                f"[file={session_rec.file_bytes()}B progress={session_rec.progress() or 'none'}]"
            )
            return 1
        events.add("recording_start", recording="microphone", start_local=session_rec.spawn_local)
        events.add("baseline", recording="microphone", name="baseline",
                   start_local=session_rec.spawn_local, end_local=now_local())

        capture_dir = run_dir / "capture"
        capture_log = (run_dir / "capture-driver.log").open("ab", buffering=0)
        capture_proc = subprocess.Popen(
            ["uv", "run", "--no-project", "--with", "pyserial", "--with", "esptool",
             "python", str(root / CAPTURE_SCRIPT),
             "--out", str(capture_dir),
             "--firmware-receipt", str(Path(args.firmware_receipt)),
             "--port", args.port, "--duration", str(args.duration), "--server-logs"],
            stdin=subprocess.DEVNULL, stdout=capture_log, stderr=subprocess.STDOUT,
        )
        session = LogSession({"serial": FileTail(capture_dir / "serial.log"),
                              "bridge": FileTail(capture_dir / "bridge.log")})

        def bind_device() -> dict[str, object]:
            """Bind the capture to this board's runtime identity; never to a log line."""

            snapshot, error = fetch_device_runtime(args, deadline)
            if snapshot is None or not snapshot.get("session_id"):
                return {"ok": False, "error": error or "the snapshot carried no session_id"}
            stream_epoch = snapshot.get("stream_epoch")
            info = session.bind(
                str(snapshot["session_id"]),
                stream_epoch=stream_epoch if isinstance(stream_epoch, int) else None,
                source="device_runtime",
                enforce_stream_epoch=args.enforce_stream_epoch,
            )
            results["bindings"].append({
                **info,
                "device_id": snapshot.get("device_id"),
                "connected": snapshot.get("connected"),
                "conversation_state": snapshot.get("conversation_state"),
                "audio_mode": snapshot.get("audio_mode"),
                "observed_at": now_local(),
                **barge_source_evidence(snapshot),
            })
            return {"ok": True, **info}

        ready = wait_for(session, lambda s: s.device_state() == "idle", args.idle_wait_s, deadline)
        results["device_ready_state"] = session.device_state()
        if not ready:
            results["blockers"].append("the device never reached idle within the wait")
            return 1

        for position, question in enumerate(args.questions, start=1):
            # The addressable-state gate runs *before* the delay is measured, so a wake or a
            # replayed greeting cannot be charged to the answer's delay: whatever the gate
            # costs is accounted for here, and the wait that follows targets an absolute
            # moment (previous playback end + delay).  A target the gate has already passed
            # is reported as such instead of being served short.
            gate_started = time.monotonic()
            gate = ensure_listening(args, session, events, run_dir, deadline, bind_device)
            gate["before_question"] = position
            gate["seconds_s"] = round(time.monotonic() - gate_started, 3)
            results["wake_gates"].append(gate)
            if not (gate.get("wake_detected") and gate.get("welcome_completed")):
                results["blockers"].append(
                    f"could not reach an addressable listening state before question {position}"
                    + (f" ({gate['binding_failed']})" if gate.get("binding_failed") else "")
                )
                results["aborted_before_turn"] = position
                aborted = "wake_or_welcome_not_reached"
                break
            continuation_refusal = follow_up_gate_verdict(position, gate)
            if continuation_refusal is not None:
                gate["continuation_refused"] = continuation_refusal
                results["blockers"].append(
                    f"question {position} would have been asked in a new session "
                    f"({continuation_refusal}): the device was not already listening when its "
                    f"{planned_follow_up_delay(follow_up_delays, position):g}s follow-up was "
                    "due, so it is not the same-session continuation the grid measures"
                )
                results["follow_up_grid"] = follow_up_grid(follow_up_delays, results["turns"])
                results["aborted_before_turn"] = position
                aborted = continuation_refusal
                break
            delay_plan = planned_follow_up_delay(follow_up_delays, position)
            follow_up: dict[str, object] | None = None
            if delay_plan is not None:
                anchor = follow_up_anchor_local(results["turns"][-1] if results["turns"] else None)
                if anchor is None:
                    follow_up = {
                        "planned_s": delay_plan, "anchor_local": None, "target_local": None,
                        "waited_s": 0.0, "cut_by_deadline": False,
                        "reason": "no_previous_playback_end",
                    }
                else:
                    follow_up = wait_for_follow_up(anchor, delay_plan, deadline)
                    follow_up["gate_seconds_s"] = gate["seconds_s"]
                    # The wait can outlast the device's own listening window. Re-waking here
                    # would move the very delay being measured, so the state is only checked
                    # and reported: the run stops instead of playing into a device that is no
                    # longer addressable, and the receipt says which delay that happened at.
                    session.poll()
                    state = session.device_state()
                    follow_up["addressable_state_after_wait"] = state
                    if state not in ADDRESSABLE_STATES:
                        results["blockers"].append(
                            f"the device left the addressable state ({state}) while waiting "
                            f"the {delay_plan:g}s follow-up delay before question {position}"
                        )
                        results["follow_up_grid"] = follow_up_grid(follow_up_delays,
                                                                   results["turns"])
                        results["aborted_before_turn"] = position
                        aborted = "follow_up_lost_addressable_state"
                        break
                print(json.dumps({"follow_up_before": position, **follow_up},
                                 ensure_ascii=False))
            turn = run_question(args, session, events, run_dir, position, question, deadline,
                                follow_up=follow_up)
            results["turns"].append(turn)
            print(json.dumps({"turn": position, "verdict": turn.get("verdict"),
                              "commit": turn.get("commit")}, ensure_ascii=False))
            if turn.get("verdict") != "turn_completed":
                results["aborted_after_turn"] = position
                results["abort_reason"] = turn.get("verdict")
                aborted = str(turn.get("verdict"))
                break
        results["foreign_session_lines"] = len(session.foreign)
        results["foreign_session_samples"] = session.foreign[:5]
        results["unscoped_lookup_lines"] = len(session.unscoped)
        results["unattributed_marker_lines"] = len(session.unattributed)
        results["unattributed_marker_samples"] = session.unattributed[:5]
        results["bound_session"] = session.binding
        results["bound_stream_epoch"] = session.stream_epoch
        results["binding_source"] = session.binding_source
        results["follow_up_grid"] = follow_up_grid(follow_up_delays, results["turns"])
        if results["follow_up_grid"]["uncovered_delays_s"]:
            print(json.dumps({
                "follow_up_grid": results["follow_up_grid"],
                "note": "this run did not exercise every planned delay; give each delay its "
                        "own follow-up question (e.g. --questions q1 q2 q3 q4 "
                        "--follow-up-delays 3,5,8) before claiming the whole grid",
            }, ensure_ascii=False))
    except Stopped as stop:
        aborted = str(stop)
        results["aborted_by"] = aborted
    except BudgetExpired as expired:
        aborted = str(expired)
        results["aborted_by"] = aborted
    finally:
        # Ignore further stops so the cleanup below always completes.
        ignore_stop_handlers()
        aborted = aborted or abort_reason(results)
        results["aborted_by"] = aborted
        cleanup(capture_proc, recorders, events, results, run_dir, capture_log,
                budget_s=args.cleanup_budget_s)
    return 3 if aborted else 0


if __name__ == "__main__":
    raise SystemExit(main())
