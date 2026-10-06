#ifndef MEMORIA_MASCOT_SAMPLER_BENCH_H
#define MEMORIA_MASCOT_SAMPLER_BENCH_H

// Microbenchmark of the sprite sampler for bench builds (TODOLIST M-6). The animation task spends about 50 ms of
// CPU per drawn frame, 55-80 % of it in BlendSpriteRow; this says what a sampled pixel costs when the sprite sits
// in internal SRAM or in PSRAM, warm or cold in the 32 KB data cache, on the fast path (four opaque neighbours)
// or the edge path (three divisions). No ESP-IDF includes: the host tests run it with a fake clock.

#include <cstddef>
#include <cstdint>
#include <cstdio>

#include "memoria_mascot_pack.h"
#include "memoria_mascot_raster.h"

namespace memoria {

constexpr int kBenchCanvas = 360;

struct SamplerBenchVariant {
    const char* name;
    int w;
    int h;
    bool soft;          // alpha 128 everywhere (every pixel blends) instead of the packs' solid 252
    bool sprite_psram;  // sprite planes in PSRAM instead of internal SRAM
    bool row_psram;     // the row being drawn into in PSRAM instead of internal SRAM
    int reps;           // sprite passes inside one timed pass
};

// 64x64 (12 KB) fits the data cache once warm; 256x256 (192 KB) streams through it like a real pose does.
constexpr SamplerBenchVariant kSamplerBenchVariants[] = {
    {"sram_solid", 64, 64, false, false, false, 16},
    {"sram_soft", 64, 64, true, false, false, 16},
    {"psram_warm_solid", 64, 64, false, true, false, 16},
    {"psram_cold_solid", 256, 256, false, true, false, 1},
    {"psram_cold_soft", 256, 256, true, true, false, 1},
    {"psram_cold_solid_rowpsram", 256, 256, false, true, true, 1},
};
constexpr std::size_t kSamplerBenchCount = sizeof(kSamplerBenchVariants) / sizeof(kSamplerBenchVariants[0]);

// Six variants, each "<name>_ns=<ns per pixel> <name>_px=<pixels in a timed pass> ", 64-bit worst case.
constexpr std::size_t kSamplerBenchLineCapacity = 768;

// A gradient in the colour plane, constant alpha, and the full-width row spans the loader would compute.
inline void FillBenchSprite(MascotSprite* s, bool soft) {
    for (int y = 0; y < s->h; ++y) {
        for (int x = 0; x < s->w; ++x) {
            const std::size_t i = static_cast<std::size_t>(y) * static_cast<std::size_t>(s->w) + static_cast<std::size_t>(x);
            s->rgb[i] = static_cast<uint16_t>(((x * 31 / s->w) << 11) | ((y * 63 / s->h) << 5) | ((x + y) & 31));
            s->alpha[i] = soft ? 128 : 252;
        }
        s->span[y * 2] = 0;
        s->span[y * 2 + 1] = static_cast<uint16_t>(s->w);
    }
}

using BenchClockFn = uint32_t (*)();  // microseconds, free running

// One timed pass: `reps` times every row of the sprite, placed at a fractional position and a breathing scale
// so the sampler takes its bilinear path. `row` holds kBenchCanvas pixels. Returns the microseconds it took
// and, in *px, the sampler's columns visited (what the time is divided by).
inline uint32_t SamplerBenchPass(const MascotSprite& s, uint16_t* row, int reps, BenchClockFn clock, uint64_t* px) {
    SpriteTransform t;
    t.ax_q = (kBenchCanvas / 2) * 16 + 5;
    t.ay_q = (kBenchCanvas - 24) * 16 + 7;
    t.sx_q = kScaleOne - 160;
    t.sy_q = kScaleOne - 96;
    const int cx = s.w / 2;
    const int fy = s.h - 1;
    const SceneRect box = SpriteBounds(s, cx, fy, t);
    const int y0 = box.y0 > 0 ? box.y0 : 0;
    const int y1 = box.y1 < kBenchCanvas ? box.y1 : kBenchCanvas;
    for (int x = 0; x < kBenchCanvas; ++x) {
        row[x] = 0x1234;
    }
    uint64_t columns = 0;
    for (int y = y0; y < y1; ++y) {
        int xs = 0;
        int xe = 0;
        if (SpriteRowSpan(s, cx, fy, t, y, 0, kBenchCanvas, &xs, &xe)) {
            columns += static_cast<uint64_t>(xe - xs);
        }
    }
    *px = columns * static_cast<uint64_t>(reps);
    const uint32_t started = clock();
    for (int rep = 0; rep < reps; ++rep) {
        for (int y = y0; y < y1; ++y) {
            BlendSpriteRow(s, cx, fy, t, y, 0, kBenchCanvas, row);
        }
    }
    return clock() - started;
}

struct SamplerBenchRow {
    uint64_t us = 0;  // best timed pass
    uint64_t px = 0;
};

// The log line (no prefix, no newline): nanoseconds per pixel and the pixel count behind it, per variant. Whole
// pairs or nothing: 0 when it would not fit.
inline std::size_t SamplerBenchLine(char* out, std::size_t capacity, const SamplerBenchRow* rows) {
    if (out == nullptr || capacity == 0 || rows == nullptr) {
        return 0;
    }
    std::size_t length = 0;
    for (std::size_t i = 0; i < kSamplerBenchCount; ++i) {
        const uint64_t ns = rows[i].px != 0 ? rows[i].us * 1000ULL / rows[i].px : 0;
        const char* name = kSamplerBenchVariants[i].name;
        char item[128];
        const int n = std::snprintf(item, sizeof(item), "%s%s_ns=%llu %s_px=%llu", i == 0 ? "sampler_bench " : " ", name,
                                    static_cast<unsigned long long>(ns), name,
                                    static_cast<unsigned long long>(rows[i].px));
        if (n <= 0 || length + static_cast<std::size_t>(n) >= capacity) {
            return 0;
        }
        for (int k = 0; k <= n; ++k) {
            out[length + static_cast<std::size_t>(k)] = item[k];
        }
        length += static_cast<std::size_t>(n);
    }
    return length;
}

}  // namespace memoria

#endif  // MEMORIA_MASCOT_SAMPLER_BENCH_H
