"""Host-side checks for the BMI270 frame-skip and stats policy.

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
HEADER = BOARD_DIR / "memoria_imu_poll.h"

HARNESS = r"""
#include "memoria_imu_poll.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

int main() {
    memoria::ImuPoller poller;
    char line[256];
    int reads = 0;
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        if (line[0] == '#' || line[0] == '\n') {
            continue;
        }
        if (strncmp(line, "fail", 4) == 0) {
            ++reads;
            int score = -1;
            poller.PollFrame([](memoria::ImuAccelSample&) { return false; },
                             [&score](int value) { score = value; });
            printf("frame ok=%u fail=%u has_prev=%d score=", poller.ok(), poller.fail(),
                   poller.has_prev() ? 1 : 0);
            if (score < 0) {
                printf("none");
            } else {
                printf("%d", score);
            }
            printf(" reads=%d\n", reads);
            continue;
        }
        if (strncmp(line, "ok", 2) == 0) {
            int x = 0;
            int y = 0;
            int z = 0;
            if (sscanf(line + 2, "%d %d %d", &x, &y, &z) != 3) {
                return 2;
            }
            ++reads;
            int score = -1;
            poller.PollFrame(
                [x, y, z](memoria::ImuAccelSample& sample) {
                    sample.x = static_cast<std::int16_t>(x);
                    sample.y = static_cast<std::int16_t>(y);
                    sample.z = static_cast<std::int16_t>(z);
                    return true;
                },
                [&score](int value) { score = value; });
            printf("frame ok=%u fail=%u has_prev=%d score=", poller.ok(), poller.fail(),
                   poller.has_prev() ? 1 : 0);
            if (score < 0) {
                printf("none");
            } else {
                printf("%d", score);
            }
            printf(" reads=%d\n", reads);
            continue;
        }
        if (strncmp(line, "log", 3) == 0) {
            long long now_ms = 0;
            if (sscanf(line + 3, "%lld", &now_ms) != 1) {
                return 2;
            }
            printf("log %d\n", poller.StatsLogDue(now_ms) ? 1 : 0);
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


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "lvgl", "lv_", "freertos"):
        assert forbidden not in source


def test_failed_read_skips_one_frame_without_retry(imu_tool: pathlib.Path) -> None:
    frames = _frames(_run(imu_tool, "ok 100 0 0\nfail\n"))
    assert frames == [
        "frame ok=1 fail=0 has_prev=1 score=none reads=1",
        "frame ok=1 fail=1 has_prev=0 score=none reads=2",
    ]


def test_frame_after_failure_reads_normally_and_reprimes_baseline(
    imu_tool: pathlib.Path,
) -> None:
    frames = _frames(_run(imu_tool, "ok 100 0 0\nfail\nok 200 0 0\nok 300 0 0\n"))
    assert frames == [
        "frame ok=1 fail=0 has_prev=1 score=none reads=1",
        "frame ok=1 fail=1 has_prev=0 score=none reads=2",
        "frame ok=2 fail=1 has_prev=1 score=none reads=3",
        "frame ok=3 fail=1 has_prev=1 score=100 reads=4",
    ]


def test_healthy_frames_keep_scoring(imu_tool: pathlib.Path) -> None:
    frames = _frames(_run(imu_tool, "ok 100 0 0\nok 130 0 0\nok 130 0 10\n"))
    assert [frame.rsplit(" score=", 1)[1].split(" reads=", 1)[0] for frame in frames] == [
        "none",
        "30",
        "10",
    ]


def test_stats_count_every_read(imu_tool: pathlib.Path) -> None:
    frames = _frames(_run(imu_tool, "ok 0 0 0\nfail\nfail\nok 0 0 0\n"))
    assert frames[-1] == "frame ok=2 fail=2 has_prev=1 score=none reads=4"


def test_stats_log_due_every_60_seconds(imu_tool: pathlib.Path) -> None:
    lines = _run(imu_tool, "log 0\nlog 59999\nlog 60000\nlog 119999\nlog 120000\n")
    assert lines == ["log 0", "log 0", "log 1", "log 0", "log 1"]
