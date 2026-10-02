#!/usr/bin/env python3
"""Back up and fingerprint the robot's flash before and after a USB write (esptool 5.x API).

Run with the ESP-IDF python env (`~/.espressif/python_env/idf6.0_py3.12_env/bin/python`), with nothing else
holding the serial port (stop `scripts/voice_soak_serial_logger.py` first):

    flash_backup.py md5 OUT.json             device-side MD5 of every region, no bulk transfer
    flash_backup.py backup OUTDIR            slot 0x20000/0x3f0000, bootloader+ptable, nvs+otadata+phy, identity

Why not `esptool read-flash`: on this board's USB-Serial-JTAG it dies deterministically on some 4 KB blocks
("No more data to read from the serial port"; 2026-10-02: 0x106000 and about one block in a hundred after it),
while the same bytes read fine as 2 KB halves. So reads go in 64 KB pieces and a piece that fails is
re-connected and re-read in 2 KB, then 1 KB, ... frames. Every backup file is checked against the MD5 the chip
computes itself. The chip is left in download mode: finish with
`esptool --chip esp32s3 -p PORT --after hard-reset read-mac` (or let the serial logger's port open reset it).

The same MD5 is the before/after comparison for the regions a write must not touch (bootloader, partition table,
nvs, phy_init, memoria_identity, assets), since a bulk read-back of them would trip over the same blocks. After
the first boot of the new app the otadata sector holds the bootloader's record again, so it is not expected to
stay blank (it was identical before and after the 2026-10-02 flash).

Write only with `esptool write-flash 0x20000 <app> 0xd000 <8 KiB of 0xff>`: never `flash.sh`, merged.bin or
erase-all (docs/runbooks/release-rollback.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import time

from esptool.cmds import attach_flash, detect_chip

DEFAULT_PORT = "/dev/cu.usbmodem2101"

REGIONS = {
    "boot_and_ptable": (0x0, 0x9000),
    "nvs": (0x9000, 0x4000),
    "otadata": (0xD000, 0x2000),
    "phy_init": (0xF000, 0x1000),
    "identity": (0x10000, 0x10000),
    "ota_0": (0x20000, 0x3F0000),
    "assets": (0x800000, 0x800000),
}
BACKUP = {
    "ota0-before.bin": (0x20000, 0x3F0000),
    "boot-and-ptable-before.bin": (0x0, 0x9000),
    "nvs-otadata-phy-before.bin": (0x9000, 0x7000),
    "identity-before.bin": (0x10000, 0x10000),
}
PIECE = 0x10000
SMALLEST_FRAME = 0x100


class Link:
    """One esptool stub session; reopened whenever the stub is stuck on a frame."""

    def __init__(self, port: str) -> None:
        self.port = port
        self.esp = None
        self.reconnects = 0

    def open(self) -> None:
        self.close()
        esp = detect_chip(port=self.port, baud=460800, connect_attempts=7)
        esp = esp.run_stub()
        attach_flash(esp)
        self.esp = esp

    def close(self) -> None:
        if self.esp is not None:
            try:
                self.esp._port.close()
            except Exception:  # the port may already be gone
                pass
            self.esp = None

    def read(self, offset: int, length: int) -> bytes:
        assert self.esp is not None
        return self.esp.read_flash(offset, length)

    def md5(self, offset: int, length: int) -> str:
        assert self.esp is not None
        return self.esp.flash_md5sum(offset, length)


def read_in_frames(link: Link, offset: int, length: int, frame: int) -> bytes:
    """[offset, offset+length) in `frame`-sized frames; a frame that fails is retried half the size after a reconnect."""
    out = b""
    pos = offset
    while pos < offset + length:
        size = min(frame, offset + length - pos)
        try:
            out += link.read(pos, size)
        except Exception as exc:  # FatalError / serial timeout: the stub is stuck on this frame
            if frame <= SMALLEST_FRAME:
                raise
            print(
                f"  frame {pos:#x}+{size:#x} failed ({str(exc)[:60]}); retrying in {frame // 2:#x}-byte frames",
                flush=True,
            )
            link.reconnects += 1
            link.open()
            out += read_in_frames(link, pos, size, frame // 2)
        pos += size
    return out


def read_region(link: Link, offset: int, length: int) -> bytes:
    out = bytearray()
    pos = offset
    started = time.time()
    while pos < offset + length:
        size = min(PIECE, offset + length - pos)
        try:
            out += link.read(pos, size)
        except Exception as exc:
            print(
                f"  piece {pos:#x}+{size:#x} failed ({str(exc)[:60]}); reconnecting, 2 KB frames",
                flush=True,
            )
            link.reconnects += 1
            link.open()
            out += read_in_frames(link, pos, size, 0x800)
        pos += size
        if (pos - offset) % 0x40000 == 0 or pos >= offset + length:
            print(
                f"  {pos - offset:#x}/{length:#x} bytes ({time.time() - started:.0f}s, {link.reconnects} reconnects)",
                flush=True,
            )
    return bytes(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("mode", choices=("md5", "backup"))
    parser.add_argument("target", help="OUT.json for md5, OUTDIR for backup")
    parser.add_argument("--port", default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    link = Link(args.port)
    link.open()
    try:
        if args.mode == "md5":
            digests = {name: link.md5(offset, size) for name, (offset, size) in REGIONS.items()}
            pathlib.Path(args.target).write_text(json.dumps(digests, indent=1), encoding="utf-8")
            print(json.dumps(digests, indent=1))
            return 0
        outdir = pathlib.Path(args.target)
        outdir.mkdir(parents=True, exist_ok=True)
        sums = []
        for name, (offset, size) in BACKUP.items():
            print(f"reading {name} {offset:#x}+{size:#x}", flush=True)
            data = read_region(link, offset, size)
            (outdir / name).write_bytes(data)
            local, device = hashlib.md5(data).hexdigest(), link.md5(offset, size)
            print(
                f"  {name}: {len(data)} bytes, md5 {local}, chip md5 {device}, {'OK' if local == device else 'MISMATCH'}",
                flush=True,
            )
            if local != device:
                return 1
            sums.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        (outdir / "SHA256SUMS-before").write_text("".join(sums), encoding="utf-8")
        print("BACKUP_OK", flush=True)
        return 0
    finally:
        link.close()


if __name__ == "__main__":
    sys.exit(main())
