"""Host checks for the experimental smooth pill face renderer."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
SOURCE = BOARD_DIR / "memoria_pill_face.cc"

HARNESS = r"""
#include "memoria_pill_face.h"

#include <cstdint>
#include <cstdio>
#include <vector>

int main() {
    constexpr int kSize = 360;
    std::vector<uint16_t> buffer(kSize * kSize);
    const auto calm = memoria::MakePillFaceFrame(memoria::FaceId::kCalm, false, 0, 0);
    const auto calm_later = memoria::MakePillFaceFrame(memoria::FaceId::kCalm, false, 0, 700);
    const auto speaking = memoria::MakePillFaceFrame(memoria::FaceId::kSpeak, false, 0, 0);
    const auto speaking_later =
        memoria::MakePillFaceFrame(memoria::FaceId::kSpeak, false, 0, 700);
    const auto closed = memoria::MakePillFaceFrame(memoria::FaceId::kCalm, true, 0, 700);
    memoria::PaintPillFace(buffer.data(), kSize, kSize, calm);
    uint32_t first = 2166136261u;
    for (uint16_t pixel : buffer) {
        first ^= pixel;
        first *= 16777619u;
    }
    memoria::PaintPillFace(buffer.data(), kSize, kSize, calm_later);
    uint32_t calm_second = 2166136261u;
    for (uint16_t pixel : buffer) {
        calm_second ^= pixel;
        calm_second *= 16777619u;
    }
    memoria::PaintPillFace(buffer.data(), kSize, kSize, speaking);
    uint32_t speech_first = 2166136261u;
    for (uint16_t pixel : buffer) {
        speech_first ^= pixel;
        speech_first *= 16777619u;
    }
    memoria::PaintPillFace(buffer.data(), kSize, kSize, speaking_later);
    uint32_t speech_second = 2166136261u;
    for (uint16_t pixel : buffer) {
        speech_second ^= pixel;
        speech_second *= 16777619u;
    }
    std::printf("sig %u %u %u %u\n", first, calm_second, speech_first, speech_second);
    std::printf("palette %06x %06x\n", calm.background_rgb, calm.eye_rgb);
    std::printf("open %d %d %u %u\n", calm.left.cx, calm.right.cx, calm.left.width,
                calm.left.height);
    std::printf("closed %u %u\n", closed.left.width, closed.left.height);
    return first != calm_second || speech_first == speech_second ||
                   closed.left.height >= calm.left.height ||
                   calm.background_rgb != memoria::kPillBackgroundRgb ||
                   calm.eye_rgb != memoria::kPillEyeRgb ||
                   calm_later.background_rgb != calm.background_rgb
               ? 1
               : 0;
}
"""


@pytest.fixture(scope="module")
def tool():
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("no host C++ compiler available")
    with tempfile.TemporaryDirectory() as tmp:
        harness = pathlib.Path(tmp) / "harness.cc"
        harness.write_text(HARNESS, encoding="utf-8")
        binary = pathlib.Path(tmp) / "pill_face_tool"
        subprocess.run(
            [
                compiler,
                "-std=c++17",
                "-O1",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-fsanitize=undefined",
                "-fno-sanitize-recover=undefined",
                "-I",
                str(BOARD_DIR),
                str(harness),
                str(SOURCE),
                "-o",
                str(binary),
            ],
            check=True,
        )
        yield binary


def test_static_faces_and_discrete_speech_expressions(tool: pathlib.Path) -> None:
    completed = subprocess.run([str(tool)], text=True, capture_output=True, check=True)
    lines = completed.stdout.splitlines()
    assert lines[0].startswith("sig ")
    assert lines[1] == "palette f28aa8 17151a"
    assert lines[2].startswith("open ")
    closed_width, closed_height = map(int, lines[3].split()[1:])
    assert closed_width > 0
    assert closed_height < 40
