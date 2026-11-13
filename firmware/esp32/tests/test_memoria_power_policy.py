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
            int quiet = 0, wants = 0, online = 0;
            int n = sscanf(line + 4, "%31s %d %d %d", device, &quiet, &wants, &online);
            if (n < 3) return 2;
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
            facts.online = (n >= 4 && online != 0);
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


@pytest.mark.parametrize("online", [0, 1])
@pytest.mark.parametrize("quiet", [0, 1])
@pytest.mark.parametrize("wants", [0, 1])
def test_busy_tap_has_no_action(tool, online, quiet, wants) -> None:
    assert _run(tool, f"tap busy {quiet} {wants} {online}") == ["ignore"]


def test_online_standby_tap_wakes_or_resumes(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "tap idle 0 0 1",
        "tap idle 0 1 1",
        "tap held 0 0 1",
        "tap held 1 1 1",
    ) == ["start", "start", "resume", "resume"]


def test_online_startup_is_ignored_and_quiet_idle_keeps_night_policy(tool) -> None:
    assert _run(
        tool, "tap starting 0 1 1", "tap starting 1 0 1",
        "tap idle 1 0 1", "tap idle 1 1 1",
    ) == ["ignore", "ignore", "night", "night"]


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


BOARD = FIRMWARE_ROOT / "overlay/files/main/boards/memoria/esp-vocat/memoria_esp_vocat.cc"


def _board_method(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == "{") - (source[end] == "}")
        end += 1
    return source[start:end]


@pytest.fixture(scope="module")
def touch_tool():
    """Compile the real board entry, classifier and dispatch with fake peripherals."""
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    board = BOARD.read_text(encoding="utf-8")
    methods = "\n".join(_board_method(board, signature) for signature in (
        "memoria::TapDevice TapDeviceFor(DeviceState state) const",
        "void HandleScreenTouchRelease(std::uint32_t hold_ms)",
        "void DispatchTapWakeAction(",
    ))
    harness = r'''
#include "memoria_power_policy.h"
#include "memoria_offline_menu.h"
#include <cstdlib>
#include <cstdio>
#define ESP_LOGI(...) ((void)0)
#define ESP_OK 0
int cues=0, wakes=0, resumes=0, menus=0, gestures=0, configs=0, sleeps=0;
bool online=false, quiet=false, playing=false;
struct Audio { bool IsPlaybackIdle() { return !playing; } };
enum DeviceState { kDeviceStateStarting, kDeviceStateIdle, kDeviceStateWifiConfiguring,
                   kDeviceStateListening, kDeviceStateSpeaking, kDeviceStateConnecting };
DeviceState current;
int64_t esp_timer_get_time() { return 10000000; }
int BeijingMinuteNowOrZero() { return 0; }
struct Display { void ShowNotification(const char*, int) {} };
struct Application {
    static Application& GetInstance() { static Application app; return app; }
    DeviceState GetDeviceState() { return current; }
    Audio& GetAudioService() { static Audio audio; return audio; }
    template<class F> void Schedule(F f) { f(); }
    void WakeWordInvoke(const char*) { ++wakes; }
};
namespace memoria {
bool ActivationNetworkErrorPageVisible() { return false; }
struct MemoriaBootstrap {
    static MemoriaBootstrap& GetInstance() { static MemoriaBootstrap b; return b; }
    bool active() { return false; }
    int Refresh(Display*) { return ESP_OK; }
};
PowerPolicyRegistry& PowerPolicyRegistry::GetInstance() { static PowerPolicyRegistry p; return p; }
bool PowerPolicyRegistry::NoteTap(std::uint32_t) { return true; }
bool PowerPolicyRegistry::in_quiet_hours(std::uint16_t) const { return quiet; }
void PowerPolicyRegistry::SetPhase(PowerPhase) {}
void SendListeningResume() { ++resumes; }
}
struct FakeMenu {
    memoria::OfflineMode mode_ = memoria::OfflineMode::kInactive;
    memoria::OfflineMode mode() const { return mode_; }
    bool WantsTap(bool net, bool idle) { return mode_ != memoria::OfflineMode::kInactive || (!net && idle); }
    memoria::OfflineMenuOutput OnTap(unsigned, unsigned, bool, bool) { ++menus; return {}; }
    void OnNetworkSample(unsigned, bool, bool) {}
};
struct Board {
    std::atomic<bool> listening_capture_closed_{false};
    static constexpr unsigned kWifiHoldNotArmed = 0;
    FakeMenu offline_menu_;
    std::atomic<unsigned> offline_prompt_generation_{0};
    Display* display_ = nullptr;
    int64_t last_qr_refresh_us_ = 0;
    void MuteImuForTouch() {}
    static bool NetworkLooksOnline() { return online; }
    Display* GetDisplay() { static Display d; return &d; }
    void OnScreenTapGesture(unsigned, bool) { ++gestures; }
    void EnterWifiConfigMode() { ++configs; }
    void EnterTapDeepSleep() { ++sleeps; }
    void PlayLocalAssetClip(const char*) { ++cues; }
    void ApplyOfflineMenuOutput(const memoria::OfflineMenuOutput&) {}
    void SyncOfflineMenuScreen() {}
''' + methods + r'''
};
int main(int argc, char** argv) {
    if (argc != 6 && argc != 7) return 2;
    playing = argc == 7 && atoi(argv[6]);
    current = static_cast<DeviceState>(atoi(argv[1]));
    online = atoi(argv[2]);
    Board b;
    b.listening_capture_closed_.store(atoi(argv[3]));
    b.offline_menu_.mode_ = static_cast<memoria::OfflineMode>(atoi(argv[4]));
    quiet = atoi(argv[5]);
    b.HandleScreenTouchRelease(50);
    printf("%d %d %d %d %d %d %d\n", wakes, resumes, cues, menus, gestures, configs, sleeps);
}
'''
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "touch.cc"
        source.write_text(harness, encoding="utf-8")
        binary = pathlib.Path(tmp) / "touch"
        subprocess.run([compiler, "-std=c++20", "-I", str(MEMORIA_DIR), str(source), "-o", str(binary)], check=True)
        yield binary


