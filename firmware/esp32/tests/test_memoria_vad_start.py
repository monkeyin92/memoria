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
    memoria::PlaybackVadHandoff handoff;
    char line[256];
    while (std::fgets(line, sizeof(line), stdin) != nullptr) {
        line[std::strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0' || line[0] == '#') {
            continue;
        }
        if (std::strncmp(line, "complete ", 9) == 0) {
            handoff.Completed(std::atoi(line + 9) != 0);
            std::puts("complete");
        } else if (std::strncmp(line, "listen ", 7) == 0 || std::strncmp(line, "arming ", 7) == 0) {
            unsigned epoch;
            unsigned long long now;
            if (std::sscanf(line + 7, "%u %llu", &epoch, &now) != 2) return 2;
            const bool armed = line[0] == 'l' ? handoff.Listening(epoch, now) : handoff.TryArm(epoch, now);
            std::puts(armed ? "armed" : "idle");
        } else if (std::strncmp(line, "packet ", 7) == 0) {
            int eligible;
            unsigned long long produced, now;
            float rms;
            if (std::sscanf(line + 7, "%d %llu %llu %f", &eligible, &produced, &now, &rms) != 4) return 2;
            std::puts(handoff.Frame(eligible != 0, produced, now, rms, .003f) ? "report" : "ignore");
        } else if (std::strncmp(line, "floor ", 6) == 0) {
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
            const bool eligible = std::strstr(line, " cancelled") == nullptr;
            std::printf("%s\n", Name(gate.Frame(ParseRms(rms_text, floor), floor, now, eligible)));
        } else if (std::strcmp(line, "peak") == 0) {
            std::printf("%.4f\n", static_cast<double>(gate.peak()));
        } else if (std::strcmp(line, "reset") == 0) {
            gate.Reset();
            std::puts("reset");
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


def test_t008_continuous_speech_recovers_once_after_the_early_peak_window(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "edge 0 1 0.00128 3 0", "frame 0.0019 3 200", "frame 0.0010 3 260",
        "frame 0.0042 3 500", "frame 0.0011 3 520",
        "frame 0.0031 3 3200", "frame 0.0055 3 3220", "frame 0.0055 3 3240",
        "edge 1 0 0.0003 3 4000",
    ) == ["await", "await", "suppress", "ignore", "ignore", "ignore", "report", "ignore", "report"]


@pytest.mark.parametrize("cancel,expected", [("edge 0 0 0.0002 3 280", "ignore"), ("reset", "reset")])
def test_ended_candidate_cannot_be_revived_by_later_energy(tool: pathlib.Path, cancel: str, expected: str) -> None:
    assert _run(
        tool, "edge 0 1 0.001 3 0", "frame 0.001 3 260", "frame 0.005 3 275",
        cancel, "frame 0.006 3 300", "frame 0.006 3 320",
    ) == ["await", "suppress", "ignore", expected, "ignore", "ignore"]


def test_a_first_frame_at_the_floor_reports_without_waiting(tool: pathlib.Path) -> None:
    assert _run(tool, "edge 0 1 floor 3 0") == ["report"]
    assert _run(tool, "edge 0 1 0.0067 3 0") == ["report"]


def test_t013_held_playback_edge_needs_fresh_quiet_then_two_loud_packets(tool: pathlib.Path) -> None:
    assert _run(tool, "complete 1", "listen 13 1000", "packet 1 980 1300 0.001",
                "packet 1 1250 1260 0.008", "packet 1 1270 1280 0.006",
                "packet 1 1500 1510 0.001", "packet 1 1870 2200 0.008",
                "packet 1 5790 5800 0.006", "packet 1 5810 5820 0.008",
                "packet 1 5830 5840 0.008") == ["complete", "armed", *["ignore"] * 6, "report", "ignore"]


@pytest.mark.parametrize("barrier", ["complete 0", "packet 0 1500 1500 0.006",
                                      "packet 1 7000 7000 0.006"])
def test_closed_changed_afe_or_expired_handoff_cannot_reopen(tool: pathlib.Path, barrier: str) -> None:
    after = 7020 if "7000" in barrier else 1520
    result = _run(tool, "complete 1", "listen 7 1000", "packet 1 1300 1300 0.001", barrier,
                  f"packet 1 {after} {after} 0.008", f"packet 1 {after+20} {after+20} 0.008")
    assert "report" not in result


@pytest.mark.parametrize("complete,current", [(0, 3), (1, 4), (1, 0)])
def test_only_normal_end_with_live_playback_speech_arms(tool: pathlib.Path, complete: int,
                                                       current: int) -> None:
    assert _run(tool, f"complete {complete}", f"listen {current} 900") == ["complete", "idle"]


@pytest.mark.parametrize("published,expected", [(1020, "armed"), (1260, "idle")])
def test_late_afe_publication_cannot_extend_playback_handoff(tool: pathlib.Path, published: int, expected: str) -> None:
    assert _run(tool, "complete 1", "listen 4 1000", f"arming 7 {published}") == ["complete", "idle", expected]


@pytest.mark.parametrize(
    "frames",
    [
        ("frame 0.001 3 260 cancelled", "frame 0.004 3 280", "frame 0.005 3 300"),
        ("frame 0.001 3 260", "frame 0.004 3 280 cancelled", "frame 0.005 3 300"),
        ("frame 0.001 3 260", "frame 0.004 3 4990", "frame 0.005 3 5010"),
    ],
)
def test_ineligible_or_expired_candidate_stays_suppressed(tool: pathlib.Path, frames: tuple[str, ...]) -> None:
    assert _run(tool, "edge 0 1 0.001 3 0", *frames) == ["await", "suppress", "ignore", "ignore"]


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
