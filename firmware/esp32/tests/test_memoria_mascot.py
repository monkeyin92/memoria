"""Host-side checks for the companion mascot on the 360x360 round LCD.

`memoria_mascot_pack.cc` and `memoria_mascot_scene.cc` have no ESP-IDF or
LVGL dependency, so the exact firmware compositor is compiled on the host by
scripts/preview_memoria_mascot.py and driven through scripted device life.
Every frame is also rendered with a forced full redraw and compared, so the
dirty rectangles can never leave stale pixels on the panel.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
ASSETS_DIR = BOARD_DIR / "assets"
MEMORIA_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "memoria"
PATCH = FIRMWARE_ROOT / "overlay" / "patches" / "0026-memoria-mascot-display.patch"
COMPANIONS = ("starlight", "taoxi", "mianmian", "axu", "xuanmo")

# MascotFrame ids (memoria_mascot_pack.h); 19 = kCount means "no mascot drawn".
DEFAULT, HAPPY, SAD, SURPRISED, THINKING, LISTENING, SLEEPY, DIZZY, GREETING = range(9)
DEFAULT_BLINK, LISTENING_BLINK = 9, 13
HAPPY_TALK = 15
NONE = 19


def _load_module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def preview():
    if shutil.which("clang++") is None and shutil.which("g++") is None:
        pytest.skip("host C++ compiler unavailable")
    return _load_module(
        "preview_memoria_mascot", FIRMWARE_ROOT / "scripts" / "preview_memoria_mascot.py"
    )


@pytest.fixture(scope="module")
def harness(preview):
    work = pathlib.Path(tempfile.mkdtemp(prefix="memoria-mascot-"))
    try:
        yield preview, work, preview.compile_harness(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _play(harness, timeline, first: str = "starlight") -> dict:
    preview, work, binary = harness
    stats = preview.run(binary, work, 25, "-", timeline=timeline, first=first)
    stats["by_ms"] = {frame[0]: frame for frame in stats["frames"]}
    return stats


def _frames_between(stats: dict, start: int, end: int) -> set[int]:
    return {frame[4] for frame in stats["frames"] if start <= frame[0] < end}


def test_scripted_day_redraws_exactly(harness, preview) -> None:
    stats = _play(harness, preview.TIMELINE)
    assert stats["mismatches"] == 0
    # A breathing mascot still leaves most 40 ms ticks with nothing to redraw
    # or only a small area, never a full-screen repaint every frame.
    assert stats["unchanged_frames"] > stats["frame_count"] // 4
    assert stats["redraw_px"] < stats["frame_count"] * 360 * 360 * 0.6


def test_boot_animation_enters_then_greets(harness) -> None:
    stats = _play(harness, [(0, "intro", ""), (0, "phase", "connecting"), (7000, "end", "")])
    assert _frames_between(stats, 0, 2500) == {NONE}  # orb, reveal and wordmark only
    assert DEFAULT in _frames_between(stats, 2600, 3600)
    assert GREETING in _frames_between(stats, 3700, 4900)
    assert THINKING in _frames_between(stats, 5600, 7000)  # still connecting after the intro


def test_conversation_states_pick_their_poses(harness) -> None:
    stats = _play(
        harness,
        [
            (0, "phase", "idle"),
            (2000, "phase", "listening"),
            (4000, "phase", "thinking"),
            (5000, "mood", "happy"),
            (5000, "phase", "speaking"),
            (8000, "phase", "idle"),
            (14000, "end", ""),
        ],
    )
    assert _frames_between(stats, 400, 2000) <= {DEFAULT, DEFAULT_BLINK}
    assert _frames_between(stats, 2400, 4000) <= {LISTENING, LISTENING_BLINK}
    assert THINKING in _frames_between(stats, 4400, 5000)
    speaking = _frames_between(stats, 5400, 8000)
    assert {HAPPY, HAPPY_TALK} <= speaking  # the mouth really moves
    assert HAPPY in _frames_between(stats, 8400, 10000)  # the reply's mood lingers
    assert _frames_between(stats, 11000, 14000) <= {DEFAULT, DEFAULT_BLINK}


def test_pat_hops_happy_and_shake_goes_dizzy(harness) -> None:
    stats = _play(
        harness, [(0, "phase", "idle"), (1000, "pat", ""), (4000, "shake", ""), (8000, "end", "")]
    )
    assert HAPPY in _frames_between(stats, 1000, 1400)
    assert DIZZY in _frames_between(stats, 4000, 6000)
    assert _frames_between(stats, 7200, 8000) <= {DEFAULT, DEFAULT_BLINK}


def test_blinks_happen_in_idle(harness) -> None:
    stats = _play(harness, [(0, "phase", "idle"), (20000, "end", "")])
    assert DEFAULT_BLINK in _frames_between(stats, 0, 20000)


def test_errors_are_dizzy_and_setup_hides_the_mascot(harness) -> None:
    stats = _play(harness, [(0, "phase", "error"), (3000, "phase", "setup"), (5000, "end", "")])
    assert DIZZY in _frames_between(stats, 400, 3000)
    assert _frames_between(stats, 3400, 5000) == {NONE}  # the QR card owns the centre


def test_companion_switch_arrives_waving(harness) -> None:
    stats = _play(harness, [(0, "phase", "idle"), (1000, "companion", "taoxi"), (5000, "end", "")])
    assert stats["mismatches"] == 0
    assert GREETING in _frames_between(stats, 1000, 2600)
    assert _frames_between(stats, 3600, 5000) <= {DEFAULT, DEFAULT_BLINK}


@pytest.mark.parametrize("companion", COMPANIONS)
def test_every_companion_pack_loads_and_renders(harness, companion: str) -> None:
    stats = _play(
        harness,
        [(0, "phase", "idle"), (600, "phase", "speaking"), (2000, "end", "")],
        first=companion,
    )
    assert stats["mismatches"] == 0
    assert NONE not in _frames_between(stats, 0, 2000)


def test_mascot_packs_match_the_source_art() -> None:
    """The shipped packs must be built from the current source art.

    Pillow's resize and palette quantisation differ slightly between x86 and
    ARM, so a rebuild is not byte-identical across machines. Instead every
    frame the device would draw is compared with the source PNG: a stale pack
    (art changed, packer not rerun) is far outside the quantisation error.
    """
    pytest.importorskip("PIL")
    np = pytest.importorskip("numpy")
    builder = _load_module("build_mascot_pack", FIRMWARE_ROOT / "scripts" / "build_mascot_pack.py")
    for companion in COMPANIONS:
        name = f"mascot_{companion}.mmp"
        header, frames = builder.decode_frames((ASSETS_DIR / name).read_bytes())
        assert set(frames) == {frame for frame, _ in builder.FRAMES}, name
        theme = builder.THEMES[companion]
        assert header[8] == theme["soft"] and header[9] == theme["ink"], name
        for frame, drawn in frames.items():
            source = builder._load(companion, frame).astype(np.int16)
            error = np.abs(drawn.astype(np.int16) - source)
            covered = (source[..., 3] > 32) | (drawn[..., 3] > 32)
            visible = (source[..., 3] > 32) & (drawn[..., 3] > 32)
            assert visible.sum() > 1000, f"{name}:{frame} is empty"
            colour_error = float(error[visible][:, :3].mean())
            alpha_error = float(error[..., 3].mean())
            # Quantisation leaves < 0.01% of pixels off by more than 48; even a
            # changed mouth alone moves > 0.2%, so this catches stale patches.
            outliers = float((error.max(axis=2)[covered] > 48).mean())
            assert colour_error < 10.0 and alpha_error < 3.0 and outliers < 5e-4, (
                f"{name}:{frame} differs from the source art (colour {colour_error:.1f}, "
                f"alpha {alpha_error:.1f}, outliers {outliers:.2%}); "
                "run scripts/build_mascot_pack.py"
            )


def test_pack_budget_fits_the_assets_partition() -> None:
    total = sum(path.stat().st_size for path in ASSETS_DIR.iterdir())
    # About 3.1 MiB of the 8 MiB assets partition was free before the mascots.
    assert total < 1_600_000


def test_compositor_stays_free_of_esp_dependencies() -> None:
    for name in (
        "memoria_mascot_pack.h",
        "memoria_mascot_pack.cc",
        "memoria_mascot_scene.h",
        "memoria_mascot_scene.cc",
    ):
        source = (BOARD_DIR / name).read_text(encoding="utf-8")
        for forbidden in ("esp_", "lvgl", "lv_", "freertos"):
            assert forbidden not in source, f"{name} must stay host-compilable"


def test_board_wires_the_mascot_display() -> None:
    board = (BOARD_DIR / "memoria_esp_vocat.cc").read_text(encoding="utf-8")
    assert '#include "memoria_mascot_display.h"' in board
    assert "new MemoriaMascotDisplay(" in board
    assert not (BOARD_DIR / "memoria_face.cc").exists()
    # The backlight comes up on the boot animation's first frame, not over
    # the panel's power-on noise.
    creation = board[
        board.index("new MemoriaMascotDisplay(") : board.index("void InitializeButtons()")
    ]
    assert "SetOnFirstFrame" in creation
    assert creation.index("SetOnFirstFrame") < creation.index("RestoreBrightness")
    assert "SetCompanionSink" in creation

    display = (BOARD_DIR / "memoria_mascot_display.cc").read_text(encoding="utf-8")
    assert "bg_image_src" in display
    assert "emoji_box_, LV_OBJ_FLAG_HIDDEN" in display
    # Below every audio task: AFE 3, Opus 5, output 6, input 8.
    assert '"mascot_anim", 6144, this, 2,' in display


def test_body_gestures_animate_without_starting_chat() -> None:
    board = (BOARD_DIR / "memoria_esp_vocat.cc").read_text(encoding="utf-8")
    gestures = board[board.index("void OnDevicePat(") : board.index("static void imu_event_task")]
    assert "kDeviceStateIdle" in gestures
    assert "display_->Pat()" in gestures
    assert "display_->Shake()" in gestures
    for forbidden in ("ToggleChatState()", "StartListening(", "AbortSpeaking("):
        assert forbidden not in gestures
    imu = board[board.index("static void imu_event_task") : board.index("void MuteImuForTouch()")]
    assert "self->OnDeviceShake(result.peak)" in imu
    assert "Device pat ignored (touch rumble" in imu


def test_display_profile_poll_follows_the_phone_pick() -> None:
    client = (MEMORIA_DIR / "memoria_activation_client.cc").read_text(encoding="utf-8")
    assert '"/display-profile"' in client
    fetch = client[client.index("MemoriaActivationClient::FetchDisplayProfile") :]
    assert "X-Device-Signature" in fetch
    assert "BuildSignedGetPayload(identity_, path)" in fetch
    assert "status == 409 ? ESP_ERR_INVALID_STATE" in fetch

    protocol = (MEMORIA_DIR / "memoria_protocol.cc").read_text(encoding="utf-8")
    task = protocol[protocol.index("void MemoriaProtocol::DisplayProfileTask") :]
    task = task[: task.index("bool MemoriaProtocol::CreateMediaSession")]
    # Never poll during a conversation.
    assert "GetDeviceState() == kDeviceStateIdle" in task
    assert "PublishCompanion(" in task
    assert "profile.display_version != applied_version" in task
    assert protocol.count("StartDisplayProfilePoll();") >= 2  # first boot and after binding


def test_patch_adds_assets_hooks_and_overridable_qr() -> None:
    patch = PATCH.read_text(encoding="utf-8")
    assert '+            "memoria/memoria_display_hooks.cc"' in patch
    assert (
        '+    set(DEFAULT_ASSETS_EXTRA_FILES "${CMAKE_CURRENT_SOURCE_DIR}/boards/memoria/esp-vocat/assets")'
        in patch
    )
    assert "+    virtual bool ShowQrCode(" in patch
    assert "+    virtual void ClearQrCode();" in patch
    assert "+#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT" in patch
