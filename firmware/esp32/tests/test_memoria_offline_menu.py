"""Host checks for the offline story menu state machine and asset pack."""

from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
BOARD_ASSETS = (
    FIRMWARE_ROOT
    / "overlay"
    / "files"
    / "main"
    / "boards"
    / "memoria"
    / "esp-vocat"
    / "assets"
)
BUILD_SCRIPT = FIRMWARE_ROOT / "scripts" / "build_offline_stories.py"

HARNESS = r"""
#include "memoria_offline_menu.h"

#include <cstdio>
#include <cstring>

static void print_out(const memoria::OfflineMenuOutput& out) {
    printf("intercept=%d mode_clips=%zu", out.intercept ? 1 : 0, out.clip_count);
    for (size_t i = 0; i < out.clip_count; ++i) {
        printf(" %s", out.clips[i]);
    }
    if (out.start_story) printf(" start_story=%u", out.story_index);
    if (out.stop_story) printf(" stop_story");
    if (out.enter_deep_sleep) printf(" deep_sleep");
    if (out.retry_network) printf(" retry");
    if (out.stop_retry_network) printf(" stop_retry");
    printf("\n");
}

int main() {
    memoria::OfflineMenuMachine menu;
    char line[240];
    while (fgets(line, sizeof(line), stdin) != nullptr) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0' || line[0] == '#') continue;
        if (strcmp(line, "reset") == 0) {
            menu.Reset();
            printf("ok mode=%s item=%u\n", memoria::OfflineModeName(menu.mode()), menu.menu_item());
        } else if (strncmp(line, "wake ", 5) == 0) {
            unsigned now = 0;
            int online = 0;
            if (sscanf(line + 5, "%u %d", &now, &online) != 2) return 2;
            print_out(menu.OnDeepSleepWake(now, online != 0));
            printf("mode=%s\n", memoria::OfflineModeName(menu.mode()));
        } else if (strncmp(line, "down ", 5) == 0) {
            unsigned now = 0;
            int convo = 0;
            if (sscanf(line + 5, "%u %d", &now, &convo) != 2) return 2;
            print_out(menu.OnNetworkDown(now, convo != 0));
            printf("mode=%s\n", memoria::OfflineModeName(menu.mode()));
        } else if (strncmp(line, "up ", 3) == 0) {
            unsigned now = 0;
            if (sscanf(line + 3, "%u", &now) != 1) return 2;
            print_out(menu.OnNetworkUp(now));
            printf("mode=%s\n", memoria::OfflineModeName(menu.mode()));
        } else if (strncmp(line, "timer ", 6) == 0) {
            unsigned now = 0;
            if (sscanf(line + 6, "%u", &now) != 1) return 2;
            print_out(menu.OnTimer(now));
            printf("mode=%s item=%u\n", memoria::OfflineModeName(menu.mode()), menu.menu_item());
        } else if (strncmp(line, "tap ", 4) == 0) {
            unsigned now = 0, hold = 0;
            int online = 0, idle = 0, convo = 0;
            if (sscanf(line + 4, "%u %u %d %d %d", &now, &hold, &online, &idle, &convo) != 5) return 2;
            print_out(menu.OnTap(now, hold, online != 0, idle != 0, convo != 0));
            printf("mode=%s item=%u\n", memoria::OfflineModeName(menu.mode()), menu.menu_item());
        } else if (strncmp(line, "done ", 5) == 0) {
            unsigned now = 0;
            if (sscanf(line + 5, "%u", &now) != 1) return 2;
            print_out(menu.OnStoryFinished(now));
            printf("mode=%s item=%u\n", memoria::OfflineModeName(menu.mode()), menu.menu_item());
        } else if (strncmp(line, "note ", 5) == 0) {
            unsigned now = 0;
            if (sscanf(line + 5, "%u", &now) != 1) return 2;
            menu.NoteMenuActivity(now);
            printf("ok\n");
        } else if (strncmp(line, "long ", 5) == 0) {
            unsigned hold = 0;
            if (sscanf(line + 5, "%u", &hold) != 1) return 2;
            printf("%s\n", memoria::OfflineIsLongPress(hold) ? "long" : "tap");
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
        binary = pathlib.Path(tmp) / "offline_menu_tool"
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


def test_header_stays_host_compilable() -> None:
    source = (MEMORIA_DIR / "memoria_offline_menu.h").read_text(encoding="utf-8")
    for forbidden in ("esp_", "lvgl", "freertos", "settings.h", "wifi_"):
        assert forbidden not in source


def test_long_press_threshold(tool: pathlib.Path) -> None:
    assert _run(tool, "long 799", "long 800", "long 801") == ["tap", "long", "long"]


def test_idle_offline_tap_opens_wake_then_menu(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "tap 1000 100 0 1 0")
    assert lines[0].startswith("ok mode=inactive")
    assert "offline-wake-no-network-v1" in lines[1]
    assert "offline-wake-ask-parent-v1" in lines[1]
    assert "offline-wake-story-hint-v2" in lines[1]
    assert "offline-menu-intro-v2" in lines[1]
    assert "offline-menu-item-01-v2" in lines[1]
    assert "offline-menu-choose-v2" in lines[1]
    assert lines[2] == "mode=menu item=1"


def test_menu_tap_cycles_items(tool: pathlib.Path) -> None:
    lines = _run(
        tool,
        "reset",
        "tap 1000 100 0 1 0",
        "tap 2000 100 0 1 0",
        "tap 3000 100 0 1 0",
        "tap 4000 100 0 1 0",
    )
    assert "offline-menu-item-02-v2" in lines[3]
    assert lines[4] == "mode=menu item=2"
    assert "offline-menu-item-03-v2" in lines[5]
    assert lines[6] == "mode=menu item=3"
    assert "offline-menu-item-01-v2" in lines[7]
    assert lines[8] == "mode=menu item=1"


def test_menu_long_press_starts_story(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "tap 1000 100 0 1 0", "tap 2000 800 0 1 0")
    assert "offline-menu-start-v1" in lines[3]
    assert "start_story=1" in lines[3]
    assert lines[4] == "mode=playing item=1"


def test_playing_tap_ignored_long_press_returns_to_menu(tool: pathlib.Path) -> None:
    lines = _run(
        tool,
        "reset",
        "tap 1000 100 0 1 0",
        "tap 2000 800 0 1 0",
        "tap 3000 100 0 1 0",
        "tap 4000 900 0 1 0",
    )
    assert lines[5].startswith("intercept=1 mode_clips=0")
    assert lines[6] == "mode=playing item=1"
    assert "stop_story" in lines[7]
    assert "offline-menu-item-01-v2" in lines[7]
    assert "offline-menu-choose-v2" in lines[7]
    assert lines[8] == "mode=menu item=1"


def test_story_finished_offline_plays_end(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "tap 1000 100 0 1 0", "tap 2000 800 0 1 0", "done 5000")
    assert "offline-menu-end-v1" in lines[5]
    assert lines[6] == "mode=menu item=1"


def test_mid_drop_then_reconnect_timeout_opens_menu(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "down 1000 1", "tap 2000 100 0 0 1", "timer 22000")
    assert "offline-mid-drop-v2" in lines[1]
    assert "offline-mid-ask-parent-v1" in lines[1]
    assert "offline-mid-retry-v1" in lines[1]
    assert lines[2] == "mode=mid_reconnecting"
    assert "offline-reconnecting-v1" in lines[3]
    assert "offline-menu-intro-v2" in lines[5]
    assert "offline-wake-no-network-v1" not in lines[5]
    assert lines[6].startswith("mode=menu item=1")


def test_idle_drop_is_silent_next_tap_opens_wake_menu(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "down 1000 0", "tap 2000 100 0 1 0")
    assert lines[1].startswith("intercept=1 mode_clips=0")
    assert "retry" in lines[1]
    assert lines[2] == "mode=standby_offline"
    assert "offline-wake-no-network-v1" in lines[3]
    assert lines[4] == "mode=menu item=1"


def test_deep_sleep_wake_reconnect_then_menu(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "wake 1000 0", "tap 2000 100 0 1 0", "timer 22000")
    assert lines[1].startswith("intercept=1 mode_clips=0")
    assert lines[2] == "mode=awake_reconnecting"
    assert "offline-reconnecting-v1" in lines[3]
    assert "offline-wake-no-network-v1" in lines[5]
    assert lines[6].startswith("mode=menu item=1")


def test_network_restored_while_idle_menu(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "tap 1000 100 0 1 0", "up 5000")
    assert "online-restored-v1" in lines[3]
    assert "online-restored-chat-v1" in lines[3]
    assert lines[4] == "mode=inactive"


def test_network_restored_during_story_waits_for_end(tool: pathlib.Path) -> None:
    lines = _run(
        tool,
        "reset",
        "tap 1000 100 0 1 0",
        "tap 2000 800 0 1 0",
        "up 3000",
        "done 12000",
    )
    assert lines[6] == "mode=pending_online_after_story"
    assert "online-restored-in-story-v2" in lines[7]
    assert lines[8] == "mode=inactive item=1"


def test_menu_idle_sleep_stops_retry(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "tap 1000 100 0 1 0", "note 2000", "timer 33000")
    assert "offline-menu-idle-v1" in lines[4]
    assert "deep_sleep" in lines[4]
    assert "stop_retry" in lines[4]
    assert lines[5].startswith("mode=inactive")


def test_online_idle_tap_is_not_intercepted(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "tap 1000 100 1 1 0")
    assert lines[1].startswith("intercept=0 mode_clips=0")
    assert lines[2] == "mode=inactive item=1"


def test_build_offline_stories_check_passes() -> None:
    completed = subprocess.run(
        ["python3", str(BUILD_SCRIPT), "--check-only"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "free_margin_bytes=" in completed.stdout
    free = int(
        next(line.split("=", 1)[1] for line in completed.stdout.splitlines() if line.startswith("free_margin_bytes="))
    )
    assert free >= 512 * 1024


def test_offline_line_manifest_matches_pack() -> None:
    manifest = json.loads((BOARD_ASSETS / "offline_lines" / "index.json").read_text(encoding="utf-8"))
    ids = [entry["id"] for entry in manifest["lines"]]
    assert len(ids) == 20
    assert "offline-chat-request-v1" not in ids
    assert ids[0] == "offline-wake-no-network-v1"
    assert ids[15] == "offline-menu-item-03-v2"
    assert ids[19] == "offline-menu-idle-v1"
    for entry in manifest["lines"]:
        assert entry["placeholder"] is True
        filename = entry["file"]
        assert len(filename) <= 32
        assert (BOARD_ASSETS / "offline_lines" / filename).is_file()


def test_story_03_title_renamed() -> None:
    manifest = json.loads((BOARD_ASSETS / "stories" / "index.json").read_text(encoding="utf-8"))
    by_id = {s["id"]: s["title"] for s in manifest["stories"]}
    assert by_id["story_03"] == "大熊和森林里的悄悄话"
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "大熊和森林里的悄悄话" in script
    assert "一只会听的熊" not in script
