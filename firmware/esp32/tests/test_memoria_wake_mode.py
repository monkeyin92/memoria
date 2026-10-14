"""Host-side checks for tap-only wake mode (keyword wake removed)."""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
PATCH_DIR = FIRMWARE_ROOT / "overlay" / "patches"
HEADER = MEMORIA_DIR / "memoria_wake_mode.h"

HARNESS = r"""
#include "memoria_wake_mode.h"
#include <cstdio>
#include <cstring>
int main() {
    char line[128];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0' || line[0] == '#') continue;
        if (strncmp(line, "parse ", 6) == 0) {
            memoria::WakeMode mode;
            if (!memoria::ParseWakeMode(line + 6, &mode)) {
                printf("reject\n");
            } else {
                printf("%s keyword=%d tap=%d\n", memoria::WakeModeName(mode),
                       memoria::KeywordWakeEnabled(mode) ? 1 : 0,
                       memoria::TapWakeEnabled(mode) ? 1 : 0);
            }
        } else if (strncmp(line, "null", 4) == 0) {
            memoria::WakeMode mode;
            printf("%s\n", memoria::ParseWakeMode(nullptr, &mode) ? "accept" : "reject");
        } else if (strncmp(line, "tap ", 4) == 0) {
            char name[64];
            int idle = 0;
            if (sscanf(line + 4, "%63s %d", name, &idle) != 2) return 2;
            memoria::WakeMode mode;
            if (!memoria::ParseWakeMode(name, &mode)) return 3;
            printf("%s\n", memoria::TapActionFor(mode, idle != 0) ==
                                   memoria::TapAction::kStartConversation
                               ? "start"
                               : "ignore");
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
        binary = pathlib.Path(tmp) / "wake_mode_tool"
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


def _function(source: str, signature: str) -> str:
    start = source.index(signature)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace : index + 1]
    raise AssertionError(f"unterminated function {signature}")


def test_header_stays_free_of_esp_dependencies() -> None:
    source = HEADER.read_text(encoding="utf-8")
    for forbidden in ("esp_", "lvgl", "lv_", "freertos", "nvs", "settings.h"):
        assert forbidden not in source


def test_all_legacy_modes_coerce_to_tap_only(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "parse keyword",
        "parse button",
        "parse button_or_keyword",
    ) == [
        "button keyword=0 tap=1",
        "button keyword=0 tap=1",
        "button keyword=0 tap=1",
    ]


def test_unknown_text_is_rejected(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "parse ",
        "parse none",
        "parse Keyword",
        "null",
    ) == ["reject"] * 4


def test_a_tap_only_wakes_an_idle_device(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "tap button 1",
        "tap button 0",
        "tap keyword 1",
        "tap button_or_keyword 0",
    ) == ["start", "ignore", "start", "ignore"]


def test_deep_sleep_reset_arms_offline_reconnect_before_the_battery_tick() -> None:
    source = (BOARD_DIR / "memoria_esp_vocat.cc").read_text(encoding="utf-8")
    ctor = source[source.index("MemoriaEspVocat() :") : source.index("virtual AudioCodec*")]
    assert "ESP_RST_DEEPSLEEP" in ctor
    assert ctor.count("OnDeepSleepWake(") == 1
    assert source.count("OnDeepSleepWake(") == 1
    assert ctor.index("OnDeepSleepWake(") < ctor.index("InitializeCharge()")
    assert "network_was_online_" not in source
    assert "OnNetworkSample(" in source
    handler = _function(source, "void HandleScreenTouchRelease(std::uint32_t hold_ms)")
    assert "OnDeepSleepWake(" not in handler
    assert handler.index("TapWakeActionFor") < handler.index("EnterWifiConfigMode()")
    overlay = FIRMWARE_ROOT / "overlay"
    hits = [
        path.relative_to(overlay).as_posix()
        for path in overlay.rglob("*")
        if path.suffix in {".h", ".cc", ".c", ".cpp", ".py", ".patch"}
        and "from_menu_sleep" in path.read_text(encoding="utf-8")
    ]
    assert hits == []


def test_board_tap_handler_uses_power_policy_and_never_stops() -> None:
    source = (BOARD_DIR / "memoria_esp_vocat.cc").read_text(encoding="utf-8")
    handler = _function(source, "void HandleScreenTouchRelease(std::uint32_t hold_ms)")
    assert "TapWakeActionFor" in handler
    assert 'PlayLocalAssetClip("wo_zai_ne.ogg")' in handler
    assert 'app.WakeWordInvoke("screen_tap")' in handler
    assert "offline_menu_" in source
    assert "HoldDurationMsOnRelease" in source
    for forbidden in ("StopListening", "AbortSpeaking", "ToggleChatState", "CloseAudioChannel"):
        assert forbidden not in handler, f"a tap must not call {forbidden}"


def test_boot_key_keeps_its_hard_stop_and_pairing_roles() -> None:
    source = (BOARD_DIR / "memoria_esp_vocat.cc").read_text(encoding="utf-8")
    buttons = _function(source, "void InitializeButtons()")
    assert "app.AbortSpeaking(kAbortReasonNone)" in buttons
    assert "app.StopListening()" in buttons
    assert "app.ToggleChatState()" in buttons


def test_application_patch_keeps_idle_wake_word_off() -> None:
    patch = (PATCH_DIR / "0038-memoria-tap-wake-power.patch").read_text(encoding="utf-8")
    assert "EnableWakeWordDetection(false)" in patch
    assert "memoria_power_policy.cc" in patch
    power_cc = (MEMORIA_DIR / "memoria_power_policy.cc").read_text(encoding="utf-8")
    assert "MemoriaMic" in power_cc


def test_display_profile_poll_applies_quiet_hours() -> None:
    protocol = (MEMORIA_DIR / "memoria_protocol.cc").read_text(encoding="utf-8")
    poll = _function(protocol, "void MemoriaProtocol::DisplayProfileTask(void* context)")
    assert "ApplyQuietHours" in poll
    client = (MEMORIA_DIR / "memoria_activation_client.cc").read_text(encoding="utf-8")
    assert '"quiet_hours"' in client


def test_idle_screen_goes_dark_after_fifteen_seconds() -> None:
    display = (BOARD_DIR / "memoria_mascot_display.cc").read_text(encoding="utf-8")
    header = (BOARD_DIR / "memoria_mascot_display.h").read_text(encoding="utf-8")
    assert "kIdleScreenOffMs = 15 * 1000" in header
    assert "on_screen_off_" in header
    update = _function(display, "void MemoriaMascotDisplay::UpdateIdleScreen(")
    assert "on_screen_off_" in update
    assert "backlight_->SetBrightness(0)" in update


def test_build_number_moved_for_tap_wake_release() -> None:
    release = (MEMORIA_DIR / "memoria_firmware_release.h").read_text(encoding="utf-8")
    build = int(re.search(r"#define MEMORIA_FIRMWARE_BUILD (\d+)", release).group(1))
    assert build >= 25
