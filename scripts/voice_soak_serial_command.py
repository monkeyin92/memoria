#!/usr/bin/env python3
"""Send one command to the robot through the resident serial logger (firmware build 20 and later).

The robot's USB port is held open by exactly one process, `voice_soak_serial_logger.py`: opening the port a
second time pulses DTR/RTS and resets the board. The logger therefore listens on a unix socket and writes the
few allowed commands into the port it already holds. This module is the client side, used by the soak driver
and by hand:

    python scripts/voice_soak_serial_command.py wake [--socket PATH | --port /dev/cu.usbmodem2101]

`wake` starts a conversation on an idle robot exactly like a tap on the round screen, under the same gate (the
phone's wake mode must include the screen). It is how computer-driven tests wake the robot now: never by
playing the wake word, which false-triggers. Whether the robot accepted it is in the serial log:
`usb wake accepted wake_mode=...` or `usb wake ignored reason=not_idle|wake_mode|pairing|starting ...`.

`snap` and `status` are answered by bench images only (firmware/esp32/scripts/build.sh --bench, TODOLIST M-2);
a product image logs `usb command ignored` and does nothing. The answer comes back through the serial log, so
with `--log FILE` (the logger's output) this client waits for it:

    snap   --log robot.log [--out shot.png]   the whole round display, text layer included, as a PNG (~8 s)
    status --log robot.log [--after 60]       what the mascot is doing; with --after, asked again that many
                                              seconds later, and what its frames cost in between

    pcmarm  --log robot.log                   record the fixed PCM window (memoria_diag_pcm.h); waits for
                                              the robot to say it armed
    pcmdump --log robot.log [--out DIR]       stream the sealed recording, verify it and write one WAV per
                                              channel plus manifest.json (scripts/diag_pcm_extract.py)
    tone                                      play a 1 kHz sine on a bench image and capture raw ES7210 slots

Without `--log` the request is only sent and the answer is left in the log for scripts/snap_to_png.py,
scripts/bench_status.py and scripts/diag_pcm_extract.py to read later.

Standard library only, so the tests and the soak driver can import it without pyserial.
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bench_status  # noqa: E402
import diag_pcm_extract  # noqa: E402
import snap_to_png  # noqa: E402

DEFAULT_PORT = "/dev/cu.usbmodem2101"

# What the logger accepts on its socket. All but `ping` reach the robot; `ping` just proves the logger is alive.
COMMANDS = ("wake", "snap", "status", "pcmarm", "pcmdump", "tone", "ping")
# The ones only a bench image answers.
BENCH_COMMANDS = ("snap", "status", "pcmarm", "pcmdump")
# Which commands each option belongs to.
_OPTION_COMMANDS = {
    "--log": BENCH_COMMANDS,
    "--wait": BENCH_COMMANDS,
    "--out": ("snap", "pcmdump"),
    "--partial": ("snap", "pcmdump"),
    "--after": ("status",),
}
_DEFAULT_WAIT = {"snap": 40.0, "status": 10.0, "pcmarm": 10.0, "pcmdump": 90.0}

_MAX_REPLY = 256


class SerialCommandError(RuntimeError):
    """The command did not reach the robot (no logger, refused by the logger, or the write failed)."""


def default_socket_path(port: str = DEFAULT_PORT) -> Path:
    """Where the logger for `port` listens by default: short, because a unix socket path is limited to ~104
    bytes on macOS, and predictable, so the soak driver finds it without being told."""
    name = Path(port).name
    for prefix in ("cu.", "tty."):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    return Path("/tmp") / f"memoria-serial-{re.sub(r'[^A-Za-z0-9_.-]', '_', name) or 'port'}.sock"


def send_command(
    command: str,
    *,
    socket_path: Path | str | None = None,
    port: str = DEFAULT_PORT,
    timeout: float = 5.0,
) -> str:
    """Ask the logger to send `command`; returns its reply (`ok`) or raises SerialCommandError."""
    if command not in COMMANDS:
        raise SerialCommandError(f"unknown command {command!r}; allowed: {', '.join(COMMANDS)}")
    path = Path(socket_path) if socket_path else default_socket_path(port)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        try:
            conn.connect(str(path))
        except OSError as exc:
            raise SerialCommandError(
                f"no serial logger listening at {path} ({exc}); start scripts/voice_soak_serial_logger.py"
            ) from exc
        try:
            conn.sendall(command.encode("ascii") + b"\n")
            reply = b""
            while b"\n" not in reply and len(reply) < _MAX_REPLY:
                chunk = conn.recv(_MAX_REPLY)
                if not chunk:
                    break
                reply += chunk
        except OSError as exc:  # includes the socket timeout
            raise SerialCommandError(f"serial logger did not answer {command!r}: {exc}") from exc
    text = reply.decode("utf-8", "replace").strip()
    if text != "ok" and not text.startswith("ok "):
        raise SerialCommandError(text or "empty reply from the serial logger")
    return text


def _log_size(log: Path) -> int:
    try:
        return log.stat().st_size
    except FileNotFoundError:
        return 0


def take_snapshot(
    log: Path,
    *,
    socket_path: Path | str | None = None,
    port: str = DEFAULT_PORT,
    wait: float = 40.0,
    partial: bool = False,
) -> tuple[snap_to_png.Snapshot, bytes]:
    """Send `snap`, wait for the picture it makes in `log`, and return it with its PNG. A whole picture takes
    the robot some 8 s to send, so `wait` is generous; a robot that is not a bench image ends it at once."""
    offset = _log_size(log)
    send_command("snap", socket_path=socket_path, port=port)
    try:
        shot = snap_to_png.wait_for_snapshot(log, offset, wait)
        return shot, snap_to_png.render(shot, partial=partial)
    except snap_to_png.SnapError as exc:
        raise SerialCommandError(str(exc)) from exc


def read_status(
    log: Path,
    *,
    socket_path: Path | str | None = None,
    port: str = DEFAULT_PORT,
    wait: float = 10.0,
) -> bench_status.Status:
    """Send `status` and return the line it makes in `log` (the robot answers within one animation frame)."""
    offset = _log_size(log)
    send_command("status", socket_path=socket_path, port=port)
    try:
        return bench_status.wait_for_status(log, offset, wait)
    except bench_status.StatusError as exc:
        raise SerialCommandError(str(exc)) from exc


def pcm_request(
    command: str,
    log: Path,
    *,
    socket_path: Path | str | None = None,
    port: str = DEFAULT_PORT,
    wait: float,
) -> diag_pcm_extract.Transcript:
    """Send `pcmarm` or `pcmdump` and return the journal it answers about in `log`: armed, fully dumped, or
    refused (Transcript.failed says why)."""
    offset = _log_size(log)
    send_command(command, socket_path=socket_path, port=port)
    done = diag_pcm_extract.armed if command == "pcmarm" else diag_pcm_extract.dumped
    try:
        return diag_pcm_extract.wait_for(log, offset, wait, done)
    except diag_pcm_extract.CaptureError as exc:
        raise SerialCommandError(str(exc)) from exc


def _bench_request(args: argparse.Namespace) -> str:
    """Run a bench command with the answer awaited in args.log; returns what to print."""
    where = {"socket_path": args.socket, "port": args.port}
    wait = args.wait or _DEFAULT_WAIT[args.command]
    if args.command in ("pcmarm", "pcmdump"):
        journal = pcm_request(args.command, args.log, wait=wait, **where)
        if journal.failed is not None:
            raise SerialCommandError(f"the robot refused `{args.command}`: reason={journal.failed}")
        if args.command == "pcmarm":
            return f"armed journal {journal.id}"
        out = args.out or Path(f"pcm-{journal.id}")
        verdict = diag_pcm_extract.export(journal, out, partial=args.partial)
        if not verdict.intact:
            raise SerialCommandError(f"journal {journal.id} is damaged: " + "; ".join(verdict.details))
        return f"wrote {out} (journal {journal.id}, intact)"
    if args.command == "snap":
        shot, png = take_snapshot(args.log, partial=args.partial, wait=wait, **where)
        out = args.out or Path(f"snap-{shot.id}.png")
        out.write_bytes(png)
        note = ", damaged, gaps painted magenta" if shot.problems() else ""
        return f"wrote {out} ({shot.width}x{shot.height}, snapshot {shot.id}{note})"
    first = read_status(args.log, wait=wait, **where)
    if args.after is None:
        return bench_status.format_status(first)
    time.sleep(args.after)
    second = read_status(args.log, wait=wait, **where)
    try:
        return bench_status.format_rates(first, second)
    except bench_status.StatusError as exc:
        raise SerialCommandError(str(exc)) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--socket", default=None, help="the logger's command socket")
    parser.add_argument(
        "--port", default=DEFAULT_PORT, help="used only to derive the default socket path"
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument(
        "--log",
        type=Path,
        default=None,
        help="bench commands: the logger's output, to wait for the answer in",
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="snap: the PNG (default snap-<id>.png); pcmdump: the directory (default pcm-<id>)"
    )
    parser.add_argument(
        "--wait", type=float, default=None, help="bench commands: seconds to wait (snap 40, status 10, pcmarm 10, pcmdump 90)"
    )
    parser.add_argument(
        "--partial", action="store_true", help="snap: draw a damaged picture, gaps in magenta; pcmdump: write a damaged journal's WAVs too"
    )
    parser.add_argument(
        "--after",
        type=float,
        default=None,
        help="status: ask again after this many seconds and print the rates between the two answers",
    )
    args = parser.parse_args(argv)
    given = {
        "--log": args.log,
        "--wait": args.wait,
        "--out": args.out,
        "--partial": args.partial or None,
        "--after": args.after,
    }
    for flag, value in given.items():
        if value is not None and args.command not in _OPTION_COMMANDS[flag]:
            parser.error(f"{flag} does not apply to {args.command}")
        if value is not None and flag != "--log" and args.log is None:
            parser.error(f"{flag} needs --log FILE: the answer comes back through the log")
    try:
        if args.command in BENCH_COMMANDS and args.log is not None:
            print(_bench_request(args))
        else:
            print(
                send_command(
                    args.command, socket_path=args.socket, port=args.port, timeout=args.timeout
                )
            )
    except SerialCommandError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
