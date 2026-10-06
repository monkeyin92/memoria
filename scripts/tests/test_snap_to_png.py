"""The PC side of the bench build's serial screenshot (TODOLIST M-2): SNAP lines in a log back to a PNG.

The robot's encoder is checked against zlib and base64 in firmware/esp32/tests/test_memoria_bench_snap.py,
which also feeds its real output to this script. Here the protocol is written down a second way, by a small
encoder in this file, so a wrong reading of the format on either side shows up as a disagreement.
"""

from __future__ import annotations

import base64
import re
import struct
import sys
import zlib
from pathlib import Path

import pytest
from scripts import snap_to_png as snap

SCRIPT = Path(snap.__file__)
HEADER = (
    Path(__file__).resolve().parents[2]
    / "firmware/esp32/overlay/files/main/memoria/memoria_bench_snap.h"
)

CHUNK = 384


def _picture(width: int, height: int, seed: int = 1) -> bytes:
    """A reproducible picture of width * height RGB565LE pixels with no repeating pattern."""
    state = seed
    out = bytearray()
    for _ in range(width * height * 2):
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        out.append(state >> 24)
    return bytes(out)


def _encode(picture: bytes, width: int, height: int, snap_id: str = "0001e240") -> list[str]:
    total = -(-len(picture) // CHUNK)
    lines = []
    for seq in range(1, total + 1):
        part = picture[(seq - 1) * CHUNK : seq * CHUNK]
        lines.append(
            f"SNAP {snap_id} {width}x{height} {seq}/{total} {zlib.crc32(part):08x} "
            f"{base64.b64encode(part).decode()}"
        )
    lines.append(
        f"SNAP {snap_id} end {total} {len(picture)} {zlib.crc32(picture):08x} fmt=rgb565le"
    )
    return lines


def _pixel(raw: bytes, index: int) -> tuple[int, int, int]:
    value = raw[index * 2] | (raw[index * 2 + 1] << 8)
    red, green, blue = value >> 11, (value >> 5) & 0x3F, value & 0x1F
    return (
        (red << 3) | (red >> 2),
        (green << 2) | (green >> 4),
        (blue << 3) | (blue >> 2),
    )


def _decode_png(data: bytes) -> tuple[int, int, list[tuple[int, int, int]]]:
    """A PNG reader that accepts exactly what the script writes, and checks it the way a viewer would."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    position = 8
    header: tuple[int, ...] = ()
    compressed = b""
    kinds = []
    while position < len(data):
        (length,) = struct.unpack(">I", data[position : position + 4])
        kind = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        (crc,) = struct.unpack(">I", data[position + 8 + length : position + 12 + length])
        assert zlib.crc32(kind + payload) == crc, kind
        kinds.append(kind)
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", payload)
        elif kind == b"IDAT":
            compressed += payload
        position += 12 + length
    assert kinds == [b"IHDR", b"IDAT", b"IEND"]
    width, height, depth, colour_type, compression, filtering, interlace = header
    assert (depth, colour_type, compression, filtering, interlace) == (8, 2, 0, 0, 0)
    rows = zlib.decompress(compressed)
    stride = 1 + width * 3
    assert len(rows) == stride * height
    pixels = []
    for row in range(height):
        line = rows[row * stride : (row + 1) * stride]
        assert line[0] == 0  # filter type "none"
        pixels += [tuple(line[1 + 3 * x : 4 + 3 * x]) for x in range(width)]
    return width, height, pixels


def _only(text: str) -> snap.Snapshot:
    snapshots = snap.collect(text)
    assert len(snapshots) == 1
    return snapshots[0]


@pytest.mark.parametrize(
    ("width", "height"),
    [(1, 1), (192, 1), (193, 1), (192, 2), (5, 7), (100, 10), (360, 360)],
)
def test_a_picture_comes_back_pixel_for_pixel(width: int, height: int) -> None:
    picture = _picture(width, height, seed=width * 1000 + height)
    shot = _only("\n".join(_encode(picture, width, height)))
    assert shot.problems() == []
    assert (shot.width, shot.height) == (width, height)
    assert shot.raw() == picture
    got_width, got_height, pixels = _decode_png(snap.render(shot))
    assert (got_width, got_height) == (width, height)
    assert pixels == [_pixel(picture, index) for index in range(width * height)]


def test_the_primary_colours_expand_to_the_full_eight_bit_range() -> None:
    values = {0xF800: (255, 0, 0), 0x07E0: (0, 255, 0), 0x001F: (0, 0, 255)}
    values |= {0xFFFF: (255, 255, 255), 0x0000: (0, 0, 0), 0x8410: (132, 130, 132)}
    raw = b"".join(struct.pack("<H", value) for value in values)
    assert snap.rgb565le_to_rgb888(raw) == b"".join(bytes(rgb) for rgb in values.values())
    with pytest.raises(snap.SnapError, match="whole number"):
        snap.rgb565le_to_rgb888(b"\x00\x00\x00")


def test_the_loggers_time_stamps_carriage_returns_and_other_output_do_not_matter() -> None:
    picture = _picture(100, 10)
    lines = _encode(picture, 100, 10)
    noisy: list[str] = ["I (1000) MemoriaMascot: anim frames=791 drawn=498", ""]
    for index, line in enumerate(lines):
        noisy.append(f"22:10:{index % 60:02d}.{index % 1000:03d} {line}\r")
        if index % 2:
            noisy.append("22:10:00.000 W (1234) wifi: beacon timeout\r")
    shot = _only("\n".join(noisy) + "\n")
    assert shot.problems() == []
    assert shot.raw() == picture
    # The same text with CRLF line ends, as the USB console may write them.
    assert _only("\r\n".join(f"12:00:00.000 {line}" for line in lines)).raw() == picture


def test_a_lost_line_is_named_refused_and_painted_magenta_only_when_asked() -> None:
    width, height = 100, 10
    picture = _picture(width, height)
    lines = _encode(picture, width, height)
    assert len(lines) == 7  # six chunks and the end line
    del lines[2]  # chunk 3
    shot = _only("\n".join(lines))
    assert shot.problems() == ["missing chunks 3 of 6"]
    with pytest.raises(snap.SnapError, match=r"missing chunks 3 of 6.*--partial"):
        snap.render(shot)
    _, _, pixels = _decode_png(snap.render(shot, partial=True))
    # Chunk 3 holds bytes 768..1151, that is pixels 384..575.
    for index, rgb in enumerate(pixels):
        expected = (255, 0, 255) if 384 <= index < 576 else _pixel(picture, index)
        assert rgb == expected, index


def test_the_gap_in_the_short_last_chunk_is_the_size_of_what_that_chunk_would_have_held() -> None:
    picture = _picture(100, 10)  # 2000 bytes: five full chunks and 80 bytes
    lines = _encode(picture, 100, 10)
    del lines[5]  # chunk 6, the short one
    shot = _only("\n".join(lines))
    assert shot.problems() == ["missing chunks 6 of 6"]
    _, _, pixels = _decode_png(snap.render(shot, partial=True))
    assert len(pixels) == 1000
    assert all(rgb == (255, 0, 255) for rgb in pixels[960:])
    assert pixels[:960] == [_pixel(picture, index) for index in range(960)]


def test_damaged_lines_are_never_trusted() -> None:
    picture = _picture(100, 10)
    base = _encode(picture, 100, 10)

    def damaged(index: int, edit) -> snap.Snapshot:
        lines = list(base)
        lines[index] = edit(lines[index])
        return _only("\n".join(lines))

    # One character of the payload changed: still base64, no longer the bytes the crc was made from.
    flipped = damaged(1, lambda line: line[:-5] + ("A" if line[-5] != "A" else "B") + line[-4:])
    assert flipped.problems() == ["chunk 2: crc mismatch"]
    # A line that stops early (another task's output cut in, or the host dropped bytes).
    cut = damaged(3, lambda line: line[:-12])
    assert len(cut.problems()) == 1 and cut.problems()[0].startswith("chunk 4: ")
    # Padding in the middle of the payload is not base64.
    broken = damaged(0, lambda line: line[:60] + "=" + line[61:])
    assert broken.problems() == ["chunk 1: not valid base64"]
    # Each of them refuses to draw, and paints the bad chunk magenta when asked.
    for shot in (flipped, cut, broken):
        with pytest.raises(snap.SnapError, match="damaged"):
            snap.render(shot)
        assert _decode_png(snap.render(shot, partial=True))[:2] == (100, 10)


def test_a_clean_repeat_replaces_a_damaged_chunk_and_a_different_repeat_is_flagged() -> None:
    picture = _picture(100, 10)
    lines = _encode(picture, 100, 10)
    bad = lines[1][:-5] + ("A" if lines[1][-5] != "A" else "B") + lines[1][-4:]
    healed = _only("\n".join([lines[0], bad, lines[1], *lines[2:]]))
    assert healed.problems() == []
    assert healed.raw() == picture
    # The same chunk twice is harmless.
    assert _only("\n".join([lines[0], lines[0], *lines[1:]])).problems() == []
    # A chunk that says something else the second time cannot both be right.
    other = _encode(_picture(100, 10, seed=99), 100, 10)
    conflict = _only("\n".join([*lines, other[0]]))
    assert any("appears twice with different data" in problem for problem in conflict.problems())


def test_the_end_line_is_held_against_what_arrived() -> None:
    picture = _picture(100, 10)
    lines = _encode(picture, 100, 10)
    body, end = lines[:-1], lines[-1]
    assert _only("\n".join(body)).problems() == ["no end line: the stream is cut or still running"]
    wrong_crc = end.replace(f"{zlib.crc32(picture):08x}", "00000000")
    problems = _only("\n".join([*body, wrong_crc])).problems()
    assert problems == [f"picture crc {zlib.crc32(picture):08x} != end line 00000000"]
    wrong_size = end.replace(f" {len(picture)} ", " 1999 ")
    assert (
        "end line says 6 chunks / 1999 bytes" in _only("\n".join([*body, wrong_size])).problems()[0]
    )
    wrong_format = end.replace("fmt=rgb565le", "fmt=argb8888")
    assert _only("\n".join([*body, wrong_format])).problems() == [
        "unsupported pixel format 'argb8888'"
    ]


def test_a_robot_that_could_not_take_the_picture_says_why_and_nothing_is_drawn() -> None:
    shot = _only("12:00:00.000 SNAP 00001000 failed reason=no_picture\n")
    assert shot.failed == "no_picture" and shot.done
    assert shot.problems() == ["the robot could not take it: reason=no_picture"]
    for partial in (False, True):
        with pytest.raises(snap.SnapError, match="reason=no_picture"):
            snap.render(shot, partial=partial)


def test_a_line_that_makes_no_sense_cannot_decide_how_big_the_picture_is() -> None:
    absurd = "SNAP 0001e240 99999x99999 1/9999 00000000 AAAA"
    wrong_total = "SNAP 0001e240 100x10 1/7 00000000 AAAA"
    zero = "SNAP 0001e240 0x0 1/1 00000000 AAAA"
    past_the_end = "SNAP 0001e240 100x10 7/6 00000000 AAAA"
    picture = _picture(100, 10)
    for nonsense in (absurd, wrong_total, zero, past_the_end):
        shot = _only("\n".join([nonsense, *_encode(picture, 100, 10)]))
        # The sound lines still make the picture; the nonsense one is reported, not obeyed.
        assert shot.raw() == picture
        assert any("does not fit" in problem for problem in shot.problems())
    # Alone it leaves nothing to draw, and nothing was allocated for it.
    alone = _only(absurd)
    assert alone.total == 0 and alone.chunks == {}
    with pytest.raises(snap.SnapError, match="no chunk arrived|does not fit"):
        snap.render(alone, partial=True)


def test_a_chunk_that_disagrees_about_the_picture_is_not_mixed_in() -> None:
    picture = _picture(100, 10)
    lines = _encode(picture, 100, 10)
    other = _encode(_picture(192, 2), 192, 2)
    shot = _only("\n".join([lines[0], other[0], *lines[1:]]))
    assert shot.raw() == picture
    assert any("disagrees about the picture" in problem for problem in shot.problems())


def test_several_snapshots_in_one_log_stay_apart_in_order_of_appearance() -> None:
    first = _picture(100, 10, seed=1)
    second = _picture(5, 7, seed=2)
    lines_a = _encode(first, 100, 10, "000000aa")
    lines_b = _encode(second, 5, 7, "000000bb")
    # Interleaved: the robot streams one at a time, but a log can still be cut and joined.
    text = "\n".join([*lines_a[:3], *lines_b, *lines_a[3:]])
    found = snap.collect(text)
    assert [shot.id for shot in found] == ["000000aa", "000000bb"]
    assert found[0].raw() == first and found[1].raw() == second
    assert all(shot.problems() == [] for shot in found)


def test_the_cli_lists_picks_and_writes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    first, second = _picture(100, 10, seed=1), _picture(5, 7, seed=2)
    log = tmp_path / "robot.log"
    lines = [*_encode(first, 100, 10, "000000aa"), *_encode(second, 5, 7, "000000bb")[:-1]]
    log.write_text("\n".join(f"10:00:00.000 {line}" for line in lines) + "\n", encoding="utf-8")

    assert snap.main([str(log), "--list"]) == 0
    listing = capsys.readouterr().out.splitlines()
    assert listing[0] == "000000aa 100x10 6/6 chunks: ok"
    assert listing[1] == "000000bb 5x7 1/1 chunks: no end line: the stream is cut or still running"

    out = tmp_path / "a.png"
    assert snap.main([str(log), "--id", "000000aa", "--out", str(out)]) == 0
    assert "wrote" in capsys.readouterr().out
    assert _decode_png(out.read_bytes())[:2] == (100, 10)

    # The latest snapshot is the unfinished one: refused, with the reason, unless --partial.
    assert snap.main([str(log), "--out", str(tmp_path / "b.png")]) == 1
    assert "no end line" in capsys.readouterr().err
    assert not (tmp_path / "b.png").exists()
    assert snap.main([str(log), "--partial", "--out", str(tmp_path / "b.png")]) == 0
    assert "damaged" in capsys.readouterr().out
    assert _decode_png((tmp_path / "b.png").read_bytes())[:2] == (5, 7)


def test_the_cli_reports_an_empty_log_and_an_unknown_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "robot.log"
    log.write_text("I (1) boot: nothing to see\n", encoding="utf-8")
    assert snap.main([str(log), "--out", str(tmp_path / "x.png")]) == 1
    assert "no SNAP lines" in capsys.readouterr().err
    log.write_text("\n".join(_encode(_picture(5, 7), 5, 7)) + "\n", encoding="utf-8")
    assert snap.main([str(log), "--id", "ffffffff", "--out", str(tmp_path / "x.png")]) == 1
    assert "for id ffffffff" in capsys.readouterr().err
    assert snap.main([str(tmp_path / "missing.log"), "--out", str(tmp_path / "x.png")]) == 1
    assert not (tmp_path / "x.png").exists()


def test_the_png_writer_makes_a_file_a_viewer_accepts() -> None:
    rgb = bytes(range(256)) * 3  # 256 pixels
    width, height, pixels = _decode_png(snap.png_bytes(16, 16, rgb))
    assert (width, height) == (16, 16)
    assert pixels == [tuple(rgb[i : i + 3]) for i in range(0, len(rgb), 3)]
    with pytest.raises(snap.SnapError, match="not a 4x4 RGB picture"):
        snap.png_bytes(4, 4, b"\x00" * 10)


def test_ranges_are_written_the_short_way() -> None:
    assert snap._ranges([1, 2, 3, 7, 9, 10]) == "1-3, 7, 9-10"
    assert snap._ranges([5]) == "5"
    assert snap._ranges([2, 4, 6]) == "2, 4, 6"


class _Rig:
    """A log file that the 'robot' keeps writing to every time the waiting code sleeps."""

    def __init__(self, path: Path, script: list[list[str]]) -> None:
        self.path = path
        self.script = script
        self.now = 0.0
        self.sleeps = 0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.sleeps += 1
        if self.script:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write("".join(f"10:00:00.000 {line}\n" for line in self.script.pop(0)))


def test_waiting_returns_the_snapshot_this_request_caused(tmp_path: Path) -> None:
    log = tmp_path / "robot.log"
    old = _picture(5, 7, seed=7)
    log.write_text("\n".join(_encode(old, 5, 7, "00000001")) + "\n", encoding="utf-8")
    offset = log.stat().st_size
    fresh = _picture(100, 10, seed=8)
    lines = _encode(fresh, 100, 10, "00000002")
    rig = _Rig(log, [["I (5) usb: snap accepted"], lines[:3], lines[3:]])
    shot = snap.wait_for_snapshot(log, offset, 30, sleep=rig.sleep, clock=rig.clock)
    assert shot.id == "00000002" and shot.problems() == [] and shot.raw() == fresh
    assert rig.sleeps == 3


def test_the_tail_of_an_older_stream_is_not_the_answer(tmp_path: Path) -> None:
    log = tmp_path / "robot.log"
    log.write_text("", encoding="utf-8")
    older = _encode(_picture(100, 10, seed=3), 100, 10, "00000001")
    fresh = _encode(_picture(5, 7, seed=4), 5, 7, "00000002")
    # The request landed while an earlier snapshot was still streaming: chunks 4-6 and its end come first.
    rig = _Rig(log, [older[3:], fresh])
    shot = snap.wait_for_snapshot(log, 0, 30, sleep=rig.sleep, clock=rig.clock)
    assert shot.id == "00000002"


def test_a_refusal_is_returned_not_waited_out(tmp_path: Path) -> None:
    log = tmp_path / "robot.log"
    log.write_text("", encoding="utf-8")
    rig = _Rig(log, [["SNAP 00000009 failed reason=display_busy"]])
    shot = snap.wait_for_snapshot(log, 0, 30, sleep=rig.sleep, clock=rig.clock)
    assert shot.failed == "display_busy"


@pytest.mark.parametrize(
    ("robot_says", "match"),
    [
        ("I (9) MemoriaUsb: usb command ignored (not a command)", "not running a bench image"),
        (
            "I (9) MemoriaUsb: usb snap ignored reason=no_display",
            "usb snap ignored reason=no_display",
        ),
    ],
)
def test_a_robot_that_does_not_take_the_request_ends_the_wait_at_once(
    tmp_path: Path, robot_says: str, match: str
) -> None:
    log = tmp_path / "robot.log"
    log.write_text("", encoding="utf-8")
    rig = _Rig(log, [[robot_says]])
    with pytest.raises(snap.SnapError, match=match):
        snap.wait_for_snapshot(log, 0, 30, sleep=rig.sleep, clock=rig.clock)
    assert rig.sleeps == 1


def test_a_stream_that_never_finishes_is_reported_with_what_did_arrive(tmp_path: Path) -> None:
    log = tmp_path / "robot.log"
    log.write_text("", encoding="utf-8")
    lines = _encode(_picture(100, 10), 100, 10, "00000005")
    rig = _Rig(log, [lines[:4]])
    with pytest.raises(
        snap.SnapError, match=r"no finished snapshot in 3 s; seen 00000005: 4/6 chunks, no end line"
    ):
        snap.wait_for_snapshot(log, 0, 3, poll=1.0, sleep=rig.sleep, clock=rig.clock)
    silent = tmp_path / "silent.log"
    rig = _Rig(silent, [])
    with pytest.raises(snap.SnapError, match="no SNAP line arrived at all"):
        snap.wait_for_snapshot(silent, 0, 2, poll=1.0, sleep=rig.sleep, clock=rig.clock)


def test_the_protocol_constants_are_the_ones_the_firmware_header_declares() -> None:
    source = HEADER.read_text(encoding="utf-8")
    assert int(re.search(r"kSnapChunkBytes = (\d+);", source)[1]) == snap.CHUNK_BYTES == CHUNK
    assert int(re.search(r"kSnapPixelBytes = (\d+);", source)[1]) == snap.PIXEL_BYTES


def test_the_script_needs_nothing_but_the_standard_library() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    imported = set(re.findall(r"^(?:import|from) ([A-Za-z_][A-Za-z0-9_]*)", source, re.MULTILINE))
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
