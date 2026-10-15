"""Host checks for explicit AIGC label policy, geometry, and notice clip wiring."""

from __future__ import annotations

import pathlib
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
BOARD_CC = (
    FIRMWARE_ROOT
    / "overlay"
    / "files"
    / "main"
    / "boards"
    / "memoria"
    / "esp-vocat"
    / "memoria_esp_vocat.cc"
)
DISPLAY_CC = (
    FIRMWARE_ROOT
    / "overlay"
    / "files"
    / "main"
    / "boards"
    / "memoria"
    / "esp-vocat"
    / "memoria_mascot_display.cc"
)
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"

HARNESS = r"""
#include "memoria_aigc_label.h"

#include <cstdio>

int main() {
    using memoria::AigcExplicitMode;
    using memoria::AigcExplicitModeFor;
    using memoria::AigcBadgeVisible;
    using memoria::AigcTurnToneWanted;
    using memoria::AigcBadgeCornersInsideSafeCircle;
    using memoria::AigcBadgeLayoutInsideSafeCircle;
    const auto idle = AigcExplicitModeFor(false, true);
    const auto badge = AigcExplicitModeFor(true, true);
    const auto tone_off = AigcExplicitModeFor(true, false);
    const auto offline = AigcExplicitModeFor(true, true);  // stuck → offline menu
    std::printf("idle=%d badge=%d tone_off=%d flag=%d story=%s bedtime=%s\n",
                static_cast<int>(idle), static_cast<int>(badge),
                static_cast<int>(tone_off),
                memoria::kAigcTurnToneEnabled ? 1 : 0,
                memoria::kAigcNoticeStoryFile, memoria::kAigcNoticeBedtimeFile);
    std::printf("badge_vis=%d tone_want=%d offline_vis=%d layout_ok=%d\n",
                AigcBadgeVisible(badge) ? 1 : 0,
                AigcTurnToneWanted(tone_off) ? 1 : 0,
                AigcBadgeVisible(offline) ? 1 : 0,
                AigcBadgeLayoutInsideSafeCircle() ? 1 : 0);
    std::printf("geom=%d,%d,%d,%d font=%d\n",
                memoria::kAigcBadgeX, memoria::kAigcBadgeY,
                memoria::kAigcBadgeW, memoria::kAigcBadgeH,
                memoria::kAigcBadgeFontPx);
    // Every corner must sit inside radius-8 (compile-time assert + runtime).
    const bool corners = AigcBadgeCornersInsideSafeCircle(
        memoria::kAigcBadgeX, memoria::kAigcBadgeY,
        memoria::kAigcBadgeW, memoria::kAigcBadgeH);
    std::printf("corners_ok=%d\n", corners ? 1 : 0);
    // A rect that clips the round panel must fail.
    const bool clipped = AigcBadgeCornersInsideSafeCircle(280, 36, 40, 20);
    std::printf("clipped_rejected=%d\n", clipped ? 0 : 1);
    return corners && !clipped ? 0 : 1;
}
"""


@pytest.fixture(scope="module")
def tool() -> pathlib.Path:
    with tempfile.TemporaryDirectory() as tmp:
        src = pathlib.Path(tmp) / "aigc_label_tool.cc"
        out = pathlib.Path(tmp) / "aigc_label_tool"
        src.write_text(HARNESS, encoding="utf-8")
        subprocess.run(
            ["g++", "-std=c++17", "-I", str(MEMORIA_DIR), "-o", str(out), str(src)],
            check=True,
        )
        yield out


def test_badge_is_primary_and_turn_tone_defaults_off(tool: pathlib.Path) -> None:
    completed = subprocess.run([str(tool)], check=True, capture_output=True, text=True)
    assert "idle=0 badge=1 tone_off=0 flag=0" in completed.stdout
    assert "story=aigc_story_intro.ogg bedtime=aigc_soothe_intro.ogg" in completed.stdout
    assert "badge_vis=1 tone_want=0 offline_vis=1 layout_ok=1" in completed.stdout
    assert "corners_ok=1" in completed.stdout
    assert "clipped_rejected=1" in completed.stdout
    assert "geom=228,40,50,30 font=20" in completed.stdout


def test_display_wires_corner_badge_inside_safe_circle() -> None:
    display = DISPLAY_CC.read_text(encoding="utf-8")
    assert 'lv_label_set_text(aigc_badge_, "AI")' in display
    assert "lv_font_montserrat_20" in display
    assert "LV_OPA_COVER" in display
    assert "memoria::kAigcBadgeX" in display
    assert "lv_obj_set_pos(aigc_badge_" in display
    assert "UpdateAigcBadge(phase)" in display
    assert "aigc_offline_active_" in display
    assert "SetAigcOfflineActive" in display


def test_board_syncs_badge_for_offline_menu() -> None:
    board = BOARD_CC.read_text(encoding="utf-8")
    assert "SetAigcOfflineActive" in board
    assert "SyncAigcOfflineBadge" in board
    assert "offline_menu_.mode() != memoria::OfflineMode::kInactive" in board


def test_night_soothe_requires_spoken_intro() -> None:
    board = BOARD_CC.read_text(encoding="utf-8")
    night = board[board.index("case memoria::TapWakeAction::kNightSoothing") :]
    night = night[: night.index("case memoria::TapWakeAction::kOfflineMenu")]
    assert "kAigcNoticeBedtimeFile" in night
    assert "refusing night_soothe without AIGC intro" in night
    assert night.index("kAigcNoticeBedtimeFile") < night.index('PlayLocalAssetClip("night_soothe.ogg")')


def test_story_path_refuses_without_intro() -> None:
    board = BOARD_CC.read_text(encoding="utf-8")
    assert "refusing story without AIGC intro" in board
    assert "story_intro_ok" in board


def test_sdkconfig_enables_montserrat_20() -> None:
    import json

    product = json.loads((BOARD_DIR / "config.json").read_text(encoding="utf-8"))
    assert "CONFIG_LV_FONT_MONTSERRAT_20=y" in product["builds"][0]["sdkconfig_append"]


def test_render_aigc_notices_refuses_without_credentials() -> None:
    script = FIRMWARE_ROOT.parents[1] / "scripts" / "render_aigc_notices.py"
    completed = subprocess.run(
        ["python3", str(script)],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert completed.returncode == 2
    assert "TODO: DOUBAO_TTS_APP_ID" in completed.stderr
    assert "下面是 AI 合成的声音给你讲故事。" in completed.stderr
    assert "AI 合成的声音陪你睡觉啦。" in completed.stderr


def test_render_aigc_notices_matches_current_doubao_api() -> None:
    script = (
        FIRMWARE_ROOT.parents[1] / "scripts" / "render_aigc_notices.py"
    ).read_text(encoding="utf-8")
    assert "sample_rate=DOUBAO_SAMPLE_RATE_HZ" in script or "sample_rate=24_000" in script
    assert "DOUBAO_SAMPLE_RATE_HZ = 24_000" in script
    assert "synthesize_stream_text([text], fence=fence)" in script
    assert "await tts.start()" not in script
    assert "await tts.aclose()" in script
    assert "GenerationFence(" in script
