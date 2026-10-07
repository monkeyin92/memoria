#!/usr/bin/env python3
"""Render the device mascot scene with the exact firmware code on the host.

Compiles ``memoria_mascot_pack.cc``, ``memoria_mascot_raster.cc`` and
``memoria_mascot_scene.cc`` (none depends on ESP-IDF or LVGL) against host zlib,
plays a scripted day in the life of the device (boot animation, idle, pat,
listening, thinking, speaking with moods, a companion switch from the phone, a
shake, an error, the binding screen, the captioned Wi-Fi setup layout) and
writes:

* ``scene.mp4`` (when ffmpeg is on PATH) and ``frames/NNNNN.png`` keyframes,
* ``sheet.png``, a contact sheet of the labelled moments,
* ``stats.json``: redraw area per frame and the verification result.

The scene runs on the device's clock: audio levels arrive in 20 ms blocks from
the same ``AudioLevelTap`` the firmware uses (speech-like curves generated here,
deterministically), it renders when its own ``FrameIntervalMs`` says so, and the
frame written for each video step is whatever is on screen then. Every render is
also done by a second scene that redraws the whole screen, and the two
framebuffers must match bit for bit, which proves the dirty rectangles never
leave stale pixels.

``--sanitize`` builds the compositor with AddressSanitizer and
UndefinedBehaviorSanitizer (it is hand-written fixed-point and pointer
arithmetic: row buffers, span clipping, ``RowPtr``); any finding aborts the run.

Usage:
    uv run python firmware/esp32/scripts/preview_memoria_mascot.py --out .tmp-mascot
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import random
import shutil
import subprocess
import tempfile

HERE = pathlib.Path(__file__).resolve()
BOARD_DIR = HERE.parents[1] / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
MEMORIA_DIR = HERE.parents[1] / "overlay" / "files" / "main" / "memoria"
ASSETS_DIR = BOARD_DIR / "assets"
SIZE = 360

HARNESS = r"""
#include "memoria_audio_level.h"
#include "memoria_mascot_pack.h"
#include "memoria_mascot_scene.h"

#include <zlib.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <map>
#include <sstream>
#include <string>
#include <vector>

namespace {

bool Inflate(const uint8_t* in, size_t in_size, uint8_t* out, size_t out_size) {
    uLongf size = out_size;
    return uncompress(out, &size, in, in_size) == Z_OK && size == out_size;
}
void* Alloc(size_t n) { return std::malloc(n); }
void Free(void* p) { std::free(p); }

std::vector<uint8_t> ReadFile(const std::string& path) {
    std::vector<uint8_t> data;
    FILE* f = fopen(path.c_str(), "rb");
    if (f == nullptr) {
        return data;
    }
    fseek(f, 0, SEEK_END);
    data.resize(static_cast<size_t>(ftell(f)));
    fseek(f, 0, SEEK_SET);
    if (fread(data.data(), 1, data.size(), f) != data.size()) {
        data.clear();
    }
    fclose(f);
    return data;
}

// An audio level curve being played into one of the level taps, one 20 ms block per tick.
struct Playback {
    const std::vector<uint8_t>* curve = nullptr;
    uint32_t start_ms = 0;
};

}  // namespace

// usage: harness <assets-dir> <script-file> <frames-out|-> <fps> <first-companion> <curves-file>
// script lines: "<ms> <command> [arg]"; the audio ones are
//   voice <curve>|off   the speaker plays that level curve (one 20 ms block per entry), or stops at once
//   mic <curve>|off     the same for the microphone side
// curves file lines: "<name> <level> <level> ..."
//
// Time runs in 20 ms ticks, like the audio blocks. The scene renders when its own FrameIntervalMs says
// so, as on the device, and the frame written for each 1/fps step is whatever is on screen then.
static uint32_t g_fake_tick = 0;
static uint32_t FakeClock() { return ++g_fake_tick; }

