#ifndef MEMORIA_MASCOT_RASTER_H
#define MEMORIA_MASCOT_RASTER_H

#include "memoria_mascot_pack.h"

#include <cstdint>

namespace memoria {

// Drawing a mascot sprite at a fractional position and scale. Position and scale used to be rounded to
// whole pixels and 1/256 before drawing, so the idle breathing (about 3 px over 3.6 s) moved in a dozen
// one-pixel jumps per cycle and was point-sampled. Here the sprite is sampled bilinearly at the exact
// position, so a 0.2 px move per frame changes the edge pixels' coverage instead of nothing at all
// (TODOLIST M-6). Like the rest of the compositor this file has no ESP-IDF or LVGL dependency; the
// preview script compiles it on the host.

struct SceneRect {
    int x0 = 0;  // inclusive
    int y0 = 0;
    int x1 = 0;  // exclusive
    int y1 = 0;
};

// Blend fg over bg with alpha 0..255 in RGB565 (5-bit alpha precision).
#if defined(__GNUC__)
__attribute__((always_inline))
#endif
inline uint16_t Blend565(uint16_t fg, uint16_t bg, uint32_t alpha) {
    if (alpha >= 250) {
        return fg;
    }
    if (alpha < 4) {
        return bg;
    }
    const uint32_t a = (alpha + 4) >> 3;  // 0..32
    uint32_t f = (fg | (static_cast<uint32_t>(fg) << 16)) & 0x07E0F81Fu;
    uint32_t b = (bg | (static_cast<uint32_t>(bg) << 16)) & 0x07E0F81Fu;
    uint32_t r = ((((f - b) * a) >> 5) + b) & 0x07E0F81Fu;
    return static_cast<uint16_t>(r | (r >> 16));
}

constexpr int kAnchorShift = 4;     // anchor positions are in 1/16 px
constexpr int kScaleOne = 4096;     // scales are in 1/4096

// Where the sprite stands: the feet centre lands on (ax, ay) and the sprite is scaled about it.
struct SpriteTransform {
    int32_t ax_q = 0;
    int32_t ay_q = 0;
    int32_t sx_q = kScaleOne;
    int32_t sy_q = kScaleOne;

    bool operator==(const SpriteTransform& o) const {
        return ax_q == o.ax_q && ay_q == o.ay_q && sx_q == o.sx_q && sy_q == o.sy_q;
    }
    bool operator!=(const SpriteTransform& o) const { return !(*this == o); }
};

// Every pixel the sprite can touch under `t`, filter margin included: [x0, x1) x [y0, y1), not clipped
// to the screen. `canvas_cx` and `foot_y` are the pack's canvas centre column and foot row.
SceneRect SpriteBounds(const MascotSprite& s, int canvas_cx, int foot_y, const SpriteTransform& t);

// Blends the sprite's pixels in screen row `y`, columns [x0, x1), over `row` (indexed by screen x).
// Pixels the sprite does not cover are left alone. Scales below about 0.85 in both directions (the
// captioned layout) are supersampled, anything else is sampled bilinearly.
void BlendSpriteRow(const MascotSprite& s, int canvas_cx, int foot_y, const SpriteTransform& t, int y,
                    int x0, int x1, uint16_t* row);

}  // namespace memoria

#endif  // MEMORIA_MASCOT_RASTER_H
