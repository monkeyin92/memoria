"""Host checks for the yellow dot-matrix face.

The mapper and the painter live in memoria_dot_face.cc, which has no ESP-IDF
dependency. These tests compile that file and drive it the way the panel does.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import tempfile

import pytest

FIRMWARE_ROOT = pathlib.Path(__file__).parents[1]
BOARD_DIR = FIRMWARE_ROOT / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
ASSETS_DIR = BOARD_DIR / "assets"
DISPLAY = BOARD_DIR / "memoria_mascot_display.cc"

WIDTH = 360
YELLOW = 0xFFE0
FACE_TOP_MIN_Y = 18
FACE_TOP_MAX_Y = 46
NAMES = ("calm", "listen", "think", "speak", "connect", "greet", "pair")
FORBIDDEN = ("surprised", "shocked", "scared", "angry", "dizzy", "惊吓", "生气")

# Patched DeviceState values. The display static_asserts the same numbers.
UNKNOWN, STARTING, WIFI, IDLE, CONNECTING, RECOVERING = 0, 1, 2, 3, 4, 5
LISTENING, SPEAKING, UPGRADING, ACTIVATING, AUDIO, FATAL = 6, 7, 8, 9, 10, 11

HARNESS = r"""
#include "memoria_dot_face.h"

#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace {

std::vector<uint16_t> g_buffer(memoria::kFaceScreen * memoria::kFaceScreen);

void PrintFacts(const char* label, const memoria::FaceFacts& facts) {
    const memoria::FaceId face = memoria::FaceForFacts(facts);
    std::printf("map %s %d %s\n", label, facts.state, memoria::FaceName(face));
}

}  // namespace

