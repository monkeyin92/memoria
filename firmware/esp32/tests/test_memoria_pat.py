"""Host-side checks for the BMI270 body-pat detector.

`memoria_pat.h` has no ESP-IDF dependency so the exact firmware state machine
can be compiled on the host and fed IMU score sequences.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
HEADER = BOARD_DIR / "memoria_pat.h"

HARNESS = r"""
#include "memoria_pat.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

int main() {
    memoria::PatDetector detector;
    char line[256];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        if (line[0] == '#' || line[0] == '\n') {
            continue;
        }
        if (strncmp(line, "reset", 5) == 0) {
            detector.Reset();
            continue;
        }
        int score = 0;
        int muted = 0;
        long long now_ms = 0;
        if (sscanf(line, "%d %d %lld", &score, &muted, &now_ms) != 3) {
            return 2;
        }
        auto result = detector.Observe(score, muted != 0, now_ms);
        const char* kind = "none";
        if (result.kind == memoria::PatKind::kPat) {
            kind = "pat";
        } else if (result.kind == memoria::PatKind::kShake) {
            kind = "shake";
        } else if (result.kind == memoria::PatKind::kTouchRumble) {
            kind = "rumble";
        }
        printf("%s %d %d\n", kind, result.peak, result.streak);
        fflush(stdout);
    }
    return 0;
}
"""


@pytest.fixture(scope="module")
def pat_tool() -> pathlib.Path:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "pat_tool"
        subprocess.run(
            [compiler, "-std=c++17", "-O0", "-I", str(BOARD_DIR), str(source), "-o", str(binary)],
            check=True,
        )
        yield binary


def _run(pat_tool: pathlib.Path, script: str) -> list[tuple[str, int, int]]:
    completed = subprocess.run(
        [str(pat_tool)],
        input=script,
        text=True,
        capture_output=True,
        check=True,
    )
    events = []
    for line in completed.stdout.splitlines():
        kind, peak, streak = line.split()
        events.append((kind, int(peak), int(streak)))
    return events


def _kinds(events: list[tuple[str, int, int]]) -> list[str]:
    return [kind for kind, _, _ in events if kind != "none"]


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "lvgl", "lv_", "freertos"):
        assert forbidden not in source


def test_short_pat_fires_after_confirm_delay(pat_tool: pathlib.Path) -> None:
    # 20 ms samples: high, fall, then 3 quiet ticks to confirm.
    script = "\n".join(
        [
            "200 0 0",
            "8000 0 20",
            "400 0 40",
            "200 0 60",
            "180 0 80",
            "160 0 100",
        ]
    ) + "\n"
    kinds = _kinds(_run(pat_tool, script))
    assert kinds == ["pat"]


def test_ringing_pat_still_counts(pat_tool: pathlib.Path) -> None:
    # Old detector required an immediate drop below 2500 after at most 2 highs.
    script = "\n".join(
        [
            "200 0 0",
            "7000 0 20",
            "4500 0 40",
            "3800 0 60",
            "900 0 80",
            "300 0 100",
            "200 0 120",
            "180 0 140",
        ]
    ) + "\n"
    events = _run(pat_tool, script)
    kinds = _kinds(events)
    assert kinds == ["pat"]
    peak = next(peak for kind, peak, _ in events if kind == "pat")
    assert peak == 7000


def test_sustained_shake_is_ignored(pat_tool: pathlib.Path) -> None:
    lines = ["200 0 0"]
    now = 20
    for _ in range(10):
        lines.append(f"5000 0 {now}")
        now += 20
    for extra in (400, 200, 180, 160):
        lines.append(f"{extra} 0 {now}")
        now += 20
    kinds = _kinds(_run(pat_tool, "\n".join(lines) + "\n"))
    assert kinds == ["shake"]


def test_touch_mute_cancels_pending_pat(pat_tool: pathlib.Path) -> None:
    script = "\n".join(
        [
            "200 0 0",
            "8000 0 20",
            "400 0 40",
            "400 1 60",
            "200 0 80",
            "180 0 100",
            "160 0 120",
        ]
    ) + "\n"
    kinds = _kinds(_run(pat_tool, script))
    assert kinds == ["rumble"]


def test_muted_high_sample_does_not_become_a_pat(pat_tool: pathlib.Path) -> None:
    script = "\n".join(
        [
            "200 0 0",
            "9000 1 20",
            "400 0 40",
            "200 0 60",
            "180 0 80",
            "160 0 100",
        ]
    ) + "\n"
    kinds = _kinds(_run(pat_tool, script))
    assert kinds == []


def test_below_threshold_motion_is_ignored(pat_tool: pathlib.Path) -> None:
    script = "\n".join(
        [
            "200 0 0",
            "3000 0 20",
            "2800 0 40",
            "400 0 60",
            "200 0 80",
            "180 0 100",
            "160 0 120",
        ]
    ) + "\n"
    assert _kinds(_run(pat_tool, script)) == []


def test_cooldown_suppresses_a_second_pat(pat_tool: pathlib.Path) -> None:
    lines = [
        "200 0 0",
        "8000 0 20",
        "400 0 40",
        "200 0 60",
        "180 0 80",
        "160 0 100",
        "8000 0 140",
        "400 0 160",
        "200 0 180",
        "180 0 200",
        "160 0 220",
    ]
    kinds = _kinds(_run(pat_tool, "\n".join(lines) + "\n"))
    assert kinds == ["pat"]

    lines.extend(
        [
            "8000 0 2700",
            "400 0 2720",
            "200 0 2740",
            "180 0 2760",
            "160 0 2780",
        ]
    )
    kinds = _kinds(_run(pat_tool, "\n".join(lines) + "\n"))
    assert kinds == ["pat", "pat"]
