#include "memoria_pill_face.h"

#include <algorithm>
#include <cmath>
#include <cstddef>

namespace memoria {
namespace {

constexpr float kPi = 3.14159265358979323846f;

struct Rgb {
    int r;
    int g;
    int b;
};

Rgb Unpack(uint32_t rgb) {
    return Rgb{static_cast<int>((rgb >> 16) & 0xFF), static_cast<int>((rgb >> 8) & 0xFF),
               static_cast<int>(rgb & 0xFF)};
}

uint32_t Pack(Rgb rgb) {
    rgb.r = std::clamp(rgb.r, 0, 255);
    rgb.g = std::clamp(rgb.g, 0, 255);
    rgb.b = std::clamp(rgb.b, 0, 255);
    return (static_cast<uint32_t>(rgb.r) << 16) | (static_cast<uint32_t>(rgb.g) << 8) |
           static_cast<uint32_t>(rgb.b);
}

uint16_t To565(uint32_t rgb) {
    const Rgb value = Unpack(rgb);
    return static_cast<uint16_t>(((value.r >> 3) << 11) | ((value.g >> 2) << 5) | (value.b >> 3));
}

uint16_t Blend565(uint16_t base, uint32_t over, uint8_t alpha) {
    const int base_r = ((base >> 11) & 0x1F) * 255 / 31;
    const int base_g = ((base >> 5) & 0x3F) * 255 / 63;
    const int base_b = (base & 0x1F) * 255 / 31;
    const Rgb overlay = Unpack(over);
    const int inverse = 255 - alpha;
    const Rgb mixed{
        (base_r * inverse + overlay.r * alpha + 127) / 255,
        (base_g * inverse + overlay.g * alpha + 127) / 255,
        (base_b * inverse + overlay.b * alpha + 127) / 255,
    };
    return To565(Pack(mixed));
}

int IntegerSqrt(int value) {
    if (value <= 0) {
        return 0;
    }
    int result = static_cast<int>(std::sqrt(static_cast<float>(value)));
    while ((result + 1) * (result + 1) <= value) {
        ++result;
    }
    while (result * result > value) {
        --result;
    }
    return result;
}

struct Palette {
    int scale;
};

Palette PaletteFor(FaceId face) {
    switch (face) {
        case FaceId::kHappy:
            return {148};
        case FaceId::kListen:
            return {138};
        case FaceId::kThink:
            return {142};
        case FaceId::kSpeak:
            return {150};
        case FaceId::kSurprised:
            return {174};
        case FaceId::kShy:
            return {138};
        case FaceId::kSleepy:
            return {126};
        case FaceId::kComfort:
            return {144};
        case FaceId::kWake:
            return {156};
        case FaceId::kCalm:
        default:
            return {136};
    }
}

PillEye WithSize(int cx, int cy, int width, int height) {
    PillEye eye;
    eye.cx = static_cast<int16_t>(std::clamp(cx, -1000, 1000));
    eye.cy = static_cast<int16_t>(std::clamp(cy, -1000, 1000));
    eye.width = static_cast<uint16_t>(std::clamp(width, 4, 180));
    eye.height = static_cast<uint16_t>(std::clamp(height, 4, 220));
    return eye;
}

void DrawCapsule(uint16_t* buffer, int width, int height, const PillEye& eye, uint32_t color,
                 uint8_t alpha) {
    const int half_width = eye.width / 2;
    const int half_height = eye.height / 2;
    const int radius = std::min(half_width, half_height);
    if (half_width <= 0 || half_height <= 0 || radius <= 0) {
        return;
    }
    const int y0 = std::max(0, static_cast<int>(eye.cy) - half_height);
    const int y1 = std::min(height - 1, static_cast<int>(eye.cy) + half_height);
    for (int y = y0; y <= y1; ++y) {
        const int distance = std::abs(y - static_cast<int>(eye.cy));
        int half_span = half_width;
        const int cap_start = half_height - radius;
        if (distance > cap_start) {
            const int cap_distance = distance - cap_start;
            half_span = IntegerSqrt(radius * radius - cap_distance * cap_distance);
        }
        const int x0 = std::max(0, static_cast<int>(eye.cx) - half_span);
        const int x1 = std::min(width - 1, static_cast<int>(eye.cx) + half_span);
        for (int x = x0; x <= x1; ++x) {
            uint16_t& pixel = buffer[static_cast<std::size_t>(y) * width + x];
            pixel = alpha == 255 ? To565(color) : Blend565(pixel, color, alpha);
        }
    }
}

}  // namespace

PillFaceFrame MakePillFaceFrame(FaceId face, bool eyes_closed, uint8_t lift, uint32_t now_ms) {
    const Palette palette = PaletteFor(face);
    const float t = static_cast<float>(now_ms % 2400) / 2400.0f;
    const float sway = std::sin(t * 2.0f * kPi);
    const float breathe = 0.5f + 0.5f * std::sin((t * 2.0f + 0.17f) * kPi);
    const int center_y = 172 - static_cast<int>(lift);
    const int drift = static_cast<int>(sway * 14.0f);
    const int separation = 48 + static_cast<int>(sway * 5.0f);
    const int width = static_cast<int>(palette.scale * (0.20f + 0.06f * breathe));
    const int height = static_cast<int>(palette.scale * (0.65f + 0.18f * breathe));

    PillFaceFrame frame;
    frame.background_rgb = kPillBackgroundRgb;
    frame.eye_rgb = kPillEyeRgb;
    frame.eyes_closed = eyes_closed;
    if (eyes_closed) {
        frame.left = WithSize(180 - separation, center_y, 42, 16);
        frame.right = WithSize(180 + separation, center_y, 42, 16);
    } else {
        frame.left = WithSize(180 - separation + drift, center_y, width, height);
        frame.right = WithSize(180 + separation + drift / 2, center_y, width, height);
    }
    return frame;
}

void PaintPillFace(uint16_t* buffer, int width, int height, const PillFaceFrame& frame) {
    if (buffer == nullptr || width <= 0 || height <= 0) {
        return;
    }
    const std::size_t pixels = static_cast<std::size_t>(width) * static_cast<std::size_t>(height);
    std::fill(buffer, buffer + pixels, To565(frame.background_rgb));

    const PillEye eyes[] = {frame.left, frame.right};
    for (const PillEye& eye : eyes) {
        DrawCapsule(buffer, width, height, eye, frame.eye_rgb, 255);
    }
}

}  // namespace memoria
