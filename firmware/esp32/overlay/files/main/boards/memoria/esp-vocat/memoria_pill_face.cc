#include "memoria_pill_face.h"

#include <algorithm>
#include <cmath>
#include <cstddef>

namespace memoria {
namespace {

constexpr int kEyeSeparation = 48;
constexpr int kBaseEyeY = 162;
constexpr int kMouthBelowEyes = 80;
constexpr int kClosedLine = 14;

struct Rgb {
    int r;
    int g;
    int b;
};

Rgb Unpack(uint32_t rgb) {
    return Rgb{static_cast<int>((rgb >> 16) & 0xFF), static_cast<int>((rgb >> 8) & 0xFF),
               static_cast<int>(rgb & 0xFF)};
}

uint16_t To565(uint32_t rgb) {
    const Rgb value = Unpack(rgb);
    return static_cast<uint16_t>(((value.r >> 3) << 11) | ((value.g >> 2) << 5) | (value.b >> 3));
}

// `alpha` 1..255 blended over what is already in the pixel.
uint16_t Blend565(uint16_t base, const Rgb& over, int alpha) {
    const int base_r = ((base >> 11) & 0x1F) * 255 / 31;
    const int base_g = ((base >> 5) & 0x3F) * 255 / 63;
    const int base_b = (base & 0x1F) * 255 / 31;
    const int inverse = 255 - alpha;
    const int r = (base_r * inverse + over.r * alpha + 127) / 255;
    const int g = (base_g * inverse + over.g * alpha + 127) / 255;
    const int b = (base_b * inverse + over.b * alpha + 127) / 255;
    return static_cast<uint16_t>(((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3));
}

struct Canvas {
    uint16_t* buffer;
    int width;
    int height;
};

// Draws one shape: `distance(x, y)` is the signed distance in pixels from the
// pixel centre to the shape's edge (negative inside), so one pixel of soft edge
// gives anti-aliasing without a supersampled buffer. Only the box is visited.
template <typename Distance>
void DrawShape(const Canvas& canvas, int x0, int y0, int x1, int y1, uint32_t color, int alpha,
               Distance distance) {
    if (alpha <= 0) {
        return;
    }
    const Rgb over = Unpack(color);
    const uint16_t solid = To565(color);
    x0 = std::max(0, x0);
    y0 = std::max(0, y0);
    x1 = std::min(canvas.width - 1, x1);
    y1 = std::min(canvas.height - 1, y1);
    for (int y = y0; y <= y1; ++y) {
        uint16_t* row = canvas.buffer + static_cast<std::size_t>(y) * canvas.width;
        for (int x = x0; x <= x1; ++x) {
            const float d = distance(static_cast<float>(x) + 0.5f, static_cast<float>(y) + 0.5f);
            if (d >= 0.5f) {
                continue;
            }
            const float coverage = d <= -0.5f ? 1.0f : 0.5f - d;
            const int a = static_cast<int>(coverage * static_cast<float>(alpha) + 0.5f);
            if (a >= 255) {
                row[x] = solid;
            } else if (a > 0) {
                row[x] = Blend565(row[x], over, a);
            }
        }
    }
}

float CapsuleDistance(float px, float py, float half_w, float half_h) {
    const float r = std::min(half_w, half_h);
    const float qx = std::fabs(px) - (half_w - r);
    const float qy = std::fabs(py) - (half_h - r);
    const float ox = std::max(qx, 0.0f);
    const float oy = std::max(qy, 0.0f);
    return std::sqrt(ox * ox + oy * oy) + std::min(std::max(qx, qy), 0.0f) - r;
}

// A thick arc whose apex is at the top: chord `2 * half_chord` at the bottom,
// rising `rise` above it, round-capped. Centre-relative coordinates.
float ArcUpDistance(float px, float py, float half_chord, float rise, float thickness) {
    rise = std::max(rise, 1.0f);
    const float radius = (half_chord * half_chord + rise * rise) / (2.0f * rise);
    const float center_y = -rise * 0.5f + radius;
    const float end_y = rise * 0.5f;
    float d;
    if (py > end_y) {
        const float ex = px < 0 ? -half_chord : half_chord;
        d = std::hypot(px - ex, py - end_y);
    } else {
        d = std::fabs(std::hypot(px, py - center_y) - radius);
        const float ex = px < 0 ? -half_chord : half_chord;
        d = std::min(d, std::hypot(px - ex, py - end_y));
    }
    return d - thickness * 0.5f;
}

float EllipseDistance(float px, float py, float rx, float ry) {
    rx = std::max(rx, 0.5f);
    ry = std::max(ry, 0.5f);
    return (std::hypot(px / rx, py / ry) - 1.0f) * std::min(rx, ry);
}

int Lerp(int a, int b, float t) { return a + static_cast<int>(std::lround(static_cast<float>(b - a) * t)); }

PillEye LerpEye(const PillEye& a, const PillEye& b, float t) {
    PillEye eye;
    eye.cx = static_cast<int16_t>(Lerp(a.cx, b.cx, t));
    eye.cy = static_cast<int16_t>(Lerp(a.cy, b.cy, t));
    eye.width = static_cast<uint16_t>(Lerp(a.width, b.width, t));
    eye.height = static_cast<uint16_t>(Lerp(a.height, b.height, t));
    return eye;
}

PillFaceFrame LerpFrame(const PillFaceFrame& a, const PillFaceFrame& b, float t) {
    PillFaceFrame out = b;
    out.left = LerpEye(a.left, b.left, t);
    out.right = LerpEye(a.right, b.right, t);
    out.style = t < 0.5f ? a.style : b.style;
    out.blush = static_cast<uint8_t>(Lerp(a.blush, b.blush, t));
    out.highlight = static_cast<uint8_t>(Lerp(a.highlight, b.highlight, t));
    out.mouth_cx = static_cast<int16_t>(Lerp(a.mouth_cx, b.mouth_cx, t));
    out.mouth_cy = static_cast<int16_t>(Lerp(a.mouth_cy, b.mouth_cy, t));
    out.mouth_width = static_cast<uint16_t>(Lerp(a.mouth_width, b.mouth_width, t));
    out.mouth_open = static_cast<uint8_t>(Lerp(a.mouth_open, b.mouth_open, t));
    return out;
}

// Everything but the blink: two looks that differ only by `lid` are the same look.
bool SameLook(const PillFaceFrame& a, const PillFaceFrame& b) {
    PillFaceFrame x = a;
    x.lid = b.lid;
    return x == b;
}

bool OnlyMouthOpenDiffers(const PillFaceFrame& a, const PillFaceFrame& b) {
    PillFaceFrame x = a;
    x.lid = b.lid;
    x.mouth_open = b.mouth_open;
    return x == b;
}

float Smooth(float t) { return t * t * (3.0f - 2.0f * t); }

struct Look {
    PillEyeStyle style;
    int width;
    int height;
    int dx;
    int dy;
    int blush;
    int mouth_width;
    int mouth_dx;
    int mouth_open;
};

Look LookFor(FaceId face) {
    switch (face) {
        case FaceId::kWake:
            return {PillEyeStyle::kCapsule, 48, 124, 0, -4, 70, 36, 0, 0};
        case FaceId::kListen:
            return {PillEyeStyle::kCapsule, 50, 118, 0, -2, 40, 26, 0, 0};
        case FaceId::kThink:
            return {PillEyeStyle::kCapsule, 44, 84, 14, -16, 0, 20, 10, 0};
        case FaceId::kSpeak:
            return {PillEyeStyle::kCapsule, 44, 108, 0, 0, 50, 40, 0, 0};
        case FaceId::kHappy:
            return {PillEyeStyle::kArcUp, 54, 26, 0, 4, 150, 46, 0, 0};
        case FaceId::kComfort:
            return {PillEyeStyle::kCapsule, 46, 88, 0, 4, 120, 32, 0, 0};
        case FaceId::kShy:
            return {PillEyeStyle::kCapsule, 42, 76, -12, 6, 200, 22, -6, 0};
        case FaceId::kSleepy:
            return {PillEyeStyle::kArcDown, 54, 18, 0, 14, 40, 22, 0, 0};
        case FaceId::kSurprised:
            return {PillEyeStyle::kCapsule, 54, 150, 0, -4, 20, 26, 0, 200};
        case FaceId::kCalm:
        default:
            return {PillEyeStyle::kCapsule, 44, 104, 0, 0, 30, 30, 0, 0};
    }
}

void DrawEye(const Canvas& canvas, const PillFaceFrame& frame, const PillEye& eye, bool left) {
    const float cx = static_cast<float>(eye.cx);
    const float cy = static_cast<float>(eye.cy);
    if (frame.style == PillEyeStyle::kCapsule) {
        float width = static_cast<float>(eye.width);
        float height = static_cast<float>(eye.height);
        if (frame.lid > 0 && height > kClosedLine) {
            const float shut = static_cast<float>(frame.lid) / 255.0f;
            height += (static_cast<float>(kClosedLine) - height) * shut;
            width += (std::max(width, 52.0f) - width) * shut;
        }
        const float hw = width * 0.5f;
        const float hh = height * 0.5f;
        DrawShape(canvas, static_cast<int>(cx - hw) - 1, static_cast<int>(cy - hh) - 1,
                  static_cast<int>(cx + hw) + 1, static_cast<int>(cy + hh) + 1, frame.eye_rgb, 255,
                  [&](float x, float y) { return CapsuleDistance(x - cx, y - cy, hw, hh); });
        if (frame.lid < 90 && height >= 36.0f && frame.highlight > 0) {
            // A small light in each eye is what makes it look alive.
            const float hx = cx + width * 0.18f;
            const float hy = cy - height * 0.26f;
            const float radius = std::clamp(width * 0.17f, 4.0f, 9.0f);
            DrawShape(canvas, static_cast<int>(hx - radius) - 1, static_cast<int>(hy - radius) - 1,
                      static_cast<int>(hx + radius) + 1, static_cast<int>(hy + radius) + 1,
                      kPillHighlightRgb, frame.highlight,
                      [&](float x, float y) { return std::hypot(x - hx, y - hy) - radius; });
        }
        (void)left;
        return;
    }
    const float half_chord = static_cast<float>(eye.width) * 0.5f;
    const float rise = static_cast<float>(eye.height);
    const float thickness = std::clamp(half_chord * 0.46f, 8.0f, 14.0f);
    const bool up = frame.style == PillEyeStyle::kArcUp;
    const int pad = static_cast<int>(half_chord + thickness) + 2;
    DrawShape(canvas, static_cast<int>(cx) - pad, static_cast<int>(cy - rise - thickness) - 2,
              static_cast<int>(cx) + pad, static_cast<int>(cy + rise + thickness) + 2, frame.eye_rgb, 255,
              [&](float x, float y) {
                  const float py = up ? y - cy : cy - y;
                  return ArcUpDistance(x - cx, py, half_chord, rise, thickness);
              });
}

void DrawBlush(const Canvas& canvas, const PillFaceFrame& frame, float cx, float cy) {
    if (frame.blush == 0) {
        return;
    }
    const float rx = 32.0f;
    const float ry = 18.0f;
    const float strength = static_cast<float>(frame.blush) / 255.0f * 0.85f;
    const Rgb over = Unpack(kPillBlushRgb);
    for (int y = static_cast<int>(cy - ry) - 1; y <= static_cast<int>(cy + ry) + 1; ++y) {
        if (y < 0 || y >= canvas.height) {
            continue;
        }
        uint16_t* row = canvas.buffer + static_cast<std::size_t>(y) * canvas.width;
        for (int x = static_cast<int>(cx - rx) - 1; x <= static_cast<int>(cx + rx) + 1; ++x) {
            if (x < 0 || x >= canvas.width) {
                continue;
            }
            const float r = std::hypot((static_cast<float>(x) + 0.5f - cx) / rx,
                                       (static_cast<float>(y) + 0.5f - cy) / ry);
            if (r >= 1.0f) {
                continue;
            }
            // Soft disc: full strength over the middle, feathered at the rim.
            const float feather = std::min(1.0f, (1.0f - r) * 2.4f);
            const int a = static_cast<int>(strength * feather * 255.0f);
            if (a > 0) {
                row[x] = Blend565(row[x], over, a);
            }
        }
    }
}

void DrawMouth(const Canvas& canvas, const PillFaceFrame& frame) {
    if (frame.mouth_width == 0) {
        return;
    }
    const float cx = static_cast<float>(frame.mouth_cx);
    const float cy = static_cast<float>(frame.mouth_cy);
    const float half = static_cast<float>(frame.mouth_width) * 0.5f;
    const float open = static_cast<float>(frame.mouth_open) / 255.0f;
    if (frame.mouth_open < 12) {
        const float rise = std::clamp(half * 0.5f, 4.0f, 11.0f);
        const int pad = static_cast<int>(half) + 6;
        DrawShape(canvas, static_cast<int>(cx) - pad, static_cast<int>(cy - rise) - 6,
                  static_cast<int>(cx) + pad, static_cast<int>(cy + rise) + 6, frame.eye_rgb, 255,
                  [&](float x, float y) {
                      // An "n" turned over: the smile's ends go up, the middle sags.
                      return ArcUpDistance(x - cx, cy - y, half, rise, 6.0f);
                  });
        return;
    }
    const float rx = half * (0.55f + 0.4f * open);
    const float ry = 3.0f + 17.0f * open;
    const float oy = cy + ry * 0.5f - 2.0f;
    DrawShape(canvas, static_cast<int>(cx - rx) - 1, static_cast<int>(oy - ry) - 1,
              static_cast<int>(cx + rx) + 1, static_cast<int>(oy + ry) + 1, frame.eye_rgb, 255,
              [&](float x, float y) { return EllipseDistance(x - cx, y - oy, rx, ry); });
    if (open > 0.45f) {
        const float tx_r = rx * 0.62f;
        const float ty_r = ry * 0.42f;
        const float ty = oy + ry * 0.55f;
        DrawShape(canvas, static_cast<int>(cx - tx_r) - 1, static_cast<int>(ty - ty_r) - 1,
                  static_cast<int>(cx + tx_r) + 1, static_cast<int>(ty + ty_r) + 1, kPillBlushRgb, 255,
                  [&](float x, float y) {
                      const float inside_mouth = EllipseDistance(x - cx, y - oy, rx, ry) + 1.5f;
                      return std::max(EllipseDistance(x - cx, y - ty, tx_r, ty_r), inside_mouth);
                  });
    }
}

}  // namespace

PillFaceFrame MakePillFaceFrame(FaceId face, uint8_t lid, uint8_t lift, bool speaking, uint8_t mouth) {
    if (speaking && (face == FaceId::kThink || face == FaceId::kListen || face == FaceId::kCalm ||
                     face == FaceId::kWake)) {
        // A reply that names a thinking look still talks: the face never freezes
        // while the voice plays.
        face = FaceId::kSpeak;
    }
    const Look look = LookFor(face);
    const int center_y = kBaseEyeY - static_cast<int>(lift) + look.dy;
    PillFaceFrame frame;
    frame.style = look.style;
    frame.lid = look.style == PillEyeStyle::kCapsule ? lid : static_cast<uint8_t>(0);
    frame.blush = static_cast<uint8_t>(look.blush);
    frame.highlight = 255;
    const int width = look.width;
    const int height = look.height;
    frame.left = PillEye{static_cast<int16_t>(180 - kEyeSeparation + look.dx),
                         static_cast<int16_t>(center_y), static_cast<uint16_t>(width),
                         static_cast<uint16_t>(height)};
    frame.right = PillEye{static_cast<int16_t>(180 + kEyeSeparation + look.dx),
                          static_cast<int16_t>(center_y), static_cast<uint16_t>(width),
                          static_cast<uint16_t>(height)};
    frame.mouth_cx = static_cast<int16_t>(180 + look.mouth_dx);
    frame.mouth_cy = static_cast<int16_t>(kBaseEyeY - static_cast<int>(lift) + kMouthBelowEyes);
    frame.mouth_width = static_cast<uint16_t>(look.mouth_width);
    int open = look.mouth_open;
    if (speaking) {
        open = std::max(open, static_cast<int>(mouth));
    }
    frame.mouth_open = static_cast<uint8_t>(std::clamp(open, 0, 255));
    return frame;
}

const PillFaceFrame& PillFaceTween::Step(const PillFaceFrame& target, uint32_t now_ms) {
    if (!started_) {
        // Waking: the eyes open from a closed line.
        PillFaceFrame closed = target;
        closed.style = PillEyeStyle::kCapsule;
        closed.left.height = closed.right.height = kClosedLine;
        closed.left.width = closed.right.width = 52;
        closed.blush = 0;
        closed.mouth_open = 0;
        from_ = closed;
        to_ = target;
        current_ = closed;
        start_ms_ = now_ms;
        duration_ms_ = 260;
        started_ = true;
    } else if (!SameLook(target, to_)) {
        from_ = current_;
        duration_ms_ = OnlyMouthOpenDiffers(target, to_) ? kPillMouthGlideMs : kPillGlideMs;
        to_ = target;
        start_ms_ = now_ms;
    }
    const uint32_t elapsed = now_ms - start_ms_;
    if (elapsed >= duration_ms_) {
        current_ = to_;
        animating_ = false;
    } else {
        current_ = LerpFrame(from_, to_, Smooth(static_cast<float>(elapsed) / static_cast<float>(duration_ms_)));
        animating_ = true;
    }
    current_.lid = target.lid;
    return current_;
}

void PaintPillFace(uint16_t* buffer, int width, int height, const PillFaceFrame& frame) {
    if (buffer == nullptr || width <= 0 || height <= 0) {
        return;
    }
    const std::size_t pixels = static_cast<std::size_t>(width) * static_cast<std::size_t>(height);
    std::fill(buffer, buffer + pixels, To565(frame.background_rgb));
    const Canvas canvas{buffer, width, height};

    const float cheek_y = static_cast<float>(frame.mouth_cy) - 6.0f;
    DrawBlush(canvas, frame, static_cast<float>(frame.left.cx) - 62.0f, cheek_y);
    DrawBlush(canvas, frame, static_cast<float>(frame.right.cx) + 62.0f, cheek_y);
    DrawEye(canvas, frame, frame.left, true);
    DrawEye(canvas, frame, frame.right, false);
    DrawMouth(canvas, frame);
}

}  // namespace memoria
