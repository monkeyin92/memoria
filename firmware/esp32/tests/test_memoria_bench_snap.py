"""Host-side checks for the serial screenshot encoder of a bench build (TODOLIST M-2).

`memoria_bench_snap.h` has no ESP-IDF dependency, so the CRC, the base64, the strided gather of an LVGL
buffer and the whole chunk/end/failure line protocol compile on the host and are compared with zlib and
`base64` from the standard library: the same two a PC script uses to put the picture back together. The
display and the board that call it only build inside ESP-IDF; their wiring is checked in
test_memoria_bench_serial.py.
"""

from __future__ import annotations

import base64
import pathlib
import re
import shutil
import subprocess
import tempfile
import zlib

import pytest
from scripts import snap_to_png

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
HEADER = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria" / "memoria_bench_snap.h"

CHUNK_BYTES = 384

HARNESS = r"""
#include "memoria_bench_snap.h"

#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

using namespace memoria::bench;

static std::vector<std::uint8_t> FromHex(const std::string& hex) {
    std::vector<std::uint8_t> out;
    if (hex == "-") return out;
    for (std::size_t i = 0; i + 1 < hex.size(); i += 2) {
        out.push_back(static_cast<std::uint8_t>(std::stoul(hex.substr(i, 2), nullptr, 16)));
    }
    return out;
}

static std::string ToHex(const std::uint8_t* data, std::size_t length) {
    std::string out;
    char two[3];
    for (std::size_t i = 0; i < length; ++i) {
        std::snprintf(two, sizeof(two), "%02x", data[i]);
        out += two;
    }
    return out.empty() ? "-" : out;
}

// The picture the tests expect: byte `column` of row `row`.
static std::uint8_t Model(std::size_t row, std::size_t column) {
    return static_cast<std::uint8_t>((row * 131 + column * 17 + (column >> 3) * 5 + 3) & 0xFF);
}

static std::vector<std::uint8_t> Picture(std::size_t stride, std::size_t row_bytes, std::size_t rows) {
    // 0xEE is the padding: it must never travel. A stride shorter than a row is a caller's mistake the
    // function under test has to refuse; the buffer is still big enough to be filled here.
    const std::size_t widest = stride > row_bytes ? stride : row_bytes;
    std::vector<std::uint8_t> buffer(widest * rows + 16, 0xEE);
    for (std::size_t r = 0; r < rows; ++r) {
        for (std::size_t c = 0; c < row_bytes; ++c) buffer[r * stride + c] = Model(r, c);
    }
    return buffer;
}

int main() {
    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in(line);
        std::string cmd;
        in >> cmd;
        if (cmd == "crc") {
            std::string hex;
            in >> hex;
            const auto data = FromHex(hex);
            std::printf("%08x\n", Crc32(0, data.data(), data.size()));
        } else if (cmd == "crcchain") {
            std::string a, b;
            in >> a >> b;
            const auto first = FromHex(a);
            const auto second = FromHex(b);
            const std::uint32_t head = Crc32(0, first.data(), first.size());
            std::printf("%08x\n", Crc32(head, second.data(), second.size()));
        } else if (cmd == "b64") {
            std::string hex;
            in >> hex;
            const auto data = FromHex(hex);
            std::vector<char> out(Base64Length(data.size()) + 1, 'X');
            const std::size_t written = Base64Encode(data.data(), data.size(), out.data());
            std::printf("%zu %zu %s\n", written, Base64Length(data.size()), out.data());
        } else if (cmd == "gather") {
            std::size_t stride, row_bytes, rows, offset, want;
            in >> stride >> row_bytes >> rows >> offset >> want;
            const auto buffer = Picture(stride, row_bytes, rows);
            std::vector<std::uint8_t> out(want + 8, 0xAB);
            const std::size_t copied =
                GatherPicture(buffer.data(), stride, row_bytes, rows, offset, out.data(), want);
            // Nothing past `want` may be touched.
            bool guard_intact = true;
            for (std::size_t i = want; i < out.size(); ++i) guard_intact = guard_intact && out[i] == 0xAB;
            std::printf("%zu %d %s\n", copied, guard_intact ? 1 : 0, ToHex(out.data(), copied).c_str());
        } else if (cmd == "gathernull") {
            std::uint8_t byte = 0;
            std::uint8_t out[4] = {};
            const std::size_t no_base = GatherPicture(nullptr, 8, 8, 1, 0, out, 4);
            const std::size_t no_out = GatherPicture(&byte, 8, 8, 1, 0, nullptr, 4);
            std::printf("%zu %zu\n", no_base, no_out);
        } else if (cmd == "chunk") {
            unsigned long id, width, height, seq, total, capacity;
            std::string hex;
            in >> id >> width >> height >> seq >> total >> capacity >> hex;
            const auto data = FromHex(hex);
            std::vector<char> out(capacity + 4, 'Z');
            const std::size_t length = SnapChunkLine(out.data(), capacity, static_cast<std::uint32_t>(id),
                                                      static_cast<unsigned>(width), static_cast<unsigned>(height),
                                                      seq, total, data.data(), data.size());
            std::printf("%zu %s\n", length, length > 0 ? out.data() : "-");
        } else if (cmd == "chunkworst") {
            // Largest numbers every field can take, a full chunk: it must fit the published capacity.
            std::vector<std::uint8_t> data(kSnapChunkBytes, 0xFF);
            std::vector<char> out(kSnapLineCapacity, 'Z');
            const std::size_t length =
                SnapChunkLine(out.data(), out.size(), 0xFFFFFFFFu, 0xFFFFFFFFu, 0xFFFFFFFFu,
                              static_cast<std::size_t>(-1), static_cast<std::size_t>(-1), data.data(), data.size());
            std::printf("%zu %zu\n", length, out.size());
        } else if (cmd == "end") {
            unsigned long id, total, bytes, crc, capacity;
            in >> id >> total >> bytes >> crc >> capacity;
            std::vector<char> out(capacity + 4, 'Z');
            const std::size_t length = SnapEndLine(out.data(), capacity, static_cast<std::uint32_t>(id), total, bytes,
                                                   static_cast<std::uint32_t>(crc));
            std::printf("%zu %s\n", length, length > 0 ? out.data() : "-");
        } else if (cmd == "fail") {
            unsigned long id, capacity;
            std::string reason;
            in >> id >> capacity >> reason;
            std::vector<char> out(capacity + 4, 'Z');
            const std::size_t length = SnapFailLine(out.data(), capacity, static_cast<std::uint32_t>(id),
                                                    reason == "-" ? nullptr : reason.c_str());
            std::printf("%zu %s\n", length, length > 0 ? out.data() : "-");
        } else if (cmd == "stream") {
            // stream <width> <height> <stride> <id> [null]
            unsigned long width, height, stride, id;
            std::string flag;
            in >> width >> height >> stride >> id >> flag;
            const auto buffer = Picture(stride, width * 2, height);
            const bool ok = StreamPicture(flag == "null" ? nullptr : buffer.data(), stride,
                                          static_cast<unsigned>(width), static_cast<unsigned>(height),
                                          static_cast<std::uint32_t>(id),
                                          [](const char* text, std::size_t length) {
                                              std::printf("LINE %zu %s\n", length, text);
                                          });
            std::printf("RETURN %d\n", ok ? 1 : 0);
        } else if (cmd == "constants") {
            std::printf("%zu %zu %zu %zu\n", kSnapChunkBytes, kSnapPixelBytes, kSnapLineCapacity,
                        SnapChunkCount(360 * 360 * 2));
        } else {
            return 2;
        }
        std::fflush(stdout);
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def tool():
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "bench_snap_tool"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O1",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fsanitize=address,undefined",
                "-fno-sanitize-recover=undefined",
                "-I",
                str(HEADER.parent),
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        yield binary


def _run(tool: pathlib.Path, *lines: str) -> list[str]:
    completed = subprocess.run(
        [str(tool)], input="\n".join(lines) + "\n", text=True, capture_output=True, check=True
    )
    return completed.stdout.splitlines()


def _hex(data: bytes) -> str:
    return data.hex() or "-"


def _model(row: int, column: int) -> int:
    return (row * 131 + column * 17 + (column >> 3) * 5 + 3) & 0xFF


def _picture(width: int, height: int) -> bytes:
    return bytes(_model(r, c) for r in range(height) for c in range(width * 2))


def _pseudo_random(length: int, seed: int) -> bytes:
    state = seed & 0xFFFFFFFF
    out = bytearray()
    for _ in range(length):
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        out.append(state >> 24)
    return bytes(out)


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in (
        "esp_",
        "lvgl",
        "lv_",
        "freertos",
        "nvs",
        "settings.h",
        "driver/",
        "sdkconfig",
    ):
        assert forbidden not in source
    assert re.findall(r"#include <([^>]+)>", source) == ["cstddef", "cstdint", "cstdio", "cstring"]
    assert re.findall(r'#include "([^"]+)"', source) == []


def test_chunk_size_keeps_base64_padding_to_the_last_chunk(tool: pathlib.Path) -> None:
    chunk_bytes, pixel_bytes, capacity, chunks = _run(tool, "constants")[0].split()
    assert int(chunk_bytes) == CHUNK_BYTES and CHUNK_BYTES % 3 == 0
    assert int(pixel_bytes) == 2
    # 360 x 360 RGB565: 259,200 bytes in 675 chunks.
    assert int(chunks) == 675
    # Room for the longest line, base64 and prefix.
    assert int(capacity) >= 96 + CHUNK_BYTES // 3 * 4 + 1


def test_crc_is_the_one_zlib_computes(tool: pathlib.Path) -> None:
    # The standard check value of CRC-32.
    assert _run(tool, f"crc {_hex(b'123456789')}") == ["cbf43926"]
    assert _run(tool, "crc -") == ["00000000"]
    cases = [_pseudo_random(n, n + 1) for n in (1, 2, 3, 4, 7, 8, 255, 256, 257, 384, 1000, 4096)]
    got = _run(tool, *[f"crc {_hex(case)}" for case in cases])
    assert got == [f"{zlib.crc32(case):08x}" for case in cases]


def test_crc_continues_from_a_previous_result(tool: pathlib.Path) -> None:
    first = _pseudo_random(1000, 5)
    second = _pseudo_random(777, 6)
    got = _run(tool, f"crcchain {_hex(first)} {_hex(second)}", f"crcchain - {_hex(second)}")
    assert got == [f"{zlib.crc32(first + second):08x}", f"{zlib.crc32(second):08x}"]


def test_base64_is_the_standard_encoding_for_every_tail_length(tool: pathlib.Path) -> None:
    cases = [_pseudo_random(n, 40 + n) for n in list(range(0, 41)) + [100, 383, 384, 385, 1000]]
    cases.append(bytes([0xFB, 0xFF, 0xBF]))  # the '+' and '/' end of the alphabet
    cases.append(bytes(range(256)))
    got = _run(tool, *[f"b64 {_hex(case)}" for case in cases])
    for case, line in zip(cases, got, strict=True):
        written, expected_length, text = (line.split(" ") + [""])[:3]
        encoded = base64.b64encode(case).decode("ascii")
        assert text == encoded
        assert int(written) == len(encoded) == int(expected_length)


@pytest.mark.parametrize(
    ("stride", "row_bytes", "rows"),
    [(8, 8, 5), (12, 8, 5), (720, 720, 3), (768, 720, 4), (7, 3, 9), (5, 1, 6), (10, 10, 1)],
)
def test_gather_follows_the_stride_and_never_copies_the_padding(
    tool: pathlib.Path, stride: int, row_bytes: int, rows: int
) -> None:
    expected = bytes(_model(r, c) for r in range(rows) for c in range(row_bytes))
    total = len(expected)
    commands = []
    cases = []
    for offset in range(0, total + 2, max(1, total // 11)):
        for want in (1, 2, row_bytes - 1 or 1, row_bytes, row_bytes + 1, total, total + 5):
            commands.append(f"gather {stride} {row_bytes} {rows} {offset} {want}")
            cases.append((offset, want))
    for line, (offset, want) in zip(_run(tool, *commands), cases, strict=True):
        copied, guard_intact, data = line.split(" ")
        wanted = expected[offset : offset + want]
        assert int(copied) == len(wanted), (offset, want)
        assert guard_intact == "1", (offset, want)
        assert (bytes.fromhex(data) if data != "-" else b"") == wanted, (offset, want)


def test_gather_refuses_what_is_not_a_picture(tool: pathlib.Path) -> None:
    got = _run(
        tool,
        "gather 8 8 4 32 4",  # offset at the end
        "gather 8 8 4 100 4",  # offset past the end
        "gather 8 8 4 0 0",  # nothing wanted
        "gather 4 8 4 0 4",  # stride smaller than a row
        "gather 8 0 4 0 4",  # rows without bytes
        "gather 8 8 0 0 4",  # a picture without rows
        "gathernull",
    )
    assert [line.split(" ")[0] for line in got[:6]] == ["0"] * 6
    assert all(line.split(" ")[1] == "1" for line in got[:6])
    assert got[6] == "0 0"


def test_a_chunk_line_decodes_back_to_its_bytes(tool: pathlib.Path) -> None:
    pattern = re.compile(
        r"^SNAP (?P<id>[0-9a-f]{8}) (?P<w>\d+)x(?P<h>\d+) (?P<seq>\d+)/(?P<total>\d+) "
        r"(?P<crc>[0-9a-f]{8}) (?P<data>[A-Za-z0-9+/]+={0,2})$"
    )
    for length in (1, 2, 3, 5, 96, 383, CHUNK_BYTES):
        payload = _pseudo_random(length, 90 + length)
        (line,) = _run(tool, f"chunk 3735928559 360 360 12 675 700 {_hex(payload)}")
        written, text = line.split(" ", 1)
        match = pattern.match(text)
        assert match, text
        assert int(written) == len(text)
        assert match["id"] == "deadbeef"
        assert (match["w"], match["h"]) == ("360", "360")
        assert (match["seq"], match["total"]) == ("12", "675")
        assert match["crc"] == f"{zlib.crc32(payload):08x}"
        assert base64.b64decode(match["data"], validate=True) == payload


def test_a_chunk_line_refuses_arguments_that_make_no_sense(tool: pathlib.Path) -> None:
    payload = _hex(_pseudo_random(10, 3))
    too_long = _hex(_pseudo_random(CHUNK_BYTES + 1, 4))
    got = _run(
        tool,
        f"chunk 1 360 360 0 5 700 {payload}",  # sequence numbers count from 1
        f"chunk 1 360 360 6 5 700 {payload}",  # past the last chunk
        "chunk 1 360 360 1 5 700 -",  # no bytes
        f"chunk 1 360 360 1 5 700 {too_long}",  # more than a chunk
    )
    assert got == ["0 -"] * 4


def test_a_chunk_line_never_writes_past_the_room_it_is_given(tool: pathlib.Path) -> None:
    payload = _pseudo_random(10, 3)
    (line,) = _run(tool, f"chunk 1 360 360 1 5 700 {_hex(payload)}")
    length = int(line.split(" ", 1)[0])
    exact, too_small, tiny = _run(
        tool,
        f"chunk 1 360 360 1 5 {length + 1} {_hex(payload)}",
        f"chunk 1 360 360 1 5 {length} {_hex(payload)}",
        f"chunk 1 360 360 1 5 1 {_hex(payload)}",
    )
    assert exact == line
    assert too_small == "0 -"
    assert tiny == "0 -"


def test_the_longest_possible_line_fits_the_published_capacity(tool: pathlib.Path) -> None:
    written, capacity = _run(tool, "chunkworst")[0].split()
    assert int(written) > 0
    assert int(written) < int(capacity)


def test_the_end_line_says_how_many_chunks_bytes_and_the_crc_of_all_of_them(
    tool: pathlib.Path,
) -> None:
    (line,) = _run(tool, "end 255 675 259200 3405691582 100")
    written, text = line.split(" ", 1)
    assert text == "SNAP 000000ff end 675 259200 cafebabe fmt=rgb565le"
    assert int(written) == len(text)
    exact, short = _run(
        tool,
        f"end 255 675 259200 3405691582 {len(text) + 1}",
        f"end 255 675 259200 3405691582 {len(text)}",
    )
    assert exact == line
    assert short == "0 -"


def _counted(entry: str) -> str:
    """The text of a `<length> <text>` answer, after checking the length it reports."""
    length, text = entry.split(" ", 1)
    assert int(length) == len(text)
    return text


def test_the_failure_line_names_a_reason(tool: pathlib.Path) -> None:
    named, unnamed, short = _run(
        tool, "fail 4096 64 no_picture", "fail 4096 64 -", "fail 4096 10 no_picture"
    )
    assert _counted(named) == "SNAP 00001000 failed reason=no_picture"
    assert _counted(unnamed) == "SNAP 00001000 failed reason=unknown"
    assert short == "0 -"


@pytest.mark.parametrize(
    ("width", "height", "stride"),
    [
        (1, 1, 2),  # two bytes: one tiny chunk
        (192, 1, 384),  # exactly one full chunk
        (193, 1, 386),  # one byte pair over: two chunks
        (192, 2, 384),  # exactly two full chunks
        (5, 7, 16),  # padded rows, a short last chunk
        (360, 360, 720),  # the real panel, packed
        (360, 360, 768),  # the real panel, rows padded to 768 bytes
    ],
)
def test_a_whole_picture_streams_in_order_and_puts_back_together(
    tool: pathlib.Path, width: int, height: int, stride: int
) -> None:
    output = _run(tool, f"stream {width} {height} {stride} 74565")
    assert output[-1] == "RETURN 1"
    lines = []
    for entry in output[:-1]:
        _, length, text = entry.split(" ", 2)
        assert int(length) == len(text)
        lines.append(text)

    picture = _picture(width, height)
    total = -(-len(picture) // CHUNK_BYTES)
    assert len(lines) == total + 1

    chunk_pattern = re.compile(
        rf"^SNAP 00012345 {width}x{height} (\d+)/{total} ([0-9a-f]{{8}}) ([A-Za-z0-9+/]+={{0,2}})$"
    )
    rebuilt = bytearray()
    for index, text in enumerate(lines[:-1], start=1):
        match = chunk_pattern.match(text)
        assert match, text
        assert int(match[1]) == index
        part = base64.b64decode(match[3], validate=True)
        assert len(part) == CHUNK_BYTES or index == total
        assert match[2] == f"{zlib.crc32(part):08x}"
        rebuilt += part
    assert bytes(rebuilt) == picture
    assert (
        lines[-1]
        == f"SNAP 00012345 end {total} {len(picture)} {zlib.crc32(picture):08x} fmt=rgb565le"
    )


def test_a_picture_that_cannot_be_streamed_says_so_in_one_line(tool: pathlib.Path) -> None:
    def failure(output: list[str]) -> tuple[str, str]:
        assert len(output) == 2
        _, length, text = output[0].split(" ", 2)
        assert int(length) == len(text)
        return text, output[1]

    assert failure(_run(tool, "stream 4 4 8 4660 null")) == (
        "SNAP 00001234 failed reason=no_picture",
        "RETURN 0",
    )
    for command in ("stream 0 4 8 4660", "stream 4 0 8 4660", "stream 4 4 7 4660"):
        assert failure(_run(tool, command)) == ("SNAP 00001234 failed reason=bad_size", "RETURN 0")


@pytest.mark.parametrize(
    ("width", "height", "stride"),
    [(1, 1, 2), (193, 1, 386), (5, 7, 16), (360, 360, 720), (360, 360, 768)],
)
def test_the_pc_script_rebuilds_what_the_robot_encoder_streams(
    tool: pathlib.Path, width: int, height: int, stride: int
) -> None:
    output = _run(tool, f"stream {width} {height} {stride} 74565")
    assert output[-1] == "RETURN 1"
    # The resident logger puts a time stamp in front of every line it records.
    text = "\n".join(f"12:34:56.789 {entry.split(' ', 2)[2]}" for entry in output[:-1])
    (shot,) = snap_to_png.collect(text)
    assert shot.problems() == []
    assert (shot.width, shot.height) == (width, height)
    assert shot.raw() == _picture(width, height)
    assert snap_to_png.render(shot).startswith(b"\x89PNG")


def test_the_pc_script_reads_the_robots_failure_lines(tool: pathlib.Path) -> None:
    for command, reason in (
        ("stream 4 4 8 4660 null", "no_picture"),
        ("stream 0 4 8 4660", "bad_size"),
    ):
        output = _run(tool, command)
        (shot,) = snap_to_png.collect(output[0].split(" ", 2)[2])
        assert shot.failed == reason and shot.id == "00001234"
