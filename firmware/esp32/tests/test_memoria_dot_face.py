"""Host checks for the dot-matrix face.

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
FACE_TOP_MIN_Y = 18
FACE_TOP_MAX_Y = 46
NAMES = (
    "calm",
    "happy",
    "listen",
    "think",
    "speak",
    "surprised",
    "shy",
    "sleepy",
    "comfort",
    "wake",
)
# The approved closed-calm art (the crisis blink), as painted before the blink was drawn from it.
CLOSED_CALM_SIG = 2976732367
# The AI badge sits at y 15..43 (compliance opinion 2026-10-22, item A): nothing that moves may reach it.
BADGE_BOTTOM = 48
FORBIDDEN = ("shocked", "scared", "angry", "dizzy", "惊吓", "生气")

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

constexpr int kScreen = memoria::kFaceScreen;

unsigned Signature(const std::vector<uint16_t>& buffer) {
    unsigned sig = 2166136261u;
    for (int y = 0; y < kScreen; ++y) {
        for (int x = 0; x < kScreen; ++x) {
            const uint16_t pixel = buffer[static_cast<size_t>(y) * kScreen + x];
            if (pixel != 0) {
                sig ^= static_cast<unsigned>(x * 131 + y + pixel);
                sig *= 16777619u;
            }
        }
    }
    return sig;
}

void Paint(std::vector<uint16_t>& buffer, memoria::FaceId face, memoria::EyeShape eyes, memoria::MouthShape mouth) {
    memoria::FaceFrame frame;
    frame.face = face;
    frame.eyes = eyes;
    frame.mouth = mouth;
    memoria::PaintDotFace(buffer.data(), kScreen, kScreen, frame);
}

bool Inside(const memoria::FaceRect& r, int x, int y) { return x >= r.x0 && x < r.x1 && y >= r.y0 && y < r.y1; }

// The art's features: lit pixels joined when they are within 11 px (city-block) of each other, which keeps a
// ring of dots together and a cheek apart from the eye beside it.
std::vector<int> Features(const std::vector<uint16_t>& buffer, int* count) {
    std::vector<int> label(buffer.size(), -1);
    int next = 0;
    std::vector<int> stack;
    for (int start = 0; start < kScreen * kScreen; ++start) {
        if (buffer[start] == 0 || label[start] >= 0) {
            continue;
        }
        label[start] = next;
        stack.push_back(start);
        while (!stack.empty()) {
            const int at = stack.back();
            stack.pop_back();
            const int ay = at / kScreen;
            const int ax = at % kScreen;
            for (int dy = -11; dy <= 11; ++dy) {
                const int span = 11 - (dy < 0 ? -dy : dy);
                for (int dx = -span; dx <= span; ++dx) {
                    const int y = ay + dy;
                    const int x = ax + dx;
                    if (x < 0 || y < 0 || x >= kScreen || y >= kScreen) {
                        continue;
                    }
                    const int i = y * kScreen + x;
                    if (buffer[i] != 0 && label[i] < 0) {
                        label[i] = next;
                        stack.push_back(i);
                    }
                }
            }
        }
        ++next;
    }
    *count = next;
    return label;
}

// For a moving region `rect` of one face: what a shape changed there. A feature of the open art that does
// not lie wholly inside the rectangle (a cheek, the speech marks) must come through unchanged; the pixels
// left lit that belong to no such feature are the shape itself, reported as their row range.
void RegionReport(const char* kind, memoria::FaceId face, int index, const memoria::FaceRect& rect,
                  const std::vector<uint16_t>& open, const std::vector<uint16_t>& shaped, const char* shape) {
    int features = 0;
    const std::vector<int> label = Features(open, &features);
    std::vector<int> x0(features, kScreen), y0(features, kScreen), x1(features, -1), y1(features, -1);
    for (int i = 0; i < kScreen * kScreen; ++i) {
        if (label[i] < 0) continue;
        const int f = label[i];
        const int x = i % kScreen;
        const int y = i / kScreen;
        x0[f] = x < x0[f] ? x : x0[f];
        y0[f] = y < y0[f] ? y : y0[f];
        x1[f] = x > x1[f] ? x : x1[f];
        y1[f] = y > y1[f] ? y : y1[f];
    }
    std::vector<bool> moving(features, false);
    for (int f = 0; f < features; ++f) {
        moving[f] = x0[f] >= rect.x0 && y0[f] >= rect.y0 && x1[f] < rect.x1 && y1[f] < rect.y1;
    }
    int kept_changed = 0;
    int outside_changed = 0;
    int moving_px = 0;
    int shape_min_y = kScreen;
    int shape_max_y = -1;
    for (int i = 0; i < kScreen * kScreen; ++i) {
        const int x = i % kScreen;
        const int y = i / kScreen;
        if (!Inside(rect, x, y)) {
            outside_changed += open[i] != shaped[i];
            continue;
        }
        const bool kept = label[i] >= 0 && !moving[label[i]];
        if (kept) {
            kept_changed += open[i] != shaped[i];
            continue;
        }
        moving_px += label[i] >= 0;
        if (shaped[i] != 0) {
            shape_min_y = y < shape_min_y ? y : shape_min_y;
            shape_max_y = y > shape_max_y ? y : shape_max_y;
        }
    }
    std::printf("region %s %s %d %s kept_changed=%d outside_changed=%d moving_px=%d shape_min_y=%d shape_max_y=%d "
                "rect_y0=%d rect_y1=%d\n",
                kind, memoria::FaceName(face), index, shape, kept_changed, outside_changed, moving_px, shape_min_y,
                shape_max_y, rect.y0, rect.y1);
}

void ShapeChecks() {
    using memoria::EyeShape;
    using memoria::MouthShape;
    std::vector<uint16_t> open(kScreen * kScreen), shaped(kScreen * kScreen), full(kScreen * kScreen);
    for (int i = 0; i < memoria::kFaceCount; ++i) {
        const auto face = static_cast<memoria::FaceId>(i);
        Paint(open, face, EyeShape::kOpen, MouthShape::kOpen);
        for (int e = 0; e < memoria::kFaceEyeCount; ++e) {
            const memoria::FaceRect rect = memoria::FaceEyeRect(face, e);
            std::printf("rect eye %s %d %d %d %d %d\n", memoria::FaceName(face), e, rect.x0, rect.y0, rect.x1, rect.y1);
            Paint(shaped, face, EyeShape::kHalf, MouthShape::kOpen);
            RegionReport("eye", face, e, rect, open, shaped, "half");
            Paint(shaped, face, EyeShape::kShut, MouthShape::kOpen);
            RegionReport("eye", face, e, rect, open, shaped, "shut");
        }
        const memoria::FaceRect mouth = memoria::FaceMouthRect(face);
        std::printf("rect mouth %s 0 %d %d %d %d\n", memoria::FaceName(face), mouth.x0, mouth.y0, mouth.x1, mouth.y1);
        Paint(shaped, face, EyeShape::kOpen, MouthShape::kShut);
        std::printf("mouthstatic %s %d\n", memoria::FaceName(face), shaped == open && mouth.x0 == mouth.x1 ? 1 : 0);
        if (mouth.x0 != mouth.x1) {
            Paint(shaped, face, EyeShape::kOpen, MouthShape::kHalf);
            RegionReport("mouth", face, 0, mouth, open, shaped, "half");
            Paint(shaped, face, EyeShape::kOpen, MouthShape::kShut);
            RegionReport("mouth", face, 0, mouth, open, shaped, "shut");
        }
        // Repainting only the dirty rectangles over any shown frame of this face leaves the full paint.
        const EyeShape eyes[] = {EyeShape::kOpen, EyeShape::kHalf, EyeShape::kShut};
        const MouthShape mouths[] = {MouthShape::kOpen, MouthShape::kHalf, MouthShape::kShut};
        int pairs = 0;
        int wrong = 0;
        int max_rects = 0;
        for (EyeShape e1 : eyes) for (MouthShape m1 : mouths) for (EyeShape e2 : eyes) for (MouthShape m2 : mouths) {
            Paint(shaped, face, e1, m1);
            memoria::FaceFrame shown{face, false, e1, m1};
            memoria::FaceFrame next{face, false, e2, m2};
            memoria::FaceRect dirty[memoria::kFaceDirtyMax];
            const int n = memoria::FaceDirtyRects(shown, next, dirty);
            max_rects = n > max_rects ? n : max_rects;
            for (int k = 0; k < n; ++k) {
                memoria::PaintDotFace(shaped.data(), kScreen, kScreen, next, dirty[k]);
            }
            Paint(full, face, e2, m2);
            ++pairs;
            wrong += shaped != full;
        }
        std::printf("partial %s pairs=%d wrong=%d max_rects=%d\n", memoria::FaceName(face), pairs, wrong, max_rects);
    }
    memoria::FaceFrame a;
    memoria::FaceFrame b;
    b.face = memoria::FaceId::kHappy;
    memoria::FaceRect dirty[memoria::kFaceDirtyMax];
    const int n = memoria::FaceDirtyRects(a, b, dirty);
    std::printf("dirtyface %d %d %d %d %d\n", n, dirty[0].x0, dirty[0].y0, dirty[0].x1, dirty[0].y1);
    b = a;
    std::printf("dirtysame %d\n", memoria::FaceDirtyRects(a, b, dirty));
}

memoria::FaceFacts Facts(int state) {
    memoria::FaceFacts facts;
    facts.state = state;
    return facts;
}

void SetExpression(memoria::FaceFacts& facts, const char* name) {
    std::snprintf(facts.expression, sizeof(facts.expression), "%s", name);
    ++facts.expression_seq;
}

const char* EyeName(memoria::EyeShape eyes) {
    return eyes == memoria::EyeShape::kOpen ? "open" : (eyes == memoria::EyeShape::kHalf ? "half" : "shut");
}

const char* MouthName(memoria::MouthShape mouth) {
    return mouth == memoria::MouthShape::kOpen ? "open" : (mouth == memoria::MouthShape::kHalf ? "half" : "shut");
}

void AnimatorChecks() {
    using memoria::FaceAnimator;
    using memoria::SpeechMouth;
    // Idle: every eye change over a minute at the 40 ms frame pace (start time arbitrary, near the wrap).
    {
        FaceAnimator animator(12345);
        memoria::EyeShape last = memoria::EyeShape::kOpen;
        const uint32_t start = 0xFFFFF000u;
        for (uint32_t t = 0; t <= 60000; t += 10) {
            const memoria::FaceFrame frame = animator.Next(Facts(3), false, SpeechMouth{}, start + t);
            if (frame.eyes != last) {
                std::printf("idleblink %u %s %d\n", t, EyeName(frame.eyes), animator.animating() ? 1 : 0);
                last = frame.eyes;
            }
        }
    }
    // Listening frames come every 200 ms unless the animator asks for 40.
    {
        FaceAnimator animator(777);
        uint32_t t = 1000;
        memoria::EyeShape last = memoria::EyeShape::kOpen;
        while (t < 61000) {
            const memoria::FaceFrame frame = animator.Next(Facts(6), false, SpeechMouth{}, t);
            if (frame.eyes != last) {
                std::printf("listenblink %u %s\n", t, EyeName(frame.eyes));
                last = frame.eyes;
            }
            t += animator.animating() ? memoria::kFaceAnimationFrameMs : 200;
        }
    }
    // A crisis round: calm, the slow crisis blink only, the mouth as drawn whatever the speaker does.
    {
        FaceAnimator animator(99);
        memoria::FaceFacts facts = Facts(7);
        facts.crisis = true;
        SetExpression(facts, "happy");
        const uint32_t start = 5000;
        int wrong = 0;
        for (uint32_t t = 0; t <= 10000; t += 10) {
            SpeechMouth mouth;
            mouth.open = (t / 30) % 2 == 0;
            mouth.wide = (t / 70) % 2 == 0;
            const memoria::FaceFrame frame = animator.Next(facts, false, mouth, start + t);
            const bool closed = memoria::CrisisEyesClosed(t);
            wrong += frame.face != memoria::FaceId::kCalm || frame.mouth != memoria::MouthShape::kOpen ||
                     frame.eyes != (closed ? memoria::EyeShape::kShut : memoria::EyeShape::kOpen);
        }
        std::printf("crisisanim wrong=%d\n", wrong);
    }
    // Speaking entry: the previous face stays until the reply's expression arrives or 200 ms pass.
    {
        FaceAnimator animator(5);
        memoria::FaceFacts facts = Facts(6);
        facts.voice_ended = true;
        SetExpression(facts, "neutral");  // upstream names neutral on entering listening
        uint32_t t = 1000;
        for (; t < 2000; t += 40) animator.Next(facts, false, SpeechMouth{}, t);
        facts = Facts(7);
        std::snprintf(facts.expression, sizeof(facts.expression), "neutral");
        facts.expression_seq = 1;
        for (uint32_t dt = 0; dt <= 280; dt += 40) {
            const memoria::FaceFrame frame = animator.Next(facts, false, SpeechMouth{}, t + dt);
            std::printf("entry timeout %u %s\n", dt, memoria::FaceName(frame.face));
        }
        // Back to listening, then speaking again with the expression arriving 120 ms in.
        facts = Facts(6);
        facts.expression_seq = 1;
        t += 2000;
        for (uint32_t dt = 0; dt < 1200; dt += 40) animator.Next(facts, false, SpeechMouth{}, t + dt);
        t += 1200;
        facts.state = 7;
        for (uint32_t dt = 0; dt <= 200; dt += 40) {
            if (dt == 120) SetExpression(facts, "happy");
            const memoria::FaceFrame frame = animator.Next(facts, false, SpeechMouth{}, t + dt);
            std::printf("entry seq %u %s\n", dt, memoria::FaceName(frame.face));
        }
        // Speaking ends: the reply's face stays kReplyHoldMs, then the state's face.
        t += 400;
        facts.state = 3;
        for (uint32_t dt = 0; dt <= 1000; dt += 40) {
            const memoria::FaceFrame frame = animator.Next(facts, false, SpeechMouth{}, t + dt);
            std::printf("hold %u %s\n", dt, memoria::FaceName(frame.face));
        }
    }
    // The speaking face's mouth follows the detector; other faces keep theirs; the hold shuts it.
    {
        FaceAnimator animator(8);
        memoria::FaceFacts facts = Facts(7);
        SetExpression(facts, "neutral");
        uint32_t t = 1000;
        animator.Next(facts, false, SpeechMouth{}, t);
        for (uint32_t dt = 0; dt <= 2000; dt += 10) {
            SpeechMouth mouth;
            mouth.open = (dt / 50) % 3 != 0;
            mouth.wide = (dt / 20) % 2 == 0;
            const memoria::FaceFrame frame = animator.Next(facts, false, mouth, t + 200 + dt);
            std::printf("mouth %u %d %d %s\n", dt, mouth.open ? 1 : 0, mouth.wide ? 1 : 0, MouthName(frame.mouth));
        }
        t += 2400;
        SetExpression(facts, "happy");
        SpeechMouth loud;
        loud.open = true;
        loud.wide = true;
        const memoria::FaceFrame happy = animator.Next(facts, false, loud, t);
        std::printf("mouthother %s %s\n", memoria::FaceName(happy.face), MouthName(happy.mouth));
        SetExpression(facts, "neutral");
        animator.Next(facts, false, loud, t + 40);
        facts.state = 6;
        const memoria::FaceFrame held = animator.Next(facts, false, loud, t + 80);
        std::printf("mouthhold %s %s\n", memoria::FaceName(held.face), MouthName(held.mouth));
    }
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
        spoke.voice_ended = true;
        PrintFacts("spoke", spoke);

        memoria::FaceFacts closed_window = plain;
        closed_window.voice_ended = true;
        closed_window.listening_closed = true;
        PrintFacts("closed", closed_window);

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
        crisis_intro.voice_ended = true;
        PrintFacts("crisis_all", crisis_intro);

        // While speaking, the reply's expression picks the face. Every other
        // state keeps the face device state already selects.
        memoria::FaceFacts expressed = plain;
        std::snprintf(expressed.expression, sizeof(expressed.expression), "caring");
        PrintFacts("expressed", expressed);
    }

    const char* expressions[] = {
        "happy", "caring", "loving", "curious", "thinking",
        "sad", "surprised", "neutral", "angry", "", "light_laughter",
        "shy", "sleepy", "comfort", "wake",
    };
    for (const char* name : expressions) {
        memoria::FaceFacts speaking;
        speaking.state = 7;
        std::snprintf(speaking.expression, sizeof(speaking.expression), "%s", name);
        std::printf("exprface %s %s\n", name[0] == '\0' ? "none" : name,
                    memoria::FaceName(memoria::FaceForFacts(speaking)));
        speaking.crisis = true;
        std::printf("exprcrisis %s %s\n", name[0] == '\0' ? "none" : name,
                    memoria::FaceName(memoria::FaceForFacts(speaking)));
    }

    memoria::FaceFacts token;
    std::snprintf(token.expression, sizeof(token.expression), "light_laughter");
    std::printf("expr %s\n", memoria::ReceivedExpression(token));
    memoria::FaceFacts empty;
    std::printf("exprnone %s\n", memoria::ReceivedExpression(empty)[0] == '\0' ? "empty" : "set");
    memoria::FaceFacts junk;
    std::snprintf(junk.expression, sizeof(junk.expression), "Happy!");
    std::printf("exprjunk %s\n", memoria::ReceivedExpression(junk)[0] == '\0' ? "empty" : "set");
    memoria::FaceFacts overrun;
    std::memset(overrun.expression, 'a', sizeof(overrun.expression));
    std::printf("exprover %s\n", memoria::ReceivedExpression(overrun)[0] == '\0' ? "empty" : "set");

    for (int i = 0; i < memoria::kFaceCount; ++i) {
        const auto id = static_cast<memoria::FaceId>(i);
        for (int captioned = 0; captioned < 2; ++captioned) {
            memoria::FaceFrame frame;
            frame.face = id;
            frame.captioned = captioned != 0;
            memoria::PaintDotFace(g_buffer.data(), memoria::kFaceScreen, memoria::kFaceScreen,
                                  frame);
            const memoria::FaceRect box = memoria::FaceBounds(captioned != 0);
            int pixels = 0;
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
                    ++pixels;
                    if (x < box.x0 || x >= box.x1 || y < box.y0 || y >= box.y1) {
                        ++outside;
                    }
                    sig ^= static_cast<unsigned>(x * 131 + y + pixel);
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
            std::printf(
                "paint %s %d pixels=%d edge=%d min_y=%d max_y=%d sig=%u outside=%d\n",
                memoria::FaceName(id), captioned, pixels, edge, min_y, max_y, sig, outside);
        }
    }
    // The crisis blink: its pace, sample-by-sample, then the closed-eyes paint.
    std::printf("blink-param %u %u\n", static_cast<unsigned>(memoria::kCrisisBlinkPeriodMs),
                static_cast<unsigned>(memoria::kCrisisBlinkClosedMs));
    const unsigned open_ms = memoria::kCrisisBlinkPeriodMs - memoria::kCrisisBlinkClosedMs;
    const unsigned elapsed_samples[] = {
        0,
        open_ms - 1,
        open_ms,
        open_ms + 1,
        memoria::kCrisisBlinkPeriodMs - 1,
        memoria::kCrisisBlinkPeriodMs,
        memoria::kCrisisBlinkPeriodMs + open_ms,
        100000,
    };
    for (unsigned elapsed : elapsed_samples) {
        std::printf("blink %u %d\n", elapsed, memoria::CrisisEyesClosed(elapsed) ? 1 : 0);
    }

    memoria::FaceFrame frame;
    frame.face = memoria::FaceId::kCalm;
    std::printf("frameeq open %d\n", frame == memoria::FaceFrame{} ? 1 : 0);
    frame.eyes = memoria::EyeShape::kShut;
    std::printf("frameeq eyes %d\n", frame == memoria::FaceFrame{} ? 1 : 0);
    frame.eyes = memoria::EyeShape::kOpen;
    frame.mouth = memoria::MouthShape::kHalf;
    std::printf("frameeq mouth %d\n", frame == memoria::FaceFrame{} ? 1 : 0);
    frame.mouth = memoria::MouthShape::kOpen;
    frame.captioned = true;
    std::printf("frameeq caption %d\n", frame == memoria::FaceFrame{} ? 1 : 0);

    memoria::FaceFrame closed;
    closed.face = memoria::FaceId::kCalm;
    closed.eyes = memoria::EyeShape::kShut;
    memoria::PaintDotFace(g_buffer.data(), memoria::kFaceScreen, memoria::kFaceScreen, closed);
    const memoria::FaceRect closed_box = memoria::FaceBounds(false);
    int closed_pixels = 0;
    int closed_edge = 0;
    int closed_outside = 0;
    int closed_min_y = memoria::kFaceScreen;
    int closed_max_y = 0;
    for (int y = 0; y < memoria::kFaceScreen; ++y) {
        for (int x = 0; x < memoria::kFaceScreen; ++x) {
            const uint16_t pixel = g_buffer[static_cast<size_t>(y) * memoria::kFaceScreen + x];
            if (pixel == 0) {
                continue;
            }
            ++closed_pixels;
            if (x < closed_box.x0 || x >= closed_box.x1 || y < closed_box.y0 ||
                y >= closed_box.y1) {
                ++closed_outside;
            }
            if (x < 8 || y < 8 || x >= memoria::kFaceScreen - 8 ||
                y >= memoria::kFaceScreen - 8) {
                ++closed_edge;
            }
            if (y < closed_min_y) closed_min_y = y;
            if (y > closed_max_y) closed_max_y = y;
        }
    }
    std::printf("paintclosed pixels=%d edge=%d outside=%d min_y=%d max_y=%d sig=%u\n",
                closed_pixels, closed_edge, closed_outside, closed_min_y,
                closed_max_y, Signature(g_buffer));
    ShapeChecks();
    AnimatorChecks();
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
    paintclosed = None
    blink_params = None
    blink: dict[int, int] = {}
    frameeq: dict[str, int] = {}
    expressions: dict[str, str] = {}
    expression_faces: dict[str, str] = {}
    expression_crisis: dict[str, str] = {}
    count = None
    regions: dict[tuple[str, str, int, str], dict[str, int]] = {}
    rects: dict[tuple[str, str, int], tuple[int, int, int, int]] = {}
    partial: dict[str, dict[str, int]] = {}
    events: dict[str, list[list[str]]] = {}
    for parts in lines:
        if parts[0] == "region":
            regions[(parts[1], parts[2], int(parts[3]), parts[4])] = {
                key: int(value) for key, value in (item.split("=") for item in parts[5:])
            }
            continue
        if parts[0] == "rect":
            rects[(parts[1], parts[2], int(parts[3]))] = tuple(int(v) for v in parts[4:8])
            continue
        if parts[0] == "partial":
            partial[parts[1]] = {key: int(value) for key, value in (item.split("=") for item in parts[2:])}
            continue
        if parts[0] in ("idleblink", "listenblink", "crisisanim", "entry", "hold", "mouth", "mouthother",
                        "mouthhold", "mouthstatic", "dirtyface", "dirtysame"):
            events.setdefault(parts[0], []).append(parts[1:])
            continue
        if parts[0] == "count":
            count = int(parts[1])
        elif parts[0] == "name":
            names.append(parts[2])
        elif parts[0] == "map":
            mapped[(parts[1], parts[2])] = parts[3]
        elif parts[0] == "paintclosed":
            paintclosed = {key: int(value) for key, value in (item.split("=") for item in parts[1:])}
        elif parts[0] == "blink-param":
            blink_params = (int(parts[1]), int(parts[2]))
        elif parts[0] == "blink":
            blink[int(parts[1])] = int(parts[2])
        elif parts[0] == "frameeq":
            frameeq[parts[1]] = int(parts[2])
        elif parts[0] == "exprface":
            expression_faces[parts[1]] = parts[2]
        elif parts[0] == "exprcrisis":
            expression_crisis[parts[1]] = parts[2]
        elif parts[0] == "expr":
            expressions["token"] = parts[1]
        elif parts[0] == "exprnone":
            expressions["none"] = parts[1]
        elif parts[0] == "exprjunk":
            expressions["junk"] = parts[1]
        elif parts[0] == "exprover":
            expressions["overrun"] = parts[1]
        elif parts[0] == "paint":
            fields = {}
            for item in parts[3:]:
                key, value = item.split("=")
                if "," in value:
                    continue
                fields[key] = int(value)
            paints[(parts[1], int(parts[2]))] = fields
    return {
        "count": count,
        "names": names,
        "mapped": mapped,
        "paints": paints,
        "paintclosed": paintclosed,
        "blink_params": blink_params,
        "blink": blink,
        "frameeq": frameeq,
        "expressions": expressions,
        "expression_faces": expression_faces,
        "expression_crisis": expression_crisis,
        "regions": regions,
        "rects": rects,
        "partial": partial,
        "events": events,
    }


def _face(report: dict, kind: str, state: int) -> str:
    return report["mapped"][(kind, str(state))]


def test_there_are_exactly_ten_faces(report: dict) -> None:
    assert report["count"] == 10
    assert tuple(report["names"]) == NAMES
    assert len(set(report["names"])) == 10
    for name in report["names"]:
        assert name not in FORBIDDEN
        for banned in FORBIDDEN:
            assert banned not in name


def test_device_state_selects_the_face(report: dict) -> None:
    expect = {
        UNKNOWN: "calm",
        STARTING: "wake",
        WIFI: "wake",
        IDLE: "calm",
        CONNECTING: "wake",
        RECOVERING: "wake",
        LISTENING: "listen",
        SPEAKING: "speak",
        UPGRADING: "wake",
        ACTIVATING: "wake",
        AUDIO: "listen",
        FATAL: "calm",
        99: "calm",
    }
    for state, name in expect.items():
        assert _face(report, "plain", state) == name
    # The device's own vad.end while listening: thinking. A capture window the server closed: resting.
    assert _face(report, "spoke", LISTENING) == "think"
    assert _face(report, "closed", LISTENING) == "calm"
    assert _face(report, "closed", AUDIO) == "listen"
    assert _face(report, "spoke", SPEAKING) == "speak"
    assert _face(report, "spoke", IDLE) == "calm"
    assert _face(report, "intro", IDLE) == "wake"
    assert _face(report, "intro", SPEAKING) == "wake"
    assert _face(report, "setup", CONNECTING) == "wake"
    assert _face(report, "setup", IDLE) == "wake"


def test_a_reply_expression_picks_the_speaking_face_and_no_other(report: dict) -> None:
    expressions = report["expressions"]
    assert expressions["token"] == "light_laughter"
    assert expressions["none"] == "empty"
    assert expressions["junk"] == "empty"
    assert expressions["overrun"] == "empty"
    drawn = {
        "happy": "happy",
        "caring": "comfort",
        "loving": "comfort",
        "curious": "think",
        "thinking": "think",
        "sad": "comfort",
        "surprised": "surprised",
        "neutral": "speak",
        "angry": "speak",
        "none": "speak",
        "light_laughter": "speak",
        "shy": "shy",
        "sleepy": "sleepy",
        "comfort": "comfort",
        "wake": "wake",
    }
    for name, face in drawn.items():
        assert report["expression_faces"][name] == face, name
        assert report["expression_crisis"][name] == "calm", name
    for state in (UNKNOWN, STARTING, WIFI, IDLE, CONNECTING, RECOVERING, LISTENING,
                  UPGRADING, ACTIVATING, AUDIO, FATAL, 99):
        assert _face(report, "expressed", state) == _face(report, "plain", state)
    assert _face(report, "expressed", SPEAKING) == "comfort"


def test_crisis_is_always_the_calm_face(report: dict) -> None:
    for state in (UNKNOWN, STARTING, WIFI, IDLE, CONNECTING, RECOVERING, LISTENING, SPEAKING,
                  UPGRADING, ACTIVATING, AUDIO, FATAL, 99):
        assert _face(report, "crisis", state) == "calm"
        assert _face(report, "crisis_all", state) == "calm"


def test_crisis_blink_is_slow_and_lands_on_cycle_tails(report: dict) -> None:
    period, closed = report["blink_params"]
    assert closed > 0
    assert closed * 4 <= period  # the eyes mostly stay open: a blink, not a stare
    open_ms = period - closed
    assert report["blink"][open_ms - 1] == 0
    assert report["blink"][open_ms] == 1
    assert report["blink"][period - 1] == 1
    assert report["blink"][period] == 0
    assert report["blink"][period + open_ms] == 1
    for elapsed, state in report["blink"].items():
        assert state == (1 if elapsed % period >= open_ms else 0), elapsed


def test_closed_calm_hides_the_eyes_and_keeps_the_mouth(report: dict) -> None:
    open_calm = report["paints"][("calm", 0)]
    closed = report["paintclosed"]
    assert closed["pixels"] > 0
    assert closed["pixels"] < open_calm["pixels"]
    assert closed["min_y"] >= open_calm["min_y"]
    assert closed["max_y"] == open_calm["max_y"]
    assert closed["edge"] == 0
    assert closed["outside"] == 0
    # The blink's shut eyes on calm are the approved closed-calm art, pixel for pixel.
    assert closed["sig"] == CLOSED_CALM_SIG


def test_face_frame_compares_every_paint_input(report: dict) -> None:
    assert report["frameeq"]["open"] == 1
    assert report["frameeq"]["eyes"] == 0
    assert report["frameeq"]["mouth"] == 0
    assert report["frameeq"]["caption"] == 0


def test_paint_is_rendered_without_a_frame(report: dict) -> None:
    signatures = set()
    for name in NAMES:
        for captioned in (0, 1):
            paint = report["paints"][(name, captioned)]
            assert paint["pixels"] > 0
            assert paint["edge"] == 0
            assert paint["outside"] == 0
            signatures.add(paint["sig"])
        rest = report["paints"][(name, 0)]
        lifted = report["paints"][(name, 1)]
        assert lifted["min_y"] < rest["min_y"]
        assert lifted["max_y"] <= 216
        assert FACE_TOP_MIN_Y <= lifted["min_y"] <= FACE_TOP_MAX_Y
        assert lifted["sig"] != rest["sig"]
    assert len(signatures) == 20


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
    assert "FaceForFacts(" not in source  # the animator calls it
    assert "face_animator_.Next(facts" in source
    assert "PlaybackStopRegistry" in source
    assert "SceneMoodFromName" not in source
    assert "kFaceStateSpeaking" in source
    # The thinking face comes from the device's own VAD, never from the recognised text.
    assert "user_spoke" not in source
    assert "voice_ended_" in source
    chat = source[source.index("void MemoriaMascotDisplay::SetChatMessage") :]
    chat = chat[: chat.index("\n}\n")]
    assert "voice_ended_" not in chat and "expression" not in chat
    loop = source[source.index("void MemoriaMascotDisplay::AnimationLoop") :]
    assert "scene_->Render(" not in loop
    assert loop.index("scene_->SetOutputLevel(") < loop.index("face_animator_.Next(")
    assert loop.index("face_animator_.Next(") < loop.index("PaintDotFace(")
    assert "!frame_shown_ || frame != shown_frame_" in loop
    assert loop.index("frame != shown_frame_") < loop.index("PaintDotFace(")
    assert "memoria::FaceDirtyRects(shown_frame_, frame, dirty)" in loop
    assert "memoria::PaintDotFace(framebuffer_, width_, height_, frame, rect);" in loop
    assert "lv_obj_invalidate_area(container_, &area)" in loop
    assert "lv_obj_invalidate(container_)" not in loop
    assert "face_animator_.animating()" in loop


def test_blink_closes_the_eyes_and_keeps_every_other_feature(report: dict) -> None:
    for name in NAMES:
        for eye in (0, 1):
            x0, y0, x1, y1 = report["rects"][("eye", name, eye)]
            assert y0 >= BADGE_BOTTOM and x0 < x1 and y0 < y1
            half = report["regions"][("eye", name, eye, "half")]
            shut = report["regions"][("eye", name, eye, "shut")]
            # A cheek, a brow or anything else reaching into the eye's rectangle stays exactly as drawn.
            assert half["kept_changed"] == 0, (name, eye)
            assert shut["kept_changed"] == 0, (name, eye)
            assert shut["moving_px"] > 1000
            # Shut leaves one lid line; half hides the eye above that line and keeps what is below it.
            assert shut["shape_max_y"] - shut["shape_min_y"] < 12, (name, eye)
            assert half["shape_min_y"] == shut["shape_min_y"]
            assert half["shape_max_y"] > shut["shape_max_y"] + 20
            assert y0 < shut["shape_min_y"] and shut["shape_max_y"] < y1


def test_only_the_speaking_face_moves_its_mouth(report: dict) -> None:
    static = {row[0]: int(row[1]) for row in report["events"]["mouthstatic"]}
    for name in NAMES:
        assert static[name] == (0 if name == "speak" else 1), name
    x0, y0, x1, y1 = report["rects"][("mouth", "speak", 0)]
    assert y0 >= BADGE_BOTTOM and (x1 - x0) * (y1 - y0) < 5000
    half = report["regions"][("mouth", "speak", 0, "half")]
    shut = report["regions"][("mouth", "speak", 0, "shut")]
    for shape in (half, shut):
        assert shape["kept_changed"] == 0 and shape["outside_changed"] == 0
    assert shut["shape_max_y"] - shut["shape_min_y"] < 12
    assert 20 <= half["shape_max_y"] - half["shape_min_y"] < 40


def test_partial_repaint_equals_a_full_paint(report: dict) -> None:
    for name in NAMES:
        result = report["partial"][name]
        assert result["pairs"] == 81
        assert result["wrong"] == 0, name
        assert result["max_rects"] == (3 if name == "speak" else 2)
    assert report["events"]["dirtyface"] == [["1", "0", "0", str(WIDTH), str(WIDTH)]]
    assert report["events"]["dirtysame"] == [["0"]]


def _blinks(rows: list[list[str]]) -> list[tuple[int, list[str]]]:
    blinks = []
    for row in rows:
        t, shape = int(row[0]), row[1]
        if shape == "half" and (not blinks or blinks[-1][1][-1] == "open"):
            blinks.append((t, []))
        blinks[-1][1].append(shape)
    return blinks


def test_idle_blinks_top_down_at_a_calm_random_pace(report: dict) -> None:
    rows = report["events"]["idleblink"]
    blinks = _blinks(rows)
    assert len(blinks) >= 10
    for _, shapes in blinks:
        assert shapes == ["half", "shut", "half", "open"]
    ends = {int(r[0]) for r in rows if r[1] == "open"}
    for (start, _), end in zip(blinks, sorted(ends)):
        assert 140 <= end - start <= 170  # spec 6.6: about 150 ms
    starts = [start for start, _ in blinks]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= 2400  # spec 6.6: at least 2 s apart, so no double blink
    assert max(gaps) <= 5600 + 200
    assert len(set(gaps)) > 3  # not a metronome
    # The animator asks for fast frames during a blink only.
    for row in rows:
        assert row[2] == ("0" if row[1] == "open" else "1")


def test_listening_blink_reaches_shut_at_the_slow_listening_pace(report: dict) -> None:
    blinks = _blinks(report["events"]["listenblink"])
    assert len(blinks) >= 10
    for _, shapes in blinks:
        assert shapes == ["half", "shut", "half", "open"]


def test_crisis_round_keeps_calm_with_the_slow_blink_only(report: dict) -> None:
    assert report["events"]["crisisanim"] == [["wrong=0"]]


def test_speaking_waits_for_the_expression_then_holds_the_reply_face(report: dict) -> None:
    entry = [(row[0], int(row[1]), row[2]) for row in report["events"]["entry"]]
    timeout = [(t, face) for kind, t, face in entry if kind == "timeout"]
    assert all(face == "think" for t, face in timeout if t < 200)
    assert all(face == "speak" for t, face in timeout if t >= 200)
    arrived = [(t, face) for kind, t, face in entry if kind == "seq"]
    assert all(face == "listen" for t, face in arrived if t < 120)
    assert all(face == "happy" for t, face in arrived if t >= 120)
    hold = [(int(row[0]), row[1]) for row in report["events"]["hold"]]
    assert all(face == "happy" for t, face in hold if t < 800)
    assert all(face == "calm" for t, face in hold if t >= 800)


def test_speaking_mouth_follows_the_playback_level(report: dict) -> None:
    rows = [(int(r[0]), r[1] == "1", r[2] == "1", r[3]) for r in report["events"]["mouth"]]
    last_change = {}
    previous = None
    for t, opened, wide, shape in rows:
        if not opened:
            assert shape == "shut", t  # shutting is never held back
        if previous is not None and shape != previous:
            if shape != "shut" and previous != "shut":
                assert t - last_change.get("t", -1000) >= 80, t
            last_change["t"] = t
        previous = shape
    shapes = {shape for _, _, _, shape in rows}
    assert shapes == {"open", "half", "shut"}
    assert report["events"]["mouthother"] == [["happy", "open"]]
    assert report["events"]["mouthhold"] == [["speak", "shut"]]
