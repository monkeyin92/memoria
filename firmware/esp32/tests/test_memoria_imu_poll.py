"""Host-side checks for the BMI270 frame-skip and per-state stats policy.

`memoria_imu_poll.h` has no ESP-IDF dependency, so the exact polling policy
that runs on the device can be driven with a fake read function on the host.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
BOARD_SOURCE = BOARD_DIR / "memoria_esp_vocat.cc"
HEADER = BOARD_DIR / "memoria_imu_poll.h"
# Mirrors main/device_state.h of the locked upstream.
DEVICE_STATE = {
    "unknown": 0,
    "starting": 1,
    "wifi": 2,
    "idle": 3,
    "connecting": 4,
    "recovering": 5,
    "listening": 6,
    "speaking": 7,
    "upgrading": 8,
    "activating": 9,
    "audio_testing": 10,
    "fatal": 11,
}

HARNESS = r"""
#include "memoria_imu_poll.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

static void PrintFrame(const memoria::ImuPoller& poller, int score, int reads) {
    unsigned ok = 0;
    unsigned fail = 0;
    for (int i = 0; i < memoria::kImuStatCount; ++i) {
        ok += poller.counters().ok[i];
        fail += poller.counters().fail[i];
    }
    printf("frame ok=%u fail=%u has_prev=%d score=", ok, fail, poller.has_prev() ? 1 : 0);
    if (score < 0) {
        printf("none");
    } else {
        printf("%d", score);
    }
    printf(" reads=%d\n", reads);
}

