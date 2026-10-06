// The firmware builds for size (-Os); the sprite sampler below is the one hot loop worth optimising
// for speed.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC optimize("O2")
#endif

#include "memoria_mascot_raster.h"

#include <climits>
#include <cstddef>

namespace memoria {

namespace {

// RGB565 pixels spread into three lanes (B at bit 0, R at 11, G at 21) with 6-bit gaps between them, so
// a 5-bit weight can scale all three channels in one 32-bit multiply without a lane spilling into the
// next: the largest lane is G, 63 * 32 = 2016 < 2048.
constexpr uint32_t kLanes = 0x07E0F81Fu;
constexpr uint32_t kLaneRound = 0x02008010u;  // +16 in each lane: rounds a >> 5

// Packs never hold a sprite wider than the canvas limit (memoria_mascot_pack.cc: kMaxCanvas).
constexpr int kMaxSpriteW = 512;
// Below this scale (1/4096) in both directions the sprite is supersampled instead of interpolated.
constexpr int kShrinkBelowQ = 3488;
// Pixels around the sprite's box that the filter can still reach.
constexpr int kBoundsMargin = 2;
constexpr int64_t kOne = 65536;
// The scene scales the mascot between about 0.6 and 1.4; anything outside 1/8..8 is a caller's bug, and
// would overflow the 32-bit sample coordinates and bounds below.
constexpr int32_t kMinScaleQ = kScaleOne / 8;
constexpr int32_t kMaxScaleQ = kScaleOne * 8;

// Sample rows above or below the sprite read as fully transparent.
const uint8_t kZeroAlpha[kMaxSpriteW] = {};
const uint16_t kZeroRgb[kMaxSpriteW] = {};

inline uint32_t Spread(uint16_t c) { return (c | (static_cast<uint32_t>(c) << 16)) & kLanes; }
inline uint16_t Gather(uint32_t v) { return static_cast<uint16_t>(v | (v >> 16)); }

// (a * (32 - w) + b * w) / 32 in every lane, rounded; w is 0..32.
inline uint32_t Lerp(uint32_t a, uint32_t b, uint32_t w) {
    return ((a * (32u - w) + b * w + kLaneRound) >> 5) & kLanes;
}

// ceil(a / b) for b > 0.
inline int64_t CeilDiv(int64_t a, int64_t b) { return a >= 0 ? (a + b - 1) / b : -((-a) / b); }

// The sprite's canvas coordinate (16.16, from its top-left corner) under screen offset d16 (1/16 px)
// from the anchor, when the sprite is scaled by 1/inv (inv in 16.16) about the anchor.
inline int64_t SourceCoord(int64_t d16, int64_t inv, int origin) {
    return ((d16 * inv) >> 4) + static_cast<int64_t>(origin) * kOne;
}

// Screen columns in [x0, x1) whose coordinate c(x) = c0 + (x - x0) * inv falls in [c_lo, c_hi).
inline bool ScreenRange(int64_t c0, int64_t inv, int64_t c_lo, int64_t c_hi, int x0, int x1, int* xs,
                        int* xe) {
    int64_t first = x0 + CeilDiv(c_lo - c0, inv);
    int64_t last = x0 + CeilDiv(c_hi - c0, inv);
    if (first < x0) {
        first = x0;
    }
    if (last > x1) {
        last = x1;
    }
    if (first >= last) {
        return false;
    }
    *xs = static_cast<int>(first);
    *xe = static_cast<int>(last);
    return true;
}

// Widen [lo, hi) with the opaque columns of sprite row r (when it has any).
inline void MergeSpan(const MascotSprite& s, int r, int* lo, int* hi) {
    if (r < 0) {
        return;
    }
    const int first = s.span[r * 2];
    const int last = s.span[r * 2 + 1];
    if (last > first) {
        *lo = first < *lo ? first : *lo;
        *hi = last > *hi ? last : *hi;
    }
}

void BilinearRow(const MascotSprite& s, int cx, int fy, const SpriteTransform& t, int y, int x0, int x1,
                 uint16_t* row) {
    const int64_t inv_y = (1LL << 28) / t.sy_q;
    const int64_t inv_x = (1LL << 28) / t.sx_q;
    // Index-space coordinate: pixel centres sit half a texel in.
    const int64_t v = SourceCoord(static_cast<int64_t>(y) * 16 + 8 - t.ay_q, inv_y, fy - s.y) - kOne / 2;
    const int j0 = static_cast<int>(v >> 16);
    const uint32_t wy = static_cast<uint32_t>((v >> 11) & 31);
    const int ra = (j0 >= 0 && j0 < s.h) ? j0 : -1;
    const int rb = (j0 + 1 >= 0 && j0 + 1 < s.h) ? j0 + 1 : -1;
    if (ra < 0 && rb < 0) {
        return;
    }
    const size_t w = static_cast<size_t>(s.w);
    const uint16_t* rgb_a = ra >= 0 ? s.rgb + static_cast<size_t>(ra) * w : kZeroRgb;
    const uint8_t* alpha_a = ra >= 0 ? s.alpha + static_cast<size_t>(ra) * w : kZeroAlpha;
    const uint16_t* rgb_b = rb >= 0 ? s.rgb + static_cast<size_t>(rb) * w : kZeroRgb;
    const uint8_t* alpha_b = rb >= 0 ? s.alpha + static_cast<size_t>(rb) * w : kZeroAlpha;
    int lo = INT_MAX;
    int hi = INT_MIN;
    MergeSpan(s, ra, &lo, &hi);
    MergeSpan(s, rb, &lo, &hi);
    if (hi <= lo) {
        return;
    }
    const int64_t u0 =
        SourceCoord(static_cast<int64_t>(x0) * 16 + 8 - t.ax_q, inv_x, cx - s.x) - kOne / 2;
    // The pixel samples columns i0 and i0 + 1, so it can only be non-transparent when i0 is in
    // [lo - 1, hi).
    int xs = 0;
    int xe = 0;
    if (!ScreenRange(u0, inv_x, static_cast<int64_t>(lo - 1) * kOne, static_cast<int64_t>(hi) * kOne,
                     x0, x1, &xs, &xe)) {
        return;
    }
    const int32_t step = static_cast<int32_t>(inv_x);
    int32_t u = static_cast<int32_t>(u0 + static_cast<int64_t>(xs - x0) * inv_x);
    const int width = s.w;
    for (int x = xs; x < xe; ++x, u += step) {
        const int i0 = u >> 16;
        const uint32_t wx = static_cast<uint32_t>(u >> 11) & 31u;
        uint32_t a00;
        uint32_t a10;
        uint32_t a01;
        uint32_t a11;
        uint32_t c00;
        uint32_t c10;
        uint32_t c01;
        uint32_t c11;
        if (i0 >= 0 && i0 + 1 < width) {
            a00 = alpha_a[i0];
            a10 = alpha_a[i0 + 1];
            a01 = alpha_b[i0];
            a11 = alpha_b[i0 + 1];
            c00 = rgb_a[i0];
            c10 = rgb_a[i0 + 1];
            c01 = rgb_b[i0];
            c11 = rgb_b[i0 + 1];
        } else {
            // At the sprite's left or right edge one of the two columns is outside it: transparent.
            const bool left = i0 >= 0 && i0 < width;
            const bool right = i0 + 1 >= 0 && i0 + 1 < width;
            a00 = left ? alpha_a[i0] : 0;
            a01 = left ? alpha_b[i0] : 0;
            c00 = left ? rgb_a[i0] : 0;
            c01 = left ? rgb_b[i0] : 0;
            a10 = right ? alpha_a[i0 + 1] : 0;
            a11 = right ? alpha_b[i0 + 1] : 0;
            c10 = right ? rgb_a[i0 + 1] : 0;
            c11 = right ? rgb_b[i0 + 1] : 0;
        }
        if ((a00 & a10 & a01 & a11) == 255u) {
            // Four opaque neighbours: interpolate the colour, nothing shows through.
            const uint32_t top = Lerp(Spread(static_cast<uint16_t>(c00)), Spread(static_cast<uint16_t>(c10)), wx);
            const uint32_t bottom =
                Lerp(Spread(static_cast<uint16_t>(c01)), Spread(static_cast<uint16_t>(c11)), wx);
            row[x] = Gather(Lerp(top, bottom, wy));
            continue;
        }
        // An edge: weight each neighbour's colour by its coverage so a transparent neighbour's
        // colour cannot tint the rim, and let the weights' sum be the pixel's coverage.
        const uint32_t w00 = (32u - wx) * (32u - wy) * a00;
        const uint32_t w10 = wx * (32u - wy) * a10;
        const uint32_t w01 = (32u - wx) * wy * a01;
        const uint32_t w11 = wx * wy * a11;
        const uint32_t total = w00 + w10 + w01 + w11;  // 0 .. 1024 * 255
        const uint32_t alpha = (total + 512) >> 10;
        if (alpha < 4) {
            continue;
        }
        const uint32_t r = (w00 * (c00 >> 11) + w10 * (c10 >> 11) + w01 * (c01 >> 11) +
                            w11 * (c11 >> 11) + total / 2) / total;
        const uint32_t g = (w00 * ((c00 >> 5) & 0x3F) + w10 * ((c10 >> 5) & 0x3F) +
                            w01 * ((c01 >> 5) & 0x3F) + w11 * ((c11 >> 5) & 0x3F) + total / 2) / total;
        const uint32_t b = (w00 * (c00 & 0x1F) + w10 * (c10 & 0x1F) + w01 * (c01 & 0x1F) +
                            w11 * (c11 & 0x1F) + total / 2) / total;
        row[x] = Blend565(static_cast<uint16_t>((r << 11) | (g << 5) | b), row[x], alpha);
    }
}

void SupersampleRow(const MascotSprite& s, int cx, int fy, const SpriteTransform& t, int y, int x0,
                    int x1, uint16_t* row) {
    // A clear shrink (the captioned layout) would alias with one sample per pixel: plush edges crawl as
    // the mascot breathes. Average a 2x2 grid of samples instead, weighting colour by coverage.
    const int64_t inv_y = (1LL << 28) / t.sy_q;
    const int64_t inv_x = (1LL << 28) / t.sx_q;
    const uint16_t* rgb[2] = {nullptr, nullptr};
    const uint8_t* alpha[2] = {nullptr, nullptr};
    int span0[2] = {0, 0};
    int span1[2] = {0, 0};
    int lo = INT_MAX;
    int hi = INT_MIN;
    for (int j = 0; j < 2; ++j) {
        const int64_t sv =
            SourceCoord(static_cast<int64_t>(y) * 16 + 4 + 8 * j - t.ay_q, inv_y, fy - s.y);
        const int sy = static_cast<int>(sv >> 16);
        if (sy < 0 || sy >= s.h) {
            continue;
        }
        rgb[j] = s.rgb + static_cast<size_t>(sy) * static_cast<size_t>(s.w);
        alpha[j] = s.alpha + static_cast<size_t>(sy) * static_cast<size_t>(s.w);
        span0[j] = s.span[sy * 2];
        span1[j] = s.span[sy * 2 + 1];
        MergeSpan(s, sy, &lo, &hi);
    }
    if (hi <= lo) {
        return;
    }
    const int64_t c0 = SourceCoord(static_cast<int64_t>(x0) * 16 + 8 - t.ax_q, inv_x, cx - s.x);
    // Taps lie within a quarter pixel (inv / 4 texels) of the pixel centre: one texel of margin is enough
    // down to a scale of 1/4; the scene never goes below about 0.6.
    int xs = 0;
    int xe = 0;
    if (!ScreenRange(c0, inv_x, static_cast<int64_t>(lo - 1) * kOne,
                     static_cast<int64_t>(hi + 1) * kOne, x0, x1, &xs, &xe)) {
        return;
    }
    const int32_t step = static_cast<int32_t>(inv_x);
    const int32_t half = static_cast<int32_t>((8 * inv_x) >> 4);
    int32_t base = static_cast<int32_t>(
        SourceCoord(static_cast<int64_t>(xs) * 16 + 4 - t.ax_q, inv_x, cx - s.x));
    for (int x = xs; x < xe; ++x, base += step) {
        uint32_t sum_a = 0;
        uint32_t sum_r = 0;
        uint32_t sum_g = 0;
        uint32_t sum_b = 0;
        for (int i = 0; i < 2; ++i) {
            const int col = (base + i * half) >> 16;
            for (int j = 0; j < 2; ++j) {
                if (alpha[j] == nullptr || col < span0[j] || col >= span1[j]) {
                    continue;
                }
                const uint32_t a = alpha[j][col];
                if (a == 0) {
                    continue;
                }
                const uint32_t c = rgb[j][col];
                sum_a += a;
                sum_r += ((c >> 11) & 0x1F) * a;
                sum_g += ((c >> 5) & 0x3F) * a;
                sum_b += (c & 0x1F) * a;
            }
        }
        if (sum_a == 0) {
            continue;
        }
        const uint16_t colour = static_cast<uint16_t>(((sum_r / sum_a) << 11) | ((sum_g / sum_a) << 5) |
                                                      (sum_b / sum_a));
        row[x] = Blend565(colour, row[x], sum_a >> 2);
    }
}

}  // namespace

SceneRect SpriteBounds(const MascotSprite& s, int canvas_cx, int foot_y, const SpriteTransform& t) {
    const int64_t sx = t.sx_q < kMinScaleQ ? kMinScaleQ : (t.sx_q > kMaxScaleQ ? kMaxScaleQ : t.sx_q);
    const int64_t sy = t.sy_q < kMinScaleQ ? kMinScaleQ : (t.sy_q > kMaxScaleQ ? kMaxScaleQ : t.sy_q);
    const int32_t left = t.ax_q + static_cast<int32_t>(((s.x - canvas_cx) * 16 * sx) >> 12);
    const int32_t right = t.ax_q + static_cast<int32_t>(((s.x + s.w - canvas_cx) * 16 * sx) >> 12);
    const int32_t top = t.ay_q + static_cast<int32_t>(((s.y - foot_y) * 16 * sy) >> 12);
    const int32_t bottom = t.ay_q + static_cast<int32_t>(((s.y + s.h - foot_y) * 16 * sy) >> 12);
    SceneRect box;
    box.x0 = (left >> kAnchorShift) - kBoundsMargin;
    box.x1 = ((right + 15) >> kAnchorShift) + kBoundsMargin;
    box.y0 = (top >> kAnchorShift) - kBoundsMargin;
    box.y1 = ((bottom + 15) >> kAnchorShift) + kBoundsMargin;
    return box;
}

void BlendSpriteRow(const MascotSprite& s, int canvas_cx, int foot_y, const SpriteTransform& t, int y,
                    int x0, int x1, uint16_t* row) {
    if (s.rgb == nullptr || s.alpha == nullptr || s.span == nullptr || s.w <= 0 || s.h <= 0 ||
        s.w > kMaxSpriteW || t.sx_q < kMinScaleQ || t.sx_q > kMaxScaleQ || t.sy_q < kMinScaleQ ||
        t.sy_q > kMaxScaleQ || x1 <= x0 || row == nullptr) {
        return;
    }
    if (t.sx_q < kShrinkBelowQ && t.sy_q < kShrinkBelowQ) {
        SupersampleRow(s, canvas_cx, foot_y, t, y, x0, x1, row);
    } else {
        BilinearRow(s, canvas_cx, foot_y, t, y, x0, x1, row);
    }
}

}  // namespace memoria
