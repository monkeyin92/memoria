"""Host checks for explicit AIGC label policy and notice clip wiring."""

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

HARNESS = r"""
#include "memoria_aigc_label.h"

#include <cstdio>

int main() {
    using memoria::AigcExplicitMode;
    using memoria::AigcExplicitModeFor;
    using memoria::AigcBadgeVisible;
    using memoria::AigcTurnToneWanted;
    const auto idle = AigcExplicitModeFor(false, true);
    const auto badge = AigcExplicitModeFor(true, true);
    const auto tone_off = AigcExplicitModeFor(true, false);
    std::printf("idle=%d badge=%d tone_off=%d flag=%d story=%s bedtime=%s\n",
                static_cast<int>(idle), static_cast<int>(badge),
                static_cast<int>(tone_off),
                memoria::kAigcTurnToneEnabled ? 1 : 0,
                memoria::kAigcNoticeStoryFile, memoria::kAigcNoticeBedtimeFile);
    std::printf("badge_vis=%d tone_want=%d\n",
                AigcBadgeVisible(badge) ? 1 : 0,
                AigcTurnToneWanted(tone_off) ? 1 : 0);
    return 0;
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
    assert "story=aigc-notice-story.ogg bedtime=aigc-notice-bedtime.ogg" in completed.stdout
    assert "badge_vis=1 tone_want=0" in completed.stdout


def test_display_wires_corner_badge() -> None:
    display = DISPLAY_CC.read_text(encoding="utf-8")
    assert 'lv_label_set_text(aigc_badge_, "AI")' in display
    assert "LV_ALIGN_TOP_RIGHT" in display
    assert "kAigcBadgeIndigo" in display
    assert "UpdateAigcBadge(phase)" in display
    assert "0x4A52C9" in display


def test_night_soothe_prepends_spoken_notice() -> None:
    board = BOARD_CC.read_text(encoding="utf-8")
    night = board[board.index("case memoria::TapWakeAction::kNightSoothing") :]
    night = night[: night.index("case memoria::TapWakeAction::kOfflineMenu")]
    assert "kAigcNoticeBedtimeFile" in night
    assert night.index("kAigcNoticeBedtimeFile") < night.index('PlayLocalAssetClip("night_soothe.ogg")')


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
