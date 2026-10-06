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
    char line[4096];  // a worst-case `tasks` request is about 1.3 KB
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
        } else if (cmd == "profile") {
            // profile <capacity> then the twelve sums in the order of RenderProfile.
            unsigned long capacity = 0;
            memoria::RenderProfile p;
            in >> capacity >> p.renders >> p.sampled >> p.total_us >> p.actor_us >> p.rect_us >> p.touch_us >>
                p.rows >> p.copy_in_us >> p.shadow_us >> p.sprite_us >> p.ring_us >> p.copy_out_us;
            if (capacity > sizeof(text)) capacity = sizeof(text);
            const size_t length = memoria::BenchProfileLine(text, capacity, p);
            printf("%zu|%s\n", length, length > 0 ? text : "");
        } else if (cmd == "tasks") {
            // tasks <capacity> <count> <up_us> <rows> then <name> <core> <priority> <runtime_us> per row; the rows
            // go through the same sort as on the robot before they are printed.
            unsigned long capacity = 0, count = 0, rows_n = 0;
            unsigned long long up = 0;
            in >> capacity >> count >> up >> rows_n;
            memoria::BenchTaskRow rows[64];
            if (rows_n > 64) rows_n = 64;
            for (unsigned long i = 0; i < rows_n; ++i) {
                std::string name;
                unsigned long long runtime = 0;
                unsigned long priority = 0;
                int core = 0;
                in >> name >> core >> priority >> runtime;
                std::strncpy(rows[i].name, name.c_str(), sizeof(rows[i].name) - 1);
                rows[i].core = core;
                rows[i].priority = static_cast<uint32_t>(priority);
                rows[i].runtime_us = runtime;
            }
            memoria::SortBenchTasksByRuntime(rows, rows_n);
            char tasks_text[memoria::kBenchTasksCapacity];
            if (capacity > sizeof(tasks_text)) capacity = sizeof(tasks_text);
            const size_t length = memoria::BenchTasksLine(tasks_text, capacity, rows, rows_n, count, up);
            printf("%zu|%s\n", length, length > 0 ? tasks_text : "");
        } else if (cmd == "tasks_capacity") {
            printf("%zu\n", memoria::kBenchTasksCapacity);
        } else if (cmd == "profile_capacity") {
            printf("%zu\n", memoria::kBenchProfileCapacity);
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


PROFILE_SAMPLE = (
    "448 1000 125 9000000 400000 8000000 250000 51000 1000000 900000 5000000 300000 700000"
)
PROFILE_LINE = (
    "profile renders=1000 sampled=125 total_us=9000000 actor_us=400000 rect_us=8000000 touch_us=250000 "
    "rows=51000 copy_in_us=1000000 shadow_us=900000 sprite_us=5000000 ring_us=300000 copy_out_us=700000"
)


def test_the_profile_line_names_every_sum_in_a_fixed_order(tool: pathlib.Path) -> None:
    assert _run(tool, f"profile {PROFILE_SAMPLE}") == [f"{len(PROFILE_LINE)}|{PROFILE_LINE}"]


def test_the_worst_case_profile_line_fits(tool: pathlib.Path) -> None:
    capacity = int(_run(tool, "profile_capacity")[0])
    worst = str(2**64 - 1)
    printed = _run(tool, "profile 448 " + " ".join([worst] * 12))[0]
    length = int(printed.split("|", 1)[0])
    assert 0 < length < capacity - 60  # room for a field or two more


def test_a_profile_line_that_does_not_fit_is_refused_not_cut(tool: pathlib.Path) -> None:
    fields = PROFILE_SAMPLE.split()
    fields[0] = str(len(PROFILE_LINE) + 1)
    assert _run(tool, "profile " + " ".join(fields)) == [f"{len(PROFILE_LINE)}|{PROFILE_LINE}"]
    fields[0] = str(len(PROFILE_LINE))
    assert _run(tool, "profile " + " ".join(fields)) == ["0|"]


def test_the_pc_script_reads_the_profile_line_the_robot_formats(tool: pathlib.Path) -> None:
    prefix = "12:00:00.000 I (123456) MemoriaBench: "
    sample = bench_status.parse_profile(
        prefix + _run(tool, f"profile {PROFILE_SAMPLE}")[0].split("|", 1)[1]
    )
    assert sample is not None
    assert (sample["sampled"], sample["rows"], sample["sprite_us"]) == (125, 51000, 5000000)
    assert (
        bench_status.parse_profile("MemoriaBench: profile renders=1 sampled=1") is None
    )  # cut: not guessed at
    assert bench_status.parse_profile(prefix + PROFILE_LINE.replace("rows=", "lines=")) is None


def test_the_profile_shares_say_where_a_sampled_frame_goes() -> None:
    first = bench_status.parse_profile("12:00:00.000 I (1) MemoriaBench: " + PROFILE_LINE)
    assert first is not None
    second = dict(first)
    for key in bench_status.PROFILE_TOTALS:
        second[key] = int(first[key]) * 2
    shares = bench_status.profile_rates(first, second)
    # Doubling every sum: the delta is the first line itself, 125 sampled renders, 51000 rows.
    assert shares["sampled"] == 125
    assert shares["total_us_per_render"] == 9000000 / 125
    assert shares["sprite_us_per_row"] == 5000000 / 51000
    assert shares["rows_per_render"] == 51000 / 125
    assert abs(sum(shares["share_of_rect"].values()) - 1.0) < 1e-9
    assert shares["share_of_rect"]["sprite"] == 5000000 / (
        1000000 + 900000 + 5000000 + 300000 + 700000
    )


def _tasks_args(
    capacity: int, count: int, up_us: int, rows: list[tuple[str, int, int, int]]
) -> str:
    body = " ".join(f"{name} {core} {priority} {runtime}" for name, core, priority, runtime in rows)
    return f"tasks {capacity} {count} {up_us} {len(rows)} {body}".strip()


def test_the_tasks_line_lists_the_busiest_tasks_first(tool: pathlib.Path) -> None:
    rows = [("IDLE1", 1, 0, 500), ("mascot_anim", 1, 2, 300), ("opus_codec", 1, 5, 900)]
    want = "tasks count=3 up_us=1000000 opus_codec:1:5:900 IDLE1:1:0:500 mascot_anim:1:2:300"
    assert _run(tool, _tasks_args(1280, 3, 1000000, rows)) == [f"{len(want)}|{want}"]


def test_a_task_name_cannot_break_the_tasks_line_apart(tool: pathlib.Path) -> None:
    # The PC splits on spaces and colons; FreeRTOS names may hold either.
    want = "tasks count=1 up_us=7 a_b_c:-1:3:9"
    assert _run(tool, _tasks_args(1280, 1, 7, [("a:b:c", -1, 3, 9)])) == [f"{len(want)}|{want}"]


def test_the_tasks_line_keeps_the_busiest_rows_when_there_are_too_many(tool: pathlib.Path) -> None:
    rows = [(f"t{i:02d}", i % 2, 1, 1000 + i) for i in range(30)]
    printed = _run(tool, _tasks_args(1280, 30, 5, rows))[0].split("|", 1)[1]
    names = [word.split(":")[0] for word in printed.split()[3:]]
    assert len(names) == 24
    assert names[0] == "t29" and names[-1] == "t06"  # the six quietest are the ones left out


def test_the_worst_case_tasks_line_is_whole_rows_and_fits(tool: pathlib.Path) -> None:
    capacity = int(_run(tool, "tasks_capacity")[0])
    big = 2**64 - 1
    rows = [("x" * 15 + str(i % 10), -1, 2**32 - 1, big) for i in range(24)]
    printed = _run(tool, _tasks_args(capacity, big, big, rows))[0]
    length, line = printed.split("|", 1)
    assert 0 < int(length) < capacity
    assert int(length) == len(line)
    assert all(len(word.split(":")) == 4 for word in line.split()[3:])  # no row cut in half


def test_a_tasks_line_that_does_not_fit_is_refused_not_cut(tool: pathlib.Path) -> None:
    rows = [("a", 0, 1, 2)]
    want = "tasks count=1 up_us=1 a:0:1:2"
    assert _run(tool, _tasks_args(len(want) + 1, 1, 1, rows)) == [f"{len(want)}|{want}"]
    assert _run(tool, _tasks_args(10, 1, 1, rows)) == ["0|"]  # not even the head fits
    # Room for the head but not for the row: the head alone is a valid line with no rows.
    head = "tasks count=1 up_us=1"
    assert _run(tool, _tasks_args(len(head) + 1, 1, 1, rows)) == [f"{len(head)}|{head}"]


def test_the_pc_script_reads_the_tasks_line_the_robot_formats(tool: pathlib.Path) -> None:
    prefix = "12:00:00.000 I (123456) MemoriaBench: "
    rows = [("mascot_anim", 1, 2, 300), ("opus_codec", 1, 5, 900)]
    printed = _run(tool, _tasks_args(1280, 2, 42, rows))[0].split("|", 1)[1]
    parsed = bench_status.parse_tasks(prefix + printed)
    assert parsed is not None
    assert (parsed["count"], parsed["up_us"]) == (2, 42)
    assert parsed["tasks"] == {"mascot_anim": (1, 2, 300), "opus_codec": (1, 5, 900)}
    assert (
        bench_status.parse_tasks("MemoriaBench: tasks count=2 up_us=4") is not None
    )  # no rows is valid
    assert (
        bench_status.parse_tasks(prefix + "tasks count=2 up_us=4 a:1:2") is None
    )  # a row cut short
    assert (
        bench_status.parse_tasks(prefix + "tasks count=2 up_us=4 a:1:2:3 a:0:1:1") is None
    )  # a name twice


def test_the_task_shares_are_cpu_time_over_wall_time_and_survive_the_counter_wrapping() -> None:
    first = bench_status.parse_tasks(
        "12:00:00.000 I (1) MemoriaBench: tasks count=3 up_us=1000000 "
        "mascot_anim:1:2:4294967000 opus_codec:1:5:100 gone:0:1:5"
    )
    second = bench_status.parse_tasks(
        "12:00:01.000 I (2) MemoriaBench: tasks count=3 up_us=2000000 "
        "mascot_anim:1:2:304 opus_codec:1:5:250100 fresh:0:1:7"
    )
    assert first is not None and second is not None
    rates = bench_status.task_rates(first, second)
    assert rates["wall_us"] == 1000000
    # mascot_anim's counter wrapped: 4294967296 - 4294967000 + 304 = 600 us of a 1 s window.
    assert rates["share"] == {"mascot_anim": 600 / 1000000, "opus_codec": 0.25}
    assert rates["per_core"] == {1: 600 / 1000000 + 0.25}
    assert rates["unmatched"] == ["fresh", "gone"]
    with pytest.raises(bench_status.StatusError):
        bench_status.task_rates(second, first)
