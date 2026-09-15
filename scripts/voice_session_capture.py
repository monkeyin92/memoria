#!/usr/bin/env python3
"""Capture one Memoria voice session from the ESP-VoCat serial console.

This is the regularised replacement for the ad-hoc capture.py that lived inside a
single acceptance run directory.  That script attached whatever
preflash.json/postflash.json happened to sit next to itself, so a capture made
after a later flash kept reporting the previous firmware release as its own.  This
tool refuses to guess:

* --firmware-receipt is required and is bound to the capture as an operator
  supplied flash receipt.  The receipt must exist, be a JSON object, and carry a
  40-hex release_head, 64-hex candidate_app_sha256 / candidate_elf_sha256 /
  identity_sha256, a verified_at timestamp and positive read-back verification
  flags.  Nothing is read from the directory this tool happens to live in.
* The receipt is recorded under firmware_receipt together with its path, the file
  sha256 and its content, and is explicitly marked
  read_from_board_this_run=false: it is evidence about the flash that was
  performed, not a live read of the board's on-chip version.  The file is read
  exactly once and both the digest and the parsed payload come from that read, so
  there is no time-of-check/time-of-use gap.  The capture's own revision is
  recorded separately under source_revision.
* --out must not exist yet, so an earlier capture is never overwritten.

Opening the serial port asserts DTR/RTS and usually resets the board, so any
timing before the device reports "StateMachine: State: activating -> idle"
belongs to the reboot and not to the conversation.  --preflight-only validates
the receipt and writes capture.json without touching the device.

Server logs: with --server-logs the tool follows the bridge/agent/edge docker log
streams over ssh.  Each stream's exit is recorded in capture.json under
log_streams; a stream that fails or ends before the capture stops marks
log_stream_health=degraded and makes the tool exit non-zero, so a capture whose
trailing server evidence is truncated is never mistaken for a complete one.

Stopping: SIGHUP, SIGQUIT, SIGINT and SIGTERM are all handled, and the first signal
received is recorded verbatim (capture.json stop_signal / exit_reason).  The handlers
the caller had are restored before this tool returns, so a capture run from another
program does not change that program's signal handling.  A SIGKILL, a power loss or a
panic cannot be caught at all: such a run keeps capture_status=in_progress and writes
no completed_at_local, which the report reads as an incomplete capture.

Finalization is one sequence in which every step is attempted even when an earlier one
fails, every wait is bounded (including the wait after SIGKILL), and each failure is
recorded under cleanup_errors and makes log_stream_health=degraded with a non-zero exit
code.  capture_status=completed is written only at the end of that sequence, so the
file never claims a finishing this tool did not reach.

Usage::

    python scripts/voice_session_capture.py
        --out outputs/acceptance/run-<stamp>-<name>/session-1
        --firmware-receipt outputs/acceptance/run-<flash>/postflash.json
        --duration 900 --server-logs

A live capture needs pyserial (plus esptool for --boot-reset), which the project
.venv does not ship:

    uv run --no-project --with pyserial --with esptool python
        scripts/voice_session_capture.py --out <dir> --firmware-receipt <path>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_PORT = "/dev/cu.usbmodem101"
DEFAULT_BAUDRATE = 460800
MAX_DURATION_S = 900
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DIGEST_FIELDS = ("candidate_app_sha256", "candidate_elf_sha256", "identity_sha256")
READBACK_FLAGS = ("app_full_readback_byte_match", "identity_byte_match")
CONTAINERS = (
    ("bridge", "memoria-voice-core-media-bridge-1"),
    ("agent", "memoria-agent-1"),
    ("edge", "memoria-media-edge-1"),
)
STOP_SIGNAL_NAMES = ("SIGHUP", "SIGQUIT", "SIGINT", "SIGTERM")
TERMINATE_WAIT_S = 5
KILL_WAIT_S = 5


class ReceiptError(Exception):
    """--firmware-receipt is missing, unreadable or fails validation."""


class StopRecorder:
    """Records the first stopping signal so a capture can say why it ended.

    Only the first signal is kept: a repeated delivery must not overwrite the
    reason the capture actually stopped for.  The handler records and returns, so
    it never raises inside a signal frame.
    """

    def __init__(self) -> None:
        self.signum: int | None = None
        self.name: str | None = None
        self.at_local: str | None = None

    @property
    def requested(self) -> bool:
        return self.signum is not None

    def handle(self, signum: int, _frame: object) -> None:
        if self.signum is not None:
            return
        self.signum = signum
        try:
            self.name = signal.Signals(signum).name
        except ValueError:
            self.name = f"signal {signum}"
        self.at_local = datetime.now().astimezone().isoformat()


def _emit(message: str, *, err: bool = False) -> None:
    """Write one progress or diagnostic line, and never let that decide the outcome.

    A live capture is normally run from a terminal or pipe that can go away mid-run,
    and the next write then raises BrokenPipeError.  Losing that message is
    acceptable; losing the closing record is not, so an unwritable stream degrades to
    a no-op with no other side effect.
    """

    try:
        print(message, file=sys.stderr if err else sys.stdout, flush=True)
    except Exception:
        return


def _install_stop_handlers(recorder: StopRecorder) -> tuple[dict[int, object], list[str]]:
    """Put a handler on every stoppable signal; report the ones that refused.

    A stop signal this tool cannot handle would kill it with no record at all, so the
    caller decides on these failures instead of a silent continue.
    """

    previous: dict[int, object] = {}
    failures: list[str] = []
    for name in STOP_SIGNAL_NAMES:
        signum = getattr(signal, name, None)
        if signum is None:
            failures.append(f"{name} is not available on this platform")
            continue
        try:
            previous[signum] = signal.signal(signum, recorder.handle)
        except (OSError, ValueError) as error:
            failures.append(
                f"installing the {name} handler failed: {type(error).__name__}: {error}"
            )
    return previous, failures


def _restore_stop_handlers(previous: dict[int, object], errors: list[str]) -> None:
    """Put the caller's handlers back so this tool never leaks its own."""

    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError, TypeError) as error:
            errors.append(f"restoring the signal {signum} handler: {type(error).__name__}: {error}")


