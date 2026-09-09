"""Host-side checks for the Memoria eyes-only device face.

The firmware renderer in ``overlay/files/main/boards/memoria/esp-vocat/
memoria_face.cc`` has no ESP-IDF or LVGL dependency on purpose, so these tests
compile the exact firmware source with the host compiler, render the faces, and
assert the geometry that was measured from the product reference photo:

    eye half width 0.26 R, eye centres +-0.35 R, dome centre on the centre line,
    closed eye = dome trimmed by a shallow cut arc (the reference crescent).

The reference photo itself cannot be shipped with the repository, so the numbers
below are the contract.
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
    printf("half_width=%.4f\n", face.left.half_width);
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


def _blobs(buffer, size: int) -> list[dict[str, float]]:
    """Bright blobs (eyes), left to right."""
    bright = [value > 0x8000 for value in buffer]
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


def _corner_y(buffer, size: int, blob, outer_is_left: bool) -> tuple[float, float]:
    """Mean y of the outermost and innermost pixel columns of one eye."""
    columns = range(blob["min_x"], blob["min_x"] + 2) if outer_is_left else range(
        blob["max_x"] - 1, blob["max_x"] + 1
    )
    outer = []
    for x in columns:
        for y in range(size):
            if buffer[y * size + x] > 0x8000:
                outer.append(y)
    inner_columns = (
        range(blob["max_x"] - 1, blob["max_x"] + 1)
        if outer_is_left
        else range(blob["min_x"], blob["min_x"] + 2)
    )
    inner = []
    for x in inner_columns:
        for y in range(size):
            if buffer[y * size + x] > 0x8000:
                inner.append(y)
    return sum(outer) / len(outer), sum(inner) / len(inner)


def test_renderer_compiles_clean(face_tool: pathlib.Path) -> None:
    assert face_tool.exists()


def test_neutral_matches_reference_geometry(face_tool: pathlib.Path) -> None:
    buffer, meta = _render(face_tool, "neutral")
    blobs = _blobs(buffer, SCREEN)
    assert len(blobs) == 2, "the neutral face must draw exactly two eyes"

    left, right = blobs
    # Eye half width 0.26 R -> 93.6 px wide on the 360 px panel.
    assert left["width"] == pytest.approx(94, abs=3)
    assert right["width"] == pytest.approx(94, abs=3)
    # Closed crescent height: dome (1.0 hw) plus the shallow trim.
    assert left["height"] == pytest.approx(47, abs=3)
    # Eye centres at +-0.35 R = 117 / 243 px.
    assert left["cx"] == pytest.approx(117, abs=2)
    assert right["cx"] == pytest.approx(243, abs=2)
    # The dome centre sits on the horizontal centre line, so the eye tips end
    # there as well.
    assert left["max_y"] == pytest.approx(RADIUS - 1, abs=3)
    assert right["max_y"] == pytest.approx(RADIUS - 1, abs=3)
    # Both eyes level.
    assert left["min_y"] == pytest.approx(right["min_y"], abs=1)

    assert meta["left_openness"] == pytest.approx(0.0)
    assert meta["right_openness"] == pytest.approx(0.0)
    assert meta["half_width"] == pytest.approx(0.26, abs=0.001)
    assert meta["left_cx"] == pytest.approx(-0.35, abs=0.001)
    assert meta["right_cx"] == pytest.approx(0.35, abs=0.001)
    assert meta["blinks"] == 0


def test_eyes_are_white_on_black(face_tool: pathlib.Path) -> None:
    buffer, _ = _render(face_tool, "neutral")
    assert buffer[0] == 0x0000
    assert max(buffer) == 0xFFFF
    # Inside the left eye, and on the screen centre between the eyes.
    assert buffer[160 * SCREEN + 117] == 0xFFFF
    assert buffer[SCREEN // 2 * SCREEN + SCREEN // 2] == 0x0000


@pytest.mark.parametrize("emotion", ["neutral", "happy", "sad", "surprised", "loving", "thinking"])
def test_every_emotion_draws_two_eyes(face_tool: pathlib.Path, emotion: str) -> None:
    buffer, _ = _render(face_tool, emotion)
    blobs = _blobs(buffer, SCREEN)
    assert len(blobs) == 2, f"{emotion} must draw exactly two eyes"


def test_happy_raises_the_outer_corners(face_tool: pathlib.Path) -> None:
    buffer, _ = _render(face_tool, "happy")
    left = _blobs(buffer, SCREEN)[0]
    outer, inner = _corner_y(buffer, SCREEN, left, outer_is_left=True)
    assert outer < inner - 2, "happy must lift the outer corner of the left eye"


def test_sad_drops_the_outer_corners(face_tool: pathlib.Path) -> None:
    buffer, _ = _render(face_tool, "sad")
    left = _blobs(buffer, SCREEN)[0]
    outer, inner = _corner_y(buffer, SCREEN, left, outer_is_left=True)
    assert outer > inner + 2, "sad must drop the outer corner of the left eye"


def test_loving_raises_the_inner_corners(face_tool: pathlib.Path) -> None:
    buffer, _ = _render(face_tool, "loving")
    left = _blobs(buffer, SCREEN)[0]
    outer, inner = _corner_y(buffer, SCREEN, left, outer_is_left=True)
    assert inner < outer - 2, "loving must lift the inner corner of the left eye"


def test_surprised_opens_round_eyes(face_tool: pathlib.Path) -> None:
    buffer, _ = _render(face_tool, "surprised")
    blobs = _blobs(buffer, SCREEN)
    for blob in blobs:
        assert abs(blob["width"] - blob["height"]) <= 4, "surprised eyes must be round"
    neutral, _ = _render(face_tool, "neutral")
    neutral_area = sum(blob["count"] for blob in _blobs(neutral, SCREEN))
    assert sum(blob["count"] for blob in blobs) > neutral_area * 1.5


def test_thinking_looks_up_and_to_the_side(face_tool: pathlib.Path) -> None:
    buffer, _ = _render(face_tool, "thinking")
    blobs = _blobs(buffer, SCREEN)
    neutral, _ = _render(face_tool, "neutral")
    neutral_blobs = _blobs(neutral, SCREEN)
    # Both eyes shift right ...
    assert blobs[0]["cx"] > neutral_blobs[0]["cx"] + 4
    assert blobs[1]["cx"] > neutral_blobs[1]["cx"] + 4
    # ... and up: the open discs are centred 0.10 R above the screen centre.
    for blob in blobs:
        assert (blob["min_y"] + blob["max_y"]) / 2 == pytest.approx(
            RADIUS - 0.10 * RADIUS, abs=3
        )


def test_blink_closes_open_eyes(face_tool: pathlib.Path) -> None:
    buffer, meta = _render(face_tool, "surprised", blink=1.0)
    assert meta["left_openness"] == pytest.approx(0.0)
    assert meta["right_openness"] == pytest.approx(0.0)
    blobs = _blobs(buffer, SCREEN)
    assert len(blobs) == 2
    assert blobs[0]["height"] == pytest.approx(47, abs=3)
    # Closed eyes keep the reference crescent even mid-blink.
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
    ):
        alias_pixels, _ = _render(face_tool, alias)
        canonical_pixels, _ = _render(face_tool, canonical)
        assert alias_pixels == canonical_pixels, f"{alias} must render as {canonical}"


def test_geometry_scales_with_the_panel(face_tool: pathlib.Path) -> None:
    buffer, _ = _render(face_tool, "neutral", size=180)
    blobs = _blobs(buffer, 180)
    assert len(blobs) == 2
    assert blobs[0]["width"] == pytest.approx(47, abs=2)
    assert blobs[0]["cx"] == pytest.approx(58.5, abs=2)


def test_board_wires_the_face_display() -> None:
    board = BOARD_SOURCE.read_text(encoding="utf-8")
    assert '#include "memoria_face_display.h"' in board
    assert "new MemoriaFaceDisplay(" in board
    assert "new SpiLcdDisplay(" not in board

    display = DISPLAY_SOURCE.read_text(encoding="utf-8")
    # White eyes need the dark theme; the light theme draws black text on black.
    assert 'GetTheme("dark")' in display
    assert "bg_image_src" in display
    # The colour emoji must never come back on this board.
    assert "emoji_image_, LV_OBJ_FLAG_HIDDEN" in display
    assert "emoji_label_, LV_OBJ_FLAG_HIDDEN" in display


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
    assert len(produced) >= 9
