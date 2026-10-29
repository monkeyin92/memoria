"""Host checks for the device vad.start RMS floor.

False starts measured in the field were 0.0005 to 0.0013. Real test speech
was 0.0067. The default floor is 3 thousandths, an rms of 0.003. A start
below the floor does not open the epoch, so the matching end is not reported.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

MEMORIA_DIR = pathlib.Path(__file__).parents[1] / "overlay" / "files" / "main" / "memoria"

HARNESS = r"""
#include "memoria_vad_start.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

const char* Name(memoria::VadEdgeAction action) {
    switch (action) {
    case memoria::VadEdgeAction::kIgnore:
        return "ignore";
    case memoria::VadEdgeAction::kSuppressStart:
        return "suppress";
    case memoria::VadEdgeAction::kReport:
        return "report";
    }
    return "invalid";
}

int main() {
    char line[256];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        line[strcspn(line, "\r\n")] = '\0';
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
            char rms_text[64] = {};
            if (std::sscanf(line + 5, "%d %d %63s %d", &epoch, &speaking, rms_text, &milli) != 4) {
                return 2;
            }
            const float floor = memoria::VadStartRmsFloorFromMilli(milli);
            const float rms = std::strcmp(rms_text, "floor") == 0
                                  ? floor
                                  : std::strtof(rms_text, nullptr);
            const memoria::VadEdgeAction action =
                memoria::ClassifyVadEdge(epoch != 0, speaking != 0, rms, floor);
            std::printf("%s\n", Name(action));
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


def test_measured_false_starts_are_suppressed_and_the_end_is_not_reported(
    tool: pathlib.Path,
) -> None:
    for rms in ("0.0005", "0.0013"):
        assert _run(tool, f"edge 0 1 {rms} 3") == ["suppress"]
    assert _run(tool, "edge 0 0 0.0005 3") == ["ignore"]


def test_measured_speech_reports_the_start_and_a_quiet_end(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 0.0067 3") == ["report"]
    assert _run(tool, "edge 1 0 0.0005 3") == ["report"]


def test_rms_equal_to_the_floor_is_reported(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 floor 3") == ["report"]


def test_a_second_start_while_the_epoch_is_open_is_ignored(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 1 1 0.0067 3") == ["ignore"]


def test_milli_zero_reports_the_false_start_band(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 0.0013 0") == ["report"]


def test_a_higher_floor_suppresses_the_measured_speech(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 0.0067 7") == ["suppress"]
