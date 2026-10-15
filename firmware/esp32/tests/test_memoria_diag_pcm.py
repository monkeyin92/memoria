"""The bench PCM journal (memoria_diag_pcm.h, TODOLIST N-10) as the robot's USB task drives it.

The header is ESP-free, so the real recorder compiles on the host through tests/diag_pcm_tool.cc. The journal's
bytes and the PC checker are covered by scripts/tests/test_diag_pcm_extract.py; this file pins the switch and
the capture's life cycle.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
HEADER = MEMORIA_DIR / "memoria_diag_pcm.h"
TOOL = pathlib.Path(__file__).parent / "diag_pcm_tool.cc"


def _compile(source: pathlib.Path, output: pathlib.Path, bench: int) -> subprocess.CompletedProcess:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    return subprocess.run(
        [compiler, "-std=c++17", "-O0", "-Wall", "-Wextra", "-Werror", "-pthread",
         f"-DCONFIG_MEMORIA_BENCH_SERIAL={bench}", "-I", str(MEMORIA_DIR), str(source), "-o", str(output)],
        capture_output=True, text=True, check=False,
    )


@pytest.fixture(scope="module")
def tool(tmp_path_factory) -> pathlib.Path:
    binary = tmp_path_factory.mktemp("diag") / "tool"
    built = _compile(TOOL, binary, 1)
    assert built.returncode == 0, built.stderr
    return binary


def _run(tool: pathlib.Path, *lines: str) -> list[str]:
    done = subprocess.run([str(tool)], input="\n".join(lines) + "\n", text=True, capture_output=True, check=True)
    return done.stdout.splitlines()


def test_raw_tdm_is_copied_inside_the_codec_read_on_bench_only() -> None:
    patch = (FIRMWARE_ROOT / "overlay" / "patches" / "0041-memoria-bench-raw-tdm.patch").read_text(
        encoding="utf-8"
    )
    assert patch.count("#if CONFIG_MEMORIA_BENCH_SERIAL") >= 3
    read = patch[patch.index("int BoxAudioCodec::Read") :]
    assert read.index("memoria::diag::RecordRawTdm") < read.index(
        "esp_codec_dev_read(input_dev_, (void*)dest"
    )
    product = (FIRMWARE_ROOT / "overlay/files/main/boards/memoria/esp-vocat/config.json").read_text(
        encoding="utf-8"
    )
    assert "CONFIG_MEMORIA_BENCH_SERIAL" not in product


def test_the_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    code = re.sub(r"//[^\n]*", "", source)
    for forbidden in ("esp_", "freertos", "heap_caps", "driver/"):
        assert forbidden not in code
    assert re.findall(r'#include "([^"]+)"', source) == ["sdkconfig.h", "memoria_bench_snap.h"]


def test_a_product_image_cannot_name_the_journal(tmp_path: pathlib.Path) -> None:
    source = tmp_path / "use.cc"
    source.write_text('#include "memoria_diag_pcm.h"\nint main() { return static_cast<int>(memoria::diag::Lane::kFeed); }\n')
    product = _compile(source, tmp_path / "product", 0)
    assert product.returncode != 0 and "diag" in product.stderr
    assert _compile(source, tmp_path / "bench", 1).returncode == 0


def test_the_window_seals_itself_three_seconds_after_arm(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "state",
        "arm 00000010 5000000",
        "state",
        "tick 7999999",
        "tick 8000000",
        "state",
        "tick 9000000",
    ) == ["idle", "ok", "rec", "-", "sealed", "sealed", "-"]


def test_a_capture_cannot_be_rearmed_or_dumped_while_it_records(tool: pathlib.Path) -> None:
    assert _run(tool, "dump", "arm 00000010 0", "arm 00000011 1", "dump") == [
        "PCMD 00000000 failed reason=empty",
        "ok",
        "recording",
        "PCMD 00000010 failed reason=recording",
    ]


def test_a_rearm_after_the_seal_starts_a_fresh_journal(tool: pathlib.Path) -> None:
    lines = _run(
        tool,
        "arm 00000020 0",
        "feed 160 2 0 10",
        "feed 160 2 0 20",
        "tick 3000000",
        "arm 00000021 4000000",
        "feed 160 2 0 4000010",
        "tick 7000000",
        "dump",
    )
    assert lines[:4] == ["ok", "sealed", "ok", "sealed"]
    begin, *_, end = [line for line in lines if line.startswith("PCMD ")]
    assert begin == "PCMD 00000021 begin fmt=memoria-diagpcm-v1 bytes=776 chunks=3"
    assert end.endswith(
        "mic=160 blocks=0,1,0,0 drops=0,0,0,0 cut=0,0,0,0 raw_hz=24000 raw_bits=16 raw_slots=4"
    )


def test_the_armed_line_names_the_raw_tdm_format(tool: pathlib.Path) -> None:
    assert _run(tool, "armline") == [
        "PCMD 0000abcd armed window_ms=3000 "
        "lanes=play_pre_i2s@24000x1,pre_afe_mic+pre_afe_ref@16000x2,post_afe@16000x1,"
        "es7210_slot0+es7210_slot1+es7210_slot2+es7210_slot3@24000x4 psram_free=7000000 "
        "raw_hz=24000 raw_bits=16 raw_slots=4"
    ]


def test_a_reference_channel_that_is_missing_is_a_drop_not_a_relabel(tool: pathlib.Path) -> None:
    lines = _run(tool, "arm 00000030 0", "feed 160 1 0 10", "tick 3000000", "dump")
    assert lines[-1].endswith(
        "mic=160 blocks=0,0,0,0 drops=0,1,0,0 cut=0,0,0,0 raw_hz=24000 raw_bits=16 raw_slots=4"
    )
