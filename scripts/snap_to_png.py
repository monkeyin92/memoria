#!/usr/bin/env python3
"""Rebuild a bench build's screenshot from the SNAP lines of a serial log (TODOLIST M-2).

A bench image (`firmware/esp32/scripts/build.sh --bench`) answers `snap` on the USB port by streaming the
whole round display, LVGL text layer included, as log lines. The format is documented in
firmware/esp32/overlay/files/main/memoria/memoria_bench_snap.h:

    SNAP <id> <w>x<h> <seq>/<total> <crc> <base64>      one chunk of the picture, seq counts from 1
    SNAP <id> end <total> <bytes> <crc> fmt=rgb565le    after the last chunk
    SNAP <id> failed reason=<why>                       nothing was sent

The resident serial logger records them like any other output, so the picture is in the log of the session it
was taken in and can be rebuilt later, or from a log that was only grepped:

    python scripts/snap_to_png.py outputs/serial/robot.log --out shot.png            # the latest snapshot
    python scripts/snap_to_png.py outputs/serial/robot.log --list                    # every one, with its state
    python scripts/snap_to_png.py outputs/serial/robot.log --id 0001e240 --out a.png # a given one

`voice_soak_serial_command.py snap --log ... --out ...` sends the request and runs this in one go.

Every chunk is checked on its own (base64, length, CRC-32), and the picture as a whole against the end line.
A picture with a lost or damaged line is refused and the script names the chunks; `--partial` writes it
anyway with those chunks painted magenta. Standard library only (zlib writes the PNG).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import re
import struct
import sys
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

# Must match kSnapChunkBytes and kSnapPixelBytes in memoria_bench_snap.h (a test compares them).
CHUNK_BYTES = 384
PIXEL_BYTES = 2
# No panel this firmware drives is anywhere near this; it keeps a damaged size field from asking for gigabytes.
MAX_PICTURE_BYTES = 4096 * 4096 * PIXEL_BYTES
# Painted where a chunk is missing or damaged (RGB565 0xF81F, little endian).
MISSING_PIXEL = b"\x1f\xf8"

_LINE = re.compile(
    r"SNAP (?P<id>[0-9a-f]{8}) (?:"
    r"(?P<w>\d+)x(?P<h>\d+) (?P<seq>\d+)/(?P<total>\d+) (?P<crc>[0-9a-f]{8}) (?P<data>[A-Za-z0-9+/=]+)"
    r"|end (?P<end_total>\d+) (?P<end_bytes>\d+) (?P<end_crc>[0-9a-f]{8}) fmt=(?P<fmt>[a-z0-9]+)"
    r"|failed reason=(?P<reason>[a-z_]+))"
)


# What the robot logs when it gets a line it does not know: a product image has no `snap` (memoria_usb_command.h).
IGNORED_COMMAND = "usb command ignored (not a command)"
# What a bench image logs when it has no display to photograph (memoria_esp_vocat.cc, HandleUsbSnap).
_SNAP_IGNORED = re.compile(r"usb snap ignored reason=\w+")


class SnapError(RuntimeError):
    """The log holds no usable snapshot (none, an unfinished or damaged one, or the robot refused)."""


@dataclass
class EndLine:
    total: int
    nbytes: int
    crc: int
    fmt: str


@dataclass
class Snapshot:
    id: str
    width: int = 0
    height: int = 0
    total: int = 0
    chunks: dict[int, bytes] = field(
        default_factory=dict
    )  # seq -> the chunk's bytes, only checked ones
    bad: dict[int, str] = field(default_factory=dict)  # seq -> why the line was refused
    notes: list[str] = field(default_factory=list)  # inconsistencies that are not one chunk's
    end: EndLine | None = None
    failed: str | None = None

    @property
    def picture_bytes(self) -> int:
        return self.width * self.height * PIXEL_BYTES

    def _chunk_length(self, seq: int) -> int:
        """How many bytes chunk `seq` must carry: a full chunk, or what is left for the last one."""
        if seq < self.total:
            return CHUNK_BYTES
        return self.picture_bytes - CHUNK_BYTES * (self.total - 1)

    def problems(self) -> list[str]:
        """Everything wrong with this snapshot; empty means the picture can be trusted."""
        if self.failed is not None:
            return [f"the robot could not take it: reason={self.failed}"]
        found = list(self.notes)
        if self.end is None:
            found.append("no end line: the stream is cut or still running")
        missing = [
            seq
            for seq in range(1, self.total + 1)
            if seq not in self.chunks and seq not in self.bad
        ]
        if missing:
            found.append(f"missing chunks {_ranges(missing)} of {self.total}")
        for seq in sorted(self.bad):
            found.append(f"chunk {seq}: {self.bad[seq]}")
        if self.end is not None:
            if self.end.fmt != "rgb565le":
                found.append(f"unsupported pixel format {self.end.fmt!r}")
            if self.end.total != self.total or self.end.nbytes != self.picture_bytes:
                found.append(
                    f"end line says {self.end.total} chunks / {self.end.nbytes} bytes, "
                    f"chunks say {self.total} / {self.picture_bytes}"
                )
            elif not missing and not self.bad:
                crc = zlib.crc32(self.raw())
                if crc != self.end.crc:
                    found.append(f"picture crc {crc:08x} != end line {self.end.crc:08x}")
        return found

    def raw(self, *, fill_missing: bool = False) -> bytes:
        """The picture as rows of little-endian RGB565, chunks in order. Gaps raise unless `fill_missing`."""
        parts: list[bytes] = []
        for seq in range(1, self.total + 1):
            chunk = self.chunks.get(seq)
            if chunk is None:
                if not fill_missing:
                    raise SnapError(f"chunk {seq} is missing")
                chunk = MISSING_PIXEL * (self._chunk_length(seq) // PIXEL_BYTES)
            parts.append(chunk)
        return b"".join(parts)

    @property
    def done(self) -> bool:
        """The robot has said all it will say about this snapshot: the end line, or a refusal."""
        return self.end is not None or self.failed is not None


def _ranges(numbers: list[int]) -> str:
    """[1, 2, 3, 7, 9, 10] -> "1-3, 7, 9-10"."""
    out: list[str] = []
    start = previous = numbers[0]
    for number in numbers[1:] + [None]:  # type: ignore[list-item]
        if number is not None and number == previous + 1:
            previous = number
            continue
        out.append(str(start) if start == previous else f"{start}-{previous}")
        if number is not None:
            start = previous = number
    return ", ".join(out)


def collect(text: str) -> list[Snapshot]:
    """Every snapshot in `text`, in the order its first line appears. Lines that are not SNAP lines, the
    logger's time stamps, CRs and the other output interleaved between chunks are ignored."""
    snapshots: dict[str, Snapshot] = {}
    for line in text.splitlines():
        match = _LINE.search(line)
        if match is None:
            continue
        snap = snapshots.setdefault(match["id"], Snapshot(match["id"]))
        if match["reason"] is not None:
            snap.failed = match["reason"]
        elif match["end_total"] is not None:
            snap.end = EndLine(
                int(match["end_total"]),
                int(match["end_bytes"]),
                int(match["end_crc"], 16),
                match["fmt"],
            )
        else:
            _add_chunk(snap, match)
    return list(snapshots.values())


