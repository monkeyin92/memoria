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

Standard library only, so the tests and the soak driver can import it without pyserial.
"""

from __future__ import annotations

import argparse
import re
import socket
import sys
from pathlib import Path

DEFAULT_PORT = "/dev/cu.usbmodem2101"

# What the logger accepts on its socket. Only `wake` reaches the robot; `ping` just proves the logger is alive.
COMMANDS = ("wake", "ping")

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--socket", default=None, help="the logger's command socket")
    parser.add_argument(
        "--port", default=DEFAULT_PORT, help="used only to derive the default socket path"
    )
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args(argv)
    try:
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
