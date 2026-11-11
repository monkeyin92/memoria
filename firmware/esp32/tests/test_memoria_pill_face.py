"""Host checks for the robot's code-drawn face: shapes, blink, speech, easing, waking."""

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

using namespace memoria;

namespace {

constexpr int kSize = 360;

std::vector<uint16_t> Render(const PillFaceFrame& frame) {
    std::vector<uint16_t> buffer(kSize * kSize);
    PaintPillFace(buffer.data(), kSize, kSize, frame);
    return buffer;
}

uint32_t Hash(const std::vector<uint16_t>& buffer) {
    uint32_t h = 2166136261u;
    for (uint16_t pixel : buffer) {
        h ^= pixel;
        h *= 16777619u;
    }
    return h;
}

// Pixels that are not the background colour.
int Ink(const PillFaceFrame& frame, int x0, int y0, int x1, int y1) {
    const auto buffer = Render(frame);
    const uint16_t background = buffer[0];
    int count = 0;
    for (int y = y0; y < y1; ++y) {
        for (int x = x0; x < x1; ++x) {
            if (buffer[y * kSize + x] != background) {
                ++count;
            }
        }
    }
    return count;
}

// Rows (inside the left eye's column) that hold dark eye pixels.
int DarkRows(const PillFaceFrame& frame, int cx) {
    const auto buffer = Render(frame);
    int rows = 0;
    for (int y = 0; y < kSize; ++y) {
        bool dark = false;
        for (int x = cx - 4; x <= cx + 4; ++x) {
            const uint16_t v = buffer[y * kSize + x];
            const int r = ((v >> 11) & 31), g = ((v >> 5) & 63), b = (v & 31);
            if (r < 6 && g < 12 && b < 6) {
                dark = true;
            }
        }
        rows += dark ? 1 : 0;
    }
    return rows;
}

}  // namespace

