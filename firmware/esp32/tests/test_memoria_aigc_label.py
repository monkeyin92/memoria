"""Host checks that explicit AI chrome and spoken intros are gone.

Implicit OpusTags stay on every shipped ogg. The live-dialogue AIGC tone is
owned by the agent and is not covered here.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

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
DISPLAY_H = (
    FIRMWARE_ROOT
    / "overlay"
    / "files"
    / "main"
    / "boards"
    / "memoria"
    / "esp-vocat"
    / "memoria_mascot_display.h"
)
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
OVERLAY_CHECK = FIRMWARE_ROOT / "scripts" / "check-overlay.sh"


def _function(source: str, signature: str) -> str:
    start = source.index(signature)
    depth = 0
    for index in range(source.index("{", start), len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"{signature} has no closing brace")


def test_display_does_not_render_an_ai_badge() -> None:
    display = DISPLAY_CC.read_text(encoding="utf-8")
    header = DISPLAY_H.read_text(encoding="utf-8")
    assert "aigc_badge_" not in display
    assert "aigc_badge_" not in header
    assert 'lv_label_set_text(aigc_badge_, "AI")' not in display
    assert '"AI"' not in display
    assert "UpdateAigcBadge" not in display
    assert "EnsureAigcBadge" not in display
    assert "lv_font_montserrat_16" not in display
    assert "kAigcBadge" not in display
    assert not (MEMORIA_DIR / "memoria_aigc_label.h").is_file()


def test_badge_font_is_not_in_board_config() -> None:
    for name in ("config.json", "config.bench.json", "config.keep-awake.json"):
        product = json.loads((BOARD_DIR / name).read_text(encoding="utf-8"))
        flags = product["builds"][0]["sdkconfig_append"]
        assert "CONFIG_LV_FONT_MONTSERRAT_16=y" not in flags


def test_story_schedule_plays_the_story_without_an_intro() -> None:
    board = BOARD_CC.read_text(encoding="utf-8")
    schedule = _function(board, "Application::GetInstance().Schedule([this, out, generation]")
    assert "PlayLocalAssetClip(memoria::OfflineStoryFile(out.story_index))" in schedule
    assert "aigc_story_intro" not in schedule
    assert "AigcNotice" not in schedule
    assert "refusing" not in schedule
    assert "story_intro_ok" not in board
    assert "PlayStoryWithAigcNoticeNow" not in board


def test_night_soothe_does_not_play_a_clip() -> None:
    board = BOARD_CC.read_text(encoding="utf-8")
    night = board[board.index("case memoria::TapWakeAction::kNightSoothing") :]
    night = night[: night.index("case memoria::TapWakeAction::kOfflineMenu")]
    assert "night_soothe.ogg" not in night
    assert "PlayLocalAssetClip" not in night
    assert "EnterTapDeepSleep" in night


def test_offline_menu_does_not_queue_a_spoken_intro() -> None:
    header = (MEMORIA_DIR / "memoria_offline_menu.h").read_text(encoding="utf-8")
    assert "aigc_story_intro" not in header
    assert "aigc_soothe_intro" not in header
    assert "AigcPlaybackStart" not in header
    menu_start = header[header.index("if (long_press) {") :]
    menu_start = menu_start[: menu_start.index("out.start_story = true;")]
    assert "kOfflineMenuStart" in menu_start
    assert "kAigc" not in menu_start


def test_overlay_check_keeps_implicit_tags_and_drops_intro_gates() -> None:
    script = OVERLAY_CHECK.read_text(encoding="utf-8")
    assert "check_device_ogg_aigc.py" in script
    assert 'build_offline_stories.py" --check-only' in script
    assert "AIGC corner badge" not in script
    assert "spoken AIGC notice" not in script
    assert "memoria_aigc_label.h" not in script
    assert "kAigcNoticeBedtimeFile" not in script


def test_render_aigc_notices_refuses_without_credentials() -> None:
    script = FIRMWARE_ROOT.parents[1] / "scripts" / "render_aigc_notices.py"
    completed = subprocess.run(
        ["uv", "run", "python", str(script)],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin:/opt/homebrew/bin"},
    )
    assert completed.returncode == 2
    assert "TODO: DOUBAO_TTS_APP_ID" in completed.stderr
    assert "下面是 AI 合成的声音给你讲故事。" in completed.stderr
    assert "AI 合成的声音陪你睡觉啦。" in completed.stderr


def test_render_aigc_notices_matches_current_doubao_api() -> None:
    script = (FIRMWARE_ROOT.parents[1] / "scripts" / "render_aigc_notices.py").read_text(
        encoding="utf-8"
    )
    assert "sample_rate=DOUBAO_SAMPLE_RATE_HZ" in script or "sample_rate=24_000" in script
    assert "DOUBAO_SAMPLE_RATE_HZ = 24_000" in script
    assert "synthesize_stream_text([text], fence=fence)" in script
    assert "await tts.start()" not in script
    assert "await tts.aclose()" in script
    assert "GenerationFence(" in script