def _add_chunk(snap: Snapshot, match: re.Match[str]) -> None:
    width, height, seq, total = (
        int(match["w"]),
        int(match["h"]),
        int(match["seq"]),
        int(match["total"]),
    )
    size = width * height * PIXEL_BYTES
    # A line has to make sense on its own before it may decide what the picture is: the crc covers the
    # pixels only, so a damaged size or sequence field is caught here and, for the rest, by the end line.
    if (
        not 0 < size <= MAX_PICTURE_BYTES
        or total != -(-size // CHUNK_BYTES)
        or not 1 <= seq <= total
    ):
        snap.notes.append(f"chunk {seq}/{total} does not fit a {width}x{height} picture")
        return
    if snap.total == 0:
        snap.width, snap.height, snap.total = width, height, total
    elif (snap.width, snap.height, snap.total) != (width, height, total):
        snap.notes.append(
            f"chunk {seq} disagrees about the picture ({width}x{height}, {total} chunks)"
        )
        return
    try:
        data = base64.b64decode(match["data"], validate=True)
    except (binascii.Error, ValueError):
        snap.bad[seq] = "not valid base64"
        return
    if len(data) != snap._chunk_length(seq):
        snap.bad[seq] = f"{len(data)} bytes, expected {snap._chunk_length(seq)}"
        return
    if zlib.crc32(data) != int(match["crc"], 16):
        snap.bad[seq] = "crc mismatch"
        return
    known = snap.chunks.get(seq)
    if known is not None and known != data:
        snap.notes.append(f"chunk {seq} appears twice with different data")
        return
    snap.chunks[seq] = data
    snap.bad.pop(seq, None)  # a clean repeat of a chunk that arrived damaged


def _rgb888_table() -> list[bytes]:
    table = []
    for pixel in range(1 << 16):
        r, g, b = pixel >> 11, (pixel >> 5) & 0x3F, pixel & 0x1F
        table.append(bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))))
    return table


def rgb565le_to_rgb888(raw: bytes) -> bytes:
    """Little-endian RGB565 to RGB888, the low bits refilled from the high ones so white stays white."""
    if len(raw) % PIXEL_BYTES:
        raise SnapError(f"{len(raw)} bytes is not a whole number of RGB565 pixels")
    table = _rgb888_table()
    return b"".join(table[pixel] for pixel in struct.unpack(f"<{len(raw) // PIXEL_BYTES}H", raw))


