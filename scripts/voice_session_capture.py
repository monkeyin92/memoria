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
import time
from dataclasses import dataclass
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


class ReceiptError(Exception):
    """--firmware-receipt is missing, unreadable or fails validation."""


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
    for field in DIGEST_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not HEX64.match(value):
            raise ReceiptError(f"--firmware-receipt {path} has no 64-hex {field} (found {value!r})")
    for field in READBACK_FLAGS:
        if payload.get(field) is not True:
            raise ReceiptError(
                f"--firmware-receipt {path} does not confirm {field}=true "
                f"(found {payload.get(field)!r}); only a receipt whose read-back verification "
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
    (out / "capture.json").write_text(json.dumps(metadata, indent=2) + "\n")


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


def stream_serial(args: argparse.Namespace, since: str) -> int:
    try:
        import serial
    except ImportError:
        print(
            "error: a live capture needs pyserial; run this tool with "
            "uv run --no-project --with pyserial --with esptool python "
            "scripts/voice_session_capture.py ... or pass --preflight-only",
            file=sys.stderr,
        )
        return 2
    hard_reset = None
    if args.boot_reset:
        try:
            from esptool.reset import HardReset
        except ImportError:
            print(
                "error: --boot-reset needs esptool; add --with esptool to the uv run command",
                file=sys.stderr,
            )
            return 2
        hard_reset = HardReset

    stop = False

    def request_stop(signum, frame) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    streams: dict[str, dict[str, object]] = {}
    processes: list[tuple[str, subprocess.Popen[bytes]]] = []
    handles: list = []
    serial_error = ""
    port = None
    try:
        port = serial.Serial(port=None, baudrate=args.baudrate, timeout=0.5, exclusive=True)
        port.dtr = False
        port.rts = False
        port.port = args.port
        port.open()
        metadata = json.loads((args.out / "capture.json").read_text())
        metadata["serial_opened"] = True
        write_metadata(args.out, metadata)
        print(
            f"SERIAL_OPEN pid={os.getpid()} port={port.port} since={since} reset_expected=True",
            flush=True,
        )
        if args.server_logs:
            for label, container in CONTAINERS:
                handle = (args.out / f"{label}.log").open("ab", buffering=0)
                handles.append(handle)
                try:
                    process = _ssh_log_stream(container, since, handle)
                except OSError as error:
                    streams[label] = {
                        "container": container,
                        "status": "start_failed",
                        "exit_code": None,
                        "error": f"{type(error).__name__}: {error}",
                    }
                    print(
                        f"LOG_STREAM_START_FAILED label={label} error={streams[label]['error']}",
                        flush=True,
                    )
                    continue
                processes.append((label, process))
                streams[label] = {"container": container, "status": "running", "exit_code": None}
        if hard_reset is not None:
            hard_reset(port, uses_usb=True)()
            print("BOOT_RESET_ISSUED", flush=True)
        deadline = time.monotonic() + args.duration
        reported: set[str] = set()
        with (args.out / "serial.log").open("ab", buffering=0) as output:
            while not stop and time.monotonic() < deadline:
                try:
                    chunk = port.readline()
                except Exception as error:  # serial backends raise many exception types
                    serial_error = f"{type(error).__name__}: {error}"
                    print(f"SERIAL_READ_ERROR error={serial_error}", file=sys.stderr, flush=True)
                    break
                if chunk:
                    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
                    output.write(f"[{stamp}] ".encode() + chunk)
                    if b"SystemInfo" in chunk:
                        print(f"HEARTBEAT {stamp}", flush=True)
                for label, process in processes:
                    if process.poll() is not None and label not in reported:
                        reported.add(label)
                        code = process.returncode
                        streams[label]["exit_code"] = code
                        streams[label]["status"] = "exited_early" if code == 0 else "failed"
                        print(f"LOG_STREAM_EXIT label={label} exit={code}", flush=True)
    except Exception as error:  # opening the port can raise many exception types
        serial_error = f"{type(error).__name__}: {error}"
        print(f"SERIAL_OPEN_ERROR error={serial_error}", file=sys.stderr, flush=True)
    finally:
        if port is not None:
            try:
                port.close()
            except Exception:
                print("warning: closing the serial port failed", file=sys.stderr, flush=True)
        for _, process in processes:
            if process.poll() is None:
                process.terminate()
        for label, process in processes:
            forced = False
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                forced = True
            if streams.get(label, {}).get("status") == "running":
                streams[label]["exit_code"] = process.returncode
                streams[label]["status"] = "stopped_by_capture"
                streams[label]["forced_kill"] = forced
        for handle in handles:
            handle.close()
        print(f"CAPTURE_STOPPED {datetime.now().astimezone().isoformat()}", flush=True)

    anomalies: list[str] = []
    for label, record in sorted(streams.items()):
        if record["status"] == "stopped_by_capture":
            if record.get("forced_kill"):
                anomalies.append(f"log stream {label} ignored SIGTERM and needed SIGKILL")
            continue
        detail = f"log stream {label} {record['status']} (exit={record['exit_code']})"
        if record.get("error"):
            detail += f" error={record['error']}"
        anomalies.append(detail)
    if serial_error:
        anomalies.append(f"serial {serial_error}")
    try:
        metadata = json.loads((args.out / "capture.json").read_text())
        metadata["serial_error"] = serial_error or None
        metadata["log_streams"] = streams
        metadata["log_stream_health"] = (
            "degraded" if anomalies else ("healthy" if args.server_logs else "not_requested")
        )
        metadata["log_stream_health_reasons"] = anomalies
        metadata["completed_at_local"] = datetime.now().astimezone().isoformat()
        write_metadata(args.out, metadata)
    except (OSError, json.JSONDecodeError) as error:
        print(f"error: could not record capture health ({error})", file=sys.stderr)
        return 2
    if anomalies:
        print(f"CAPTURE_HEALTH degraded reasons={anomalies}", flush=True)
        return 1
    print("CAPTURE_HEALTH healthy", flush=True)
    return 0


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
        print(
            f"error: --duration must be between 1 and {MAX_DURATION_S} seconds (got {args.duration})",
            file=sys.stderr,
        )
        return 2
    try:
        receipt = load_firmware_receipt(args.firmware_receipt)
    except ReceiptError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.out.exists():
        print(
            f"error: --out {args.out} already exists; a capture never overwrites an earlier one",
            file=sys.stderr,
        )
        return 2

    os.umask(0o077)
    try:
        args.out.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        print(f"error: could not create --out {args.out} ({error})", file=sys.stderr)
        return 2
    started_utc = datetime.now(UTC)
    metadata = build_metadata(args, receipt, started_utc, datetime.now().astimezone())
    try:
        write_metadata(args.out, metadata)
    except OSError as error:
        print(f"error: could not write capture.json ({error})", file=sys.stderr)
        return 2
    print(
        "WARNING: opening the serial port asserts DTR/RTS and may reset the board; wait for "
        "StateMachine: State: activating -> idle before speaking.",
        flush=True,
    )
    print(
        f"CAPTURE_METADATA out={args.out} mode={metadata['capture_mode']} "
        f"firmware_release_head={receipt.payload['release_head']} "
        f"firmware_receipt_sha256={receipt.sha256}",
        flush=True,
    )
    if args.preflight_only:
        print("PREFLIGHT_ONLY serial_opened=False", flush=True)
        return 0
    return stream_serial(args, str(metadata["since_utc"]))


if __name__ == "__main__":
    raise SystemExit(main())