@dataclass
class Capture:
    """Mutable state of one live capture; the caller owns every resource in it."""

    port: object | None = None
    serial_opened: bool = False
    serial_error: str = ""
    serial_stage: str = ""
    startup_error: str = ""
    streams: dict[str, dict[str, object]] = field(default_factory=dict)
    processes: list[tuple[str, subprocess.Popen[bytes]]] = field(default_factory=list)
    handles: list = field(default_factory=list)
    cleanup_errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BoundReceipt:
    """A validated flash receipt bound to the exact bytes that were hashed."""

    path: Path
    sha256: str
    size_bytes: int
    payload: dict[str, object]


def load_firmware_receipt(path: Path) -> BoundReceipt:
    """Load and validate an operator supplied flash receipt.

    Fails closed: a receipt that does not prove its own read-back verification is
    refused, because binding an unverified receipt to a capture would recreate the
    very confusion this tool exists to prevent.

    The file is read exactly once; the digest and the parsed payload both come from
    that single read, so a concurrent edit of the receipt between the hash and the
    parse cannot make the recorded digest describe different content.
    """

    if not path.exists():
        raise ReceiptError(f"--firmware-receipt {path} does not exist")
    if not path.is_file():
        raise ReceiptError(f"--firmware-receipt {path} is not a regular file")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ReceiptError(f"--firmware-receipt {path} could not be read ({error})") from error
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReceiptError(f"--firmware-receipt {path} is not valid JSON ({error})") from error
    if not isinstance(payload, dict):
        raise ReceiptError(f"--firmware-receipt {path} must be a JSON object")

    release_head = payload.get("release_head")
    if not isinstance(release_head, str) or not HEX40.match(release_head):
        raise ReceiptError(
            f"--firmware-receipt {path} has no 40-hex release_head (found {release_head!r})"
        )
    for name in DIGEST_FIELDS:
        value = payload.get(name)
        if not isinstance(value, str) or not HEX64.match(value):
            raise ReceiptError(f"--firmware-receipt {path} has no 64-hex {name} (found {value!r})")
    for name in READBACK_FLAGS:
        if payload.get(name) is not True:
            raise ReceiptError(
                f"--firmware-receipt {path} does not confirm {name}=true "
                f"(found {payload.get(name)!r}); only a receipt whose read-back verification "
                "passed can be bound to a capture"
            )
    verified_at = payload.get("verified_at")
    if not isinstance(verified_at, str) or not verified_at:
        raise ReceiptError(f"--firmware-receipt {path} has no verified_at timestamp")
    return BoundReceipt(path=path.resolve(), sha256=digest, size_bytes=len(raw), payload=payload)