int main() {
    const auto calm = MakePillFaceFrame(FaceId::kCalm, 0, 0, false, 0);
    const auto blink = MakePillFaceFrame(FaceId::kCalm, 255, 0, false, 0);
    const auto half = MakePillFaceFrame(FaceId::kCalm, 150, 0, false, 0);
    std::printf("calm_rows %d half_rows %d shut_rows %d\n", DarkRows(calm, calm.left.cx),
                DarkRows(half, half.left.cx), DarkRows(blink, blink.left.cx));

    // Faces differ from each other.
    const FaceId faces[] = {FaceId::kCalm,  FaceId::kWake,      FaceId::kListen, FaceId::kThink,
                            FaceId::kSpeak, FaceId::kHappy,     FaceId::kComfort, FaceId::kShy,
                            FaceId::kSleepy, FaceId::kSurprised};
    int distinct = 0;
    std::vector<uint32_t> seen;
    for (FaceId face : faces) {
        const uint32_t h = Hash(Render(MakePillFaceFrame(face, 0, 0, false, 0)));
        bool fresh = true;
        for (uint32_t other : seen) {
            fresh = fresh && other != h;
        }
        distinct += fresh ? 1 : 0;
        seen.push_back(h);
    }
    std::printf("distinct_faces %d of %zu\n", distinct, sizeof(faces) / sizeof(faces[0]));

    // The comfort and sleepy looks used to draw as a dot with a hairline; every
    // look's eye must now cover a real area.
    const auto sleepy = MakePillFaceFrame(FaceId::kSleepy, 0, 0, false, 0);
    const auto happy = MakePillFaceFrame(FaceId::kHappy, 0, 0, false, 0);
    std::printf("sleepy_ink %d happy_ink %d\n", Ink(sleepy, 90, 130, 180, 200),
                Ink(happy, 90, 130, 180, 200));

    // Speech: the mouth opens with the voice level, and a thinking look that
    // arrives while speaking still talks.
    const auto speak_closed = MakePillFaceFrame(FaceId::kSpeak, 0, 0, true, 0);
    const auto speak_open = MakePillFaceFrame(FaceId::kSpeak, 0, 0, true, 255);
    const auto think_speaking = MakePillFaceFrame(FaceId::kThink, 0, 0, true, 255);
    const auto think_silent = MakePillFaceFrame(FaceId::kThink, 0, 0, false, 255);
    std::printf("speech open %d %d think_speaking_open %d think_silent_open %d same_look %d\n",
                speak_closed.mouth_open, speak_open.mouth_open, think_speaking.mouth_open,
                think_silent.mouth_open, think_speaking.left.height == speak_open.left.height);

    // Easing: from calm to a happy look the eyes pass through in-between sizes.
    PillFaceTween tween;
    tween.Step(calm, 1000);          // starts the wake-up
    tween.Step(calm, 1300);          // wake-up done
    const bool idle_after = tween.animating();
    const auto happy_target = MakePillFaceFrame(FaceId::kListen, 0, 0, false, 0);
    const uint16_t from_h = tween.Step(calm, 1400).left.height;
    tween.Step(happy_target, 1500);  // the glide starts here and lasts 180 ms
    const uint16_t mid_h = tween.Step(happy_target, 1580).left.height;
    const uint16_t mid2_h = tween.Step(happy_target, 1620).left.height;
    const bool animating_mid = tween.animating();
    const uint16_t end_h = tween.Step(happy_target, 1800).left.height;
    std::printf("glide %u %u %u %u idle_after %d mid %d\n", from_h, mid_h, mid2_h, end_h, idle_after,
                animating_mid);

    // A blink passes straight through the easing.
    const uint8_t lid_now = tween.Step(MakePillFaceFrame(FaceId::kListen, 255, 0, false, 0), 1801).lid;
    std::printf("blink_lid %u\n", lid_now);

    // Waking: the first look opens the eyes from a closed line.
    PillFaceTween waking;
    const uint16_t wake0 = waking.Step(calm, 5000).left.height;
    const uint16_t wake1 = waking.Step(calm, 5130).left.height;
    const uint16_t wake2 = waking.Step(calm, 5400).left.height;
    waking.Reset();
    const uint16_t wake3 = waking.Step(calm, 9000).left.height;
    std::printf("wake %u %u %u %u target %u\n", wake0, wake1, wake2, wake3, calm.left.height);

    // Everything stays on the panel.
    int out_of_circle = 0;
    for (FaceId face : faces) {
        for (int lift : {0, 32}) {
            const auto buffer = Render(MakePillFaceFrame(face, 0, static_cast<uint8_t>(lift), true, 255));
            const uint16_t background = buffer[0];
            for (int y = 0; y < kSize; ++y) {
                for (int x = 0; x < kSize; ++x) {
                    const int dx = x - 180, dy = y - 180;
                    if (dx * dx + dy * dy > 168 * 168 && buffer[y * kSize + x] != background) {
                        ++out_of_circle;
                    }
                }
            }
        }
    }
    std::printf("outside_circle %d\n", out_of_circle);
    std::printf("palette %06x %06x\n", calm.background_rgb, calm.eye_rgb);
    return 0;
}
"""


@pytest.fixture(scope="module")
def output() -> dict[str, list[str]]:
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
        completed = subprocess.run([str(binary)], text=True, capture_output=True, check=True)
    parsed: dict[str, list[str]] = {}
    for line in completed.stdout.splitlines():
        key, *rest = line.split()
        parsed[key] = rest
    return parsed


def test_blink_closes_the_eye_to_a_line(output: dict[str, list[str]]) -> None:
    fields = output["calm_rows"]
    calm, half, shut = int(fields[0]), int(fields[2]), int(fields[4])
    assert calm > 90
    assert shut < 24
    assert shut < half < calm


def test_every_look_is_different(output: dict[str, list[str]]) -> None:
    distinct, _of, total = output["distinct_faces"]
    assert distinct == total


def test_arc_and_horizontal_eyes_cover_a_real_area(output: dict[str, list[str]]) -> None:
    # The squint look used to draw a round dot with a hairline through it.
    sleepy = int(output["sleepy_ink"][0])
    happy = int(output["sleepy_ink"][2])
    assert sleepy > 400
    assert happy > 400


def test_speech_opens_the_mouth_and_never_freezes(output: dict[str, list[str]]) -> None:
    fields = output["speech"]
    closed, opened = int(fields[1]), int(fields[2])
    assert closed == 0 < opened
    assert int(fields[4]) > 0, "a thinking look that arrives mid-reply must still talk"
    assert int(fields[6]) == 0, "a silent thinking face keeps its mouth shut"
    assert fields[8] == "1", "mid-reply thinking uses the speaking look"


def test_looks_glide_and_blinks_do_not(output: dict[str, list[str]]) -> None:
    start, mid, mid2, end = (int(value) for value in output["glide"][:4])
    assert start != end
    assert min(start, end) < mid < max(start, end)
    assert min(start, end) < mid2 < max(start, end)
    assert output["glide"][5] == "0"  # idle once the wake-up has settled
    assert output["glide"][7] == "1"  # still moving mid-glide
    assert output["blink_lid"] == ["255"]


def test_waking_opens_the_eyes_from_a_closed_line(output: dict[str, list[str]]) -> None:
    fields = output["wake"]
    first, second, settled, again = (int(value) for value in fields[:4])
    target = int(fields[5])
    assert first <= 16
    assert first < second < target
    assert settled == target
    assert again <= 16


def test_the_face_stays_inside_the_round_panel(output: dict[str, list[str]]) -> None:
    assert output["outside_circle"] == ["0"]
    assert output["palette"] == ["f28aa8", "17151a"]
