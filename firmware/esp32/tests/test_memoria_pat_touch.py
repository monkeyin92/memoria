"""Host-side checks for what a body pat or a screen tap does in every device state.

2026-10-21 15:24 field test on the #350 firmware: while the companion spoke,
patting the robot and tapping the screen neither stopped playback nor changed
the companion, and the serial log carried no pat or touch line at all. The old
board code logged only an accepted pat, animated only in idle and had no
speaking path, so the log could not tell "no impulse" from "event ignored".

`memoria_pat.h` now owns that policy next to the pulse classifier: the
classifier's reaction to a high delta, what a confirmed pat decides in each
device state, and what a screen tap release decides. All of it is host-tested
here without ESP-IDF.
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
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
BOARD_SOURCE = BOARD_DIR / "memoria_esp_vocat.cc"


# Feeds accel scores through the classifier and prints each sample's verdict.
HARNESS = r"""
#include "memoria_pat.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>

struct Feed {
    memoria::PatDetector detector;
    long long now = 0;
    void sample(int score, bool boot_quiet) {
        const auto result = detector.Observe(score, false, now, boot_quiet);
        printf("%s %d %d %s %s\n", memoria::PatEventName(result.kind), result.peak,
               result.streak, memoria::PatReactionName(memoria::PatReactionFor(score, boot_quiet)),
               memoria::PatIgnoreName(result.ignore));
        fflush(stdout);
        now += 20;
    }
};

int main() {
    Feed feed;
    char line[128];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        if (line[0] == '#' || line[0] == '\n') {
            continue;
        }
        int score = 0;
        int boot_quiet = 0;
        if (sscanf(line, "%d %d", &score, &boot_quiet) < 1) {
            return 2;
        }
        feed.sample(score, boot_quiet != 0);
    }
    return 0;
}
"""

DECIDE = r"""
#include "memoria_pat.h"

#include <cstdio>
#include <cstring>

// The states memoria_pat.h names for the board, which static_asserts them
// against device_state.h at firmware build time.
enum : int {
    mem_0 = memoria::kPatStateIdle,
    mem_1 = memoria::kPatStateStarting,
    mem_2 = memoria::kPatStateListening,
    mem_3 = memoria::kPatStateSpeaking,
};

