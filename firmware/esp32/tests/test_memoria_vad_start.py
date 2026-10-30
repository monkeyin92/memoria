"""Host checks for the device vad.start RMS floor and its peak window.

False starts measured in the field were 0.0005 to 0.0013. Real test speech
was 0.0067, but the AFE reports the start edge on its first speaking frame,
which for weak speech is 0.0007 to 0.0019; the frames that follow the same
utterance reach 0.0031 to 0.0088. A start below the floor therefore holds the
loudest frame for 250 ms and decides on that peak. The default floor is 3
thousandths, an rms of 0.003.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

MEMORIA_DIR = pathlib.Path(__file__).parents[1] / "overlay" / "files" / "main" / "memoria"
AUDIO_LEVEL_PATCH = (
    pathlib.Path(__file__).parents[1] / "overlay" / "patches" / "0034-memoria-audio-level.patch"
)

HARNESS = r"""
#include "memoria_vad_start.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

namespace {

const char* Name(memoria::VadEdgeAction action) {
    switch (action) {
    case memoria::VadEdgeAction::kIgnore:
        return "ignore";
    case memoria::VadEdgeAction::kSuppressStart:
        return "suppress";
    case memoria::VadEdgeAction::kAwaitPeak:
        return "await";
    case memoria::VadEdgeAction::kReport:
        return "report";
    }
    return "invalid";
}

float ParseRms(const char* text, float floor) {
    if (std::strcmp(text, "floor") == 0) {
        return floor;
    }
    return std::strtof(text, nullptr);
}

}  // namespace

int main() {
    memoria::VadStartGate gate;
    char line[256];
    while (std::fgets(line, sizeof(line), stdin) != nullptr) {
        line[std::strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0' || line[0] == '#') {
            continue;
        }
        if (std::strncmp(line, "floor ", 6) == 0) {
            const int milli = std::atoi(line + 6);
            std::printf("%.9f\n", static_cast<double>(memoria::VadStartRmsFloorFromMilli(milli)));
        } else if (std::strncmp(line, "edge ", 5) == 0) {
            int epoch = 0;
            int speaking = 0;
            int milli = 0;
            unsigned long long now = 0;
            char rms_text[64] = {};
            if (std::sscanf(line + 5, "%d %d %63s %d %llu", &epoch, &speaking, rms_text, &milli,
                            &now) != 5) {
                return 2;
            }
            const float floor = memoria::VadStartRmsFloorFromMilli(milli);
            const memoria::VadEdgeAction action = gate.Edge(
                epoch != 0, speaking != 0, ParseRms(rms_text, floor), floor, now);
            std::printf("%s\n", Name(action));
        } else if (std::strncmp(line, "frame ", 6) == 0) {
            int milli = 0;
            unsigned long long now = 0;
            char rms_text[64] = {};
            if (std::sscanf(line + 6, "%63s %d %llu", rms_text, &milli, &now) != 3) {
                return 2;
            }
            const float floor = memoria::VadStartRmsFloorFromMilli(milli);
            std::printf("%s\n", Name(gate.Frame(ParseRms(rms_text, floor), floor, now)));
        } else if (std::strcmp(line, "peak") == 0) {
            std::printf("%.4f\n", static_cast<double>(gate.peak()));
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
        binary = pathlib.Path(tmp) / "vad_start_tool"
        completed = subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-I",
                str(MEMORIA_DIR),
                str(source),
                "-o",
                str(binary),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            pytest.fail(completed.stderr or completed.stdout)
        yield binary


def _run(binary: pathlib.Path, *lines: str) -> list[str]:
    completed = subprocess.run(
        [str(binary)],
        input="\n".join(lines) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.splitlines()


def test_floor_milli_is_thousandths_of_full_scale(tool: pathlib.Path) -> None:
    assert abs(float(_run(tool, "floor 3")[0]) - 0.003) < 1e-6
    assert abs(float(_run(tool, "floor 0")[0]) - 0.0) < 1e-6


def test_a_weak_first_frame_is_held_until_the_speech_peak(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "edge 0 1 0.0007 3 0",
        "frame 0.0009 3 20",
        "frame 0.0050 3 60",
        "peak",
    ) == ["await", "await", "report", "0.0050"]


def test_the_window_suppresses_speech_that_never_reaches_the_floor(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "edge 0 1 0.0007 3 0",
        "frame 0.0013 3 100",
        "frame 0.0019 3 240",
        "frame 0.0008 3 260",
    ) == ["await", "await", "await", "suppress"]


def test_an_end_edge_inside_the_window_suppresses_the_start(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 0.0007 3 0", "edge 0 0 0.0005 3 40") == ["await", "suppress"]


def test_a_first_frame_at_the_floor_reports_without_waiting(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 floor 3 0") == ["report"]
    assert _run(tool, "edge 0 1 0.0067 3 0") == ["report"]


def test_a_second_start_while_the_epoch_is_open_is_ignored(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 1 1 0.0067 3 0") == ["ignore"]


def test_a_quiet_end_is_reported(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 1 0 0.0005 3 0") == ["report"]


def test_milli_zero_reports_the_false_start_band(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 0.0013 0 0") == ["report"]


def test_a_higher_floor_suppresses_the_measured_speech(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 0.0067 7 0", "frame 0.0068 7 260") == ["await", "suppress"]


def test_the_peak_is_cleared_between_starts(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "edge 0 1 0.0007 3 0",
        "edge 0 0 0.0005 3 40",
        "edge 0 1 0.0007 3 1000",
        "frame 0.0009 3 1010",
    ) == ["await", "suppress", "await", "await"]


def test_the_audio_task_publishes_its_measured_rms_for_the_window() -> None:
    added = "\n".join(
        line[1:]
        for line in AUDIO_LEVEL_PATCH.read_text(encoding="utf-8").splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    assert '#include "memoria_vad_start.h"' in added
    assert "memoria::uplink_frame_rms.store(" in added
