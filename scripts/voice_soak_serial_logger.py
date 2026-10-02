#!/usr/bin/env python3
"""Hold the robot's serial port open and write it to a timestamped log, until stopped.

On macOS every open of the USB-Serial-JTAG port pulses DTR/RTS and resets the board
(`rst:0x15 USB_UART_CHIP_RESET`); a few resets in a row left the board silent at the bootloader once and it
needed a USB re-plug. So the port is opened exactly once, here, and the soak driver / trials read this log
(`voice_soak.py --serial-log FILE`) instead of opening the port again.

    uv run --no-project --with pyserial python scripts/voice_soak_serial_logger.py outputs/serial/robot.log \
        [--port /dev/cu.usbmodem2101] [--command-socket PATH]

Since firmware build 20 this process is also the only way to talk TO the robot. It listens on a unix socket
(default /tmp/memoria-serial-<port>.sock, owner-only) and writes the one allowed command, `wake`, into the
port it already holds: `python scripts/voice_soak_serial_command.py wake`, or the soak driver, wakes an idle
robot exactly like a tap on the round screen, so computer-driven tests never have to play the wake word.
The log records every command as a `LOGGER: command wake sent` line, and the robot answers with
`usb wake accepted ...` or `usb wake ignored reason=...`. Nothing but `wake` is ever written to the port.

Stop it (Ctrl-C / kill) before flashing: esptool needs the port to itself.
"""

from __future__ import annotations

import argparse
import os
import signal
import socket
import stat
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice_soak_serial_command as serial_command  # noqa: E402

# What may be written into the port. The robot knows exactly one command; everything else is refused here, so
# a typo or another local process cannot type into the board.
WIRE_COMMANDS = {"wake": b"wake\n"}

_MAX_REQUEST = 64


def stamp(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def claim_socket_path(path: Path) -> None:
    """Free `path` for binding: refuse when a live logger serves it, remove the file a dead one left behind."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise SystemExit(f"{path} exists and is not a socket; choose another --command-socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        path.unlink(missing_ok=True)  # nobody listens: left behind by a logger that died
        return
    except OSError as exc:
        raise SystemExit(f"cannot probe {path}: {exc}") from exc
    finally:
        probe.close()
    raise SystemExit(f"another serial logger already serves {path}; stop it first")


class CommandServer:
    """The logger's command socket: one short request per connection, one reply line."""

    def __init__(
        self, path: Path, send: Callable[[bytes], None], log: Callable[[str], None]
    ) -> None:
        self.path = path
        self._send = send
        self._log = log
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def dispatch(self, request: str) -> str:
        """The reply to one request line; the only place a command becomes bytes for the port."""
        verb = request.strip()
        if verb == "ping":
            return "ok"
        payload = WIRE_COMMANDS.get(verb)
        if payload is None:
            return f"error: unknown command (allowed: {', '.join(serial_command.COMMANDS)})"
        try:
            self._send(payload)
        except Exception as exc:  # the port vanished, or the write timed out
            return f"error: serial write failed: {exc}"
        self._log(f"LOGGER: command {verb} sent")
        return "ok"

    def start(self) -> None:
        claim_socket_path(self.path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous_umask = os.umask(0o177)  # the socket file is owner-only
        try:
            sock.bind(str(self.path))
        finally:
            os.umask(previous_umask)
        sock.listen(16)
        sock.settimeout(0.5)
        self._sock = sock
        self._thread = threading.Thread(target=self._serve, name="serial-command", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self.path.unlink(missing_ok=True)

    def _serve(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                self._handle(conn)

    def _handle(self, conn: socket.socket) -> None:
        conn.settimeout(2.0)
        try:
            data = b""
            while b"\n" not in data and len(data) < _MAX_REQUEST:
                chunk = conn.recv(_MAX_REQUEST)
                if not chunk:
                    break
                data += chunk
            reply = self.dispatch(data.decode("ascii", "replace").split("\n", 1)[0])
            conn.sendall(reply.encode() + b"\n")
        except OSError:
            pass  # the client went away; there is nobody to answer


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out", type=Path)
    ap.add_argument("--port", default=serial_command.DEFAULT_PORT)
    ap.add_argument(
        "--command-socket", type=Path, default=None, help="default: /tmp/memoria-serial-<port>.sock"
    )
    ap.add_argument(
        "--no-command-socket", action="store_true", help="only log; never write to the port"
    )
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)

    import serial  # pyserial, from the ESP-IDF python env; imported here so the tests need not have it

    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout, ser.write_timeout = args.port, 115200, 0.2, 2.0
    ser.dtr = False
    ser.rts = False
    ser.open()

    log_lock = threading.Lock()
    write_lock = threading.Lock()
    status = 0
    with args.out.open("ab") as fh:

        def emit(text: str) -> None:
            with log_lock:
                fh.write(f"{stamp(time.time())} {text}\n".encode())
                fh.flush()

        def send(payload: bytes) -> None:
            with write_lock:
                ser.write(payload)
                ser.flush()

        server: CommandServer | None = None
        if not args.no_command_socket:
            path = args.command_socket or serial_command.default_socket_path(args.port)
            server = CommandServer(path, send, emit)
            try:
                server.start()
            except BaseException:
                ser.close()
                raise
            emit(f"LOGGER: started port={args.port} command_socket={path}")
        buf = b""
        try:
            while not stopping:
                chunk = ser.read(4096)
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    emit(raw.decode("utf-8", "replace").rstrip(chr(13)))
        except serial.SerialException as exc:
            emit(f"LOGGER: serial port lost: {exc}")
            status = 1
        finally:
            if server is not None:
                server.stop()
            ser.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
