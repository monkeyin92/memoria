"""Host checks for the offline story menu state machine and asset pack."""

from __future__ import annotations

import json
import pathlib
import shutil
import struct
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
            int quiet = 0;
            if (sscanf(line + 5, "%u %d", &now, &quiet) != 2) return 2;
            print_out(menu.OnDeepSleepWake(now, quiet != 0));
            printf("mode=%s\n", memoria::OfflineModeName(menu.mode()));
        } else if (strncmp(line, "down ", 5) == 0) {
            unsigned now = 0;
            int convo = 0;
            if (sscanf(line + 5, "%u %d", &now, &convo) != 2) return 2;
            // An offline edge needs a prior online sample. Discard that sample.
            menu.OnNetworkSample(now, true, convo != 0);
            print_out(menu.OnNetworkSample(now, false, convo != 0));
            printf("mode=%s\n", memoria::OfflineModeName(menu.mode()));
        } else if (strncmp(line, "up ", 3) == 0) {
            unsigned now = 0;
            if (sscanf(line + 3, "%u", &now) != 1) return 2;
            print_out(menu.OnNetworkSample(now, true, false));
            printf("mode=%s\n", memoria::OfflineModeName(menu.mode()));
        } else if (strncmp(line, "net ", 4) == 0) {
            unsigned now = 0;
            int online = 0, convo = 0;
            if (sscanf(line + 4, "%u %d %d", &now, &online, &convo) != 3) return 2;
            print_out(menu.OnNetworkSample(now, online != 0, convo != 0));
            printf("mode=%s\n", memoria::OfflineModeName(menu.mode()));
        } else if (strncmp(line, "wants ", 6) == 0) {
            int online = 0, idle = 0;
            if (sscanf(line + 6, "%d %d", &online, &idle) != 2) return 2;
            printf("%s\n", menu.WantsTap(online != 0, idle != 0) ? "yes" : "no");
        } else if (strncmp(line, "timer ", 6) == 0) {
            unsigned now = 0;
            if (sscanf(line + 6, "%u", &now) != 1) return 2;
            print_out(menu.OnTimer(now));
            printf("mode=%s item=%u\n", memoria::OfflineModeName(menu.mode()), menu.menu_item());
        } else if (strncmp(line, "tap ", 4) == 0) {
            unsigned now = 0, hold = 0;
            int online = 0, idle = 0;
            if (sscanf(line + 4, "%u %u %d %d", &now, &hold, &online, &idle) != 4) return 2;
            print_out(menu.OnTap(now, hold, online != 0, idle != 0));
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
        } else if (strncmp(line, "llm ", 4) == 0) {
            unsigned now = 0;
            int play = 0;
            if (sscanf(line + 4, "%u %d", &now, &play) != 2) return 2;
            print_out(menu.OnLlmUnavailable(now, play != 0));
            printf("mode=%s item=%u\n", memoria::OfflineModeName(menu.mode()), menu.menu_item());
        } else if (strncmp(line, "post_llm ", 9) == 0) {
            int play = 0;
            if (sscanf(line + 9, "%d", &play) != 1) return 2;
            memoria::OfflineMenuSignals::PostLlmUnavailable(play != 0);
            printf("ok\n");
        } else if (strcmp(line, "consume_llm") == 0) {
            bool play = false;
            if (!memoria::OfflineMenuSignals::ConsumeLlmUnavailable(&play)) {
                printf("none\n");
            } else {
                printf("play=%d\n", play ? 1 : 0);
            }
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
    assert "aigc_story_intro" in lines[3]
    assert lines[3].index("offline-menu-start-v1") < lines[3].index("aigc_story_intro")
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


def test_awake_known_offline_tap_skips_20s_reconnect(tool: pathlib.Path) -> None:
    lines = _run(
        tool,
        "reset",
        "tap 1000 100 0 1 0",
        "note 2000",
        "timer 33000",
        "tap 34000 100 0 1 0",
    )
    assert "offline-menu-idle-v1" in lines[4]
    assert lines[5].startswith("mode=inactive")
    joined = "\n".join(lines)
    assert "awake_reconnecting" not in joined
    assert "offline-wake-no-network-v1" in lines[6]
    assert "offline-menu-intro-v2" in lines[6]
    assert lines[7] == "mode=menu item=1"


def test_deep_sleep_wake_arms_20s_reconnect_then_menu(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "wake 300 0", "wants 0 0", "tap 2000 100 0 0 0", "timer 20300")
    assert lines[1].startswith("intercept=1 mode_clips=0")
    assert lines[2] == "mode=awake_reconnecting"
    assert lines[3] == "yes"
    assert "offline-reconnecting-v1" in lines[4]
    assert "offline-wake-no-network-v1" in lines[6]
    assert "offline-menu-intro-v2" in lines[6]
    assert lines[7].startswith("mode=menu item=1")


def test_deep_sleep_wake_in_quiet_hours_stays_inactive(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "wake 300 1", "wants 0 0")
    assert lines[1].startswith("intercept=0 mode_clips=0")
    assert lines[2] == "mode=inactive"
    assert lines[3] == "no"


def test_deep_sleep_wake_reconnect_says_restored(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "wake 300 0", "up 1000")
    assert lines[2] == "mode=awake_reconnecting"
    assert "online-restored-v1" in lines[3]
    assert "online-restored-chat-v1" in lines[3]
    assert lines[4] == "mode=inactive"


def test_cold_boot_samples_never_speak(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "net 1000 0 0", "net 4000 1 0")
    assert lines[1].startswith("intercept=0 mode_clips=0")
    assert lines[2] == "mode=inactive"
    assert lines[3].startswith("intercept=0 mode_clips=0")
    assert lines[4] == "mode=inactive"


def test_idle_drop_restore_still_says_network_is_back(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "net 1000 1 0", "net 2000 0 0", "net 3000 1 0")
    assert lines[1].startswith("intercept=0 mode_clips=0")
    assert lines[2] == "mode=inactive"
    assert lines[3].startswith("intercept=1 mode_clips=0")
    assert lines[4] == "mode=standby_offline"
    assert "online-restored-v1" in lines[5]
    assert "online-restored-chat-v1" in lines[5]
    assert lines[6] == "mode=inactive"


def test_mid_conversation_drop_keeps_20s_reconnect(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "net 500 1 0", "net 1000 0 1", "timer 21000")
    assert lines[1].startswith("intercept=0 mode_clips=0")
    assert "offline-mid-drop-v2" in lines[3]
    assert lines[4] == "mode=mid_reconnecting"
    assert "offline-menu-intro-v2" in lines[5]
    assert "offline-wake-no-network-v1" not in lines[5]
    assert lines[6].startswith("mode=menu item=1")


def test_llm_unavailable_enters_menu_without_replay_when_server_spoke(
    tool: pathlib.Path,
) -> None:
    lines = _run(tool, "reset", "llm 1000 0", "net 2000 1 0")
    assert "llm_unavailable" not in lines[1]
    assert "offline-menu-intro-v2" in lines[1]
    assert lines[2].startswith("mode=menu item=1")
    # Still online: a steady online sample must not fire OnNetworkUp.
    assert lines[3].startswith("intercept=0 mode_clips=0")
    assert lines[4] == "mode=menu"


def test_llm_unavailable_can_play_local_line(tool: pathlib.Path) -> None:
    lines = _run(tool, "reset", "llm 1000 1")
    assert "llm_unavailable" in lines[1]
    assert "offline-menu-intro-v2" in lines[1]
    assert lines[2].startswith("mode=menu item=1")


def test_offline_menu_signals_post_and_consume(tool: pathlib.Path) -> None:
    lines = _run(tool, "consume_llm", "post_llm 0", "consume_llm", "consume_llm")
    assert lines[0] == "none"
    assert lines[1] == "ok"
    assert lines[2] == "play=0"
    assert lines[3] == "none"


VOICE = {
    "id": "zh_female_tianmeitaozi_uranus_bigtts",
    "generated": "2026-10-14",
    "terms": "trial/demo only pending Volcengine terms",
}


def _pack_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", str(BUILD_SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _ogg_bytes(rel: pathlib.Path) -> dict[str, bytes]:
    return {path.relative_to(rel).as_posix(): path.read_bytes() for path in rel.rglob("*.ogg")}


def _ogg_with_title(title: str) -> bytes:
    vendor = b"memoria-test"
    comment = f"TITLE={title}".encode()
    return (
        b"OggS"
        + b"\x00" * 24
        + b"OpusTags"
        + struct.pack("<I", len(vendor))
        + vendor
        + struct.pack("<I", 1)
        + struct.pack("<I", len(comment))
        + comment
    )


def _install_required_intro_stubs(root: pathlib.Path) -> None:
    """Copy a short production clip and retag CONTENT_ID for pack-gate tests.

    Never used as shipped audio; the real intros come from Doubao via
    scripts/render_aigc_notices.py.
    """
    import sys

    repo = BUILD_SCRIPT.resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from services.common.aigc_opus import rewrite_opus_comments

    src = (root / "offline_lines" / "offline-menu-start-v1.ogg").read_bytes()
    for intro_id in ("aigc_story_intro", "aigc_soothe_intro"):
        tagged = rewrite_opus_comments(
            src,
            aigc={
                "CONTENT_ID": intro_id,
                "SERVICE_PROVIDER": "Volcengine Doubao",
                "AI_GENERATED": "true",
                "ARTIST": "zh_female_tianmeitaozi_uranus_bigtts",
                "VOICE_ID": "zh_female_tianmeitaozi_uranus_bigtts",
                "DATE": "2026-10-14",
                "CONTENT_PRODUCER": "Memoria",
            },
        )
        (root / "offline_lines" / f"{intro_id}.ogg").write_bytes(tagged)


def test_check_only_requires_aigc_intros_in_board_pack() -> None:
    completed = _pack_script("--check-only")
    story = BOARD_ASSETS / "offline_lines" / "aigc_story_intro.ogg"
    soothe = BOARD_ASSETS / "offline_lines" / "aigc_soothe_intro.ogg"
    if story.is_file() and soothe.is_file():
        assert completed.returncode == 0, completed.stderr
        fields = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
        assert fields["production_clips"] == "29"
        assert fields["placeholder_clips"] == "0"
        assert fields["pending_placeholder_clips"] == "0"
        assert fields["required_aigc_intros"] == "2"
        assert "pending_aigc_audit_exceptions" not in completed.stdout
    else:
        assert completed.returncode == 1
        assert "missing required AIGC intro offline_lines/aigc_story_intro.ogg" in completed.stderr
        assert "missing required AIGC intro offline_lines/aigc_soothe_intro.ogg" in completed.stderr


def test_check_only_passes_with_intro_stubs(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "assets"
    shutil.copytree(BOARD_ASSETS, root)
    _install_required_intro_stubs(root)
    completed = _pack_script("--assets-root", str(root), "--check-only")
    # Manifests may be stale until a write pass; run once to refresh then check.
    if completed.returncode != 0 and "is stale" in completed.stderr:
        written = _pack_script("--assets-root", str(root))
        assert written.returncode == 0, written.stderr
        completed = _pack_script("--assets-root", str(root), "--check-only")
    assert completed.returncode == 0, completed.stderr
    fields = dict(line.split("=", 1) for line in completed.stdout.splitlines() if "=" in line)
    assert int(fields["free_margin_bytes"]) >= 512 * 1024
    assert fields["production_clips"] == "29"
    assert fields["placeholder_clips"] == "0"
    assert fields["pending_placeholder_clips"] == "0"
    assert fields["required_aigc_intros"] == "2"


def test_default_run_never_writes_audio_and_converges(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "assets"
    shutil.copytree(BOARD_ASSETS, root)
    _install_required_intro_stubs(root)
    before = _ogg_bytes(root)
    first = _pack_script("--assets-root", str(root))
    assert first.returncode == 0, first.stderr
    manifests = {
        rel: (root / rel).read_bytes()
        for rel in ("stories/index.json", "offline_lines/index.json")
    }
    second = _pack_script("--assets-root", str(root))
    assert second.returncode == 0, second.stderr
    assert _ogg_bytes(root) == before
    assert {
        rel: (root / rel).read_bytes()
        for rel in ("stories/index.json", "offline_lines/index.json")
    } == manifests
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "ffmpeg" not in script
    assert "subprocess" not in script


def test_placeholder_clip_fails_check(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "assets"
    shutil.copytree(BOARD_ASSETS, root)
    _install_required_intro_stubs(root)
    target = root / "stories" / "story_01.ogg"
    blob = _ogg_with_title("story_01_placeholder")
    target.write_bytes(blob)
    completed = _pack_script("--assets-root", str(root))
    assert completed.returncode == 1
    assert "stories/story_01.ogg is a placeholder; copy the production clip" in completed.stderr
    assert target.read_bytes() == blob


def test_missing_stories_index_fails_check_only(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "assets"
    shutil.copytree(BOARD_ASSETS, root)
    _install_required_intro_stubs(root)
    prepared = _pack_script("--assets-root", str(root))
    assert prepared.returncode == 0, prepared.stderr
    index = root / "stories" / "index.json"
    index.unlink()
    completed = _pack_script("--assets-root", str(root), "--check-only")
    assert completed.returncode == 1
    assert "stories/index.json is stale; run build_offline_stories.py" in completed.stderr
    assert not index.exists()


def test_clip_without_opus_tags_is_not_production(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "assets"
    shutil.copytree(BOARD_ASSETS, root)
    _install_required_intro_stubs(root)
    target = root / "offline_lines" / "offline-menu-idle-v1.ogg"
    blob = b"OggS" + b"\x00" * 32
    target.write_bytes(blob)
    completed = _pack_script("--assets-root", str(root))
    assert completed.returncode == 1
    assert "offline_lines/offline-menu-idle-v1.ogg: no OpusTags header" in completed.stderr
    assert target.read_bytes() == blob
    assert "pending_placeholder_clips=0" in completed.stdout


def test_missing_clip_fails_without_creating_it(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "assets"
    shutil.copytree(BOARD_ASSETS, root)
    _install_required_intro_stubs(root)
    target = root / "stories" / "story_03.ogg"
    target.unlink()
    completed = _pack_script("--assets-root", str(root))
    assert completed.returncode == 1
    assert "missing asset stories/story_03.ogg; copy the production clip" in completed.stderr
    assert not target.exists()


def test_unlabelled_production_clip_fails_check(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "assets"
    shutil.copytree(BOARD_ASSETS, root)
    _install_required_intro_stubs(root)
    target = root / "offline_lines" / "offline-menu-idle-v1.ogg"
    vendor = b"Lavf-test"
    comment = b"encoder=test"
    body = (
        b"OpusTags"
        + struct.pack("<I", len(vendor))
        + vendor
        + struct.pack("<I", 1)
        + struct.pack("<I", len(comment))
        + comment
    )
    # Tiny Ogg-shaped blob with OpusTags but no implicit AIGC keys.
    target.write_bytes(b"OggS" + b"\x00" * 23 + bytes([1, len(body)]) + body)
    completed = _pack_script("--assets-root", str(root))
    assert completed.returncode == 1
    assert "offline_lines/offline-menu-idle-v1.ogg missing implicit AIGC labels" in completed.stderr


def test_hand_edited_manifest_fails_check_only(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "assets"
    shutil.copytree(BOARD_ASSETS, root)
    _install_required_intro_stubs(root)
    prepared = _pack_script("--assets-root", str(root))
    assert prepared.returncode == 0, prepared.stderr
    manifest = root / "stories" / "index.json"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("大熊和森林里的悄悄话", "一只会听的熊"),
        encoding="utf-8",
    )
    completed = _pack_script("--assets-root", str(root), "--check-only")
    assert completed.returncode == 1
    assert "stories/index.json is stale; run build_offline_stories.py" in completed.stderr
    assert "一只会听的熊" in manifest.read_text(encoding="utf-8")


def test_manifests_record_production_voice() -> None:
    for rel in ("stories/index.json", "offline_lines/index.json"):
        manifest = json.loads((BOARD_ASSETS / rel).read_text(encoding="utf-8"))
        assert manifest["voice"] == VOICE
        entries = manifest["stories"] if "stories" in manifest else manifest["lines"]
        assert entries
        assert all(entry["placeholder"] is False for entry in entries)


def test_offline_line_manifest_matches_pack() -> None:
    manifest = json.loads((BOARD_ASSETS / "offline_lines" / "index.json").read_text(encoding="utf-8"))
    ids = [entry["id"] for entry in manifest["lines"]]
    assert len(ids) == 24
    assert "offline-chat-request-v1" not in ids
    assert ids[0] == "offline-wake-no-network-v1"
    assert ids[15] == "offline-menu-item-03-v2"
    assert ids[19] == "offline-menu-idle-v1"
    assert ids[20] == "llm_unavailable"
    assert ids[21] == "llm_thinking"
    assert ids[22] == "aigc_story_intro"
    assert ids[23] == "aigc_soothe_intro"
    by_id = {entry["id"]: entry for entry in manifest["lines"]}
    assert by_id["llm_unavailable"]["placeholder"] is False
    assert by_id["llm_unavailable"]["text"] == (
        "我现在有点卡住了，等一会儿再来找我聊吧。"
    )
    assert by_id["llm_thinking"]["placeholder"] is False
    assert by_id["llm_thinking"]["text"] == "嗯，我想一想。"
    assert by_id["aigc_story_intro"]["text"] == "下面是 AI 合成的声音给你讲故事。"
    assert by_id["aigc_soothe_intro"]["text"] == "AI 合成的声音陪你睡觉啦。"
    assert "pending_tts" not in by_id["aigc_story_intro"]
    assert "pending_tts" not in by_id["aigc_soothe_intro"]
    for entry in manifest["lines"]:
        assert entry["placeholder"] is False
        filename = entry["file"]
        assert len(filename) <= 32
        assert (BOARD_ASSETS / "offline_lines" / filename).is_file()


def test_offline_line_ids_match_header_and_script() -> None:
    import importlib.util
    import re

    header = (MEMORIA_DIR / "memoria_offline_menu.h").read_text(encoding="utf-8")
    consts = dict(
        re.findall(
            r'inline constexpr const char\* (k\w+) = "([a-z0-9_-]+)";',
            header,
        )
    )
    start = header.index("inline constexpr const char* kOfflineLineIds[] = {")
    end = header.index("};", start)
    symbols = re.findall(r"\b(k[A-Za-z0-9]+)\b", header[start:end])
    header_ids = [consts[name] for name in symbols if name in consts]
    spec = importlib.util.spec_from_file_location("build_offline_stories", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    script_ids = [line_id for line_id, _ in mod.OFFLINE_LINES]
    assert header_ids == script_ids
    manifest = json.loads((BOARD_ASSETS / "offline_lines" / "index.json").read_text(encoding="utf-8"))
    assert [entry["id"] for entry in manifest["lines"]] == script_ids


def test_story_03_title_renamed() -> None:
    manifest = json.loads((BOARD_ASSETS / "stories" / "index.json").read_text(encoding="utf-8"))
    by_id = {s["id"]: s["title"] for s in manifest["stories"]}
    assert by_id["story_03"] == "大熊和森林里的悄悄话"
    script = BUILD_SCRIPT.read_text(encoding="utf-8")
    assert "大熊和森林里的悄悄话" in script
    assert "一只会听的熊" not in script


def test_device_ogg_aigc_check_passes_on_board_pack() -> None:
    script = FIRMWARE_ROOT / "scripts" / "check_device_ogg_aigc.py"
    completed = subprocess.run(
        ["python3", str(script), "--assets-root", str(BOARD_ASSETS)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert "checked_oggs=29" in completed.stdout
    assert completed.stderr == ""