@pytest.mark.parametrize("state,online,closed,mode,quiet,expected", [
    pytest.param(1, 1, 0, 0, 0, "1 0 1 0 0 0 0", id="online_idle_starts_with_cue"),
    pytest.param(3, 1, 1, 0, 0, "0 1 1 0 0 0 0", id="online_closed_listening_resumes"),
    pytest.param(3, 0, 1, 0, 0, "0 1 1 0 0 0 0", id="offline_closed_listening_resumes"),
    pytest.param(1, 0, 0, 0, 0, "0 0 0 1 0 0 0", id="offline_idle_opens_menu"),
    pytest.param(0, 0, 0, 0, 0, "0 0 0 0 0 1 0", id="offline_starting_wifi_config"),
    pytest.param(0, 1, 0, 0, 0, "0 0 0 0 0 0 0", id="online_starting_ignored"),
    pytest.param(1, 1, 0, 0, 1, "0 0 0 0 0 0 1", id="quiet_online_idle_soothing"),
    pytest.param(1, 0, 0, 0, 1, "0 0 0 0 0 0 1", id="quiet_offline_idle_soothing"),
])
def test_touch_entry_dispatches_standby_actions(touch_tool, state, online, closed, mode, quiet, expected):
    result = subprocess.run([str(touch_tool), *map(str, (state, online, closed, mode, quiet))],
                            text=True, capture_output=True, check=True)
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("online", [0, 1])
@pytest.mark.parametrize("state,mode,closed", [
    (state, mode, closed)
    for state, mode in [(4, 0), (4, 4), (5, 0), (5, 4), (3, 0), (1, 5), (1, 6)]
    for closed in ([0] if state == 3 else [0, 1])
])
def test_touch_during_playback_or_busy_has_no_effect(touch_tool, online, state, mode, closed):
    result = subprocess.run([str(touch_tool), *map(str, (state, online, closed, mode, 0))],
                            text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "0 0 0 0 0 0 0"


@pytest.mark.parametrize("online", [0, 1])
@pytest.mark.parametrize("state,closed,mode", [(1, 0, 0), (1, 0, 4), (3, 1, 0)])
def test_touch_ignores_queued_or_active_audio_even_in_standby(touch_tool, online, state, closed, mode):
    result = subprocess.run([str(touch_tool), *map(str, (state, online, closed, mode, 0, 1))],
                            text=True, capture_output=True, check=True)
    assert result.stdout.strip() == "0 0 0 0 0 0 0"
