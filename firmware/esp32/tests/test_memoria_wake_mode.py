"""Host-side checks for the wake mode and the idle screen-off (build 18).

`memoria_wake_mode.h` has no ESP-IDF dependency, so the exact parse, enable and
tap-action tables compile on the host. The wiring (display-profile poll, the
board's tap handler, the Application patch, the mascot display loop) is checked
as source text, because those files only build inside ESP-IDF.

Product rules pinned here (user, 2026-10-01): the phone can turn the wake word
and the screen tap on independently but never both off; with the wake word as
the only way in a tap does nothing at all; with the screen as the only way in
the wake word is off and a tap only wakes, never stops the robot or sends it
back to standby; with no conversation the screen goes dark.
"""

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
        if (line[0] == '\0' || line[0] == '#') {
            continue;
        }
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
            if (sscanf(line + 4, "%63s %d", name, &idle) != 2) {
                return 2;
            }
            memoria::WakeMode mode;
            if (!memoria::ParseWakeMode(name, &mode)) {
                return 3;
            }
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
    compiler = shutil.which("clang++") or shutil.which("g++")
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
    """The body of the first function whose definition starts with `signature`."""
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


def test_wire_modes_map_to_what_the_phone_shows(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "parse keyword",
        "parse button",
        "parse button_or_keyword",
    ) == [
        "keyword keyword=1 tap=0",
        "button keyword=0 tap=1",
        "button_or_keyword keyword=1 tap=1",
    ]


def test_no_mode_turns_both_ways_off(tool: pathlib.Path) -> None:
    lines = _run(tool, "parse keyword", "parse button", "parse button_or_keyword")
    for line in lines:
        assert "keyword=0 tap=0" not in line


def test_unknown_text_is_rejected_so_the_device_keeps_its_mode(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "parse ",
        "parse none",
        "parse Keyword",
        "parse keyword ",
        "parse button_or_keywords",
        "null",
    ) == ["reject"] * 6


def test_a_tap_only_wakes_an_idle_device_when_the_screen_is_enabled(tool: pathlib.Path) -> None:
    assert _run(
        tool,
        "tap keyword 1",
        "tap keyword 0",
        "tap button 1",
        "tap button 0",
        "tap button_or_keyword 1",
        "tap button_or_keyword 0",
    ) == ["ignore", "ignore", "start", "ignore", "start", "ignore"]


def test_board_tap_handler_only_wakes_and_never_stops_or_standbys() -> None:
    source = (BOARD_DIR / "memoria_esp_vocat.cc").read_text(encoding="utf-8")
    handler = _function(source, "void HandleScreenTouchRelease()")
    # Pairing and start-up come first and keep their behaviour.
    assert handler.index("MemoriaBootstrap") < handler.index("TapActionFor")
    assert handler.index("kDeviceStateStarting") < handler.index("TapActionFor")
    # From there the phone's mode decides, and a tap can only wake.
    assert "WakeModeRegistry::GetInstance().mode()" in handler
    assert "TapActionFor(mode, state == kDeviceStateIdle)" in handler
    assert 'app.WakeWordInvoke("screen_tap")' in handler
    for forbidden in ("StopListening", "AbortSpeaking", "ToggleChatState", "CloseAudioChannel"):
        assert forbidden not in handler, f"a tap must not call {forbidden}"


def test_boot_key_keeps_its_hard_stop_and_pairing_roles() -> None:
    source = (BOARD_DIR / "memoria_esp_vocat.cc").read_text(encoding="utf-8")
    buttons = _function(source, "void InitializeButtons()")
    assert "app.AbortSpeaking(kAbortReasonNone)" in buttons
    assert "app.StopListening()" in buttons
    assert "app.ToggleChatState()" in buttons


