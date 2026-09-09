"""Host-side checks for the Memoria conversation face.

The firmware renderer in ``overlay/files/main/boards/memoria/esp-vocat/
memoria_face.cc`` has no ESP-IDF or LVGL dependency on purpose, so these tests
compile the exact firmware source with the host compiler and assert the v3
conversation-face contract:

    eyes at +-0.30 R, y=-0.20 R, half width 0.205 R;
    closed eye = crescent; open eye = almond sclera minus pupil;
    mouth is the signature; unknown names fall back to neutral, never emoji.
"""

from __future__ import annotations

import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
RENDERER = BOARD_DIR / "memoria_face.cc"
HEADER = BOARD_DIR / "memoria_face.h"
DISPLAY_SOURCE = BOARD_DIR / "memoria_face_display.cc"
BOARD_SOURCE = BOARD_DIR / "memoria_esp_vocat.cc"
PREVIEW_SCRIPT = FIRMWARE_ROOT / "scripts" / "preview_memoria_face.py"

SCREEN = 360
RADIUS = SCREEN / 2

HARNESS = r"""
#include "memoria_face.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>

int main(int argc, char** argv) {
    if (argc < 2) {
        return 2;
    }
    const int size = argc > 2 ? atoi(argv[2]) : 360;
    static uint16_t buffer[512 * 512];
    if (size <= 0 || size > 512) {
        return 2;
    }
    memoria::Face face = memoria::FaceForEmotion(argv[1], argc > 3 ? atof(argv[3]) : 0.0f);
    printf("left_openness=%.4f\n", face.left.openness);
    printf("right_openness=%.4f\n", face.right.openness);
    printf("left_cx=%.4f\n", face.left.center_x);
    printf("right_cx=%.4f\n", face.right.center_x);
    printf("left_cy=%.4f\n", face.left.center_y);
    printf("right_cy=%.4f\n", face.right.center_y);
    printf("half_width=%.4f\n", face.left.half_width);
    printf("left_almond=%d\n", face.left.almond ? 1 : 0);
    printf("right_almond=%d\n", face.right.almond ? 1 : 0);
    printf("left_gaze_x=%.4f\n", face.left.gaze_x);
    printf("left_gaze_y=%.4f\n", face.left.gaze_y);
    printf("kind=%u\n", static_cast<unsigned>(face.kind));
    printf("blinks=%d\n", memoria::FaceBlinks(argv[1]) ? 1 : 0);
    printf("DATA\n");
    fflush(stdout);
    memoria::RenderFaceRgb565(buffer, size, size, face);
    fwrite(buffer, sizeof(uint16_t), static_cast<size_t>(size) * size, stdout);
    return 0;
}
"""


@pytest.fixture(scope="module")
def face_tool() -> pathlib.Path:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "harness.cc"
        source.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "face_tool"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                f"-I{BOARD_DIR}",
                str(source),
                str(RENDERER),
                "-o",
                str(binary),
            ],
            check=True,
        )
        yield binary


def _render(face_tool: pathlib.Path, emotion: str, size: int = SCREEN, blink: float = 0.0):
    result = subprocess.run(
        [str(face_tool), emotion, str(size), str(blink)],
        check=True,
        capture_output=True,
    )
    stdout = result.stdout
    marker = stdout.index(b"DATA\n")
    meta = {}
    for line in stdout[:marker].decode().splitlines():
        key, _, value = line.partition("=")
        meta[key] = float(value)
    pixels = stdout[marker + len(b"DATA\n") :]
    expected = size * size * 2
    assert len(pixels) >= expected, (emotion, len(pixels), expected)
    buffer = struct.unpack(f"<{size * size}H", pixels[:expected])
    return buffer, meta


def _px(buffer, x: int, y: int, size: int = SCREEN) -> int:
    return buffer[y * size + x]


def _bright(buffer, x: int, y: int, size: int = SCREEN, threshold: int = 0x8000) -> bool:
    return _px(buffer, x, y, size) > threshold