int main() {
    std::printf("count %d\n", memoria::kFaceCount);
    for (int i = 0; i < memoria::kFaceCount; ++i) {
        const auto id = static_cast<memoria::FaceId>(i);
        std::printf("name %d %s\n", i, memoria::FaceName(id));
    }

    const int states[] = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 99};
    for (int state : states) {
        memoria::FaceFacts plain;
        plain.state = state;
        PrintFacts("plain", plain);

        memoria::FaceFacts crisis = plain;
        crisis.crisis = true;
        PrintFacts("crisis", crisis);

        memoria::FaceFacts spoke = plain;
        spoke.user_spoke = true;
        PrintFacts("spoke", spoke);

        memoria::FaceFacts intro = plain;
        intro.intro = true;
        PrintFacts("intro", intro);

        memoria::FaceFacts setup = plain;
        setup.setup = true;
        PrintFacts("setup", setup);

        memoria::FaceFacts crisis_intro = plain;
        crisis_intro.crisis = true;
        crisis_intro.intro = true;
        crisis_intro.setup = true;
        crisis_intro.user_spoke = true;
        PrintFacts("crisis_all", crisis_intro);
    }

    for (int i = 0; i < memoria::kFaceCount; ++i) {
        const auto id = static_cast<memoria::FaceId>(i);
        for (int captioned = 0; captioned < 2; ++captioned) {
            memoria::PaintDotFace(g_buffer.data(), memoria::kFaceScreen, memoria::kFaceScreen, id,
                                  captioned != 0);
            const memoria::FaceRect box = memoria::FaceBounds(captioned != 0);
            int yellow = 0;
            int other = 0;
            int edge = 0;
            int outside = 0;
            unsigned sig = 2166136261u;
            int min_y = memoria::kFaceScreen;
            int max_y = 0;
            int min_x = memoria::kFaceScreen;
            int max_x = 0;
            for (int y = 0; y < memoria::kFaceScreen; ++y) {
                for (int x = 0; x < memoria::kFaceScreen; ++x) {
                    const uint16_t pixel = g_buffer[static_cast<size_t>(y) * memoria::kFaceScreen + x];
                    if (pixel == 0) {
                        continue;
                    }
                    if (pixel != memoria::kFaceYellow) {
                        ++other;
                        continue;
                    }
                    ++yellow;
                    if (x < box.x0 || x >= box.x1 || y < box.y0 || y >= box.y1) {
                        ++outside;
                    }
                    sig ^= static_cast<unsigned>(x * 131 + y);
                    sig *= 16777619u;
                    if (x < 8 || y < 8 || x >= memoria::kFaceScreen - 8 || y >= memoria::kFaceScreen - 8) {
                        ++edge;
                    }
                    if (y < min_y) min_y = y;
                    if (y > max_y) max_y = y;
                    if (x < min_x) min_x = x;
                    if (x > max_x) max_x = x;
                }
            }
            int rim = 0;
            int rim_yellow = 0;
            if (yellow > 0) {
                for (int x = min_x; x <= max_x; ++x) {
                    for (int y : {min_y, max_y}) {
                        ++rim;
                        const uint16_t pixel =
                            g_buffer[static_cast<size_t>(y) * memoria::kFaceScreen + x];
                        if (pixel == memoria::kFaceYellow) ++rim_yellow;
                    }
                }
                for (int y = min_y + 1; y < max_y; ++y) {
                    for (int x : {min_x, max_x}) {
                        ++rim;
                        const uint16_t pixel =
                            g_buffer[static_cast<size_t>(y) * memoria::kFaceScreen + x];
                        if (pixel == memoria::kFaceYellow) ++rim_yellow;
                    }
                }
            }
            std::printf(
                "paint %s %d yellow=%d other=%d edge=%d min_y=%d max_y=%d sig=%u rim=%d "
                "rim_yellow=%d outside=%d\n",
                memoria::FaceName(id), captioned, yellow, other, edge, min_y, max_y, sig, rim,
                rim_yellow, outside);
        }
    }
    return 0;
}
"""


def _compile(work: pathlib.Path) -> pathlib.Path:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        pytest.skip("host C++ compiler unavailable")
    source = work / "harness.cc"
    source.write_text(HARNESS, encoding="utf-8")
    binary = work / "dot_face"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            f"-I{BOARD_DIR}",
            str(source),
            str(BOARD_DIR / "memoria_dot_face.cc"),
            "-o",
            str(binary),
        ],
        check=True,
    )
    return binary


@pytest.fixture(scope="module")
def report() -> dict:
    work = pathlib.Path(tempfile.mkdtemp(prefix="memoria-dot-face-"))
    try:
        binary = _compile(work)
        completed = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    lines = [line.split() for line in completed.stdout.splitlines() if line]
    mapped: dict[tuple[str, str], str] = {}
    names: list[str] = []
    paints: dict[tuple[str, int], dict[str, int]] = {}
    count = None
    for parts in lines:
        if parts[0] == "count":
            count = int(parts[1])
        elif parts[0] == "name":
            names.append(parts[2])
        elif parts[0] == "map":
            mapped[(parts[1], parts[2])] = parts[3]
        elif parts[0] == "paint":
            fields = {}
            for item in parts[3:]:
                key, value = item.split("=")
                if "," in value:
                    continue
                fields[key] = int(value)
            paints[(parts[1], int(parts[2]))] = fields
    return {"count": count, "names": names, "mapped": mapped, "paints": paints}


def _face(report: dict, kind: str, state: int) -> str:
    return report["mapped"][(kind, str(state))]


def test_there_are_exactly_seven_faces(report: dict) -> None:
    assert report["count"] == 7
    assert tuple(report["names"]) == NAMES
    assert len(set(report["names"])) == 7
    for name in report["names"]:
        assert name not in FORBIDDEN
        for banned in FORBIDDEN:
            assert banned not in name


def test_device_state_selects_the_face(report: dict) -> None:
    expect = {
        UNKNOWN: "calm",
        STARTING: "connect",
        WIFI: "connect",
        IDLE: "calm",
        CONNECTING: "connect",
        RECOVERING: "connect",
        LISTENING: "listen",
        SPEAKING: "speak",
        UPGRADING: "connect",
        ACTIVATING: "connect",
        AUDIO: "listen",
        FATAL: "calm",
        99: "calm",
    }
    for state, name in expect.items():
        assert _face(report, "plain", state) == name
    assert _face(report, "spoke", LISTENING) == "think"
    assert _face(report, "spoke", SPEAKING) == "speak"
    assert _face(report, "spoke", IDLE) == "calm"
    assert _face(report, "intro", IDLE) == "greet"
    assert _face(report, "intro", SPEAKING) == "greet"
    assert _face(report, "setup", CONNECTING) == "pair"
    assert _face(report, "setup", IDLE) == "pair"


def test_crisis_is_always_the_calm_face(report: dict) -> None:
    for state in (UNKNOWN, STARTING, WIFI, IDLE, CONNECTING, RECOVERING, LISTENING, SPEAKING,
                  UPGRADING, ACTIVATING, AUDIO, FATAL, 99):
        assert _face(report, "crisis", state) == "calm"
        assert _face(report, "crisis_all", state) == "calm"


def test_paint_is_yellow_dots_without_a_frame(report: dict) -> None:
    signatures = set()
    for name in NAMES:
        for captioned in (0, 1):
            paint = report["paints"][(name, captioned)]
            assert paint["yellow"] > 0
            assert paint["other"] == 0
            assert paint["edge"] == 0
            assert paint["outside"] == 0
            assert paint["rim"] > 0
            assert paint["rim_yellow"] * 2 < paint["rim"]
            signatures.add(paint["sig"])
        rest = report["paints"][(name, 0)]
        lifted = report["paints"][(name, 1)]
        assert lifted["min_y"] < rest["min_y"]
        assert lifted["max_y"] <= 216
        assert FACE_TOP_MIN_Y <= lifted["min_y"] <= FACE_TOP_MAX_Y
        assert lifted["sig"] != rest["sig"]
    assert len(signatures) == 14


def test_mascot_stickers_are_not_in_the_board_assets() -> None:
    packed = sorted(path.name for path in ASSETS_DIR.glob("mascot_*.mmp"))
    assert packed == []
    fixtures = FIRMWARE_ROOT / "tests" / "fixtures" / "mascots"
    kept = sorted(path.name for path in fixtures.glob("mascot_*.mmp"))
    assert kept == [
        "mascot_axu.mmp",
        "mascot_mianmian.mmp",
        "mascot_starlight.mmp",
        "mascot_taoxi.mmp",
        "mascot_xuanmo.mmp",
    ]


def test_display_paints_from_device_state_not_emotion_names() -> None:
    source = DISPLAY.read_text(encoding="utf-8")
    assert "PaintDotFace(" in source
    assert "FaceForFacts(" in source
    assert "PlaybackStopRegistry" in source
    assert "SceneMoodFromName" not in source
    assert "kFaceStateSpeaking" in source
    loop = source[source.index("void MemoriaMascotDisplay::AnimationLoop") :]
    assert "scene_->Render(" not in loop
    assert loop.index("scene_->SetOutputLevel(") < loop.index("PaintDotFace(")