def receipt_record(receipt: BoundReceipt) -> dict[str, object]:
    payload = receipt.payload
    return {
        "source": "operator_supplied_--firmware-receipt",
        "path": str(receipt.path),
        # Digest and payload come from one read of the file (see load_firmware_receipt).
        "sha256": receipt.sha256,
        "size_bytes": receipt.size_bytes,
        "release_head": payload["release_head"],
        "candidate_app_sha256": payload["candidate_app_sha256"],
        "candidate_elf_sha256": payload["candidate_elf_sha256"],
        "identity_sha256": payload["identity_sha256"],
        "verified_at": payload["verified_at"],
        "write_offset": payload.get("write_offset"),
        "read_from_board_this_run": False,
        "note": (
            "Bound by the operator to describe the flash performed before this capture; the "
            "board's on-chip version was not read during this capture."
        ),
        "raw": payload,
    }


def source_revision() -> dict[str, object]:
    """Revision of the source tree this capture tool ran from (not the firmware)."""

    def git(*argv: str) -> str:
        try:
            return subprocess.check_output(
                ("git", *argv), text=True, stderr=subprocess.DEVNULL
            ).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return ""

    return {
        "git_head": git("rev-parse", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(git("status", "--porcelain")),
    }


def build_metadata(
    args: argparse.Namespace,
    receipt: BoundReceipt,
    started_utc: datetime,
    started_local: datetime,
) -> dict[str, object]:
    return {
        "tool": "scripts/voice_session_capture.py",
        "capture_mode": "preflight_only" if args.preflight_only else "live",
        # The only lifecycle state written before the run: a capture that stops here
        # keeps capture_status=in_progress, which is what an uncatchable SIGKILL,
        # power loss or panic leaves behind.  Nothing here pre-writes an ending.
        "capture_status": "preflight_only" if args.preflight_only else "in_progress",
        "serial_opened": False,
        "serial_data_writes": False,
        "pid": os.getpid(),
        "started_at_utc": started_utc.isoformat(),
        "started_at_local": started_local.isoformat(),
        "capture_duration_limit_s": args.duration,
        "port": args.port,
        "baudrate": args.baudrate,
        "boot_reset": args.boot_reset,
        "server_logs_requested": args.server_logs,
        "log_streams": {},
        "log_stream_health": "not_requested",
        "log_stream_health_reasons": [],
        "serial_error": None,
        "services_restarted": False,
        "since_utc": started_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_revision": source_revision(),
        "firmware_receipt": receipt_record(receipt),
        "warnings": [
            "Opening the serial port asserts DTR/RTS and usually resets the board; discard every "
            "timing before the device reports activating -> idle.",
            "firmware_receipt is an operator supplied flash receipt, not a live read of the "
            "board's on-chip version.",
        ],
    }


def write_metadata(out: Path, metadata: dict[str, object]) -> None:
    # Same-directory replacement keeps the last complete record if a write fails;
    # this is atomic publication, not a backup or fsync-level power-loss guarantee.
    payload = json.dumps(metadata, indent=2) + "\n"
    descriptor, name = tempfile.mkstemp(prefix="capture.json.tmp-", dir=out)
    temporary = Path(name)
    try:
        os.close(descriptor)
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(out / "capture.json")
    finally:
        temporary.unlink(missing_ok=True)


def _ssh_log_stream(container: str, since: str, handle) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "memoria-prod",
            "docker",
            "logs",
            "--follow",
            "--timestamps",
            "--since",
            since,
            container,
        ],
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )


def _open_log_handle(path: Path):
    return path.open("ab", buffering=0)