def _region_max(buffer, x0: int, y0: int, x1: int, y1: int, size: int = SCREEN) -> int:
    brightest = 0
    for y in range(y0, y1 + 1):
        row = y * size
        for x in range(x0, x1 + 1):
            brightest = max(brightest, buffer[row + x])
    return brightest


def _region_min(buffer, x0: int, y0: int, x1: int, y1: int, size: int = SCREEN) -> int:
    darkest = 0xFFFF
    for y in range(y0, y1 + 1):
        row = y * size
        for x in range(x0, x1 + 1):
            darkest = min(darkest, buffer[row + x])
    return darkest


def _blobs(buffer, size: int, threshold: int = 0x8000) -> list[dict[str, float]]:
    """Bright blobs, left to right."""
    bright = [value > threshold for value in buffer]
    seen = bytearray(size * size)
    found = []
    for start in range(size * size):
        if seen[start] or not bright[start]:
            continue
        stack = [start]
        seen[start] = 1
        count = 0
        min_x = max_x = start % size
        min_y = max_y = start // size
        while stack:
            index = stack.pop()
            count += 1
            x = index % size
            y = index // size
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            for nxt in (index + 1, index - 1, index + size, index - size):
                if 0 <= nxt < size * size and not seen[nxt] and bright[nxt]:
                    seen[nxt] = 1
                    stack.append(nxt)
        found.append(
            {
                "count": count,
                "min_x": min_x,
                "max_x": max_x,
                "min_y": min_y,
                "max_y": max_y,
                "width": max_x - min_x + 1,
                "height": max_y - min_y + 1,
                "cx": (min_x + max_x) / 2,
                "cy": (min_y + max_y) / 2,
            }
        )
    found.sort(key=lambda blob: blob["min_x"])
    return found


def _left_eye_blob(buffer, size: int = SCREEN) -> dict[str, float]:
    candidates = [
        blob
        for blob in _blobs(buffer, size)
        if blob["cx"] < size / 2 and blob["cy"] < size * 0.55 and blob["count"] > 40
    ]
    assert candidates, "left eye blob missing"
    return max(candidates, key=lambda blob: blob["count"])


def _corner_y(buffer, size: int, blob, outer_is_left: bool) -> tuple[float, float]:
    """Mean y of the outermost and innermost pixel columns of one eye."""
    y0 = int(blob["min_y"])
    y1 = int(blob["max_y"]) + 1
    columns = (
        range(blob["min_x"], blob["min_x"] + 2)
        if outer_is_left
        else range(blob["max_x"] - 1, blob["max_x"] + 1)
    )
    outer = []
    for x in columns:
        for y in range(y0, y1):
            if buffer[y * size + x] > 0x8000:
                outer.append(y)
    inner_columns = (
        range(blob["max_x"] - 1, blob["max_x"] + 1)
        if outer_is_left
        else range(blob["min_x"], blob["min_x"] + 2)
    )
    inner = []
    for x in inner_columns:
        for y in range(y0, y1):
            if buffer[y * size + x] > 0x8000:
                inner.append(y)
    assert outer and inner, "eye corner samples missing"
    return sum(outer) / len(outer), sum(inner) / len(inner)


def test_renderer_compiles_clean(face_tool: pathlib.Path) -> None:
    assert face_tool.exists()