int main(int argc, char** argv) {
    if (argc < 5) {
        fprintf(stderr, "usage\n");
        return 2;
    }
    const std::string assets = argv[1];
    const int fps = atoi(argv[4]);
    const std::string first = argc > 5 ? argv[5] : "starlight";

    struct Event { uint32_t ms; std::string cmd; std::string arg; };
    std::vector<Event> events;
    {
        FILE* f = fopen(argv[2], "r");
        char cmd[64];
        char arg[64];
        unsigned ms;
        char line[256];
        while (f != nullptr && fgets(line, sizeof(line), f) != nullptr) {
            arg[0] = '\0';
            if (sscanf(line, "%u %63s %63s", &ms, cmd, arg) >= 2) {
                events.push_back({ms, cmd, arg});
            }
        }
        if (f != nullptr) fclose(f);
    }
    std::map<std::string, std::vector<uint8_t>> curves;
    if (argc > 6) {
        FILE* f = fopen(argv[6], "r");
        std::string text;
        char chunk[4096];
        size_t got;
        while (f != nullptr && (got = fread(chunk, 1, sizeof(chunk), f)) > 0) text.append(chunk, got);
        if (f != nullptr) fclose(f);
        std::istringstream lines(text);
        std::string line;
        while (std::getline(lines, line)) {
            std::istringstream fields(line);
            std::string name;
            if (!(fields >> name)) continue;
            std::vector<uint8_t>& curve = curves[name];
            int level;
            while (fields >> level) curve.push_back(static_cast<uint8_t>(level));
        }
    }

    const std::vector<uint8_t> brand_bytes = ReadFile(assets + "/brand_mark.mma");
    memoria::BrandMark brand;
    if (!brand_bytes.empty()) {
        memoria::LoadBrandMark(brand_bytes.data(), brand_bytes.size(), Alloc, Free, Inflate, &brand);
    }

    constexpr int kSize = memoria::MascotScene::kSize;
    std::vector<uint16_t> fb_a(kSize * kSize), fb_b(kSize * kSize);
    memoria::MascotScene a(fb_a.data(), Alloc, Free), b(fb_b.data(), Alloc, Free);
    if (!a.Init() || !b.Init()) {
        fprintf(stderr, "scene init failed\n");
        return 1;
    }
    a.Seed(7);
    b.Seed(7);
    // MASCOT_FAKE_CLOCK: scene a counts clock reads instead of microseconds, so the stage sums are exact.
    if (getenv("MASCOT_FAKE_CLOCK") != nullptr) a.SetProfileClock(FakeClock);
    a.SetBrand(&brand);
    b.SetBrand(&brand);

    // Two packs per scene so a companion switch can be exercised.
    auto load = [&](const std::string& id, memoria::MascotPack* pack) {
        const std::vector<uint8_t> data = ReadFile(assets + "/mascot_" + id + ".mmp");
        return !data.empty() && pack->Load(data.data(), data.size());
    };
    memoria::MascotPack pa1(Alloc, Free, Inflate), pa2(Alloc, Free, Inflate);
    memoria::MascotPack pb1(Alloc, Free, Inflate), pb2(Alloc, Free, Inflate);
    if (!load(first, &pa1) || !load(first, &pb1)) {
        fprintf(stderr, "pack load failed: %s\n", first.c_str());
        return 1;
    }
    a.SetPack(&pa1, 0);
    b.SetPack(&pb1, 0);

    FILE* out = strcmp(argv[3], "-") == 0 ? nullptr : fopen(argv[3], "wb");
    const uint32_t end_ms = events.empty() ? 0 : events.back().ms;
    const uint32_t step = 1000 / (fps > 0 ? fps : 25);
    constexpr uint32_t kBlockMs = 20;
    memoria::AudioLevelTap out_tap, in_tap;
    Playback voice, mic;
    size_t next_event = 0;
    uint32_t next_render_ms = 0;
    uint32_t next_frame_ms = 0;
    unsigned long long redraw_px = 0, composed_px = 0;
    unsigned frames = 0, mismatches = 0, idle_frames = 0, renders = 0;
    // What the renders since the last written frame did.
    int pending_rects = 0;
    unsigned long long pending_area = 0, pending_composed = 0;
    bool pending_same = true;
    std::vector<uint16_t> previous(fb_a.size(), 0);
    // The panel, as LVGL would leave it: pixels are only re-blitted where the display was invalidated.
    // fb_a/fb_b compare what the scene *composed*; this compares what would actually reach the glass, so a
    // dirty rectangle that misses a composed pixel (the 2026-10-07 probe's failure mode) shows up as a
    // panel mismatch even though the scene's own framebuffer was right.
    std::vector<uint16_t> panel(fb_a.size(), 0);
    unsigned long long panel_mismatch_px = 0, panel_mismatch_frames = 0;
    // The mascot lives inside the state ring's inner circle; the ring and its comet animate outside it.
    std::vector<uint8_t> inside(fb_a.size(), 0);
    for (int y = 0; y < kSize; ++y) {
        for (int x = 0; x < kSize; ++x) {
            const float dx = x + 0.5f - kSize / 2.0f, dy = y + 0.5f - kSize / 2.0f;
            inside[static_cast<size_t>(y) * kSize + x] = dx * dx + dy * dy < 146.0f * 146.0f;
        }
    }
    printf("{\"frames\":[");
    for (uint32_t now = 0; now <= end_ms; now += kBlockMs) {
        while (next_event < events.size() && events[next_event].ms <= now) {
            const Event& e = events[next_event++];
            if (e.cmd == "voice" || e.cmd == "mic") {
                Playback& play = e.cmd == "voice" ? voice : mic;
                const auto found = curves.find(e.arg);
                play.curve = found == curves.end() ? nullptr : &found->second;
                play.start_ms = now;
                continue;
            }
            for (int which = 0; which < 2; ++which) {
                memoria::MascotScene& s = which == 0 ? a : b;
                if (e.cmd == "intro") s.StartIntro(now);
                else if (e.cmd == "phase") {
                    static const char* kNames[] = {"intro", "setup", "wifi", "connecting", "idle",
                                                   "listening", "thinking", "speaking", "error"};
                    for (int i = 0; i < 9; ++i) {
                        if (e.arg == kNames[i]) s.SetPhase(static_cast<memoria::ScenePhase>(i), now);
                    }
                } else if (e.cmd == "mood") {
                    memoria::SceneMood mood;
                    if (memoria::SceneMoodFromName(e.arg.c_str(), &mood)) s.SetMood(mood, now);
                } else if (e.cmd == "caption") s.SetCaptioned(e.arg == "on", now);
                else if (e.cmd == "pat") s.Pat(now);
                else if (e.cmd == "shake") s.Shake(now);
                else if (e.cmd == "companion") {
                    memoria::MascotPack* pack = which == 0 ? &pa2 : &pb2;
                    if (load(e.arg, pack)) s.SetPack(pack, now);
                }
            }
        }
        // The audio tasks: one block of each playing curve per tick.
        for (int side = 0; side < 2; ++side) {
            Playback& play = side == 0 ? voice : mic;
            if (play.curve == nullptr) continue;
            const size_t index = (now - play.start_ms) / kBlockMs;
            if (index >= play.curve->size()) {
                play.curve = nullptr;  // the stream ended: no more blocks, as on the device
                continue;
            }
            (side == 0 ? out_tap : in_tap).Publish((*play.curve)[index]);
        }
        // The display task: poll the taps and render when the scene says it is time.
        if (now >= next_render_ms) {
            const memoria::AudioLevelTap::Reading spoken = out_tap.Take();
            const memoria::AudioLevelTap::Reading heard = in_tap.Take();
            for (memoria::MascotScene* s : {&a, &b}) {
                s->SetOutputLevel(spoken.valley, spoken.peak, spoken.blocks, now);
                s->SetInputLevel(heard.peak, heard.blocks, now);
            }
            memoria::SceneRect dirty[memoria::MascotScene::kMaxDirty];
            const int n = a.Render(now, dirty, memoria::MascotScene::kMaxDirty);
            b.Invalidate();
            memoria::SceneRect full[memoria::MascotScene::kMaxDirty];
            b.Render(now, full, memoria::MascotScene::kMaxDirty);
            unsigned long long area = 0;
            for (int i = 0; i < n; ++i) {
                area += static_cast<unsigned long long>(dirty[i].x1 - dirty[i].x0) * (dirty[i].y1 - dirty[i].y0);
            }
            // Overlapping rectangles can count twice; cap at the screen.
            if (area > static_cast<unsigned long long>(kSize) * kSize) area = kSize * kSize;
            pending_rects += n;
            pending_area += area;
            pending_composed += a.last_composed_px();
            // Blit exactly the invalidated rectangles into the simulated panel, as LVGL does, and check
            // that the result equals what a full redraw would have put there.
            for (int i = 0; i < n; ++i) {
                const int x0 = dirty[i].x0 < 0 ? 0 : dirty[i].x0;
                const int y0 = dirty[i].y0 < 0 ? 0 : dirty[i].y0;
                const int x1 = dirty[i].x1 > kSize ? kSize : dirty[i].x1;
                const int y1 = dirty[i].y1 > kSize ? kSize : dirty[i].y1;
                for (int y = y0; y < y1; ++y) {
                    std::memcpy(&panel[static_cast<size_t>(y) * kSize + x0],
                                &fb_a[static_cast<size_t>(y) * kSize + x0],
                                static_cast<size_t>(x1 - x0) * 2);
                }
            }
            {
                unsigned long long diff = 0;
                for (size_t i = 0; i < panel.size(); ++i) diff += panel[i] != fb_b[i];
                if (diff != 0) {
                    ++panel_mismatch_frames;
                    panel_mismatch_px += diff;
                }
            }
            if (memcmp(fb_a.data(), fb_b.data(), fb_a.size() * 2) != 0) {
                pending_same = false;
                if (mismatches < 4) {
                    // find and print the first differing pixel with its screen position
                    for (size_t i = 0; i < fb_a.size(); ++i) {
                        if (fb_a[i] != fb_b[i]) {
                            fprintf(stderr, "MISMATCH at now=%u ms frame=%d x=%zu y=%zu a=%04x b=%04x\n",
                                    now, static_cast<int>(a.last_frame()), i % kSize, i / kSize,
                                    fb_a[i], fb_b[i]);
                            break;
                        }
                    }
                }
                ++mismatches;
            }
            ++renders;
            next_render_ms = now + a.FrameIntervalMs(now);
        }
        if (now >= next_frame_ms) {
            next_frame_ms += step;
            redraw_px += pending_area;
            composed_px += pending_composed;
            if (pending_rects == 0) ++idle_frames;
            // Pixels of the mascot's area that differ from the previous frame: what the eye can see move.
            unsigned changed = 0;
            for (size_t i = 0; i < fb_a.size(); ++i) changed += inside[i] && fb_a[i] != previous[i];
            previous = fb_a;
            printf("%s[%u,%d,%llu,%d,%d,%u,%llu]", frames ? "," : "", now, pending_rects, pending_area,
                   pending_same ? 1 : 0, static_cast<int>(a.last_frame()), changed, pending_composed);
            if (out != nullptr) fwrite(fb_a.data(), 2, fb_a.size(), out);
            ++frames;
            pending_rects = 0;
            pending_area = pending_composed = 0;
            pending_same = true;
        }
    }
    if (out != nullptr) fclose(out);
    const memoria::RenderProfile& pr = a.profile();
    printf("],\"frame_count\":%u,\"mismatches\":%u,\"panel_mismatch_frames\":%llu,"
           "\"panel_mismatch_px\":%llu,\"unchanged_frames\":%u,\"redraw_px\":%llu,"
           "\"composed_px\":%llu,\"renders\":%u,\"profile\":{\"renders\":%llu,\"sampled\":%llu,"
           "\"total_us\":%llu,\"actor_us\":%llu,\"rect_us\":%llu,\"touch_us\":%llu,\"rows\":%llu,"
           "\"copy_in_us\":%llu,\"shadow_us\":%llu,\"sprite_us\":%llu,\"ring_us\":%llu,"
           "\"copy_out_us\":%llu}}\n",
           frames, mismatches, panel_mismatch_frames, panel_mismatch_px, idle_frames, redraw_px,
           composed_px, renders,
           static_cast<unsigned long long>(pr.renders), static_cast<unsigned long long>(pr.sampled),
           static_cast<unsigned long long>(pr.total_us), static_cast<unsigned long long>(pr.actor_us),
           static_cast<unsigned long long>(pr.rect_us), static_cast<unsigned long long>(pr.touch_us),
           static_cast<unsigned long long>(pr.rows), static_cast<unsigned long long>(pr.copy_in_us),
           static_cast<unsigned long long>(pr.shadow_us), static_cast<unsigned long long>(pr.sprite_us),
           static_cast<unsigned long long>(pr.ring_us), static_cast<unsigned long long>(pr.copy_out_us));
    return mismatches == 0 ? 0 : 3;
}
"""

# A scripted stretch of device life: (ms, command, argument).
TIMELINE = [
    (0, "intro", ""),
    (0, "phase", "connecting"),
    (5600, "phase", "idle"),
    (7600, "pat", ""),
    (10200, "phase", "listening"),
    (10200, "mic", "child"),
    (12600, "phase", "thinking"),
    (12600, "mic", "off"),
    (14200, "mood", "happy"),
    (14200, "phase", "speaking"),
    (14200, "voice", "reply"),
    (17200, "mood", "sad"),
    (19200, "mood", "surprised"),
    (20600, "voice", "off"),
    (20600, "phase", "idle"),
    (23600, "companion", "taoxi"),
    (26600, "shake", ""),
    (29600, "phase", "error"),
    (32000, "phase", "idle"),
    (33600, "phase", "setup"),
    (36000, "phase", "wifi"),
    (36000, "caption", "on"),
    (38000, "phase", "connecting"),
    (40000, "caption", "off"),
    (40000, "phase", "idle"),
    (41600, "end", ""),
]

# Moments for the contact sheet: (ms, label).
MOMENTS = [
    (600, "boot: orb"),
    (1400, "boot: reveal"),
    (2000, "boot: wordmark"),
    (2900, "boot: rise"),
    (4200, "boot: hello"),
    (6400, "idle"),
    (7900, "pat: hop"),
    (11200, "listening"),
    (13200, "thinking"),
    (15000, "speaking happy"),
    (17800, "speaking sad"),
    (21000, "after reply"),
    (24000, "new companion"),
    (27200, "shaken"),
    (30400, "error"),
    (34600, "binding (QR on top)"),
    (36200, "wifi: shrinking"),
    (37000, "wifi setup (captioned)"),
    (39000, "connecting (captioned)"),
    (41200, "back to idle"),
]


SANITIZER_FLAGS = [
    "-fsanitize=address,undefined",
    # An undefined-behaviour report must stop the run, not scroll past.
    "-fno-sanitize-recover=undefined",
    "-fno-omit-frame-pointer",
    "-g",
]
# The harness exits without freeing its packs; leaks are not what this run looks for
# (LeakSanitizer exists on Linux only), memory errors and undefined behaviour are.
SANITIZER_ENV = {"ASAN_OPTIONS": "detect_leaks=0:abort_on_error=0", "UBSAN_OPTIONS": "print_stacktrace=1"}


def sanitizers_available() -> bool:
    """True when the host compiler can build and run a program with the sanitizers."""
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        source = pathlib.Path(tmp) / "probe.cc"
        source.write_text("int main() { return 0; }\n")
        binary = pathlib.Path(tmp) / "probe"
        build = subprocess.run(
            [compiler, "-std=c++17", *SANITIZER_FLAGS, str(source), "-o", str(binary)],
            capture_output=True,
        )
        if build.returncode != 0:
            return False
        return subprocess.run([str(binary)], capture_output=True, env=_env(SANITIZER_ENV)).returncode == 0


def _env(extra: dict[str, str]) -> dict[str, str]:
    return {**os.environ, **extra}


BLOCK_MS = 20  # one audio level per block, as the firmware's level taps publish them


def speech_curve(
    seconds: float,
    seed: int,
    *,
    base: tuple[int, int] = (160, 215),
    lead_s: float = 0.0,
    sentence_s: tuple[float, float] = (1.4, 2.6),
) -> list[int]:
    """Deterministic audio levels (RmsToLevel's 0..255, one per 20 ms) that look like speech.

    The shape follows the levels measured from the production TTS (13 replies, 48 phrases): phrases of 0.3-1.1 s
    whose level crests every 80-160 ms (a syllable) and dips between the crests by a random depth, a median 17 %
    but sometimes over 30 %; word gaps of 20-160 ms that dip to 40-95; sentences a second or two long and about
    0.8 s of near silence between them. The numbers come from those measurements, the samples from a seeded
    random generator: nothing here is a recording.
    """
    rng = random.Random(seed)
    total = int(seconds * 1000 / BLOCK_MS)
    out = [rng.randint(0, 8) for _ in range(int(lead_s * 1000 / BLOCK_MS))]
    while len(out) < total:
        sentence_blocks = int(rng.uniform(*sentence_s) * 1000 / BLOCK_MS)
        used = 0
        while used < sentence_blocks and len(out) < total:
            phrase = rng.randint(15, 55)
            loudness = rng.uniform(*base)
            marks: list[tuple[float, float]] = []  # (block, level) of every crest and dip
            at = 0
            while at < phrase:
                spacing = rng.randint(4, 8)
                depth = min(0.55, max(0.04, rng.lognormvariate(math.log(0.17), 0.7)))
                crest = loudness * rng.uniform(0.94, 1.06)
                marks += [(at, crest), (at + spacing / 2, crest * (1 - depth))]
                at += spacing
            marks.append((at, loudness))
            mark = 0
            for i in range(phrase):
                while marks[mark + 1][0] < i:
                    mark += 1
                (x0, y0), (x1, y1) = marks[mark], marks[mark + 1]
                level = y0 + (y1 - y0) * (i - x0) / (x1 - x0) + rng.uniform(-5, 5)
                edge = min(i, phrase - 1 - i)
                if edge < 3:  # a phrase fades in and out over 60 ms
                    level *= 0.35 + 0.65 * edge / 3
                out.append(int(max(0, min(255, level))))
            gap = rng.randint(1, 8)
            out.extend(rng.randint(40, 95) for _ in range(gap))
            used += phrase + gap
        out.extend(rng.randint(0, 12) for _ in range(int(rng.uniform(0.7, 1.0) * 1000 / BLOCK_MS)))
    return out[:total]


# The curves the scripted days play: "reply" through the speaker (ten seconds), "child" into the microphone.
CURVES = {
    "reply": speech_curve(10.0, seed=11),
    "child": speech_curve(8.0, seed=5, base=(95, 175), lead_s=0.8, sentence_s=(1.0, 1.8)),
}


# A longer, steadier stretch of each state than the tour, for judging motion: the screen is lit throughout
# (the device goes dark 10 s into idle), the child talks into the microphone for the first part of listening
# and the reply is spoken with a speech-like audio level.
LIVELINESS_DAY = [
    (0, "phase", "idle"),
    (8000, "phase", "listening"),
    (8000, "mic", "child"),
    (16000, "phase", "thinking"),
    (16000, "mic", "off"),
    (20000, "mood", "happy"),
    (20000, "phase", "speaking"),
    (20000, "voice", "reply"),
    (30000, "voice", "off"),
    (30000, "phase", "idle"),
    (34000, "end", ""),
]

# Stretches of LIVELINESS_DAY (ms) whose full seconds are counted for visible change, one per state.
LIVELINESS_WINDOWS = {
    "idle": (1000, 8000),
    "listening": (9000, 16000),
    "thinking": (17000, 20000),
    "speaking": (21000, 30000),
    "idle_after_reply": (31000, 34000),
}

# Frame fields in the harness output: [ms, dirty rectangles, redrawn px, equal to full redraw, mascot frame,
# px inside the state ring that differ from the previous frame (the mascot's area, not the ring's), px the
# renders since the previous frame composed (the dirty rectangles' area minus what a ring-only redraw skips)].
CHANGED_PX = 5
COMPOSED_PX = 6


def visible_frames_per_second(frames: list[list[int]], start_ms: int, end_ms: int) -> list[int]:
    """For each whole second in [start_ms, end_ms): how many frames differ from the one before them."""
    counts = []
    for second in range(start_ms, end_ms - 999, 1000):
        counts.append(sum(1 for f in frames if second <= f[0] < second + 1000 and f[CHANGED_PX] > 0))
    return counts


def compile_harness(work: pathlib.Path, sanitize: bool = False) -> pathlib.Path:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        raise SystemExit("no host C++ compiler found (clang++ or g++)")
    source = work / "harness.cc"
    source.write_text(HARNESS)
    binary = work / ("harness-sanitized" if sanitize else "harness")
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            *(SANITIZER_FLAGS if sanitize else []),
            f"-I{BOARD_DIR}",
            f"-I{MEMORIA_DIR}",
            str(source),
            str(BOARD_DIR / "memoria_mascot_pack.cc"),
            str(BOARD_DIR / "memoria_mascot_raster.cc"),
            str(BOARD_DIR / "memoria_mascot_scene.cc"),
            "-lz",
            "-o",
            str(binary),
        ],
        check=True,
    )
    return binary


def run(
    binary: pathlib.Path,
    work: pathlib.Path,
    fps: int,
    frames_path: str,
    timeline: list[tuple[int, str, str]] = TIMELINE,
    first: str = "starlight",
    curves: dict[str, list[int]] | None = None,
) -> dict:
    script = work / "timeline.txt"
    script.write_text(
        "".join(f"{ms} {cmd} {arg}\n".replace(" \n", "\n") for ms, cmd, arg in timeline)
    )
    curves_file = work / "curves.txt"
    curves_file.write_text(
        "".join(f"{name} {' '.join(map(str, levels))}\n" for name, levels in (curves or CURVES).items())
    )
    result = subprocess.run(
        [str(binary), str(ASSETS_DIR), str(script), frames_path, str(fps), first, str(curves_file)],
        capture_output=True,
        text=True,
        env=_env(SANITIZER_ENV) if binary.name.endswith("-sanitized") else None,
    )
    if result.returncode not in (0, 3):
        raise SystemExit(f"harness failed ({result.returncode}): {result.stderr}")
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path(".tmp-mascot"))
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--companion", default="starlight")
    parser.add_argument(
        "--day",
        choices=("tour", "liveliness"),
        default="tour",
        help="tour: every state once, with the contact sheet; "
        "liveliness: longer stretches of idle, listening, thinking and speaking, with the "
        "per-second visible-change counts",
    )
    parser.add_argument(
        "--sanitize",
        action="store_true",
        help="build the compositor with AddressSanitizer + UndefinedBehaviorSanitizer",
    )
    args = parser.parse_args()

    import numpy as np
    from PIL import Image, ImageDraw

    out = args.out
    (out / "frames").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp)
        binary = compile_harness(work, sanitize=args.sanitize)
        raw = work / "frames.raw"
        timeline = TIMELINE if args.day == "tour" else LIVELINESS_DAY
        stats = run(binary, work, args.fps, str(raw), timeline=timeline, first=args.companion)
        data = np.fromfile(raw, dtype="<u2").reshape(-1, SIZE, SIZE)

    def to_rgb(frame: np.ndarray) -> np.ndarray:
        r = ((frame >> 11) & 0x1F).astype(np.uint16)
        g = ((frame >> 5) & 0x3F).astype(np.uint16)
        b = (frame & 0x1F).astype(np.uint16)
        rgb = np.stack([(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)], axis=-1)
        yy, xx = np.mgrid[0:SIZE, 0:SIZE]
        outside = (xx + 0.5 - SIZE / 2) ** 2 + (yy + 0.5 - SIZE / 2) ** 2 > (SIZE / 2) ** 2
        rgb[outside] = 24  # the panel is round
        return rgb.astype(np.uint8)

    step = 1000 // args.fps
    tiles = []
    for ms, label in MOMENTS if args.day == "tour" else []:
        index = min(ms // step, data.shape[0] - 1)
        image = Image.fromarray(to_rgb(data[index]))
        image.save(out / "frames" / f"{ms:05d}.png")
        tiles.append((label, image))
    if tiles:
        columns = 6
        tile = 240
        rows = (len(tiles) + columns - 1) // columns
        sheet = Image.new("RGB", (columns * tile, rows * (tile + 24)), (24, 24, 24))
        draw = ImageDraw.Draw(sheet)
        for i, (label, image) in enumerate(tiles):
            x = (i % columns) * tile
            y = (i // columns) * (tile + 24)
            sheet.paste(image.resize((tile, tile), Image.LANCZOS), (x, y))
            draw.text((x + 8, y + tile + 4), label, fill=(230, 230, 230))
        sheet.save(out / "sheet.png")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        proc = subprocess.Popen(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-s",
                f"{SIZE}x{SIZE}",
                "-r",
                str(args.fps),
                "-i",
                "-",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "18",
                str(out / "scene.mp4"),
            ],
            stdin=subprocess.PIPE,
        )
        assert proc.stdin is not None
        for frame in data:
            proc.stdin.write(to_rgb(frame).tobytes())
        proc.stdin.close()
        proc.wait()

    frames = stats.pop("frames")
    if args.day == "liveliness":
        counts = {
            state: visible_frames_per_second(frames, start, end)
            for state, (start, end) in LIVELINESS_WINDOWS.items()
        }
        stats["visible_frames_per_second"] = {
            state: {"per_second": per, "median": sorted(per)[len(per) // 2], "of": args.fps}
            for state, per in counts.items()
        }
        # What a frame costs the device to compose, per state: px composed by an average video frame, and
        # how many of the frames had a render in them at all.
        stats["composed_px_per_frame"] = {}
        for state, (start, end) in LIVELINESS_WINDOWS.items():
            window = [f for f in frames if start <= f[0] < end]
            stats["composed_px_per_frame"][state] = {
                "mean": round(sum(f[COMPOSED_PX] for f in window) / len(window)),
                "max": max(f[COMPOSED_PX] for f in window),
                "frames_with_a_render": sum(1 for f in window if f[1] > 0),
                "of": len(window),
            }
    stats["mean_redraw_fraction"] = round(stats["redraw_px"] / (len(frames) * SIZE * SIZE), 3)
    (out / "stats.json").write_text(json.dumps(stats, indent=2))
    print(json.dumps(stats, indent=2))
    # `mismatches` proves the scene's own dirty rectangles leave nothing stale. `panel_mismatch_frames`
    # proves the rectangles the *display* is told to invalidate cover everything the scene composed — the
    # check that would have caught the 2026-10-07 probe dropping the state ring's rim strips.
    return 0 if stats["mismatches"] == 0 and stats["panel_mismatch_frames"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
