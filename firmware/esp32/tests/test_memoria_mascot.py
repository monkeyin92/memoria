"""Host-side checks for the companion mascot on the 360x360 round LCD.

`memoria_mascot_pack.cc`, `memoria_mascot_raster.cc` and `memoria_mascot_scene.cc` have no ESP-IDF or
LVGL dependency, so the exact firmware compositor is compiled on the host by
scripts/preview_memoria_mascot.py and driven through scripted device life, with audio levels fed
through the firmware's own `AudioLevelTap` in 20 ms blocks and renders at the scene's own cadence.
Every render is also done with a forced full redraw and compared, so the
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
PATCH = FIRMWARE_ROOT / "overlay" / "patches" / "0027-memoria-mascot-display.patch"
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


@pytest.fixture(scope="module")
def sanitized_harness(preview):
    """The same compositor built with AddressSanitizer + UndefinedBehaviorSanitizer."""
    if not preview.sanitizers_available():
        pytest.skip("host compiler cannot build with -fsanitize=address,undefined")
    work = pathlib.Path(tempfile.mkdtemp(prefix="memoria-mascot-san-"))
    try:
        yield preview, work, preview.compile_harness(work, sanitize=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _play(harness, timeline, first: str = "starlight", curves=None) -> dict:
    preview, work, binary = harness
    stats = preview.run(binary, work, 25, "-", timeline=timeline, first=first, curves=curves)
    stats["by_ms"] = {frame[0]: frame for frame in stats["frames"]}
    return stats


def _frames_between(stats: dict, start: int, end: int) -> set[int]:
    return {frame[4] for frame in stats["frames"] if start <= frame[0] < end}


def test_scripted_day_redraws_exactly(harness, preview) -> None:
    stats = _play(harness, preview.TIMELINE)
    assert stats["mismatches"] == 0
    # The mascot breathes and sways in sub-pixel steps, so in a lit state nearly every 40 ms tick redraws its
    # box (test_every_state_moves_visibly_20_frames_a_second counts what shows). What stays bounded is the
    # area: the mascot's box plus the ring, never a full-screen repaint every frame.
    assert stats["redraw_px"] < stats["frame_count"] * 360 * 360 * 0.7
    # A redraw of the ring alone leaves the middle of the screen alone, so composing never costs more than
    # the rectangles' area, and over a day it costs measurably less.
    assert stats["composed_px"] < stats["redraw_px"] * 0.95


def test_every_state_moves_visibly_20_frames_a_second(harness, preview) -> None:
    """TODOLIST M-6: the mascot used to hold still for most of the second (4 visible frames in idle and
    listening, 7-8 in thinking and speaking, measured on this same script).

    "Visible" is a frame that differs from the one before it by at least five pixels inside the state ring.
    Whole-pixel steps of a 3 px breath could not do that; sampling the sprite at its exact sub-pixel
    position does.
    """
    stats = _play(harness, preview.LIVELINESS_DAY)
    assert stats["mismatches"] == 0
    for state, (start, end) in preview.LIVELINESS_WINDOWS.items():
        per_second = preview.visible_frames_per_second(stats["frames"], start, end)
        assert sorted(per_second)[len(per_second) // 2] >= 20, (state, per_second)
        assert min(per_second) >= 18, (state, per_second)


def test_the_display_invalidates_everything_the_scene_composed(harness, preview) -> None:
    """The dirty rectangles Render hands out are what the display invalidates, and not everything the
    scene composes necessarily fits in one of them: the state ring is redrawn as four rim strips outside
    the sprite's box, so a display that invalidates only the sprite (as the 2026-10-07 LVGL-rect probe
    did while measuring taskLVGL) leaves the previous glow on the panel.

    The harness simulates the panel — a framebuffer that is only updated where the rectangles say so —
    and compares it against the forced-full-redraw scene after every render. On the liveliness day, which
    exercises the ring in every state, this is the check that fails when the ring strips are dropped;
    `mismatches` alone cannot see it, because the scene's own framebuffer is right either way.
    """
    stats = _play(harness, preview.LIVELINESS_DAY)
    assert stats["panel_mismatch_frames"] == 0, stats["panel_mismatch_px"]
    assert stats["panel_mismatch_px"] == 0


def test_a_sprite_redraw_only_recomposes_the_columns_it_covered_or_covers(harness, preview) -> None:
    """TODOLIST M-6: on the robot a frame costs about 0.8-1.2 us per composed pixel, so the pixels composed per
    frame are what decides the frame rate. The sprite's box is the union of two frames' boxes and about 40 %
    of it is transparent margin; recomposing only the columns the sprite covered last frame or covers now
    (test_scripted_day_redraws_exactly proves nothing stale is left behind) took the per-frame average, on the
    taoxi liveliness day, from 63k / 73k / 101k / 71k to 39k / 51k / 79k / 46k (idle / listening / thinking /
    speaking). The ceilings leave a few percent above that; going back to whole boxes would exceed them.
    """
    stats = _play(harness, preview.LIVELINESS_DAY, first="taoxi")
    assert stats["mismatches"] == 0
    ceilings = {"idle": 42000, "listening": 55000, "thinking": 84000, "speaking": 50000}
    for state, ceiling in ceilings.items():
        start, end = preview.LIVELINESS_WINDOWS[state]
        window = [f for f in stats["frames"] if start <= f[0] < end]
        mean = sum(f[preview.COMPOSED_PX] for f in window) / len(window)
        assert mean < ceiling, (state, round(mean), ceiling)


def test_the_stage_timing_counts_what_it_times_and_changes_no_pixel(
    harness, preview, monkeypatch
) -> None:
    """TODOLIST M-6: the bench image times every 8th Render by stage. With a clock that counts its own reads
    every stage of a composed row costs exactly 1, so the sums are the row counts, and switching the clock on
    must not move a pixel or a composed-pixel count."""
    plain = _play(harness, preview.LIVELINESS_DAY, first="taoxi")
    # no clock, no bookkeeping
    assert plain["profile"]["renders"] == 0 and plain["profile"]["sampled"] == 0
    monkeypatch.setenv("MASCOT_FAKE_CLOCK", "1")
    timed = _play(harness, preview.LIVELINESS_DAY, first="taoxi")
    assert timed["mismatches"] == 0
    assert (timed["composed_px"], timed["redraw_px"], timed["renders"]) == (
        plain["composed_px"],
        plain["redraw_px"],
        plain["renders"],
    )
    profile = timed["profile"]
    assert profile["renders"] == timed["renders"]
    assert profile["sampled"] == (timed["renders"] + 7) // 8
    assert profile["rows"] > 0
    for stage in ("copy_in_us", "shadow_us", "sprite_us", "ring_us", "copy_out_us"):
        assert profile[stage] == profile["rows"], stage
    assert profile["touch_us"] > 0
    # the whole Render contains its parts
    assert profile["total_us"] >= profile["actor_us"] + profile["rect_us"]
    assert profile["rect_us"] >= profile["copy_in_us"] + profile["copy_out_us"]


CAPTION_DAY = [
    (0, "phase", "setup"),
    (400, "phase", "wifi"),
    (2000, "caption", "on"),
    (4000, "phase", "connecting"),
    (6000, "caption", "off"),
    (6000, "phase", "idle"),
    (8000, "end", ""),
]


@pytest.mark.parametrize("companion", COMPANIONS)
def test_compositor_is_clean_under_sanitizers(sanitized_harness, companion: str) -> None:
    """The row buffer, span clipping and fixed-point sampling are hand-written pointer arithmetic.

    One scripted day (boot, idle, pat, listening, thinking, speaking in three moods, a companion switch, a
    shake, an error, the binding screen, the captioned layout) per companion, plus the long steady one that
    sweeps the sub-pixel breathing through every phase, with speech-like audio levels throughout, built with
    -fsanitize=address,undefined and -fno-sanitize-recover: any out-of-bounds read, overflow or shift aborts
    the run (the harness exits non-zero and `run` raises). The result must still match the full redraw bit
    for bit.
    """
    preview, work, binary = sanitized_harness
    for timeline in (preview.TIMELINE, preview.LIVELINESS_DAY, CAPTION_DAY):
        stats = preview.run(binary, work, 25, "-", timeline=timeline, first=companion)
        assert stats["mismatches"] == 0


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
            (5000, "voice", "reply"),
            (8000, "voice", "off"),
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


def _speaking(voice_events, end_ms: int):
    """A reply being spoken from 1 s on, in the happy mood, with the given audio events."""
    return [
        (0, "phase", "idle"),
        (1000, "mood", "happy"),
        (1000, "phase", "speaking"),
        *voice_events,
        (end_ms, "end", ""),
    ]


def _mouth(stats: dict, start: int, end: int) -> list[tuple[int, bool]]:
    return [(f[0], f[4] == HAPPY_TALK) for f in stats["frames"] if start <= f[0] < end]


def test_a_stalled_reply_keeps_the_mouth_shut(harness) -> None:
    """Round 18, p03: 30.8 s in "speaking" while nothing played, and the mouth flapped on a random timer.

    The mouth now follows the audio that reaches the speaker. No blocks, no talking, however long the
    state says "speaking".
    """
    stats = _play(harness, _speaking([], 31000))
    assert [ms for ms, talking in _mouth(stats, 1400, 31000) if talking] == []
    assert HAPPY in _frames_between(stats, 1400, 31000)  # still the happy pose, mouth shut


def test_the_mouth_flaps_with_the_syllables_of_the_reply(harness, preview) -> None:
    stats = _play(harness, _speaking([(1000, "voice", "reply"), (11000, "voice", "off")], 12000))
    mouth = _mouth(stats, 1000, 11000)
    opened = sum(1 for (_, before), (_, now) in zip(mouth, mouth[1:], strict=False) if now and not before)
    speech_s = sum(1 for level in preview.CURVES["reply"] if level >= 100) * 0.02
    assert 2.5 <= opened / speech_s <= 5.5, f"{opened} openings in {speech_s:.1f} s of speech"
    # Never a one-frame flicker (a dwell of at least 60 ms), and open only while something is being said.
    states = [talking for _, talking in mouth]
    runs = [len(list(group)) for _, group in __import__("itertools").groupby(states)]
    assert min(runs[1:-1]) >= 2, runs
    quiet_blocks = [i for i, level in enumerate(preview.CURVES["reply"]) if level < 20]
    quiet_frames = {(1000 + i * 20) // 40 * 40 for i in quiet_blocks}
    open_in_the_quiet = [ms for ms, talking in mouth if talking and ms in quiet_frames]
    assert len(open_in_the_quiet) <= len(quiet_frames) // 8, "the mouth hangs open through pauses"


def test_the_mouth_shuts_within_a_quarter_second_of_the_audio_stopping(harness) -> None:
    # Two seconds of syllables (crest 205, dip 150, every 120 ms), then either the stream just ends (a
    # stalled playback queue publishes nothing) or the reply is cut (`voice off`, as a barge-in does).
    steady = {"phrase": ([205] * 4 + [150] * 2) * 17}
    ends = _play(harness, _speaking([(1000, "voice", "phrase")], 6000), curves=steady)
    cut = _play(harness, _speaking([(1000, "voice", "phrase"), (2500, "voice", "off")], 6000), curves=steady)
    for stats, stopped in ((ends, 1000 + 17 * 120), (cut, 2500)):
        assert any(talking for _, talking in _mouth(stats, stopped - 500, stopped)), "it was flapping"
        assert [ms for ms, talking in _mouth(stats, stopped + 250, 6000) if talking] == []


def test_the_ring_glow_follows_the_voice_it_hears_and_the_voice_it_speaks(harness, preview) -> None:
    np = pytest.importorskip("numpy")
    work = harness[1]
    yy, xx = np.mgrid[0:360, 0:360]
    radius = np.sqrt((xx + 0.5 - 180) ** 2 + (yy + 0.5 - 180) ** 2)
    band = (radius >= 149) & (radius <= 179)
    curves = {"loud": [225] * 400, "quiet": [6] * 400}

    def ring(phase: str, key: str, curve: str | None) -> np.ndarray:
        raw = work / "ring.raw"
        events = [] if curve is None else [(1000, key, curve)]
        timeline = [(0, "phase", "idle"), (1000, "phase", phase), *events, (6000, "end", "")]
        stats = preview.run(harness[2], work, 25, str(raw), timeline=timeline, curves=curves)
        assert stats["mismatches"] == 0
        frames = np.fromfile(raw, dtype="<u2").reshape(-1, 360, 360).astype(np.int32)
        pixel = np.stack([(frames >> 11) & 31, (frames >> 5) & 63, frames & 31], axis=-1)
        return pixel[:, band]

    for phase, key in (("listening", "mic"), ("speaking", "voice")):
        loud, quiet, nothing = ring(phase, key, "loud"), ring(phase, key, "quiet"), ring(phase, key, None)
        at = 4200 // 40
        assert np.abs(loud[at] - quiet[at]).sum(axis=-1).mean() >= 3.0, phase  # a clearly different ring
        # A quiet stream and no stream at all look alike: the glow dims, it does not fall back to a pulse.
        assert np.abs(quiet[at] - nothing[at]).sum(axis=-1).mean() <= 1.5, phase


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


def _mascot_rows(preview, work, binary, timeline, moments, first="starlight"):
    """Top and bottom screen rows the mascot (and its shadow) cover at each moment."""
    np = pytest.importorskip("numpy")
    raw = work / "caption.raw"
    stats = preview.run(binary, work, 25, str(raw), timeline=timeline, first=first)
    frames = np.fromfile(raw, dtype="<u2").reshape(-1, 360, 360).astype(np.int32)
    backdrop = frames[0]  # "setup": the ring glow over the backdrop, no mascot
    yy, xx = np.mgrid[0:360, 0:360]
    inside_ring = (xx - 180) ** 2 + (yy - 180) ** 2 < 146**2
    rows = {}
    for ms in moments:
        changed = (np.abs(frames[ms // 40] - backdrop) > 0) & inside_ring
        ys = np.where(changed.any(axis=1))[0]
        rows[ms] = (int(ys.min()), int(ys.max()))
    return stats, rows


def test_captioned_layout_leaves_the_text_band_free(harness) -> None:
    preview, work, binary = harness
    stats, rows = _mascot_rows(
        preview,
        work,
        binary,
        [
            (0, "phase", "setup"),
            (400, "phase", "wifi"),
            (2000, "caption", "on"),
            (4000, "phase", "connecting"),
            (6000, "caption", "off"),
            (6000, "phase", "idle"),
            (8000, "end", ""),
        ],
        [1600, 3600, 5600, 7600],
    )
    assert stats["mismatches"] == 0  # the shrink and regrow redraw exactly
    text_y = 226  # MascotScene::kCaptionTextY
    for ms in (3600, 5600):  # Wi-Fi setup, then connecting, both captioned
        top, bottom = rows[ms]
        assert bottom < text_y, f"mascot reaches row {bottom} at {ms} ms"
        assert top > 40
    for ms in (1600, 7600):  # full-size companion before and after
        assert rows[ms][1] > 290


def test_captioned_layout_is_wired_to_network_states() -> None:
    display = (BOARD_DIR / "memoria_mascot_display.cc").read_text(encoding="utf-8")
    wants = display[display.index("bool MemoriaMascotDisplay::WantsCaption") :]
    wants = wants[: wants.index("void MemoriaMascotDisplay::SetEmotion")]
    for state in ("kDeviceStateStarting", "kDeviceStateWifiConfiguring", "kDeviceStateActivating"):
        assert f"case {state}:" in wants
    assert "qr_visible_.load()" in wants
    assert "intro_active(now_ms)" in wants
    assert "scene_->SetCaptioned(want_caption, frame_now);" in display
    # Text on the caption band sits below the shrunken mascot.
    assert "memoria::MascotScene::kCaptionTextY" in display
    # Nothing is drawn over the QR card, and the 30 s "已连接" notice is
    # dropped once the device is online.
    opacity = display[display.index("void MemoriaMascotDisplay::ApplyChromeOpacity") :]
    opacity = opacity[: opacity.index("void MemoriaMascotDisplay::StyleQrCard")]
    assert "if (qr_visible_.load()) {\n        text_opa = 0;" in opacity
    assert "lv_obj_add_flag(notification_label_, LV_OBJ_FLAG_HIDDEN);" in display
    # The hotspot fallback hint is split into two short lines.
    assert "Lang::Strings::CONNECT_TO_HOTSPOT" in display
    assert '"\\n浏览器打开 "' in display


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
    for path in (
        BOARD_DIR / "memoria_mascot_pack.h",
        BOARD_DIR / "memoria_mascot_pack.cc",
        BOARD_DIR / "memoria_mascot_raster.h",
        BOARD_DIR / "memoria_mascot_raster.cc",
        BOARD_DIR / "memoria_mascot_scene.h",
        BOARD_DIR / "memoria_mascot_scene.cc",
        MEMORIA_DIR / "memoria_audio_level.h",
    ):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("esp_", "lvgl", "lv_", "freertos"):
            assert forbidden not in source, f"{path.name} must stay host-compilable"


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
    # The audio tasks' level taps drive the mouth and the glow: drained inside the display lock on every
    # frame (lit or not, so nothing stale plays back after the screen was off) and handed to the scene
    # before it renders.
    assert '#include "memoria_audio_level.h"' in display
    loop = display[display.index("void MemoriaMascotDisplay::AnimationLoop") :]
    take_spoken = loop.index("memoria::output_level_tap.Take()")
    take_heard = loop.index("memoria::input_level_tap.Take()")
    set_output = loop.index("scene_->SetOutputLevel(spoken.valley, spoken.peak, spoken.blocks, frame_now)")
    set_input = loop.index("scene_->SetInputLevel(heard.peak, heard.blocks, frame_now)")
    assert loop.index("Lock(100)") < take_spoken < set_output < loop.index("scene_->Render(")
    assert loop.index("Lock(100)") < take_heard < set_input < loop.index("scene_->Render(")
    assert "screen_off_ ?" not in loop[take_spoken - 200 : set_input]


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
    # Started on a bound boot; after a nearby binding the board restarts into
    # that same boot path instead of starting a second poll.
    assert protocol.count("StartDisplayProfilePoll();") == 1


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


def test_idle_clock_is_never_shown_on_the_screen() -> None:
    # Upstream's idle status bar writes "HH:MM" every 10 s; the product shows
    # no clock (user decision 2026-09-28), so SetStatus swallows it.
    display = (BOARD_DIR / "memoria_mascot_display.cc").read_text(encoding="utf-8")
    assert "bool IsClockText(const char* text)" in display
    set_status = display[display.index("void MemoriaMascotDisplay::SetStatus") :]
    set_status = set_status[: set_status.index("void MemoriaMascotDisplay::ShowNotification")]
    clock_branch = set_status[: set_status.index('LvglDisplay::SetStatus("");')]
    assert "IsClockText(status)" in clock_branch