def png_bytes(width: int, height: int, rgb: bytes) -> bytes:
    """A truecolor PNG of `rgb` (width * height * 3 bytes, rows top to bottom)."""
    if len(rgb) != width * height * 3:
        raise SnapError(f"{len(rgb)} bytes is not a {width}x{height} RGB picture")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    stride = width * 3
    rows = b"".join(b"\x00" + rgb[row * stride : (row + 1) * stride] for row in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows, 6))
        + chunk(b"IEND", b"")
    )


def render(snapshot: Snapshot, *, partial: bool = False) -> bytes:
    """The PNG of a snapshot. Refused unless it is whole and its checksums hold; `partial` paints what is
    missing or damaged magenta instead (a picture is still refused when the robot reported a failure or no
    chunk arrived at all)."""
    problems = snapshot.problems()
    if snapshot.failed is not None or not snapshot.chunks:
        raise SnapError(f"snapshot {snapshot.id}: " + ("; ".join(problems) or "no chunk arrived"))
    if problems and not partial:
        raise SnapError(
            f"snapshot {snapshot.id} is damaged: "
            + "; ".join(problems)
            + " (--partial draws it anyway)"
        )
    rgb = rgb565le_to_rgb888(snapshot.raw(fill_missing=True))
    return png_bytes(snapshot.width, snapshot.height, rgb)


def read_text(path: Path, offset: int = 0) -> str:
    """The log from byte `offset` on; a log that does not exist yet reads as empty."""
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            return handle.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return ""


def wait_for_snapshot(
    path: Path,
    offset: int,
    timeout: float,
    *,
    poll: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Snapshot:
    """Wait for the snapshot a `snap` sent at log position `offset` produces: the first one from there on
    that is finished (its end line is in) and whose first chunk is in the text too, since the tail of an older
    stream is not the answer, or that the robot refused. Raises SnapError when the robot ignores the request or
    nothing finishes in time."""
    deadline = clock() + timeout
    while True:
        text = read_text(path, offset)
        found = collect(text)
        for snap in found:
            if snap.failed is not None or (
                snap.end is not None and (1 in snap.chunks or 1 in snap.bad)
            ):
                return snap
        refused = _SNAP_IGNORED.search(text)
        if refused is not None:
            raise SnapError(f"the robot refused `snap`: {refused[0]}")
        if IGNORED_COMMAND in text:
            raise SnapError(
                "the robot ignored `snap` (usb command ignored): it is not running a bench image; "
                "build and flash one with firmware/esp32/scripts/build.sh --bench and flash.sh --bench"
            )
        if clock() >= deadline:
            break
        sleep(poll)
    seen = [
        f"{snap.id}: {len(snap.chunks)}/{snap.total} chunks, {'with' if snap.end else 'no'} end line"
        for snap in found
        if snap.chunks or snap.bad
    ]
    raise SnapError(
        f"no finished snapshot in {timeout:.0f} s; "
        + (("seen " + "; ".join(seen)) if seen else "no SNAP line arrived at all")
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "log", type=Path, help="serial log with the SNAP lines (the logger's output)"
    )
    parser.add_argument(
        "--out", type=Path, default=None, help="PNG to write (default: snap-<id>.png)"
    )
    parser.add_argument(
        "--id", default=None, help="which snapshot (8 hex digits); default: the latest"
    )
    parser.add_argument(
        "--list", action="store_true", help="list the snapshots in the log and exit"
    )
    parser.add_argument(
        "--partial", action="store_true", help="draw a damaged picture, gaps in magenta"
    )
    args = parser.parse_args(argv)

    snapshots = collect(read_text(args.log))
    if args.list:
        for snap in snapshots:
            problems = snap.problems()
            state = "ok" if not problems else "; ".join(problems)
            print(
                f"{snap.id} {snap.width}x{snap.height} {len(snap.chunks)}/{snap.total} chunks: {state}"
            )
        return 0
    if args.id is not None:
        snapshots = [snap for snap in snapshots if snap.id == args.id]
    if not snapshots:
        print(
            f"error: no SNAP lines{f' for id {args.id}' if args.id else ''} in {args.log}",
            file=sys.stderr,
        )
        return 1
    snap = snapshots[-1]
    try:
        png = render(snap, partial=args.partial)
    except SnapError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out = args.out or Path(f"snap-{snap.id}.png")
    out.write_bytes(png)
    note = " (damaged, gaps painted magenta)" if snap.problems() else ""
    print(f"wrote {out} ({snap.width}x{snap.height}, snapshot {snap.id}){note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
