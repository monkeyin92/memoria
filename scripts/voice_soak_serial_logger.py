#!/usr/bin/env python3
"""Hold the robot's serial port open and write it to a timestamped log, until stopped.

On macOS a default pyserial open of the USB-Serial-JTAG port resets the board
(`rst:0x15 USB_UART_CHIP_RESET`), and so does setting DTR and RTS both low before the open; a few resets in a
row left the board silent at the bootloader once and it needed a USB re-plug. RTS low with DTR high before the
open does not reset it (measured on the robot 2026-10-21), so `open_port` is the only way these scripts open
the port, and nothing touches DTR/RTS afterwards. The port is still held by this one process, and the soak
driver / trials read this log (`voice_soak.py --serial-log FILE`) instead of opening the port again.

    uv run --no-project --with pyserial python scripts/voice_soak_serial_logger.py outputs/serial/robot.log \
        [--port /dev/cu.usbmodem2101] [--command-socket PATH] [--last-seen-cmd 'CMD']

When the port gives no data for 30 s (or reading it fails), the logger closes it, looks for /dev/cu.usbmodem*
again (the name may change after a re-enumeration), reopens it the same way and waits another 30 s, at most
3 times. A handle opened before an `esptool --after hard-reset` stays silent, which is how that case looks.
The firmware has no reboot command on its USB line, so the logger never resets the board. If all 3 reopens
stay silent it logs `LOGGER: 需要人工拔插 USB ...` and exits with status 2; with `--last-seen-cmd` it runs that
shell command first and appends its output, meant to be when the server last heard from this device.

Since firmware build 20 this process is also the only way to talk TO the robot. It listens on a unix socket
(default /tmp/memoria-serial-<port>.sock, owner-only) and writes the few allowed commands into the port it
already holds. `wake`: `python scripts/voice_soak_serial_command.py wake`, or the soak driver, wakes an idle
robot exactly like a tap on the round screen, so computer-driven tests never have to play the wake word.
`snap`, `status`, `pcmarm` and `pcmdump` are answered by bench images only (firmware/esp32/scripts/build.sh
--bench): the robot streams its screenshot as SNAP lines, logs one status line, records a PCM window and
streams it as PCMD lines, which this log records like all other output;
a product image does not know them and logs `usb command ignored`. The log records every command as a
`LOGGER: command <word> sent` line, and the robot answers with `usb wake accepted ...` / `usb wake ignored
reason=...` and `usb snap accepted`. Nothing but these five words is ever written to the port.

Stop it (Ctrl-C / kill) before flashing: esptool needs the port to itself.
"""

from __future__ import annotations

import argparse
import glob
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import voice_soak_serial_command as serial_command  # noqa: E402

# What may be written into the port: `wake`, which every image knows, and the bench-only `snap`, `status`,
# `pcmarm` and `pcmdump`, none of which changes what the robot does. Everything else is refused here, so a typo
# or another local process cannot type into the board.
WIRE_COMMANDS = {
    "wake": b"wake\n",
    "snap": b"snap\n",
    "status": b"status\n",
    "pcmarm": b"pcmarm\n",
    "pcmdump": b"pcmdump\n",
}

_MAX_REQUEST = 64

PORT_GLOB = "/dev/cu.usbmodem*"
SILENCE_S = 30.0
REOPEN_ATTEMPTS = 3
REOPEN_PAUSE_S = 5.0
REPLUG_HINT = "LOGGER: 需要人工拔插 USB"