int main() {
    struct Scene {
        const char* name;
        int state;
        bool protected_playback;      // a fixed safety script plays
        bool pat;                     // a confirmed body pat
    };
    const Scene scenes[] = {
        {"idle_pat", mem_0, false, true},
        {"speaking_pat", mem_3, false, true},
        {"listening_pat", mem_2, false, true},
        {"speaking_shake", mem_3, false, false},
        {"speaking_crisis_pat", mem_3, true, true},
        {"idle_crisis_pat", mem_0, true, true},
        {"idle_tap", mem_0, false, false},
        {"speaking_tap", mem_3, false, false},
        {"listening_tap", mem_2, false, false},
        {"speaking_tap_crisis", mem_3, true, false},
        {"starting_tap", mem_1, false, false},
        {"idle_tap_offline", mem_0, false, false},
        {"idle_crisis_tap", mem_0, true, false},
    };
    for (const auto& scene : scenes) {
        memoria::PatGestureFacts facts;
        facts.state = scene.state;
        facts.speaking_crisis_playback = scene.protected_playback;
        facts.is_pat = scene.pat;
        const auto decision = memoria::PatGestureDecisionFor(facts);
        memoria::TapGestureFacts tap;
        tap.state = scene.state;
        tap.speaking_crisis_playback = scene.protected_playback;
        tap.wifi_configuring = scene.state == mem_1;
        tap.offline_wants_tap = std::strcmp(scene.name, "idle_tap_offline") == 0;
        const auto tap_decision = memoria::ScreenTapDecisionFor(tap);
        printf("%s pat=%s tap=%s\n", scene.name, memoria::PatGestureDecisionName(decision),
               memoria::ScreenTapDecisionName(tap_decision));
    }
    return 0;
}
"""


def _compiler() -> str:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    return compiler


@pytest.fixture(scope="module")
def pat_tool() -> pathlib.Path:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        source = root / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = root / "pat_touch_tool"
        subprocess.run(
            [
                _compiler(),
                "-std=c++17",
                "-O0",
                "-I",
                str(BOARD_DIR),
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        yield binary


@pytest.fixture(scope="module")
def decision_tool() -> pathlib.Path:
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "decision.cc"
        source.write_text(DECIDE, encoding="utf-8")
        binary = pathlib.Path(tmp) / "decision_tool"
        subprocess.run(
            [_compiler(), "-std=c++17", "-O0", "-I", str(BOARD_DIR), str(source), "-o", str(binary)],
            check=True,
        )
        yield binary


def _feed(pat_tool: pathlib.Path, samples: list[tuple[int, bool]]) -> list[str]:
    completed = subprocess.run(
        [str(pat_tool)],
        input="\n".join(f"{score} {1 if quiet else 0}" for score, quiet in samples) + "\n",
        text=True,
        capture_output=True,
        check=True,
    )
    return completed.stdout.splitlines()


def _decisions(decision_tool: pathlib.Path) -> dict[str, tuple[str, str]]:
    completed = subprocess.run([str(decision_tool)], text=True, capture_output=True, check=True)
    out = {}
    for line in completed.stdout.splitlines():
        scene, pat, tap = line.split()
        assert pat.startswith("pat=") and tap.startswith("tap=")
        out[scene] = (pat[len("pat="):], tap[len("tap="):])
    return out


def _pats(lines: list[str]) -> list[str]:
    return [line.split()[0] for line in lines if line.split()[0] == "pat"]


def _boot(scores: list[int]) -> list[tuple[int, bool]]:
    # The BMI270 start-up self-calibration. Every field "Device pat detected"
    # line is this pulse: 4083-4141 at about 1.2 s uptime.
    return [(score, True) for score in scores]


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "lvgl", "lv_", "freertos"):
        assert forbidden not in source


def test_boot_calibration_pulse_never_fires_a_pat(pat_tool: pathlib.Path) -> None:
    # The sensor's own start-up pulse at ~4100 is what older firmware logged
    # as "Device pat detected (score: 4098) state=0" on every boot.
    lines = _feed(
        pat_tool,
        _boot([200, 4118, 200, 180, 160, 150, 140]) + [(200, False), (180, False)],
    )
    assert _pats(lines) == []
    assert lines[1].split()[3] == "boot_quiet"
    assert lines[1].split()[4] == "boot_quiet"


def test_boot_pulse_does_not_mask_the_first_real_pat(pat_tool: pathlib.Path) -> None:
    # Ignored without arming the confirm chain, so a real pat right after the
    # start-up pulse still fires instead of being eaten by the cooldown.
    lines = _feed(
        pat_tool,
        _boot([200, 4118, 200]) + [(9000, False), (400, False), (200, False), (180, False)],
    )
    assert _pats(lines) == ["pat"]


def test_screen_rumble_and_table_bumps_do_not_stop_a_sentence(pat_tool: pathlib.Path) -> None:
    # 3200 is the floor the bench equipment needed for a real pat; a screen tap
    # is cancelled by the CST816S mute window, not by raising that floor.
    lines = _feed(pat_tool, [(0, False), (200, False), (5200, False), (400, False), (200, False)])
    assert _pats(lines) == []


def test_pat_ringing_through_rumble_still_counts(pat_tool: pathlib.Path) -> None:
    # A real pat rings after its peak; the pulse is judged by its peak.
    lines = _feed(
        pat_tool,
        [(0, False), (200, False), (8000, False), (9000, False), (4100, False),
         (900, False), (200, False), (180, False)],
    )
    assert _pats(lines) == ["pat"]


def test_sustained_motion_is_a_shake_not_a_pat(pat_tool: pathlib.Path) -> None:
    lines = _feed(
        pat_tool,
        [(0, False), (200, False)] + [(7000, False)] * 8 + [(400, False), (200, False), (180, False)],
    )
    assert [line.split()[0] for line in lines if line.split()[0] != "none"] == ["shake"]


def test_pat_stops_playback_in_idle_and_speaking_only(decision_tool: pathlib.Path) -> None:
    decisions = _decisions(decision_tool)
    # An idle tap navigates (wake / offline menu); a pat is its own gesture.
    assert decisions["idle_pat"] == ("stop_speaking", "tap_navigation")
    assert decisions["speaking_pat"] == ("stop_speaking", "stop_speaking")
    assert decisions["listening_pat"] == ("none", "none")
    # A shake is a dizzy companion, never a stop.
    assert decisions["speaking_shake"] == ("none", "stop_speaking")


def test_crisis_playback_never_stops(decision_tool: pathlib.Path) -> None:
    decisions = _decisions(decision_tool)
    assert decisions["speaking_crisis_pat"] == ("crisis_keep_playback", "crisis_keep_playback")
    assert decisions["speaking_tap_crisis"] == ("none", "crisis_keep_playback")


def test_screen_tap_stops_playback_and_keeps_idle_wake(decision_tool: pathlib.Path) -> None:
    decisions = _decisions(decision_tool)
    assert decisions["speaking_tap"] == ("none", "stop_speaking")
    assert decisions["listening_tap"] == ("none", "none")
    assert decisions["starting_tap"] == ("none", "tap_navigation")


def test_offline_menu_tap_keeps_its_navigation(decision_tool: pathlib.Path) -> None:
    decisions = _decisions(decision_tool)
    assert decisions["idle_tap"] == ("none", "tap_navigation")
    assert decisions["idle_tap_offline"] == ("none", "tap_navigation")
    assert decisions["idle_crisis_tap"] == ("none", "tap_navigation")


def test_protected_playback_flag_is_decoded_from_the_wire() -> None:
    harness = """
