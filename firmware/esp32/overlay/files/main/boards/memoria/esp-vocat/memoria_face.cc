#include "memoria_face.h"

#include <cmath>
#include <cstring>

namespace memoria {
namespace {

constexpr float kPi = 3.14159265358979f;

// v3 conversation-face layout, normalised to the screen radius R.
constexpr float kEyeX = 0.30f;
constexpr float kEyeY = -0.20f;
constexpr float kEyeHalfWidth = 0.205f;
constexpr float kNoseY = 0.07f;
constexpr float kMouthY = 0.27f;
constexpr float kEyeSquash = 1.12f;

// Closed eye: dome disc minus a shallow cut disc (the reference crescent).
constexpr float kClosedCutTop = -0.20f;
constexpr float kClosedCutRadius = 2.70f;
constexpr float kOpenCutTop = 1.00f;
constexpr float kOpenCutRadius = 12.0f;

// One pixel of analytic edge softness keeps strokes smooth on the LCD.
constexpr float kEdgeSoftness = 0.5f;

struct EyeSpec {
    float openness;
    float tilt_deg;
    float squint;
    float scale;
    float offset_x;
    float offset_y;
    bool almond;
    float gaze_x;
    float gaze_y;
    float roundness;
    float pupil;
};

struct ExpressionEntry {
    const char* name;
    EyeSpec left;
    EyeSpec right;
};

constexpr EyeSpec ClosedEye(float tilt, float squint = 0.0f, float scale = 1.0f,
                           float ox = 0.0f, float oy = 0.0f) {
    return EyeSpec{0.0f, tilt, squint, scale, ox, oy, false, 0.0f, 0.0f, 0.84f, 0.40f};
}

constexpr EyeSpec OpenEye(float gaze_x, float gaze_y, float scale = 1.0f, float ox = 0.0f,
                         float oy = 0.0f, float roundness = 0.84f, float pupil = 0.40f) {
    return EyeSpec{1.0f, 0.0f, 0.0f, scale, ox, oy, true, gaze_x, gaze_y, roundness, pupil};
}

constexpr ExpressionEntry kExpressions[] = {
    {"neutral", ClosedEye(0.0f), ClosedEye(0.0f)},
    {"happy", ClosedEye(14.0f, 0.08f, 1.02f), ClosedEye(14.0f, 0.08f, 1.02f)},
    {"sad", ClosedEye(-15.0f, 0.10f, 0.97f, 0.0f, 0.02f),
            ClosedEye(-15.0f, 0.10f, 0.97f, 0.0f, 0.02f)},
    {"surprised", OpenEye(0.0f, 0.012f, 1.08f, 0.0f, -0.02f, 0.92f, 0.38f),
                  OpenEye(0.0f, 0.012f, 1.08f, 0.0f, -0.02f, 0.92f, 0.38f)},
    {"loving", ClosedEye(-8.0f, -0.08f), ClosedEye(-8.0f, -0.08f)},
    {"thinking", OpenEye(0.055f, -0.045f, 1.0f, 0.04f, -0.04f, 0.80f, 0.40f),
                 OpenEye(0.055f, -0.045f, 1.0f, 0.04f, -0.04f, 0.80f, 0.40f)},
    {"embarrassed", ClosedEye(-5.0f, 0.12f, 0.96f, 0.0f, 0.02f),
                    ClosedEye(-5.0f, 0.12f, 0.96f, 0.0f, 0.02f)},
    {"wink", ClosedEye(16.0f, 0.04f),
             OpenEye(0.01f, 0.01f, 0.98f, 0.0f, 0.0f, 0.86f, 0.40f)},
    {"speaking", OpenEye(0.0f, 0.008f, 1.0f, 0.0f, 0.0f, 0.86f, 0.38f),
                 OpenEye(0.0f, 0.008f, 1.0f, 0.0f, 0.0f, 0.86f, 0.38f)},
};

struct AliasEntry {
    const char* name;
    const char* canonical;
};

// Server-side emotion names (screen.expression) mapped onto the faces above.
// Unknown names fall back to neutral rather than to the colour emoji set.
constexpr AliasEntry kAliases[] = {
    {"idle", "neutral"},      {"sleepy", "neutral"},     {"neutral", "neutral"},
    {"laughing", "happy"},    {"funny", "happy"},        {"delicious", "happy"},
    {"confident", "happy"},   {"silly", "happy"},        {"relaxed", "happy"},
    {"kissy", "happy"},       {"happy", "happy"},
    {"embarrassed", "embarrassed"},
    {"winking", "wink"},      {"wink", "wink"},
    {"crying", "sad"},        {"angry", "sad"},          {"sad", "sad"},
    {"shocked", "surprised"}, {"surprised", "surprised"},
    {"caring", "loving"},     {"loving", "loving"},
    {"curious", "thinking"},  {"confused", "thinking"},  {"thinking", "thinking"},
    {"speaking", "speaking"}, {"talking", "speaking"},
};

inline float Clamp(float value, float low, float high) {
    return value < low ? low : (value > high ? high : value);
}

inline void Acc(float& distance, float value) {
    distance = fmaxf(distance, value);
}

inline float WrapDeg(float deg) {
    deg = fmodf(deg, 360.0f);
    if (deg < 0.0f) {
        deg += 360.0f;
    }
    return deg;
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

const ExpressionEntry* FindExpression(const char* emotion) {
    if (emotion == nullptr || emotion[0] == '\0') {
        return &kExpressions[0];
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
            return &entry;
        }
    }
    return &kExpressions[0];
}

uint8_t ExpressionKind(const ExpressionEntry* entry) {
    return static_cast<uint8_t>(entry - kExpressions);
}

FaceEye MakeEye(const EyeSpec& spec, float openness, bool left) {
    FaceEye eye = {};
    eye.center_x = (left ? -1.0f : 1.0f) * kEyeX + spec.offset_x;
    eye.center_y = kEyeY + spec.offset_y;
    eye.half_width = kEyeHalfWidth;
    eye.openness = openness;
    eye.rotation_deg = spec.tilt_deg * (left ? 1.0f : -1.0f);
    eye.scale = spec.scale;
    eye.squint = spec.squint;
    eye.gaze_x = spec.gaze_x;
    eye.gaze_y = spec.gaze_y;
    eye.roundness = spec.roundness;
    eye.pupil = spec.pupil;
    eye.almond = spec.almond;
    return eye;
}

float Disk(float px, float py, float radius) {
    return radius - sqrtf(px * px + py * py);
}

float Ellipse(float px, float py, float rx, float ry) {
    if (rx <= 1.0e-6f || ry <= 1.0e-6f) {
        return -1.0f;
    }
    const float scale = fminf(rx, ry);
    return scale * (1.0f - sqrtf((px / rx) * (px / rx) + (py / ry) * (py / ry)));
}

void Rotate(float& px, float& py, float deg) {
    const float angle = deg * (kPi / 180.0f);
    const float ca = cosf(angle);
    const float sa = sinf(angle);
    const float rx = px * ca + py * sa;
    const float ry = -px * sa + py * ca;
    px = rx;
    py = ry;
}

float Capsule(float px, float py, float x0, float y0, float x1, float y1, float radius) {
    const float dx = x1 - x0;
    const float dy = y1 - y0;
    const float length2 = dx * dx + dy * dy;
    if (length2 < 1.0e-12f) {
        return Disk(px - x0, py - y0, radius);
    }
    const float t = Clamp(((px - x0) * dx + (py - y0) * dy) / length2, 0.0f, 1.0f);
    return radius - sqrtf((px - (x0 + t * dx)) * (px - (x0 + t * dx)) +
                          (py - (y0 + t * dy)) * (py - (y0 + t * dy)));
}

float Crescent(float x, float y, const FaceEye& eye) {
    const float hw = eye.half_width * eye.scale;
    if (hw <= 0.0f) {
        return -1.0f;
    }
    float dx = (x - eye.center_x) / hw;
    float dy = (y - eye.center_y) / hw;
    if (eye.rotation_deg != 0.0f) {
        Rotate(dx, dy, eye.rotation_deg);
    }
    dy *= kEyeSquash;
    const float openness = Clamp(eye.openness, 0.0f, 1.0f);
    const float dome = 1.0f - sqrtf(dx * dx + dy * dy);
    const float cut_top = kClosedCutTop + (kOpenCutTop - kClosedCutTop) * openness -
                          eye.squint * (1.0f - openness);
    const float cut_radius =
        kClosedCutRadius + (kOpenCutRadius - kClosedCutRadius) * openness * openness;
    const float cut_dy = dy - (cut_top + cut_radius);
    const float cut = sqrtf(dx * dx + cut_dy * cut_dy) - cut_radius;
    return fminf(dome, cut) * hw;
}

float Almond(float x, float y, const FaceEye& eye) {
    const float hw = eye.half_width * eye.scale;
    if (hw <= 0.0f) {
        return -1.0f;
    }
    const float openness = Clamp(eye.openness, 0.0f, 1.0f);
    const float roundness = eye.roundness * (0.35f + 0.65f * openness);
    const float sclera = Ellipse(x - eye.center_x, y - eye.center_y, hw, hw * roundness);
    const float pr = hw * eye.pupil;
    const float hole = Disk(x - (eye.center_x + eye.gaze_x), y - (eye.center_y + eye.gaze_y), pr);
    return fminf(sclera, -hole);
}

float EyeSdf(float x, float y, const FaceEye& eye) {
    if (eye.almond && eye.openness > 0.12f) {
        return Almond(x, y, eye);
    }
    FaceEye closed = eye;
    closed.openness = 0.0f;
    return Crescent(x, y, closed);
}

float NoseGrain(float px, float py, float ox = 0.0f, float oy = 0.0f, float scale = 1.0f) {
    return Ellipse(px - ox, py - (kNoseY + oy), 0.012f * scale, 0.028f * scale);
}

float MouthLine(float px, float py, float width, float thick, float ox = 0.0f, float oy = 0.0f) {
    return Capsule(px, py, -width + ox, kMouthY + oy, width + ox, kMouthY + oy, thick);
}

float ArcStroke(float px, float py, float cx, float cy, float radius, float thickness,
                float deg0, float deg1) {
    const float dx = px - cx;
    const float dy = py - cy;
    const float rad = sqrtf(dx * dx + dy * dy);
    float d = (thickness * 0.5f) - fabsf(rad - radius);
    const float ang = WrapDeg(atan2f(dy, dx) * (180.0f / kPi));
    const float a0 = WrapDeg(deg0);
    const float a1 = WrapDeg(deg1);
    const bool inside = (a0 <= a1) ? (ang >= a0 && ang <= a1) : (ang >= a0 || ang <= a1);
    if (!inside) {
        d = -1.0f;
    }
    const float a0r = deg0 * (kPi / 180.0f);
    const float a1r = deg1 * (kPi / 180.0f);
    Acc(d, Disk(px - (cx + radius * cosf(a0r)), py - (cy + radius * sinf(a0r)), thickness * 0.5f));
    Acc(d, Disk(px - (cx + radius * cosf(a1r)), py - (cy + radius * sinf(a1r)), thickness * 0.5f));
    return d;
}

float MouthSmile(float px, float py, float width, float depth, float thick, float oy = 0.0f) {
    const float radius = (width * width + depth * depth) / (2.0f * depth);
    const float cy = kMouthY + oy - (radius - depth);
    const float span = asinf(Clamp(width / radius, 0.0f, 1.0f)) * (180.0f / kPi);
    return ArcStroke(px, py, 0.0f, cy, radius, thick, 90.0f - span, 90.0f + span);
}

float MouthFrown(float px, float py, float width, float depth, float thick = 0.016f,
                 float oy = 0.0f) {
    const float radius = (width * width + depth * depth) / (2.0f * depth);
    const float cy = kMouthY + oy + (radius - depth);
    const float span = asinf(Clamp(width / radius, 0.0f, 1.0f)) * (180.0f / kPi);
    return ArcStroke(px, py, 0.0f, cy, radius, thick, -90.0f - span, -90.0f + span);
}

float MouthO(float px, float py, float r_out, float r_in, float oy = 0.0f) {
    const float rad = sqrtf(px * px + (py - (kMouthY + oy - 0.02f)) * (py - (kMouthY + oy - 0.02f)));
    return fminf(r_out - rad, rad - r_in);
}

float MouthSpeak(float px, float py, float oy = 0.0f) {
    const float cy = kMouthY + oy;
    const float outer = Ellipse(px, py - cy, 0.092f, 0.044f);
    const float inner = Ellipse(px, py - cy, 0.058f, 0.016f);
    return fminf(outer, -inner);
}

float Brow(float px, float py, float cx, float cy, float length, float thick, float tilt) {
    const float a = tilt * (kPi / 180.0f);
    const float dx = cosf(a) * length * 0.5f;
    const float dy = sinf(a) * length * 0.5f;
    return Capsule(px, py, cx - dx, cy - dy, cx + dx, cy + dy, thick);
}

float Star4(float px, float py, float cx, float cy, float r) {
    const float dx = px - cx;
    const float dy = py - cy;
    return fmaxf(Ellipse(dx, dy, r * 0.20f, r), Ellipse(dx, dy, r, r * 0.20f));
}

float Teardrop(float px, float py, float cx, float cy, float length, float width, float tip_deg) {
    float lx = px - cx;
    float ly = py - cy;
    Rotate(lx, ly, tip_deg - 90.0f);
    const float tip_y = -length * 0.50f;
    const float bulb_y = length * 0.16f;
    const float d_bulb = Disk(lx, ly - bulb_y, width);
    const float body = Ellipse(lx, ly - (tip_y + length * 0.38f), width * 0.72f, length * 0.42f);
    return fmaxf(d_bulb, body);
}

float BlushPatch(float px, float py, float cx, float cy) {
    return Ellipse(px - cx, py - cy, 0.095f, 0.042f);
}

void AddExtras(float px, float py, uint8_t kind, float& ink, float& blush) {
    switch (kind) {
        case 0:  // neutral
            Acc(ink, NoseGrain(px, py));
            Acc(ink, MouthLine(px, py, 0.14f, 0.014f));
            break;
        case 1:  // happy
            Acc(ink, NoseGrain(px, py, 0.0f, 0.008f));
            Acc(ink, MouthSmile(px, py, 0.19f, 0.06f, 0.018f));
            Acc(ink, Star4(px, py, -0.62f, -0.50f, 0.046f));
            Acc(ink, Star4(px, py, 0.62f, -0.50f, 0.046f));
            break;
        case 2:  // sad
            Acc(ink, NoseGrain(px, py, 0.0f, 0.012f, 0.92f));
            Acc(ink, MouthFrown(px, py, 0.145f, 0.048f));
            Acc(ink, Teardrop(px, py, 0.48f, -0.02f, 0.12f, 0.028f, 112.0f));
            break;
        case 3:  // surprised
            Acc(ink, Brow(px, py, -kEyeX, kEyeY - 0.22f, 0.15f, 0.012f, 8.0f));
            Acc(ink, Brow(px, py, kEyeX, kEyeY - 0.22f, 0.15f, 0.012f, -8.0f));
            Acc(ink, NoseGrain(px, py, 0.0f, -0.01f, 0.90f));
            Acc(ink, MouthO(px, py, 0.058f, 0.030f, -0.01f));
            break;
        case 4:  // loving
            Acc(ink, NoseGrain(px, py));
            Acc(ink, MouthSmile(px, py, 0.165f, 0.045f, 0.017f));
            Acc(blush, BlushPatch(px, py, -0.50f, 0.16f));
            Acc(blush, BlushPatch(px, py, 0.50f, 0.16f));
            break;
        case 5:  // thinking
            Acc(ink, Brow(px, py, -kEyeX + 0.04f, kEyeY - 0.20f, 0.14f, 0.011f, 6.0f));
            Acc(ink, Brow(px, py, kEyeX + 0.06f, kEyeY - 0.24f, 0.15f, 0.012f, -26.0f));
            Acc(ink, NoseGrain(px, py, 0.04f, -0.02f));
            Acc(ink, MouthLine(px, py, 0.12f, 0.014f, 0.03f, -0.02f));
            Acc(ink, Disk(px - 0.52f, py + 0.48f, 0.024f));
            Acc(ink, Disk(px - 0.62f, py + 0.58f, 0.017f));
            Acc(ink, Disk(px - 0.70f, py + 0.66f, 0.011f));
            break;
        case 6:  // embarrassed
            Acc(ink, NoseGrain(px, py, 0.0f, 0.01f, 0.92f));
            Acc(ink, MouthLine(px, py, 0.08f, 0.015f, 0.0f, 0.01f));
            Acc(ink, Teardrop(px, py, -0.58f, -0.46f, 0.14f, 0.032f, -48.0f));
            Acc(blush, BlushPatch(px, py, -0.50f, 0.12f));
            Acc(blush, BlushPatch(px, py, 0.50f, 0.12f));
            break;
        case 7:  // wink
            Acc(ink, NoseGrain(px, py));
            Acc(ink, MouthSmile(px, py, 0.16f, 0.048f, 0.016f));
            break;
        case 8:  // speaking
            Acc(ink, NoseGrain(px, py));
            Acc(ink, MouthSpeak(px, py));
            break;
        default:
            Acc(ink, NoseGrain(px, py));
            Acc(ink, MouthLine(px, py, 0.14f, 0.014f));
            break;
    }
}

float BlushGain(uint8_t kind) {
    if (kind == 4) {
        return 0.36f;  // loving
    }
    if (kind == 6) {
        return 0.42f;  // embarrassed
    }
    return 0.0f;
}

inline uint16_t GrayRgb565(float coverage) {
    const uint8_t level = static_cast<uint8_t>(Clamp(coverage, 0.0f, 1.0f) * 255.0f + 0.5f);
    return static_cast<uint16_t>(((level >> 3) << 11) | ((level >> 2) << 5) | (level >> 3));
}

}  // namespace

const char* const kFaceEmotions[] = {
    "neutral", "happy", "sad", "surprised", "loving", "thinking",
    "embarrassed", "wink", "speaking",
};
const std::size_t kFaceEmotionCount = sizeof(kFaceEmotions) / sizeof(kFaceEmotions[0]);

Face FaceForEmotion(const char* emotion, float blink) {
    const ExpressionEntry* entry = FindExpression(emotion);
    const float blink_amount = Clamp(blink, 0.0f, 1.0f);
    Face face = {};
    face.kind = ExpressionKind(entry);
    const float left_open = entry->left.almond
                                ? Clamp(entry->left.openness - blink_amount, 0.0f, 1.0f)
                                : entry->left.openness;
    const float right_open = entry->right.almond
                                 ? Clamp(entry->right.openness - blink_amount, 0.0f, 1.0f)
                                 : entry->right.openness;
    face.left = MakeEye(entry->left, left_open, true);
    face.right = MakeEye(entry->right, right_open, false);
    return face;
}

bool FaceBlinks(const char* emotion) {
    const ExpressionEntry* entry = FindExpression(emotion);
    return entry->left.almond && entry->right.almond;
}

void RenderFaceRgb565(uint16_t* buffer, int width, int height, const Face& face) {
    if (buffer == nullptr || width <= 0 || height <= 0) {
        return;
    }
    std::memset(buffer, 0, sizeof(uint16_t) * static_cast<size_t>(width) * height);

    const float radius = 0.5f * static_cast<float>(width < height ? width : height);
    const float origin_x = 0.5f * static_cast<float>(width);
    const float origin_y = 0.5f * static_cast<float>(height);
    const float blush_gain = BlushGain(face.kind);
    const float r2_limit = 1.05f * 1.05f;

    for (int y = 0; y < height; ++y) {
        uint16_t* row = buffer + static_cast<size_t>(y) * width;
        const float py = (static_cast<float>(y) + 0.5f - origin_y) / radius;
        for (int x = 0; x < width; ++x) {
            const float px = (static_cast<float>(x) + 0.5f - origin_x) / radius;
            if (px * px + py * py > r2_limit) {
                continue;
            }
            float ink = fmaxf(EyeSdf(px, py, face.left), EyeSdf(px, py, face.right));
            float blush = -1.0f;
            AddExtras(px, py, face.kind, ink, blush);
            const float ink_cov = Clamp(ink * radius + kEdgeSoftness, 0.0f, 1.0f);
            const float blush_cov =
                blush_gain > 0.0f ? Clamp(blush * radius + kEdgeSoftness, 0.0f, 1.0f) * blush_gain
                                  : 0.0f;
            const float coverage = fmaxf(ink_cov, blush_cov);
            if (coverage <= 0.0f) {
                continue;
            }
            row[x] = GrayRgb565(coverage);
        }
    }
}

}  // namespace memoria
