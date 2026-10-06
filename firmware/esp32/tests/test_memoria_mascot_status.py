"""Host-side checks for the bench build's `status` line (TODOLIST M-2).

`memoria_mascot_status.h` has no ESP-IDF dependency, so the names it prints and the exact line it makes compile
on the host against the real scene enums. Where the line is produced (the animation task, on a `status`
request) only builds inside ESP-IDF and is checked as source text in test_memoria_bench_serial.py.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest
from scripts import bench_status

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
HEADER = BOARD_DIR / "memoria_mascot_status.h"
SCENE_HEADER = BOARD_DIR / "memoria_mascot_scene.h"
PACK_HEADER = BOARD_DIR / "memoria_mascot_pack.h"

HARNESS = r"""
#include "memoria_mascot_status.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <string>

int main() {
    char line[512];
    char text[memoria::kBenchStatusCapacity];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        line[strcspn(line, "\r\n")] = '\0';
        std::istringstream in(line);
        std::string cmd;
        in >> cmd;
        if (cmd == "names") {
            // names <phase|mood|frame> <count>: the name of every value up to count (inclusive: one past the end).
            std::string kind;
            unsigned long count = 0;
            in >> kind >> count;
            for (unsigned long i = 0; i <= count; ++i) {
                const char* name = "?";
                if (kind == "phase") name = memoria::ScenePhaseName(static_cast<memoria::ScenePhase>(i));
                if (kind == "mood") name = memoria::SceneMoodName(static_cast<memoria::SceneMood>(i));
                if (kind == "frame") name = memoria::MascotFrameName(static_cast<memoria::MascotFrame>(i));
                printf("%s\n", name);
            }
        } else if (cmd == "line" || cmd == "tiny" || cmd == "nullbuf") {
            // line <capacity> <up_ms> <phase> <mood> <frame> <off> <sleep> <caption> <frames> <drawn> <render_us>
            //      <render_max_us> <busy_us> <px> <composed_px> <extra_ms> <heap> <psram> <stack>
            unsigned long capacity = 0;
            memoria::BenchStatus s;
            unsigned long long up = 0, phase = 0, mood = 0, frame = 0, off = 0, sleeping = 0, caption = 0;
            unsigned long long frames = 0, drawn = 0, render = 0, render_max = 0, busy = 0, px = 0, composed = 0;
            unsigned long long extra = 0, heap = 0, psram = 0, stack = 0;
            in >> capacity >> up >> phase >> mood >> frame >> off >> sleeping >> caption >> frames >> drawn >>
                render >> render_max >> busy >> px >> composed >> extra >> heap >> psram >> stack;
            s.up_ms = static_cast<uint32_t>(up);
            s.phase = static_cast<memoria::ScenePhase>(phase);
            s.mood = static_cast<memoria::SceneMood>(mood);
            s.frame = static_cast<memoria::MascotFrame>(frame);
            s.screen_off = off != 0;
            s.sleeping = sleeping != 0;
            s.captioned = caption != 0;
            s.frames = static_cast<uint32_t>(frames);
            s.drawn = static_cast<uint32_t>(drawn);
            s.render_us = render;
            s.render_max_us = static_cast<uint32_t>(render_max);
            s.busy_us = busy;
            s.pixels = px;
            s.composed_px = composed;
            s.extra_ms = static_cast<uint32_t>(extra);
            s.heap_free = static_cast<uint32_t>(heap);
            s.psram_free = static_cast<uint32_t>(psram);
            s.anim_stack_free = static_cast<uint32_t>(stack);
            if (capacity > sizeof(text)) capacity = sizeof(text);
            const size_t length = memoria::BenchStatusLine(cmd == "nullbuf" ? nullptr : text, capacity, s);
            printf("%zu|%s\n", length, length > 0 ? text : "");
        } else if (cmd == "capacity") {
            printf("%zu\n", memoria::kBenchStatusCapacity);
        } else {
            return 2;
        }
        fflush(stdout);
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
        binary = pathlib.Path(tmp) / "mascot_status_tool"
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
                str(BOARD_DIR),
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


def _enumerators(path: pathlib.Path, enum_name: str) -> list[str]:
    source = re.sub(r"//[^\n]*", "", path.read_text(encoding="utf-8"))
    body = re.search(rf"enum class {enum_name}\s*:\s*uint8_t\s*\{{(.*?)\}};", source, re.DOTALL)
    assert body is not None, enum_name
    return re.findall(r"\bk([A-Za-z0-9]+)\b", body.group(1))


def _snake(camel: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


# A line for an idle companion halfway through a day on the bench.
SAMPLE = "512 123456 4 0 9 0 0 0 5000 4000 36000000 21000 52000000 800000000 650000000 10 150000 4100000 2300"
SAMPLE_LINE = (
    "status up_ms=123456 phase=idle mood=neutral frame=default_blink screen_off=0 sleeping=0 captioned=0 "
    "frames=5000 drawn=4000 render_us=36000000 render_max_us=21000 busy_us=52000000 px=800000000 "
    "composed_px=650000000 extra_ms=10 heap_free=150000 psram_free=4100000 anim_stack_free=2300"
)


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "lvgl", "lv_", "freertos", "nvs", "settings.h", "driver/"):
        assert forbidden not in source
    assert re.findall(r'#include "([^"]+)"', source) == [
        "memoria_mascot_pack.h",
        "memoria_mascot_scene.h",
    ]


@pytest.mark.parametrize(
    ("kind", "path", "enum_name"),
    [
        ("phase", SCENE_HEADER, "ScenePhase"),
        ("mood", SCENE_HEADER, "SceneMood"),
        ("frame", PACK_HEADER, "MascotFrame"),
    ],
)
def test_every_enumerator_prints_its_own_snake_case_name(
    tool: pathlib.Path, kind: str, path: pathlib.Path, enum_name: str
) -> None:
    names = [_snake(item) for item in _enumerators(path, enum_name) if item != "Count"]
    printed = _run(tool, f"names {kind} {len(names)}")
    assert printed[: len(names)] == names
    # Past the last value there is no name to find: say so instead of reading beyond the table.
    assert printed[len(names)] == ("unknown" if kind != "frame" else "none")


def test_the_frame_that_does_not_exist_yet_reads_none(tool: pathlib.Path) -> None:
    count = len([item for item in _enumerators(PACK_HEADER, "MascotFrame") if item != "Count"])
    assert _run(tool, f"names frame {count}")[-1] == "none"
    assert _run(tool, f"names frame {count + 40}")[-1] == "none"


def test_the_line_names_every_field_in_a_fixed_order(tool: pathlib.Path) -> None:
    # phase 4 = idle, mood 0 = neutral, frame 9 = default_blink
    assert _run(tool, f"line {SAMPLE}") == [f"{len(SAMPLE_LINE)}|{SAMPLE_LINE}"]


def test_the_counters_keep_their_full_64_bit_width(tool: pathlib.Path) -> None:
    fields = SAMPLE.split()
    fields[10] = str(2**40 + 5)  # render_us
    fields[12] = str(2**63 + 7)  # busy_us
    fields[13] = str(2**41)  # px
    fields[14] = str(2**42 + 1)  # composed_px
    line = _run(tool, "line " + " ".join(fields))[0].split("|", 1)[1]
    assert f"render_us={2**40 + 5} " in line
    assert f"busy_us={2**63 + 7} " in line
    assert f"px={2**41} " in line
    assert f"composed_px={2**42 + 1} " in line


def test_the_32_bit_counters_print_unsigned(tool: pathlib.Path) -> None:
    fields = SAMPLE.split()
    fields[1] = str(2**32 - 1)  # up_ms
    fields[8] = str(2**32 - 2)  # frames
    line = _run(tool, "line " + " ".join(fields))[0].split("|", 1)[1]
    assert f"up_ms={2**32 - 1} " in line
    assert f"frames={2**32 - 2} " in line


def test_the_flags_print_as_zero_or_one(tool: pathlib.Path) -> None:
    fields = SAMPLE.split()
    fields[5:8] = ["1", "1", "1"]
    line = _run(tool, "line " + " ".join(fields))[0].split("|", 1)[1]
    assert "screen_off=1 sleeping=1 captioned=1 " in line
    fields[5:8] = ["0", "1", "0"]
    line = _run(tool, "line " + " ".join(fields))[0].split("|", 1)[1]
    assert "screen_off=0 sleeping=1 captioned=0 " in line


def _worst_fields() -> list[str]:
    """The harness arguments of the longest line there can be: every counter at its maximum, the longest names."""
    worst = str(2**64 - 1)
    longest_frame = max(
        (item for item in _enumerators(PACK_HEADER, "MascotFrame") if item != "Count"),
        key=lambda item: len(_snake(item)),
    )
    frame_index = [item for item in _enumerators(PACK_HEADER, "MascotFrame")].index(longest_frame)
    return [
        "512",  # capacity
        str(2**32 - 1),  # up_ms
        "2",  # wifi_config, the longest phase name
        "3",  # surprised, the longest mood name
        str(frame_index),
        "1",
        "1",
        "1",
        str(2**32 - 1),  # frames
        str(2**32 - 1),  # drawn
        worst,
        str(2**32 - 1),
        worst,
        worst,
        worst,
        str(2**32 - 1),
        str(2**32 - 1),
        str(2**32 - 1),
        str(2**32 - 1),
    ]


def test_the_worst_case_line_fits_with_room_to_spare(tool: pathlib.Path) -> None:
    capacity = int(_run(tool, "capacity")[0])
    printed = _run(tool, "line " + " ".join(_worst_fields()))[0]
    length = int(printed.split("|", 1)[0])
    assert 0 < length == len(printed.split("|", 1)[1])
    # Under the capacity with at least a hundred characters to spare: a field or two may still be added.
    assert length <= capacity - 100


def test_the_pc_script_reads_the_line_the_robot_formats(tool: pathlib.Path) -> None:
    # The resident logger puts a time stamp in front, the ESP log its own prefix.
    prefix = "12:00:00.000 I (123456) MemoriaBench: "
    sample = bench_status.parse_status(prefix + _run(tool, f"line {SAMPLE}")[0].split("|", 1)[1])
    assert sample is not None
    assert (sample["phase"], sample["mood"], sample["frame"]) == (
        "idle",
        "neutral",
        "default_blink",
    )
    assert (sample["up_ms"], sample["frames"], sample["drawn"]) == (123456, 5000, 4000)
    assert (sample["render_us"], sample["busy_us"], sample["px"]) == (36000000, 52000000, 800000000)
    assert sample["anim_stack_free"] == 2300
    # Nothing in the longest line (64-bit maxima, longest names) confuses the parser either.
    worst = bench_status.parse_status(
        prefix + _run(tool, "line " + " ".join(_worst_fields()))[0].split("|", 1)[1]
    )
    assert worst is not None
    assert (worst["phase"], worst["mood"]) == ("wifi_config", "surprised")
    assert worst["busy_us"] == 2**64 - 1 and worst["frames"] == 2**32 - 1


def test_the_pc_script_knows_the_name_of_every_frame_the_firmware_can_report(
    tool: pathlib.Path,
) -> None:
    names = [_snake(item) for item in _enumerators(PACK_HEADER, "MascotFrame") if item != "Count"]
    for name in [*names, "none"]:
        line = (
            "10:00:00.000 I (1) MemoriaBench: " + _run(tool, f"line {SAMPLE}")[0].split("|", 1)[1]
        )
        assert (
            bench_status.parse_status(line.replace("frame=default_blink", f"frame={name}"))
            is not None
        )


def test_a_line_that_does_not_fit_is_refused_not_cut(tool: pathlib.Path) -> None:
    exact = len(SAMPLE_LINE)
    fields = SAMPLE.split()
    # capacity exactly length + 1 (the NUL) fits; one less does not.
    fields[0] = str(exact + 1)
    assert _run(tool, "line " + " ".join(fields)) == [f"{exact}|{SAMPLE_LINE}"]
    fields[0] = str(exact)
    assert _run(tool, "line " + " ".join(fields)) == ["0|"]
    fields[0] = "0"
    assert _run(tool, "line " + " ".join(fields)) == ["0|"]
    assert _run(tool, "nullbuf " + SAMPLE) == ["0|"]
