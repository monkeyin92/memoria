#!/usr/bin/env python3
"""Hold the robot's serial port open and write it to a timestamped log, until stopped.

On macOS every open of the USB-Serial-JTAG port pulses DTR/RTS and resets the board
(`rst:0x15 USB_UART_CHIP_RESET`); a few resets in a row left the board silent at the bootloader once and it
needed a USB re-plug. So the port is opened exactly once, here, and the soak driver / trials read this log
(`voice_soak.py --serial-log FILE`) instead of opening the port again.

    uv run --no-project --with pyserial python scripts/voice_soak_serial_logger.py outputs/serial/robot.log [--port /dev/cu.usbmodem2101]

Stop it (Ctrl-C / kill) before flashing: esptool needs the port to itself.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

import serial  # pyserial, from the ESP-IDF python env


def stamp(t: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(t)) + f".{int((t % 1) * 1000):03d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--port", default="/dev/cu.usbmodem2101")
    args = ap.parse_args()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = args.port, 115200, 0.2
    ser.dtr = False
    ser.rts = False
    ser.open()
    buf = b""
    with args.out.open("ab") as fh:
        while not stopping:
            chunk = ser.read(4096)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                fh.write(f"{stamp(time.time())} {raw.decode('utf-8', 'replace').rstrip(chr(13))}\n".encode())
            fh.flush()
    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
