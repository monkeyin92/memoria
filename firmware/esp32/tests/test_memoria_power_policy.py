"""Host checks for tap-only power policy, quiet hours, and mic gate helpers."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"

HARNESS = r"""
#include "memoria_power_policy.h"
#include "memoria_quiet_hours.h"
#include "memoria_mic_gate.h"

#include <cstdio>
#include <cstring>

int main() {
    char line[160];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0' || line[0] == '#') continue;
        if (strncmp(line, "tap ", 4) == 0) {
            char device[32];
            int quiet = 0, wants = 0;
            if (sscanf(line + 4, "%31s %d %d", device, &quiet, &wants) != 3) return 2;
            memoria::TapDevice tap_device = memoria::TapDevice::kBusy;
            if (strcmp(device, "starting") == 0) tap_device = memoria::TapDevice::kStarting;
            else if (strcmp(device, "idle") == 0) tap_device = memoria::TapDevice::kIdle;
            else if (strcmp(device, "busy") == 0) tap_device = memoria::TapDevice::kBusy;
            else if (strcmp(device, "held") == 0) tap_device = memoria::TapDevice::kListeningClosed;
            else return 2;
            memoria::TapFacts facts;
            facts.device = tap_device;
            facts.in_quiet_hours = quiet != 0;
            facts.offline_wants_tap = wants != 0;
            printf("%s\n", memoria::TapWakeActionName(memoria::TapWakeActionFor(facts)));
        } else if (strncmp(line, "mic ", 4) == 0) {
            char phase[32];
            int open = 0;
            if (sscanf(line + 4, "%31s %d", phase, &open) != 2) return 2;
            memoria::PowerPhase p = memoria::PowerPhase::kWifiHold;
            if (strcmp(phase, "conversing") == 0) p = memoria::PowerPhase::kConversing;
            else if (strcmp(phase, "waking") == 0) p = memoria::PowerPhase::kWaking;
            else if (strcmp(phase, "deep_sleep") == 0) p = memoria::PowerPhase::kDeepSleep;
            printf("%s\n", memoria::MicGateActionName(
                memoria::MicGateTransition(open != 0, p)));
        } else if (strncmp(line, "quiet ", 6) == 0) {
            char start[8], end[8];
            int hour = 0, minute = 0;
            if (sscanf(line + 6, "%7s %7s %d:%d", start, end, &hour, &minute) != 4) return 2;
            memoria::QuietHoursWindow window;
            if (!memoria::ParseQuietHours(start, end, &window)) {
                printf("reject\n");
            } else {
                printf("%s\n", memoria::InQuietHours(
                    static_cast<uint16_t>(hour * 60 + minute), window) ? "in" : "out");
            }
        } else if (strncmp(line, "wanted ", 7) == 0) {
            char phase[32];
            if (sscanf(line + 7, "%31s", phase) != 1) return 2;
            memoria::PowerPhase p = memoria::PowerPhase::kWifiHold;
            if (strcmp(phase, "wifi_hold") == 0) p = memoria::PowerPhase::kWifiHold;
            else if (strcmp(phase, "deep_sleep") == 0) p = memoria::PowerPhase::kDeepSleep;
            else if (strcmp(phase, "conversing") == 0) p = memoria::PowerPhase::kConversing;
            else if (strcmp(phase, "waking") == 0) p = memoria::PowerPhase::kWaking;
            else if (strcmp(phase, "night") == 0) p = memoria::PowerPhase::kNightSoothing;
            else return 2;
            printf("%s\n", memoria::MicWantedOpen(p) ? "yes" : "no");
        } else if (strncmp(line, "debounce ", 9) == 0) {
            unsigned now = 0, last = 0, window = 250;
            if (sscanf(line + 9, "%u %u %u", &now, &last, &window) != 3) return 2;
            printf("%s\n", memoria::TapDebounceAllows(now, last, window) ? "yes" : "no");
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
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "power_tool"
        subprocess.run(
            [compiler, "-std=c++17", "-O0", "-I", str(MEMORIA_DIR), str(source), "-o", str(binary)],
            check=True,
        )
        yield binary


def _run(tool: pathlib.Path, *lines: str) -> list[str]:
    completed = subprocess.run(
        [str(tool)], input="\n".join(lines) + "\n", text=True, capture_output=True, check=True
    )
    return completed.stdout.splitlines()


def test_headers_stay_host_compilable() -> None:
    for name in ("memoria_power_policy.h", "memoria_quiet_hours.h", "memoria_mic_gate.h"):
        source = (MEMORIA_DIR / name).read_text(encoding="utf-8")
        for forbidden in ("esp_", "lvgl", "freertos", "settings.h"):
            assert forbidden not in source, name


def test_starting_offline_tap_opens_the_menu_not_the_hotspot(tool: pathlib.Path) -> None:
    assert _run(tool, "tap starting 0 1", "tap starting 0 0") == ["offline", "wifi_config"]


def test_quiet_idle_tap_soothes_and_skips_the_offline_menu(tool: pathlib.Path) -> None:
    assert _run(tool, "tap idle 1 1", "tap idle 1 0") == ["night", "night"]


def test_awake_idle_tap_opens_the_menu_only_when_offline_wants_it(tool: pathlib.Path) -> None:
    assert _run(tool, "tap idle 0 1", "tap idle 0 0") == ["offline", "start"]


def test_listening_closed_tap_resumes_without_a_new_wake(tool: pathlib.Path) -> None:
    assert _run(tool, "tap held 0 0", "tap held 1 1", "tap held 0 1") == [
        "resume",
        "resume",
        "resume",
    ]


def test_busy_tap_reaches_the_offline_menu_only_when_it_wants_the_tap(tool: pathlib.Path) -> None:
    assert _run(tool, "tap busy 0 0", "tap busy 0 1", "tap busy 1 0") == [
        "ignore",
        "offline",
        "ignore",
    ]


def test_idle_keep_awake_park_does_not_want_the_mic_open(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "wanted wifi_hold",
        "wanted deep_sleep",
        "wanted night",
        "wanted waking",
        "wanted conversing",
        "mic wifi_hold 0",
        "mic wifi_hold 1",
    ) == ["no", "no", "no", "yes", "yes", "noop", "close"]


def test_mic_only_opens_while_waking_or_conversing(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "mic wifi_hold 0",
        "mic wifi_hold 1",
        "mic conversing 0",
        "mic conversing 1",
        "mic deep_sleep 1",
        "mic waking 0",
    ) == ["noop", "close", "open", "noop", "close", "open"]


def test_quiet_hours_match_agent_semantics(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "quiet 21:30 06:30 22:00",
        "quiet 21:30 06:30 06:29",
        "quiet 21:30 06:30 06:30",
        "quiet 12:00 14:00 13:00",
        "quiet 12:00 12:00 13:00",
        "quiet 25:00 07:00 01:00",
    ) == ["in", "in", "out", "in", "out", "reject"]


def test_tap_debounce_is_wrap_safe(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "debounce 1000 0 250",
        "debounce 1000 900 250",
        "debounce 1000 750 250",
        "debounce 50 4294967096 250",
    ) == ["yes", "no", "yes", "yes"]