int main() {
    memoria::ImuPoller poller;
    char line[256];
    int reads = 0;
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        if (line[0] == '#' || line[0] == '\n') {
            continue;
        }
        if (strncmp(line, "fail", 4) == 0) {
            int state = 0;
            if (sscanf(line + 4, "%d", &state) != 1) {
                return 2;
            }
            ++reads;
            int score = -1;
            poller.PollFrameInState(
                state, [](memoria::ImuAccelSample&) { return false; },
                [&score](int value) { score = value; });
            PrintFrame(poller, score, reads);
            continue;
        }
        if (strncmp(line, "ok", 2) == 0) {
            int state = 0;
            int x = 0;
            int y = 0;
            int z = 0;
            if (sscanf(line + 2, "%d %d %d %d", &state, &x, &y, &z) != 4) {
                return 2;
            }
            ++reads;
            int score = -1;
            poller.PollFrameInState(
                state,
                [x, y, z](memoria::ImuAccelSample& sample) {
                    sample.x = static_cast<std::int16_t>(x);
                    sample.y = static_cast<std::int16_t>(y);
                    sample.z = static_cast<std::int16_t>(z);
                    return true;
                },
                [&score](int value) { score = value; });
            PrintFrame(poller, score, reads);
            continue;
        }
        if (strncmp(line, "log", 3) == 0) {
            long long now_ms = 0;
            if (sscanf(line + 3, "%lld", &now_ms) != 1) {
                return 2;
            }
            if (!poller.StatsLogDue(now_ms)) {
                printf("log 0\n");
                continue;
            }
            memoria::ImuStatCounters stats;
            poller.TakeStats(stats);
            char text[256];
            memoria::FormatImuStatsLine(stats, text, sizeof(text));
            memoria::ImuStatCounters again;
            poller.TakeStats(again);
            printf("log 1 %s reset_ok=%u\n", text, static_cast<unsigned>(again.ok[0]));
            continue;
        }
        return 2;
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def imu_tool() -> pathlib.Path:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "imu_tool"
        subprocess.run(
            [compiler, "-std=c++17", "-O0", "-I", str(BOARD_DIR), str(source), "-o", str(binary)],
            check=True,
        )
        yield binary


def _run(imu_tool: pathlib.Path, script: str) -> list[str]:
    completed = subprocess.run(
        [str(imu_tool)],
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.splitlines()


def _frames(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("frame ")]


def _stats(lines: list[str]) -> list[str]:
    return [line[len("log 1 ") :] for line in lines if line.startswith("log 1 ")]


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "lvgl", "lv_", "freertos"):
        assert forbidden not in source


def test_failed_read_skips_one_frame_without_retry(imu_tool: pathlib.Path) -> None:
    frames = _frames(_run(imu_tool, "ok 3 100 0 0\nfail 3\n"))
    assert frames == [
        "frame ok=1 fail=0 has_prev=1 score=none reads=1",
        "frame ok=1 fail=1 has_prev=0 score=none reads=2",
    ]


def test_frame_after_failure_reads_normally_and_reprimes_baseline(
    imu_tool: pathlib.Path,
) -> None:
    frames = _frames(_run(imu_tool, "ok 3 100 0 0\nfail 3\nok 3 200 0 0\nok 3 300 0 0\n"))
    assert frames == [
        "frame ok=1 fail=0 has_prev=1 score=none reads=1",
        "frame ok=1 fail=1 has_prev=0 score=none reads=2",
        "frame ok=2 fail=1 has_prev=1 score=none reads=3",
        "frame ok=3 fail=1 has_prev=1 score=100 reads=4",
    ]


def test_healthy_frames_keep_scoring(imu_tool: pathlib.Path) -> None:
    frames = _frames(_run(imu_tool, "ok 3 100 0 0\nok 3 130 0 0\nok 3 130 0 10\n"))
    assert [frame.rsplit(" score=", 1)[1].split(" reads=", 1)[0] for frame in frames] == [
        "none",
        "30",
        "10",
    ]


def test_stats_count_every_read(imu_tool: pathlib.Path) -> None:
    frames = _frames(_run(imu_tool, "ok 3 0 0 0\nfail 3\nfail 3\nok 3 0 0 0\n"))
    assert frames[-1] == "frame ok=2 fail=2 has_prev=1 score=none reads=4"


def test_stats_log_due_every_60_seconds(imu_tool: pathlib.Path) -> None:
    lines = _run(imu_tool, "log 0\nlog 59999\nlog 60000\nlog 119999\nlog 120000\n")
    assert [line.split(" ", 2)[:2] for line in lines] == [
        ["log", "0"],
        ["log", "0"],
        ["log", "1"],
        ["log", "0"],
        ["log", "1"],
    ]


def test_stats_are_split_by_device_state(imu_tool: pathlib.Path) -> None:
    script = "\n".join(
        [
            "ok 7 0 0 0",  # speaking
            "fail 7",
            "ok 7 0 0 0",
            "ok 6 0 0 0",  # listening
            "ok 6 0 0 0",
            "ok 3 0 0 0",  # idle
            "fail 3",
            "fail 3",
            "ok 1 0 0 0",  # starting
            "fail 4",  # connecting
            "log 60000",
        ]
    )
    assert _stats(_run(imu_tool, script + "\n")) == [
        "BMI2 stats speaking ok=2 fail=1 listening ok=2 fail=0 idle ok=1 fail=2 "
        "other ok=1 fail=1 reset_ok=0"
    ]


def test_each_minute_line_starts_from_zero(imu_tool: pathlib.Path) -> None:
    script = "ok 7 0 0 0\nfail 3\nlog 60000\nlog 120000\nfail 6\nlog 180000\n"
    assert _stats(_run(imu_tool, script)) == [
        "BMI2 stats speaking ok=1 fail=0 listening ok=0 fail=0 idle ok=0 fail=1 "
        "other ok=0 fail=0 reset_ok=0",
        "BMI2 stats speaking ok=0 fail=0 listening ok=0 fail=0 idle ok=0 fail=0 "
        "other ok=0 fail=0 reset_ok=0",
        "BMI2 stats speaking ok=0 fail=0 listening ok=0 fail=1 idle ok=0 fail=0 "
        "other ok=0 fail=0 reset_ok=0",
    ]


def test_state_mapping_follows_upstream_device_state() -> None:
    source = HEADER.read_text(encoding="utf-8")
    mapping = source[source.index("inline int ImuStatStateFromDeviceState") :]
    mapping = mapping[: mapping.index("\n}\n")]
    for name in ("idle", "listening", "speaking"):
        assert f"case {DEVICE_STATE[name]}:\n" in mapping
    assert mapping.count("case ") == 3


def test_board_logs_per_state_stats_without_changing_the_loop() -> None:
    board = BOARD_SOURCE.read_text(encoding="utf-8")
    imu = board[board.index("static void imu_event_task") : board.index("void MuteImuForTouch()")]
    assert "poller.PollFrameInState(" in imu
    assert "Application::GetInstance().GetDeviceState()" in imu
    assert "poller.TakeStats(stats)" in imu
    assert "memoria::FormatImuStatsLine(stats, line, sizeof(line))" in imu
    assert "vTaskDelay(pdMS_TO_TICKS(20))" in imu
    assert "state=%d" not in imu
