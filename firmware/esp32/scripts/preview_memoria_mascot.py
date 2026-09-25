#!/usr/bin/env python3
"""Render the device mascot scene with the exact firmware code on the host.

Compiles ``memoria_mascot_pack.cc`` and ``memoria_mascot_scene.cc`` (neither
depends on ESP-IDF or LVGL) against host zlib, plays a scripted day in the
life of the device (boot animation, idle, pat, listening, thinking, speaking
with moods, a companion switch from the phone, a shake, an error, the binding
screen) and writes:

* ``scene.mp4`` (when ffmpeg is on PATH) and ``frames/NNNNN.png`` keyframes,
* ``sheet.png``, a contact sheet of the labelled moments,
* ``stats.json``: redraw area per frame and the verification result.

Every frame is also rendered by a second scene that redraws the whole screen,
and the two framebuffers must match bit for bit, which proves the dirty
rectangles never leave stale pixels.

Usage:
    uv run python firmware/esp32/scripts/preview_memoria_mascot.py --out .tmp-mascot
"""

from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import subprocess
import tempfile

HERE = pathlib.Path(__file__).resolve()
BOARD_DIR = HERE.parents[1] / "overlay" / "files" / "main" / "boards" / "memoria" / "esp-vocat"
ASSETS_DIR = BOARD_DIR / "assets"
SIZE = 360

HARNESS = r"""
#include "memoria_mascot_pack.h"
#include "memoria_mascot_scene.h"

#include <zlib.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
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

}  // namespace

// usage: harness <assets-dir> <script-file> <frames-out|-> <fps> [first-companion]
// script lines: "<ms> <command> [arg]"
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
    size_t next_event = 0;
    unsigned long long redraw_px = 0;
    unsigned frames = 0, mismatches = 0, idle_frames = 0;
    printf("{\"frames\":[");
    for (uint32_t now = 0; now <= end_ms; now += step) {
        while (next_event < events.size() && events[next_event].ms <= now) {
            const Event& e = events[next_event++];
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
                } else if (e.cmd == "pat") s.Pat(now);
                else if (e.cmd == "shake") s.Shake(now);
                else if (e.cmd == "companion") {
                    memoria::MascotPack* pack = which == 0 ? &pa2 : &pb2;
                    if (load(e.arg, pack)) s.SetPack(pack, now);
                }
            }
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
        redraw_px += area;
        if (n == 0) ++idle_frames;
        const bool same = memcmp(fb_a.data(), fb_b.data(), fb_a.size() * 2) == 0;
        if (!same) ++mismatches;
        printf("%s[%u,%d,%llu,%d,%d]", frames ? "," : "", now, n, area, same ? 1 : 0,
               static_cast<int>(a.last_frame()));
        if (out != nullptr) fwrite(fb_a.data(), 2, fb_a.size(), out);
        ++frames;
    }
    if (out != nullptr) fclose(out);
    printf("],\"frame_count\":%u,\"mismatches\":%u,\"unchanged_frames\":%u,\"redraw_px\":%llu}\n",
           frames, mismatches, idle_frames, redraw_px);
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
    (12600, "phase", "thinking"),
    (14200, "mood", "happy"),
    (14200, "phase", "speaking"),
    (17200, "mood", "sad"),
    (19200, "mood", "surprised"),
    (20600, "phase", "idle"),
    (23600, "companion", "taoxi"),
    (26600, "shake", ""),
    (29600, "phase", "error"),
    (32000, "phase", "idle"),
    (33600, "phase", "setup"),
    (36000, "phase", "wifi"),
    (38000, "end", ""),
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
    (37000, "wifi setup"),
]


def compile_harness(work: pathlib.Path) -> pathlib.Path:
    compiler = shutil.which("clang++") or shutil.which("g++")
    if compiler is None:
        raise SystemExit("no host C++ compiler found (clang++ or g++)")
    source = work / "harness.cc"
    source.write_text(HARNESS)
    binary = work / "harness"
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
            str(BOARD_DIR / "memoria_mascot_pack.cc"),
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
) -> dict:
    script = work / "timeline.txt"
    script.write_text(
        "".join(f"{ms} {cmd} {arg}\n".replace(" \n", "\n") for ms, cmd, arg in timeline)
    )
    result = subprocess.run(
        [str(binary), str(ASSETS_DIR), str(script), frames_path, str(fps), first],
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 3):
        raise SystemExit(f"harness failed ({result.returncode}): {result.stderr}")
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path(".tmp-mascot"))
    parser.add_argument("--fps", type=int, default=25)
    parser.add_argument("--companion", default="starlight")
    args = parser.parse_args()

    import numpy as np
    from PIL import Image, ImageDraw

    out = args.out
    (out / "frames").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        work = pathlib.Path(tmp)
        binary = compile_harness(work)
        raw = work / "frames.raw"
        stats = run(binary, work, args.fps, str(raw), first=args.companion)
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
    for ms, label in MOMENTS:
        index = min(ms // step, data.shape[0] - 1)
        image = Image.fromarray(to_rgb(data[index]))
        image.save(out / "frames" / f"{ms:05d}.png")
        tiles.append((label, image))
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
    stats["mean_redraw_fraction"] = round(stats["redraw_px"] / (len(frames) * SIZE * SIZE), 3)
    (out / "stats.json").write_text(json.dumps(stats, indent=2))
    print(json.dumps(stats, indent=2))
    return 0 if stats["mismatches"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