def _run_capture(
    args: argparse.Namespace,
    since: str,
    serial_module,
    hard_reset,
    recorder: StopRecorder,
    state: Capture,
) -> None:
    """Open the port, follow the log streams and read until duration or a stop.

    Once a stop has been requested nothing new is opened: neither a serial port, nor a
    log stream, nor serial.log.  Every device failure is recorded in `state`, whose
    resources the caller owns and closes, instead of being raised, so the caller
    always reaches its finalization.
    """

    try:
        if recorder.requested:
            _emit(f"STOP_BEFORE_START signal={recorder.name} serial_not_opened=True")
        else:
            state.port = serial_module.Serial(
                port=None, baudrate=args.baudrate, timeout=0.5, exclusive=True
            )
            state.port.dtr = False
            state.port.rts = False
            state.port.port = args.port
            state.port.open()
            state.serial_opened = True
            metadata = json.loads((args.out / "capture.json").read_text())
            metadata["serial_opened"] = True
            write_metadata(args.out, metadata)
            _emit(
                f"SERIAL_OPEN pid={os.getpid()} port={state.port.port} since={since} "
                "reset_expected=True"
            )
        if args.server_logs:
            for label, container in CONTAINERS:
                if recorder.requested:
                    state.streams[label] = {
                        "container": container,
                        "status": "not_started",
                        "exit_code": None,
                    }
                    _emit(f"LOG_STREAM_NOT_STARTED label={label} reason=stopping")
                    continue
                handle = _open_log_handle(args.out / f"{label}.log")
                state.handles.append(handle)
                try:
                    process = _ssh_log_stream(container, since, handle)
                except OSError as error:
                    state.streams[label] = {
                        "container": container,
                        "status": "start_failed",
                        "exit_code": None,
                        "error": f"{type(error).__name__}: {error}",
                    }
                    _emit(
                        f"LOG_STREAM_START_FAILED label={label} "
                        f"error={state.streams[label]['error']}"
                    )
                    continue
                state.processes.append((label, process))
                state.streams[label] = {
                    "container": container,
                    "status": "running",
                    "exit_code": None,
                }
        if hard_reset is not None and not recorder.requested:
            hard_reset(state.port, uses_usb=True)()
            _emit("BOOT_RESET_ISSUED")
        if not recorder.requested:
            deadline = time.monotonic() + args.duration
            reported: set[str] = set()
            with (args.out / "serial.log").open("ab", buffering=0) as output:
                while not recorder.requested and time.monotonic() < deadline:
                    try:
                        chunk = state.port.readline()
                    except Exception as error:  # serial backends raise many exception types
                        state.serial_error = f"{type(error).__name__}: {error}"
                        state.serial_stage = "serial_read_error"
                        _emit(f"SERIAL_READ_ERROR error={state.serial_error}", err=True)
                        return
                    if chunk:
                        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
                        output.write(f"[{stamp}] ".encode() + chunk)
                        if b"SystemInfo" in chunk:
                            _emit(f"HEARTBEAT {stamp}")
                    for label, process in state.processes:
                        if process.poll() is not None and label not in reported:
                            reported.add(label)
                            code = process.returncode
                            state.streams[label]["exit_code"] = code
                            state.streams[label]["status"] = (
                                "exited_early" if code == 0 else "failed"
                            )
                            _emit(f"LOG_STREAM_EXIT label={label} exit={code}")
    except Exception as error:  # opening the port can raise many exception types
        state.serial_error = f"{type(error).__name__}: {error}"
        state.serial_stage = "serial_stream_error" if state.serial_opened else "serial_open_failed"
        label = "STREAM" if state.serial_opened else "OPEN"
        _emit(f"SERIAL_{label}_ERROR error={state.serial_error}", err=True)
    if recorder.requested:
        _emit(f"CAPTURE_STOP_REQUESTED signal={recorder.name} at={recorder.at_local}")