#include "memoria_playback_stop.h"
#include <cstdio>
int main() {
    // present + true, present + false, absent, present but not a bool
    const bool cases[4] = {
        memoria::InterruptibleFromWire(true, true, true),
        memoria::InterruptibleFromWire(true, true, false),
        memoria::InterruptibleFromWire(false, false, false),
        memoria::InterruptibleFromWire(true, false, false),
    };
    for (bool interruptible : cases) {
        const auto stop = memoria::PlaybackStopFromInterruptible(interruptible);
        printf("%d %s\\n", interruptible ? 1 : 0, memoria::PlaybackStopName(stop));
    }
    return 0;
}
"""
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        source = root / "stop.cc"
        source.write_text(harness, encoding="utf-8")
        binary = root / "stop_tool"
        subprocess.run(
            [
                _compiler(),
                "-std=c++17",
                "-O0",
                "-I",
                str(MEMORIA_DIR),
                str(source),
                "-o",
                str(binary),
            ],
            check=True,
        )
        out = subprocess.run([str(binary)], text=True, capture_output=True, check=True).stdout
    # Only an explicit false protects playback; a missing or malformed field
    # must leave an ordinary reply stoppable.
    assert out.splitlines() == ["1 allowed", "0 protected", "1 allowed", "1 allowed"]


def test_board_wires_pat_and_tap_to_the_shared_policy() -> None:
    # The policy lives in the header; the board asks it on every event and
    # logs the decision and the per-sample reason, which the 2026-10-21 logs
    # lacked.
    source = BOARD_SOURCE.read_text(encoding="utf-8")
    for required in (
        "memoria::PatGestureFacts facts;",
        "memoria::TapGestureFacts facts;",
        "memoria::PatGestureDecisionFor(facts)",
        "memoria::ScreenTapDecisionFor(",
        "memoria::PatReactionName(reaction)",
        "memoria::PatIgnoreName(memoria::PatIgnore::kNone)",
        "memoria::PatIgnoreReasonName(result.kind, result.ignore)",
        "memoria::ScreenTapDecisionName(decision)",
    ):
        assert required in source, required
    for line in (
        "Device pat (accepted) kind=%s score=%d state=%d decision=%s silent=%d",
        "Screen tap (accepted) hold_ms=%u state=%d decision=%s",
        "pat motion watcher armed 20 ms poll",
        "Device pat ignored (score: %d, reason=%s) state=%d touch_mute=%d",
    ):
        assert line in source, line
