#include "memoria_face.h"

#include <cmath>
#include <cstring>

namespace memoria {
namespace {

constexpr float kPi = 3.14159265358979f;

// Reference geometry, normalised to the screen radius R (180 px on the
// 360x360 panel).  Measured from the product reference photo.
constexpr float kEyeHalfWidthRatio = 0.26f;
constexpr float kEyeOffsetXRatio = 0.35f;
constexpr float kEyeBaselineRatio = 0.00f;

// Closed eye: dome disc minus a shallow cut disc (the reference crescent).
constexpr float kClosedCutTop = -0.20f;
constexpr float kClosedCutRadius = 2.70f;
// Open eye: the cut is pushed down until it is tangent to the dome, leaving a
// full circle.
constexpr float kOpenCutTop = 1.00f;
constexpr float kOpenCutRadius = 12.0f;

// One pixel of analytic edge softness keeps the crescents smooth on the LCD.
constexpr float kEdgeSoftness = 0.5f;

struct EyeSpec {
    float openness;  // 0 = closed crescent, 1 = open disc
    float tilt_deg;  // positive raises the outer corner
    float squint;    // extra closed-eye trim, in half-width units
    float scale;
    float offset_x;  // extra offset from the default position, in R units
    float offset_y;
};

struct ExpressionEntry {
    const char* name;
    EyeSpec eye;  // symmetric faces use the same spec for both eyes
};

constexpr ExpressionEntry kExpressions[] = {
    // The product reference face: two level closed crescents.
    {"neutral", {0.00f, 0.0f, 0.00f, 1.00f, 0.00f, 0.00f}},
    // Smiling squint: outer corners raised, crescents trimmed a little deeper.
    {"happy", {0.00f, 13.0f, 0.10f, 1.02f, 0.00f, 0.00f}},
    // Drooping thin crescents.
    {"sad", {0.00f, -16.0f, 0.10f, 0.97f, 0.00f, 0.02f}},
    // Wide open round eyes.
    {"surprised", {1.00f, 0.0f, 0.00f, 1.06f, 0.00f, -0.02f}},
    // Soft, round crescents tilted gently inward: warm and concerned.
    {"loving", {0.00f, -9.0f, -0.10f, 1.00f, 0.00f, 0.00f}},
    // Looking up and to the side.
    {"thinking", {1.00f, 0.0f, 0.00f, 1.00f, 0.05f, -0.10f}},
};

struct AliasEntry {
    const char* name;
    const char* canonical;
};

// Server-side emotion names (screen.expression) mapped onto the faces above.
// Unknown names fall back to neutral rather than to the colour emoji set.
constexpr AliasEntry kAliases[] = {
    {"idle", "neutral"},     {"sleepy", "neutral"},    {"neutral", "neutral"},
    {"laughing", "happy"},   {"funny", "happy"},      {"delicious", "happy"},
    {"confident", "happy"},  {"embarrassed", "happy"},{"silly", "happy"},
    {"relaxed", "happy"},    {"kissy", "happy"},      {"winking", "happy"},
    {"crying", "sad"},       {"angry", "sad"},        {"sad", "sad"},
    {"shocked", "surprised"},{"surprised", "surprised"},
    {"caring", "loving"},    {"loving", "loving"},
    {"curious", "thinking"}, {"confused", "thinking"},{"thinking", "thinking"},
};

inline float Clamp(float value, float low, float high) {
    return value < low ? low : (value > high ? high : value);
}

bool EqualsIgnoreCase(const char* left, const char* right) {
    while (*left != '\0' && *right != '\0') {
        const char a = (*left >= 'A' && *left <= 'Z') ? static_cast<char>(*left + 32) : *left;
        const char b = (*right >= 'A' && *right <= 'Z') ? static_cast<char>(*right + 32) : *right;
        if (a != b) {
            return false;
        }
        ++left;
        ++right;
    }
    return *left == '\0' && *right == '\0';
}

const EyeSpec* FindEyeSpec(const char* emotion) {
    if (emotion == nullptr || emotion[0] == '\0') {
        return &kExpressions[0].eye;
    }
    const char* canonical = emotion;
    for (const AliasEntry& alias : kAliases) {
        if (EqualsIgnoreCase(emotion, alias.name)) {
            canonical = alias.canonical;
            break;
        }
    }
    for (const ExpressionEntry& entry : kExpressions) {
        if (EqualsIgnoreCase(canonical, entry.name)) {
            return &entry.eye;
        }
    }
    return &kExpressions[0].eye;
}

FaceEye MakeEye(const EyeSpec& spec, float openness, bool left) {
    FaceEye eye = {};
    eye.center_x = (left ? -1.0f : 1.0f) * kEyeOffsetXRatio + spec.offset_x;
    eye.center_y = kEyeBaselineRatio + spec.offset_y;
    eye.half_width = kEyeHalfWidthRatio;
    eye.openness = openness;
    // Positive tilt raises the outer corner: the renderer rotates the shape by
    // -rotation_deg, so the left eye needs the opposite sign.
    eye.rotation_deg = spec.tilt_deg * (left ? 1.0f : -1.0f);
    eye.scale = spec.scale;
    eye.squint = spec.squint;
    return eye;
}

// Signed distance (in screen-radius units, positive inside) of one eye at a
// point expressed in screen-radius units relative to the screen centre.
float EyeDistance(float x, float y, const FaceEye& eye) {
    const float hw = eye.half_width * eye.scale;
    if (hw <= 0.0f) {
        return -1.0f;
    }
    float dx = (x - eye.center_x) / hw;
    float dy = (y - eye.center_y) / hw;
    if (eye.rotation_deg != 0.0f) {
        const float angle = eye.rotation_deg * (kPi / 180.0f);
        const float ca = cosf(angle);
        const float sa = sinf(angle);
        const float rx = dx * ca + dy * sa;
        const float ry = -dx * sa + dy * ca;
        dx = rx;
        dy = ry;
    }

    const float openness = Clamp(eye.openness, 0.0f, 1.0f);
    const float dome = 1.0f - sqrtf(dx * dx + dy * dy);
    // The trim only shapes a closed eye; it fades out as the eye opens.
    const float cut_top = kClosedCutTop + (kOpenCutTop - kClosedCutTop) * openness -
                          eye.squint * (1.0f - openness);
    const float cut_radius =
        kClosedCutRadius + (kOpenCutRadius - kClosedCutRadius) * openness * openness;
    const float cut_dy = dy - (cut_top + cut_radius);
    const float cut = sqrtf(dx * dx + cut_dy * cut_dy) - cut_radius;
    return fminf(dome, cut) * hw;
}

inline uint16_t GrayRgb565(float coverage) {
    const uint8_t level = static_cast<uint8_t>(Clamp(coverage, 0.0f, 1.0f) * 255.0f + 0.5f);
    return static_cast<uint16_t>(((level >> 3) << 11) | ((level >> 2) << 5) | (level >> 3));
}

}  // namespace

const char* const kFaceEmotions[] = {
    "neutral", "happy", "sad", "surprised", "loving", "thinking",
};
const std::size_t kFaceEmotionCount = sizeof(kFaceEmotions) / sizeof(kFaceEmotions[0]);

Face FaceForEmotion(const char* emotion, float blink) {
    const EyeSpec& spec = *FindEyeSpec(emotion);
    const float openness = spec.openness <= 0.0f
                               ? 0.0f
                               : Clamp(spec.openness - Clamp(blink, 0.0f, 1.0f), 0.0f, 1.0f);
    Face face = {};
    face.left = MakeEye(spec, openness, true);
    face.right = MakeEye(spec, openness, false);
    return face;
}

bool FaceBlinks(const char* emotion) {
    return FindEyeSpec(emotion)->openness > 0.0f;
}

void RenderFaceRgb565(uint16_t* buffer, int width, int height, const Face& face) {
    if (buffer == nullptr || width <= 0 || height <= 0) {
        return;
    }
    // The reference face is white eyes on a black screen.
    std::memset(buffer, 0, sizeof(uint16_t) * static_cast<size_t>(width) * height);

    const float radius = 0.5f * static_cast<float>(width < height ? width : height);
    const float origin_x = 0.5f * static_cast<float>(width);
    const float origin_y = 0.5f * static_cast<float>(height);
    const FaceEye* eyes[2] = {&face.left, &face.right};

    float min_x = static_cast<float>(width);
    float max_x = 0.0f;
    float min_y = static_cast<float>(height);
    float max_y = 0.0f;
    for (const FaceEye* eye : eyes) {
        const float extent = eye->half_width * eye->scale * radius * 1.3f + 2.0f;
        const float cx = origin_x + eye->center_x * radius;
        const float cy = origin_y + eye->center_y * radius;
        min_x = fminf(min_x, cx - extent);
        max_x = fmaxf(max_x, cx + extent);
        min_y = fminf(min_y, cy - extent);
        max_y = fmaxf(max_y, cy + extent);
    }
    const int x0 = static_cast<int>(fmaxf(0.0f, floorf(min_x)));
    const int x1 = static_cast<int>(fminf(static_cast<float>(width - 1), ceilf(max_x)));
    const int y0 = static_cast<int>(fmaxf(0.0f, floorf(min_y)));
    const int y1 = static_cast<int>(fminf(static_cast<float>(height - 1), ceilf(max_y)));

    for (int y = y0; y <= y1; ++y) {
        uint16_t* row = buffer + static_cast<size_t>(y) * width;
        const float py = (static_cast<float>(y) + 0.5f - origin_y) / radius;
        for (int x = x0; x <= x1; ++x) {
            const float px = (static_cast<float>(x) + 0.5f - origin_x) / radius;
            // EyeDistance works in screen-radius units; convert to pixels so the
            // one-pixel edge softness stays a pixel.
            const float distance =
                fmaxf(EyeDistance(px, py, face.left), EyeDistance(px, py, face.right)) * radius;
            if (distance <= -kEdgeSoftness) {
                continue;
            }
            row[x] = GrayRgb565(distance + kEdgeSoftness);
        }
    }
}

}  // namespace memoria