def _is_exit_code(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _wait_exit_code(process, timeout: float, observed: int | None = None) -> int:
    code = process.wait(timeout=timeout)
    if _is_exit_code(code):
        return code
    # Popen.poll() already reaps an exited child. Preserve that observed result
    # even if a backend's later wait() does not repeat its exit code.
    if observed is not None:
        return observed
    code = process.returncode
    if _is_exit_code(code):
        return code
    raise RuntimeError("wait returned without an integer exit code")


def _force_kill(label: str, process, errors: list[str]) -> int | None:
    """SIGKILL one stuck stream and return only a confirmed exit code."""

    try:
        process.kill()
    except Exception as error:
        errors.append(f"SIGKILL for log stream {label}: {type(error).__name__}: {error}")
    try:
        # Even the wait after SIGKILL is bounded: a stuck process must never hang
        # the capture's own finalization.
        return _wait_exit_code(process, KILL_WAIT_S)
    except Exception as error:
        errors.append(f"wait after SIGKILL for log stream {label}: {type(error).__name__}: {error}")
        return None


def _stop_log_processes(state: Capture) -> None:
    """Stop and reclaim every log stream; never report a stop that was not observed."""

    errors = state.cleanup_errors
    observed: dict[str, int] = {}
    for label, process in state.processes:
        record = state.streams.get(label)
        try:
            code = process.poll()
            if code is not None and not _is_exit_code(code):
                raise RuntimeError("poll returned a non-integer exit code")
        except Exception as error:
            errors.append(f"polling log stream {label}: {type(error).__name__}: {error}")
            continue
        if code is None:
            continue
        observed[label] = code
        if isinstance(record, dict) and record.get("status") == "running":
            # It exited between the last read-loop poll and this drain: that is an early
            # exit (0) or a failure, never something this capture stopped.
            record["exit_code"] = code
            record["status"] = "exited_early" if code == 0 else "failed"
            _emit(f"LOG_STREAM_EXIT label={label} exit={code} observed=at_drain")
    for label, process in state.processes:
        if label in observed:
            continue
        try:
            # An unavailable poll is not proof of exit: still try to terminate
            # this stream without preventing cleanup of the others.
            process.terminate()
        except Exception as error:
            errors.append(f"terminating log stream {label}: {type(error).__name__}: {error}")
    for label, process in state.processes:
        forced = False
        code = observed.get(label)
        forced_reason = ""
        try:
            code = _wait_exit_code(process, TERMINATE_WAIT_S, code)
        except Exception as error:
            timed_out = isinstance(error, subprocess.TimeoutExpired)
            if not timed_out or label in observed:
                errors.append(f"waiting for log stream {label}: {type(error).__name__}: {error}")
            if label not in observed:
                forced = True
                forced_reason = "SIGTERM timed out" if timed_out else "its wait failed"
                code = _force_kill(label, process, errors)
        record = state.streams.get(label)
        if isinstance(record, dict) and record.get("status") == "running":
            record["forced_kill"] = forced
            if forced:
                record["forced_reason"] = forced_reason
            record["exit_code"] = code
            record["status"] = "stopped_by_capture" if code is not None else "cleanup_failed"


def _anomalies(state: Capture) -> list[str]:
    anomalies: list[str] = []
    for label, record in sorted(state.streams.items()):
        if record["status"] == "stopped_by_capture":
            if record.get("forced_kill"):
                anomalies.append(
                    f"log stream {label} needed SIGKILL to be reclaimed "
                    f"({record.get('forced_reason') or 'no reason recorded'})"
                )
            continue
        detail = f"log stream {label} {record['status']} (exit={record['exit_code']})"
        if record.get("error"):
            detail += f" error={record['error']}"
        anomalies.append(detail)
    if state.serial_error:
        anomalies.append(f"serial {state.serial_error}")
    elif not state.serial_opened:
        anomalies.append(
            state.startup_error
            or "the serial port was never opened (the capture stopped before it opened)"
        )
    anomalies.extend(f"cleanup {error}" for error in state.cleanup_errors)
    return anomalies


def _write_closing_record(
    args: argparse.Namespace, recorder: StopRecorder, state: Capture, handled_signals: list[str]
) -> int:
    """Write the closing record from the current state (safe to call again).

    `capture_status=completed` is written here and nowhere earlier: a capture that
    never reaches this function keeps `in_progress`, which is what an uncatchable
    SIGKILL, power loss or panic leaves behind.
    """

    anomalies = _anomalies(state)
    try:
        metadata = json.loads((args.out / "capture.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        _emit(f"error: could not record capture health ({error})", err=True)
        return 2
    metadata["serial_opened"] = state.serial_opened
    metadata["serial_error"] = state.serial_error or None
    metadata["log_streams"] = state.streams
    metadata["log_stream_health"] = (
        "degraded" if anomalies else ("healthy" if args.server_logs else "not_requested")
    )
    metadata["log_stream_health_reasons"] = anomalies
    if state.serial_stage:
        exit_reason = state.serial_stage
    elif recorder.requested:
        exit_reason = f"stop_signal {recorder.name}"
    else:
        exit_reason = "duration_elapsed"
    metadata["exit_reason"] = exit_reason
    metadata["stop_signal"] = recorder.name
    metadata["stop_signal_number"] = recorder.signum
    metadata["stop_requested_at_local"] = recorder.at_local
    metadata["stop_signal_handlers"] = handled_signals
    metadata["cleanup_errors"] = list(state.cleanup_errors)
    metadata["capture_status"] = "completed"
    metadata["completed_at_local"] = datetime.now().astimezone().isoformat()
    try:
        write_metadata(args.out, metadata)
    except OSError as error:
        _emit(f"error: could not record capture health ({error})", err=True)
        return 2
    if anomalies:
        _emit(f"CAPTURE_HEALTH degraded reasons={anomalies}")
        return 1
    _emit(f"CAPTURE_HEALTH healthy exit_reason={exit_reason}")
    return 0


def _finalize_capture(
    args: argparse.Namespace,
    recorder: StopRecorder,
    state: Capture,
    handled_signals: list[str],
    previous_handlers: dict[int, object],
) -> int:
    """The single finalization: cleanup, closing record, then hand handlers back.

    Runs whatever happened above it, attempts every cleanup even when an earlier one
    failed, and keeps this tool's own handlers installed until the closing record is
    on disk: a terminal or ssh session that goes away mid-capture must not be able to
    kill the record.  Only after the record is written are the caller's handlers
    restored, and a failure to do so is recorded like any other cleanup failure.
    """

    if state.port is not None:
        try:
            state.port.close()
        except Exception as error:  # serial backends raise many exception types
            detail = f"closing the serial port: {type(error).__name__}: {error}"
            state.cleanup_errors.append(detail)
            _emit(f"warning: {detail}", err=True)
    _stop_log_processes(state)
    for handle in state.handles:
        try:
            handle.close()
        except Exception as error:
            state.cleanup_errors.append(
                f"closing a log file handle: {type(error).__name__}: {error}"
            )
    _emit(f"CAPTURE_STOPPED {datetime.now().astimezone().isoformat()}")

    before = (recorder.signum, tuple(state.cleanup_errors))
    code = _write_closing_record(args, recorder, state, handled_signals)
    restore_errors: list[str] = []
    _restore_stop_handlers(previous_handlers, restore_errors)
    state.cleanup_errors.extend(restore_errors)
    if (recorder.signum, tuple(state.cleanup_errors)) != before:
        # A stop arrived while the record was being written, or a handler could not be
        # handed back: rewrite the same record with that fact instead of diverging.
        code = _write_closing_record(args, recorder, state, handled_signals)
    return code


def stream_serial(
    args: argparse.Namespace,
    since: str,
    recorder: StopRecorder,
    previous_handlers: dict[int, object],
) -> int:
    """Run one live capture; every refusal still leaves a closing record behind."""

    state = Capture()
    handled_signals = [signal.Signals(number).name for number in sorted(previous_handlers)]
    dependency_failure = False
    try:
        dependencies = _capture_dependencies(args, state)
        if dependencies is None:
            dependency_failure = True
        else:
            serial_module, hard_reset = dependencies
            try:
                _run_capture(args, since, serial_module, hard_reset, recorder, state)
            except Exception as error:  # defensive: _run_capture reports device errors as data
                state.serial_error = f"{type(error).__name__}: {error}"
                state.serial_stage = "capture_failed"
                _emit(f"CAPTURE_ERROR error={state.serial_error}", err=True)
    finally:
        code = _finalize_capture(args, recorder, state, handled_signals, previous_handlers)
    return 2 if dependency_failure else code


def _capture_dependencies(args: argparse.Namespace, state: Capture):
    """Import pyserial (and esptool for --boot-reset), recording a refusal in `state`.

    Both run after the first metadata write, so a missing dependency has to leave a
    closing record that says the capture never started, instead of an in_progress file
    whose ending nobody can explain later.
    """

    try:
        import serial
    except ImportError:
        state.serial_stage = "capture_not_started"
        state.startup_error = "the live capture never started: pyserial is not importable"
        _emit(
            "error: a live capture needs pyserial; run this tool with "
            "uv run --no-project --with pyserial --with esptool python "
            "scripts/voice_session_capture.py ... or pass --preflight-only",
            err=True,
        )
        return None
    if not args.boot_reset:
        return serial, None
    try:
        from esptool.reset import HardReset
    except ImportError:
        state.serial_stage = "capture_not_started"
        state.startup_error = "the live capture never started: --boot-reset needs esptool"
        _emit(
            "error: --boot-reset needs esptool; add --with esptool to the uv run command",
            err=True,
        )
        return None
    return serial, HardReset


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="new capture directory (must not exist)"
    )
    parser.add_argument(
        "--firmware-receipt",
        required=True,
        type=Path,
        help="flash receipt of the firmware on the board (e.g. the run's postflash.json)",
    )
    parser.add_argument("--port", default=DEFAULT_PORT)
    parser.add_argument("--baudrate", type=int, default=DEFAULT_BAUDRATE)
    parser.add_argument("--duration", type=int, default=MAX_DURATION_S)
    parser.add_argument(
        "--boot-reset", action="store_true", help="issue a hard reset before streaming"
    )
    parser.add_argument(
        "--server-logs",
        action="store_true",
        help="also follow bridge/agent/edge docker logs over ssh",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="validate the receipt and write capture.json without opening the serial port",
    )
    args = parser.parse_args(argv)

    if not 0 < args.duration <= MAX_DURATION_S:
        _emit(
            f"error: --duration must be between 1 and {MAX_DURATION_S} seconds (got {args.duration})",
            err=True,
        )
        return 2
    try:
        receipt = load_firmware_receipt(args.firmware_receipt)
    except ReceiptError as error:
        _emit(f"error: {error}", err=True)
        return 2
    if args.out.exists():
        _emit(
            f"error: --out {args.out} already exists; a capture never overwrites an earlier one",
            err=True,
        )
        return 2

    recorder = StopRecorder()
    previous_handlers: dict[int, object] = {}
    if not args.preflight_only:
        # Installed before the first live metadata write, so the record covers the whole
        # live lifecycle: a stop that arrives while the capture is still starting is
        # recorded and keeps the serial port closed.  A signal this tool cannot handle
        # is refused up front, because such a signal would kill the run with no record
        # at all rather than leaving an honest one.
        previous_handlers, failures = _install_stop_handlers(recorder)
        if failures:
            _restore_stop_handlers(previous_handlers, [])
            for failure in failures:
                _emit(f"error: {failure}", err=True)
            _emit(
                "error: refusing to start a capture that cannot record every stop signal; "
                "--out was not created and the serial port was not opened",
                err=True,
            )
            return 2
    try:
        os.umask(0o077)
        try:
            args.out.mkdir(parents=True, exist_ok=False)
        except OSError as error:
            _emit(f"error: could not create --out {args.out} ({error})", err=True)
            return 2
        started_utc = datetime.now(UTC)
        metadata = build_metadata(args, receipt, started_utc, datetime.now().astimezone())
        try:
            write_metadata(args.out, metadata)
        except OSError as error:
            _emit(f"error: could not write capture.json ({error})", err=True)
            return 2
        _emit(
            "WARNING: opening the serial port asserts DTR/RTS and may reset the board; wait for "
            "StateMachine: State: activating -> idle before speaking."
        )
        _emit(
            f"CAPTURE_METADATA out={args.out} mode={metadata['capture_mode']} "
            f"firmware_release_head={receipt.payload['release_head']} "
            f"firmware_receipt_sha256={receipt.sha256}"
        )
        if args.preflight_only:
            _emit("PREFLIGHT_ONLY serial_opened=False")
            return 0
        return stream_serial(args, str(metadata["since_utc"]), recorder, previous_handlers)
    finally:
        # Backstop only: the live capture hands these back itself once its closing
        # record is written; this covers a run that returned before it could.
        backstop: list[str] = []
        _restore_stop_handlers(previous_handlers, backstop)
        for error in backstop:
            _emit(f"warning: {error}", err=True)


if __name__ == "__main__":
    raise SystemExit(main())