def test_application_patch_arms_the_wake_word_from_the_mode() -> None:
    patch = (PATCH_DIR / "0032-memoria-wake-mode.patch").read_text(encoding="utf-8")
    # Idle no longer enables the wake word unconditionally.
    assert "-            audio_service_.EnableWakeWordDetection(true);\n+            ArmIdleWakeWord();" in patch
    assert "keyword_enabled()" in patch
    # A hit queued before the phone turned the wake word off is dropped.
    assert "Ignoring wake word: wake mode is tap only" in patch
    # The persisted mode is loaded next to the wake word selection.
    assert "WakeModeRegistry::GetInstance().LoadFromNvs();" in patch
    assert '"memoria/memoria_wake_mode.cc"' in patch
    assert "void RefreshWakeMode();" in patch


def test_display_profile_poll_applies_the_mode_on_every_answer() -> None:
    protocol = (MEMORIA_DIR / "memoria_protocol.cc").read_text(encoding="utf-8")
    poll = _function(protocol, "void MemoriaProtocol::DisplayProfileTask(void* context)")
    apply_at = poll.index("WakeModeRegistry::GetInstance().Apply(profile.wake_mode)")
    # Not gated on the companion's display_version: that only moves on a new mascot.
    assert apply_at < poll.index("profile.display_version != applied_version")
    assert "result == ESP_OK && !profile.wake_mode.empty()" in poll
    assert "Application::GetInstance().RefreshWakeMode()" in poll


def test_activation_client_reads_the_optional_wake_mode() -> None:
    client = (MEMORIA_DIR / "memoria_activation_client.cc").read_text(encoding="utf-8")
    fetch = _function(client, "esp_err_t MemoriaActivationClient::FetchDisplayProfile(")
    assert 'cJSON_GetObjectItemCaseSensitive(root.value, "wake_mode")' in fetch
    # Optional: the required fields (and the error for missing ones) are unchanged.
    assert 'RequiredString(root.value, "companion_id"' in fetch
    assert 'RequiredString(root.value, "wake_mode"' not in fetch
    header = (MEMORIA_DIR / "memoria_activation_client.h").read_text(encoding="utf-8")
    assert "std::string wake_mode;" in header


def test_registry_ignores_unknown_modes_and_writes_only_on_change() -> None:
    source = (MEMORIA_DIR / "memoria_wake_mode.cc").read_text(encoding="utf-8")
    apply_body = _function(source, "bool WakeModeRegistry::Apply(")
    assert apply_body.index("ParseWakeMode") < apply_body.index("exchange")
    assert apply_body.index("previous == next") < apply_body.index("SetString")


def test_idle_screen_goes_dark_and_nothing_is_rendered_while_dark() -> None:
    display = (BOARD_DIR / "memoria_mascot_display.cc").read_text(encoding="utf-8")
    header = (BOARD_DIR / "memoria_mascot_display.h").read_text(encoding="utf-8")
    update = _function(display, "void MemoriaMascotDisplay::UpdateIdleScreen(")
    assert "phase != memoria::ScenePhase::kIdle" in update
    assert "backlight_->SetBrightness(0)" in update
    assert "backlight_->RestoreBrightness()" in update
    assert re.search(r"kIdleScreenOffMs = \d+ \* 1000;", header)
    loop = _function(display, "void MemoriaMascotDisplay::AnimationLoop()")
    # The phase is decided before anything is drawn, and a dark panel is not rendered.
    assert loop.index("UpdateIdleScreen(phase, frame_now)") < loop.index("scene_->Render(")
    assert "screen_off_ ? 0 : scene_->Render(" in loop
    # The 3-minute doze dim must not turn a dark panel back on.
    assert "!screen_off_ && scene_->sleeping() != dimmed_" in loop
    # Anything but idle lights the panel: only kIdle is a reason to be dark.
    assert update.count("ScenePhase::kIdle") == 1


def test_build_number_moved_past_the_last_flashed_image() -> None:
    release = (MEMORIA_DIR / "memoria_firmware_release.h").read_text(encoding="utf-8")
    build = int(re.search(r"#define MEMORIA_FIRMWARE_BUILD (\d+)", release).group(1))
    assert build >= 18
