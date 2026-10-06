"""The sampler microbenchmark (memoria_mascot_sampler_bench.h) on the host: a fake clock, guard words around the row."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"

HARNESS = r"""
#include "memoria_mascot_sampler_bench.h"

#include <cstdio>
#include <cstdlib>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

static uint32_t g_now = 0;
static uint32_t FakeClock() { g_now += 7; return g_now; }

int main() {
    std::string line;
    while (std::getline(std::cin, line)) {
        std::istringstream in(line);
        std::string cmd;
        in >> cmd;
        if (cmd == "pass") {
            // pass <variant index> <reps>: one timed pass; prints us, px, and whether the guard words survived.
            size_t index = 0;
            int reps = 0;
            in >> index >> reps;
            if (index >= memoria::kSamplerBenchCount) return 2;
            const auto& v = memoria::kSamplerBenchVariants[index];
            std::vector<uint16_t> rgb(static_cast<size_t>(v.w) * v.h);
            std::vector<uint8_t> alpha(rgb.size());
            std::vector<uint16_t> span(static_cast<size_t>(v.h) * 2);
            memoria::MascotSprite s;
            s.w = v.w;
            s.h = v.h;
            s.rgb = rgb.data();
            s.alpha = alpha.data();
            s.span = span.data();
            memoria::FillBenchSprite(&s, v.soft);
            constexpr int kGuard = 64;
            std::vector<uint16_t> buffer(memoria::kBenchCanvas + 2 * kGuard, 0xBEEF);
            uint16_t* row = buffer.data() + kGuard;
            g_now = 0;
            uint64_t px = 0;
            const uint32_t us = memoria::SamplerBenchPass(s, row, reps, FakeClock, &px);
            int broken = 0;
            for (int i = 0; i < kGuard; ++i) {
                broken += buffer[i] != 0xBEEF;
                broken += buffer[kGuard + memoria::kBenchCanvas + i] != 0xBEEF;
            }
            int changed = 0;
            for (int i = 0; i < memoria::kBenchCanvas; ++i) changed += row[i] != 0x1234;
            printf("%u %llu %d %d\n", us, static_cast<unsigned long long>(px), broken, changed);
        } else if (cmd == "line") {
            // line <capacity> then six "<us> <px>" pairs
            size_t capacity = 0;
            in >> capacity;
            memoria::SamplerBenchRow rows[memoria::kSamplerBenchCount];
            for (auto& r : rows) {
                unsigned long long us = 0, px = 0;
                in >> us >> px;
                r.us = us;
                r.px = px;
            }
            char text[memoria::kSamplerBenchLineCapacity + 64];
            if (capacity > sizeof(text)) capacity = sizeof(text);
            const size_t n = memoria::SamplerBenchLine(text, capacity, rows);
            printf("%zu|%s\n", n, n > 0 ? text : "");
        } else if (cmd == "count") {
            printf("%zu\n", memoria::kSamplerBenchCount);
        } else {
            return 2;
        }
        fflush(stdout);
    }
    return 0;
}
"""


def _compile(sanitize: bool) -> pathlib.Path:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="memoria-sbench-"))
    source = tmp / "harness.cc"
    source.write_text(HARNESS, encoding="utf-8")
    binary = tmp / ("sbench_tool_san" if sanitize else "sbench_tool")
    flags = (
        [
            "-fsanitize=address,undefined",
            "-fno-sanitize-recover=undefined",
            "-fno-omit-frame-pointer",
            "-g",
        ]
        if sanitize
        else []
    )
    probe = subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            *flags,
            f"-I{BOARD_DIR}",
            str(source),
            str(BOARD_DIR / "memoria_mascot_raster.cc"),
            "-o",
            str(binary),
        ],
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        if sanitize:
            pytest.skip(f"host compiler cannot build with sanitizers: {probe.stderr[-200:]}")
        raise AssertionError(probe.stderr)
    return binary


@pytest.fixture(scope="module", params=["plain", "sanitized"])
def tool(request):
    binary = _compile(request.param == "sanitized")
    try:
        yield binary
    finally:
        shutil.rmtree(binary.parent, ignore_errors=True)


def _run(tool: pathlib.Path, *commands: str) -> list[str]:
    result = subprocess.run(
        [str(tool)], input="\n".join(commands) + "\n", capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def test_six_variants_and_the_names_are_unique(tool) -> None:
    assert _run(tool, "count") == ["6"]
    names = [
        "sram_solid",
        "sram_soft",
        "psram_warm_solid",
        "psram_cold_solid",
        "psram_cold_soft",
        "psram_cold_solid_rowpsram",
    ]
    out = _run(tool, "line 768 " + " ".join("1000 100" for _ in names))[0]
    assert out.startswith("")
    for name in names:
        assert f"{name}_ns=10000 {name}_px=100" in out


@pytest.mark.parametrize("index", range(6))
def test_a_pass_times_two_clock_reads_and_never_leaves_the_row(tool, index: int) -> None:
    # the fake clock moves 7 us per read, so a pass that reads it twice reports exactly 7
    (line,) = _run(tool, f"pass {index} 3")
    us, px, broken, changed = (int(v) for v in line.split())
    assert us == 7
    assert broken == 0, "the sampler wrote outside the 360 pixel row"
    assert px > 0 and changed > 0
    assert changed <= 360


def test_the_pixel_count_scales_with_the_repetitions(tool) -> None:
    (one,) = _run(tool, "pass 0 1")
    (four,) = _run(tool, "pass 0 4")
    assert int(four.split()[1]) == 4 * int(one.split()[1])


def test_a_bigger_sprite_visits_more_pixels(tool) -> None:
    (small,) = _run(tool, "pass 0 1")
    (big,) = _run(tool, "pass 3 1")
    assert int(big.split()[1]) > 4 * int(small.split()[1])


def test_the_line_is_whole_pairs_or_nothing(tool) -> None:
    rows = " ".join("5000 500" for _ in range(6))
    full = _run(tool, "line 768 " + rows)[0]
    length = int(full.split("|")[0])
    assert 0 < length < 768
    # one byte short of the full line: refused outright, never a half-written pair
    assert _run(tool, f"line {length} " + rows) == ["0|"]
    assert _run(tool, f"line {length + 1} " + rows)[0].startswith(f"{length}|")
    assert _run(tool, "line 0 " + rows) == ["0|"]


def test_the_worst_case_line_fits_its_capacity(tool) -> None:
    worst = " ".join("18446744073709551 18446744073709551" for _ in range(6))
    out = _run(tool, "line 768 " + worst)[0]
    assert int(out.split("|")[0]) > 0
    assert int(out.split("|")[0]) < 768