def stamp(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def open_port(serial_module: Any, port: str) -> Any:
    """Open the robot's port without resetting it: RTS low and DTR high must be set before the open."""
    ser = serial_module.Serial()
    ser.port, ser.baudrate, ser.timeout, ser.write_timeout = port, 115200, 0.2, 2.0
    ser.rts = False
    ser.dtr = True
    ser.open()
    return ser


def pick_port(preferred: str, found: list[str]) -> str | None:
    """`preferred` while it exists, else the first /dev/cu.usbmodem* the system lists now."""
    if preferred in found:
        return preferred
    return min(found) if found else None


class PortGaveUp(RuntimeError):
    """The port stayed silent through every reopen; only a USB re-plug helps now."""


class ReopeningPort:
    """The open port, reopened (never reset) when it gives no data for `silence_s` seconds."""

    def __init__(
        self,
        serial_module: Any,
        port: str,
        log: Callable[[str], None],
        *,
        silence_s: float = SILENCE_S,
        attempts: int = REOPEN_ATTEMPTS,
        pause_s: float = REOPEN_PAUSE_S,
        list_ports: Callable[[], list[str]] = lambda: glob.glob(PORT_GLOB),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._serial = serial_module
        self.port = port
        self._log = log
        self._silence_s = silence_s
        self._attempts = attempts
        self._pause_s = pause_s
        self._list_ports = list_ports
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._ser: Any = open_port(serial_module, port)
        self._last_data = clock()
        self._reopens = 0

    def read(self) -> bytes:
        """Up to 4096 bytes, b"" when none came yet. Raises PortGaveUp after the last silent reopen."""
        ser = self._ser
        if ser is not None:
            try:
                chunk = ser.read(4096)
            except self._serial.SerialException as exc:
                self._log(f"LOGGER: serial read failed: {exc}")
            else:
                if chunk:
                    self._last_data = self._clock()
                    self._reopens = 0
                    return bytes(chunk)
                if self._clock() - self._last_data < self._silence_s:
                    return b""
                self._log(f"LOGGER: no serial data for {self._silence_s:.0f} s on {self.port}")
        self._reopen()
        return b""

    def write(self, payload: bytes) -> None:
        with self._lock:
            if self._ser is None:
                raise self._serial.SerialException("the port is being reopened")
            self._ser.write(payload)
            self._ser.flush()

    def close(self) -> None:
        with self._lock:
            ser, self._ser = self._ser, None
        if ser is not None:
            ser.close()

    def _reopen(self) -> None:
        if self._reopens >= self._attempts:
            raise PortGaveUp(
                f"{self.port}: no serial data after {self._attempts} reopens, {self._silence_s:.0f} s each"
            )
        self._reopens += 1
        attempt = f"reopen {self._reopens}/{self._attempts}"
        try:
            self.close()
        except (self._serial.SerialException, OSError):
            pass  # the handle is dead already; the reopen below is what matters
        self._sleep(self._pause_s)
        port = pick_port(self.port, self._list_ports())
        if port is None:
            self._log(f"LOGGER: {attempt}: no {PORT_GLOB} present")
            return
        try:
            ser = open_port(self._serial, port)
        except (self._serial.SerialException, OSError) as exc:
            self._log(f"LOGGER: {attempt}: cannot open {port}: {exc}")
            return
        with self._lock:
            self._ser = ser
        self._last_data = self._clock()
        if port != self.port:
            self._log(f"LOGGER: {attempt}: port is now {port} (was {self.port})")
        self.port = port
        self._log(f"LOGGER: {attempt}: opened {port}")


def last_seen(command: str | None) -> str:
    """What `--last-seen-cmd` prints (its first line), for the re-plug hint; nothing runs without it."""
    if not command:
        return "未提供 --last-seen-cmd"
    try:
        done = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired:
        return "--last-seen-cmd 30 秒未返回"
    lines = done.stdout.strip().splitlines()
    if done.returncode != 0 or not lines:
        return f"--last-seen-cmd 没有结果 (exit {done.returncode})"
    return lines[0][:200]


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
    ap.add_argument(
        "--last-seen-cmd",
        default=None,
        help="shell command printing when the server last heard from this device; run only when the "
        "logger gives up and asks for a USB re-plug",
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

    log_lock = threading.Lock()
    status = 0
    with args.out.open("ab") as fh:

        def emit(text: str) -> None:
            with log_lock:
                fh.write(f"{stamp(time.time())} {text}\n".encode())
                fh.flush()

        ser = ReopeningPort(serial, args.port, emit)
        server: CommandServer | None = None
        if not args.no_command_socket:
            path = args.command_socket or serial_command.default_socket_path(args.port)
            server = CommandServer(path, ser.write, emit)
            try:
                server.start()
            except BaseException:
                ser.close()
                raise
            emit(f"LOGGER: started port={args.port} command_socket={path}")
        buf = b""
        try:
            while not stopping:
                chunk = ser.read()
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    emit(raw.decode("utf-8", "replace").rstrip(chr(13)))
        except PortGaveUp as exc:
            hint = f"{REPLUG_HINT}（{exc}；固件没有串口重启命令，未复位）；服务器最后一次请求: {last_seen(args.last_seen_cmd)}"
            emit(hint)
            print(hint, file=sys.stderr, flush=True)
            status = 2
        finally:
            if server is not None:
                server.stop()
            ser.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