def test_neutral_is_a_closed_conversation_face(face_tool: pathlib.Path) -> None:
    buffer, meta = _render(face_tool, "neutral")
    assert meta["left_openness"] == pytest.approx(0.0)
    assert meta["right_openness"] == pytest.approx(0.0)
    assert meta["left_almond"] == 0
    assert meta["half_width"] == pytest.approx(0.205, abs=0.001)
    assert meta["left_cx"] == pytest.approx(-0.30, abs=0.001)
    assert meta["right_cx"] == pytest.approx(0.30, abs=0.001)
    assert meta["left_cy"] == pytest.approx(-0.20, abs=0.001)
    assert meta["blinks"] == 0

    left = _left_eye_blob(buffer)
    assert left["cx"] == pytest.approx(126, abs=6)
    assert left["cy"] == pytest.approx(127, abs=8)
    assert left["width"] == pytest.approx(74, abs=8)
    assert left["max_y"] < RADIUS - 8

    # Mouth sits in the lower half; the exact screen centre stays dark.
    assert _region_max(buffer, 160, 220, 200, 240) > 0x8000
    assert _px(buffer, SCREEN // 2, SCREEN // 2) == 0x0000


def test_face_is_white_on_black(face_tool: pathlib.Path) -> None:
    buffer, _ = _render(face_tool, "neutral")
    assert buffer[0] == 0x0000
    assert max(buffer) == 0xFFFF
    left = _left_eye_blob(buffer)
    assert _bright(buffer, int(round(left["cx"])), int(round(left["cy"])))
    assert _px(buffer, SCREEN // 2, SCREEN // 2) == 0x0000


@pytest.mark.parametrize("emotion", ["neutral", "happy", "sad", "surprised", "loving", "thinking", "embarrassed", "wink", "speaking"])
def test_every_emotion_draws_ink(face_tool: pathlib.Path, emotion: str) -> None:
    buffer, _ = _render(face_tool, emotion)
    assert max(buffer) == 0xFFFF
    assert buffer[0] == 0x0000
    assert any(value > 0x8000 for value in buffer)


def test_happy_raises_the_outer_corners_and_smiles(face_tool: pathlib.Path) -> None:
    buffer, _ = _render(face_tool, "happy")
    left = _left_eye_blob(buffer)
    outer, inner = _corner_y(buffer, SCREEN, left, outer_is_left=True)
    assert outer < inner - 2, "happy must lift the outer corner of the left eye"
    # Four-pointed stars in the upper corners, smile in the lower half.
    assert _region_max(buffer, 50, 70, 90, 110) > 0x8000
    assert _region_max(buffer, 270, 70, 310, 110) > 0x8000
    assert _region_max(buffer, 150, 215, 210, 250) > 0x8000


def test_sad_drops_the_outer_corners_and_has_a_tear(face_tool: pathlib.Path) -> None:
    buffer, _ = _render(face_tool, "sad")
    left = _left_eye_blob(buffer)
    outer, inner = _corner_y(buffer, SCREEN, left, outer_is_left=True)
    assert outer > inner + 2, "sad must drop the outer corner of the left eye"
    # Tear on the right cheek.
    assert _region_max(buffer, 240, 155, 290, 200) > 0x8000


def test_loving_raises_the_inner_corners(face_tool: pathlib.Path) -> None:
    buffer, _ = _render(face_tool, "loving")
    left = _left_eye_blob(buffer)
    outer, inner = _corner_y(buffer, SCREEN, left, outer_is_left=True)
    assert inner < outer - 2, "loving must lift the inner corner of the left eye"
    # Blush is grey, not full white.
    cheek = _region_max(buffer, 70, 190, 110, 220)
    assert 0x2000 < cheek < 0xE000


def test_surprised_has_pupils_not_solid_discs(face_tool: pathlib.Path) -> None:
    buffer, meta = _render(face_tool, "surprised")
    assert meta["left_almond"] == 1
    assert meta["blinks"] == 1
    # Eye centres are the punched-out pupils.
    left_cx = int(RADIUS + meta["left_cx"] * RADIUS + meta["left_gaze_x"] * RADIUS)
    left_cy = int(RADIUS + meta["left_cy"] * RADIUS + meta["left_gaze_y"] * RADIUS)
    assert _px(buffer, left_cx, left_cy) < 0x2000
    assert _region_max(buffer, left_cx - 24, left_cy - 8, left_cx - 12, left_cy + 8) > 0x8000
    # Small round mouth in the lower half.
    assert _region_max(buffer, 165, 210, 195, 245) > 0x8000
    assert _region_min(buffer, 176, 226, 184, 234) < 0x4000


def test_thinking_looks_up_and_to_the_side(face_tool: pathlib.Path) -> None:
    buffer, meta = _render(face_tool, "thinking")
    assert meta["left_gaze_x"] == pytest.approx(0.055, abs=0.001)
    assert meta["left_gaze_y"] == pytest.approx(-0.045, abs=0.001)
    assert meta["left_cx"] > -0.30
    surprised, _ = _render(face_tool, "surprised")
    # Thought dots sit above the surprised almond, not in the eye itself.
    assert _region_max(buffer, 280, 55, 320, 95) > 0x8000
    assert _region_max(surprised, 280, 55, 320, 95) < 0x4000


def test_embarrassed_is_not_happy(face_tool: pathlib.Path) -> None:
    embarrassed, meta_e = _render(face_tool, "embarrassed")
    happy, meta_h = _render(face_tool, "happy")
    assert embarrassed != happy
    assert meta_e["kind"] != meta_h["kind"]
    # Sweat drop at the left temple.
    assert _region_max(embarrassed, 55, 70, 95, 120) > 0x8000


def test_wink_is_asymmetric(face_tool: pathlib.Path) -> None:
    buffer, meta = _render(face_tool, "wink")
    assert meta["left_openness"] == pytest.approx(0.0)
    assert meta["right_openness"] == pytest.approx(1.0)
    assert meta["left_almond"] == 0
    assert meta["right_almond"] == 1
    assert meta["blinks"] == 0
    left = _left_eye_blob(buffer)
    right_cx = int(RADIUS + meta["right_cx"] * RADIUS)
    right_cy = int(RADIUS + meta["right_cy"] * RADIUS)
    # Closed left crescent is solid; open right eye has a pupil hole.
    assert _bright(buffer, int(round(left["cx"])), int(round(left["cy"])))
    assert _px(buffer, right_cx, right_cy) < 0x2000


def test_speaking_is_an_open_viseme(face_tool: pathlib.Path) -> None:
    buffer, meta = _render(face_tool, "speaking")
    assert meta["left_almond"] == 1
    assert meta["blinks"] == 1
    # Flattened open mouth, hollow in the middle.
    assert _region_max(buffer, 155, 215, 205, 245) > 0x8000
    assert _region_min(buffer, 172, 222, 188, 232) < 0x4000


def test_blink_closes_open_eyes(face_tool: pathlib.Path) -> None:
    buffer, meta = _render(face_tool, "surprised", blink=1.0)
    assert meta["left_openness"] == pytest.approx(0.0)
    assert meta["right_openness"] == pytest.approx(0.0)
    left = _left_eye_blob(buffer)
    assert left["height"] <= 42
    _, meta = _render(face_tool, "surprised", blink=0.5)
    assert 0.0 < meta["left_openness"] < 1.0


def test_closed_faces_ignore_blink(face_tool: pathlib.Path) -> None:
    _, meta = _render(face_tool, "neutral", blink=1.0)
    assert meta["left_openness"] == pytest.approx(0.0)


def test_unknown_emotions_fall_back_to_neutral(face_tool: pathlib.Path) -> None:
    unknown, _ = _render(face_tool, "cancel")
    neutral, _ = _render(face_tool, "neutral")
    assert unknown == neutral


def test_emotion_names_are_case_insensitive(face_tool: pathlib.Path) -> None:
    upper, _ = _render(face_tool, "Happy")
    lower, _ = _render(face_tool, "happy")
    assert upper == lower


def test_aliases_map_onto_the_canonical_faces(face_tool: pathlib.Path) -> None:
    for alias, canonical in (
        ("idle", "neutral"),
        ("sleepy", "neutral"),
        ("laughing", "happy"),
        ("crying", "sad"),
        ("angry", "sad"),
        ("shocked", "surprised"),
        ("caring", "loving"),
        ("curious", "thinking"),
        ("confused", "thinking"),
        ("winking", "wink"),
        ("talking", "speaking"),
    ):
        alias_pixels, _ = _render(face_tool, alias)
        canonical_pixels, _ = _render(face_tool, canonical)
        assert alias_pixels == canonical_pixels, f"{alias} must render as {canonical}"


def test_geometry_scales_with_the_panel(face_tool: pathlib.Path) -> None:
    buffer, _ = _render(face_tool, "neutral", size=180)
    left = _left_eye_blob(buffer, 180)
    assert left["width"] == pytest.approx(37, abs=5)
    assert left["cx"] == pytest.approx(63, abs=4)


def test_board_wires_the_face_display() -> None:
    board = BOARD_SOURCE.read_text(encoding="utf-8")
    assert '#include "memoria_face_display.h"' in board
    assert "new MemoriaFaceDisplay(" in board
    assert "new SpiLcdDisplay(" not in board

    display = DISPLAY_SOURCE.read_text(encoding="utf-8")
    # White face needs the dark theme; the light theme draws black text on black.
    assert 'GetTheme("dark")' in display
    assert "bg_image_src" in display
    # The colour emoji must never come back on this board.
    assert "emoji_image_, LV_OBJ_FLAG_HIDDEN" in display
    assert "emoji_label_, LV_OBJ_FLAG_HIDDEN" in display


def test_idle_screen_tap_and_body_pat_do_not_start_chat() -> None:
    board = BOARD_SOURCE.read_text(encoding="utf-8")
    touch = board[
        board.index("void HandleScreenTouchRelease()") : board.index(
            "void InitializeI2c()"
        )
    ]
    assert "kDeviceStateSpeaking" in touch
    assert "AbortSpeaking(kAbortReasonNone)" in touch
    assert "kDeviceStateListening" in touch
    assert "StopListening()" in touch
    assert "idle screen tap ignored; wake word or BOOT starts chat" in touch
    assert "kDeviceStateIdle || state == kDeviceStateConnecting" in touch
    idle_branch = touch[touch.index("kDeviceStateIdle") :]
    assert "ToggleChatState()" not in idle_branch
    assert "StartListening(" not in touch
    assert "MuteImuForTouch()" in touch
    assert "SetEmotion(" not in touch

    imu = board[board.index("static void imu_event_task") : board.index("void MuteImuForTouch()")]
    assert '#include "memoria_pat.h"' in board
    assert "memoria::PatDetector detector" in imu
    assert "Device shake ignored" in imu
    assert "Device pat ignored (touch rumble" in imu
    assert "imu_mute_until_ms_" in imu
    assert "kPatDeltaThreshold = 6000" not in board
    assert "kPatQuietMax" not in board

    pat = board[board.index("void OnDevicePat(") : board.index("static void imu_event_task")]
    assert 'SetEmotion("surprised")' in pat
    assert "kDeviceStateIdle" in pat
    assert "ToggleChatState()" not in pat
    assert "StartListening(" not in pat
    assert "AbortSpeaking(" not in pat
    restore = board[
        board.index("static void PatRestoreCallback") : board.index(
            "void EnsurePatRestoreTimer()"
        )
    ]
    assert 'SetEmotion("neutral")' in restore
    assert "ToggleChatState()" not in restore


def test_renderer_stays_free_of_esp_dependencies() -> None:
    for path in (HEADER, RENDERER):
        source = path.read_text(encoding="utf-8")
        for forbidden in ("esp_", "lvgl", "lv_", "freertos"):
            assert forbidden not in source, f"{path.name} must stay host-compilable"


def test_preview_script_is_shipped() -> None:
    assert PREVIEW_SCRIPT.exists()
    text = PREVIEW_SCRIPT.read_text(encoding="utf-8")
    assert "memoria_face.cc" in text
    assert "--out" in text


def test_preview_script_runs(face_tool: pathlib.Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [sys.executable, str(PREVIEW_SCRIPT), "--out", tmp],
            check=True,
            capture_output=True,
        )
        produced = sorted(path.name for path in pathlib.Path(tmp).glob("*.png"))
    assert "neutral.png" in produced
    assert "sheet.png" in produced
    assert "speaking.png" in produced
    assert len(produced) >= 12
