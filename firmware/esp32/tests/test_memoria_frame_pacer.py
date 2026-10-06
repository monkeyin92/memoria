"""Host-side checks for the animation frame pacer (TODOLIST M-6).

`memoria_frame_pacer.h` has no ESP-IDF dependency, so the governor that stretches the mascot's frame
interval when frames cost the chip too much compiles on the host. How it is wired into the display task is
checked as source text, because that only builds inside ESP-IDF.

Why it exists: the mascot now asks for a frame every 40 ms in every lit state (the breathing moves in
sub-pixel steps). What a frame costs on the real panel was last measured at 14-34 ms per redraw of the old
renderer; the new one is expected to be close, not known. The pacer follows the measured cost instead of a
guess.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
HEADER = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria" / "memoria_frame_pacer.h"

HARNESS = r"""
#include "memoria_frame_pacer.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <string>

int main() {
    memoria::FramePacer pacer;
    char line[256];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        line[strcspn(line, "\r\n")] = '\0';
        std::istringstream in(line);
        std::string cmd;
        in >> cmd;
        if (cmd == "run") {
            // run <frames> <busy us> <wanted ms>: that many drawn frames, each followed by one interval query;
            // prints the interval after the last one and the extra time it carries.
            unsigned long frames = 0, busy = 0, wanted = 0;
            in >> frames >> busy >> wanted;
            uint32_t interval = wanted;
            for (unsigned long i = 0; i < frames; ++i) {
                pacer.NoteDrawn(static_cast<uint32_t>(busy));
                interval = pacer.NextIntervalMs(static_cast<uint32_t>(wanted));
            }
            printf("%u %u\n", interval, pacer.extra_ms());
        } else if (cmd == "spike") {
            // spike <busy us> <wanted ms>: one frame
            unsigned long busy = 0, wanted = 0;
            in >> busy >> wanted;
            pacer.NoteDrawn(static_cast<uint32_t>(busy));
            printf("%u\n", pacer.NextIntervalMs(static_cast<uint32_t>(wanted)));
        } else if (cmd == "idle") {
            // idle <calls> <wanted ms>: interval queries with no new frame (a static scene)
            unsigned long calls = 0, wanted = 0;
            in >> calls >> wanted;
            uint32_t interval = 0;
            for (unsigned long i = 0; i < calls; ++i) interval = pacer.NextIntervalMs(static_cast<uint32_t>(wanted));
            printf("%u\n", interval);
        } else if (cmd == "constants") {
            printf("%u %u %u %u %u\n", memoria::FramePacer::kBudgetPercent, memoria::FramePacer::kRelaxPercent,
                   memoria::FramePacer::kStepMs, memoria::FramePacer::kMaxExtraMs,
                   memoria::FramePacer::kRelaxAfterCalls);
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
        binary = pathlib.Path(tmp) / "frame_pacer_tool"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O1",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fsanitize=undefined",
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


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "lvgl", "lv_", "freertos", "nvs", "settings.h", "driver/"):
        assert forbidden not in source
    assert re.findall(r'#include "([^"]+)"', source) == []


def test_the_budget_is_two_thirds_of_an_interval_in_ten_ms_steps(tool: pathlib.Path) -> None:
    assert _run(tool, "constants") == ["65 55 10 120 25"]


def test_cheap_frames_never_stretch_the_interval(tool: pathlib.Path) -> None:
    assert _run(tool, "run 500 8000 40") == ["40 0"]
    # 26 ms of a 40 ms interval is exactly the budget: not over it.
    assert _run(tool, "run 500 26000 40") == ["40 0"]


def test_frames_over_the_budget_stretch_it_until_they_fit_and_then_hold(tool: pathlib.Path) -> None:
    # A 30 ms frame takes 75 % of 40 ms; 50 ms is the first interval it fits (60 %), and it stays there.
    assert _run(tool, "run 60 30000 40", "run 400 30000 40") == ["50 10", "50 10"]
    # 36 ms: 40 -> 50 (72 %) -> 60 (60 %).
    assert _run(tool, "run 400 36000 40") == ["60 20"]


def test_a_very_expensive_frame_is_capped_not_chased_forever(tool: pathlib.Path) -> None:
    assert _run(tool, "run 400 100000 40") == ["160 120"]


def test_one_slow_frame_among_cheap_ones_changes_nothing(tool: pathlib.Path) -> None:
    # A 90 ms frame (a companion switch) among 10 ms ones: the average stays under 26 ms.
    assert _run(tool, "run 50 10000 40", "spike 90000 40") == ["40 0", "40"]


def test_the_time_comes_back_slowly_once_frames_are_cheap_again(tool: pathlib.Path) -> None:
    got = _run(
        tool,
        "run 400 100000 40",  # stretched to the cap
        "run 20 10000 40",  # the average falls, but about a second of comfort is needed per step
        "run 2000 10000 40",
    )
    assert got[0] == "160 120"
    assert int(got[1].split()[1]) >= 100  # most of it is still there after twenty cheap frames
    assert got[2] == "40 0"


def test_a_static_scene_that_draws_nothing_keeps_the_interval_it_had(tool: pathlib.Path) -> None:
    # No new measurement: the old average still says the frames were expensive, so nothing relaxes either
    # way by accident of the interval changing under it.
    assert _run(tool, "run 100 30000 40", "idle 3 40") == ["50 10", "50"]


def test_the_wanted_interval_can_change_under_the_pacer(tool: pathlib.Path) -> None:
    # 30 ms frames at a 40 ms cadence are stretched to 50; the scene then asks for 80 ms (a dozing
    # companion): 30 ms is 37 % of that, so the extra time is given back.
    assert _run(tool, "run 100 30000 40", "run 1000 30000 80") == ["50 10", "80 0"]


def test_the_display_task_measures_the_whole_frame_and_logs_what_the_pacer_did() -> None:
    display = (BOARD_DIR / "memoria_mascot_display.cc").read_text(encoding="utf-8")
    assert '#include "memoria_frame_pacer.h"' in display
    loop = display[display.index("void MemoriaMascotDisplay::AnimationLoop") :]
    # The clock starts before the display lock is requested, so waiting for LVGL counts as busy time.
    assert loop.index("frame_started") < loop.index("Lock(100)")
    assert "pacer.NoteDrawn(" in loop
    assert "pacer.NextIntervalMs(" in loop
    # The screen-off poll is not paced, and a stats line says what the cadence became.
    assert "screen_off_ ? kScreenOffPollMs" in loop
    assert "extra_ms=" in loop
