#include "memoria_dot_face.h"

#include <cstring>

namespace memoria {
namespace {

constexpr int kCols = 13;
constexpr int kRows = 11;
constexpr int kPitch = 16;
constexpr int kDot = 10;
constexpr int kCaptionBottom = 216;

constexpr uint16_t Row(const char* text) {
    uint16_t bits = 0;
    for (int col = 0; col < kCols; ++col) {
        if (text[col] == '#') {
            bits = static_cast<uint16_t>(bits | (1u << col));
        }
    }
    return bits;
}

struct Glyph {
    uint16_t row[kRows];
};

constexpr Glyph kGlyphs[kFaceCount] = {
    {{
        Row("............."),
        Row("..###...###.."),
        Row("..###...###.."),
        Row("............."),
        Row("............."),
        Row("............."),
        Row("....#####...."),
        Row("............."),
        Row("............."),
        Row("............."),
        Row("............."),
    }},
    {{
        Row("............."),
        Row("..##.....##.."),
        Row("..##.....##.."),
        Row("..##.....##.."),
        Row("............."),
        Row("............."),
        Row(".....#####..."),
        Row("....##...##.."),
        Row(".....#####..."),
        Row("............."),
        Row("............."),
    }},
    {{
        Row("............."),
        Row("....##....##."),
        Row("..##....##..."),
        Row("............."),
        Row("............."),
        Row("............."),
        Row("....#####...."),
        Row("............."),
        Row("...#........."),
        Row("............."),
        Row("............."),
    }},
    {{
        Row("............."),
        Row("..##.....##.."),
        Row("..##.....##.."),
        Row("............."),
        Row("............."),
        Row("...#######..."),
        Row("..##.....##.."),
        Row("..##.....##.."),
        Row("...#######..."),
        Row("............."),
        Row("............."),
    }},
    {{
        Row("............."),
        Row(".##......##.."),
        Row("...##......##"),
        Row("............."),
        Row("............."),
        Row("............."),
        Row(".....#.#.#..."),
        Row("............."),
        Row("............."),
        Row("............."),
        Row("............."),
    }},
    {{
        Row(".##.......##."),
        Row("...##...##..."),
        Row("....#...#...."),
        Row("............."),
        Row("............."),
        Row("............."),
        Row("....#...#...."),
        Row("...#######..."),
        Row("............."),
        Row("............."),
        Row("............."),
    }},
    {{
        Row("..##.....##.."),
        Row("..##.....##.."),
        Row("............."),
        Row("............."),
        Row("....#####...."),
        Row("....#...#...."),
        Row("....#...#...."),
        Row("....#####...."),
        Row("............."),
        Row("............."),
        Row("............."),
    }},
};

const char* const kNames[kFaceCount] = {
    "calm", "listen", "think", "speak", "connect", "greet", "pair",
};

static_assert(static_cast<int>(FaceId::kCalm) == 0, "name table starts at calm");
static_assert(static_cast<int>(FaceId::kPair) + 1 == kFaceCount, "seven faces");

FaceId ClampFace(FaceId id) {
    const auto index = static_cast<uint8_t>(id);
    return index < kFaceCount ? id : FaceId::kCalm;
}

int OriginX() { return (kFaceScreen - kCols * kPitch) / 2; }

int OriginY(bool captioned) {
    const int height = kRows * kPitch;
    if (captioned) {
        return kCaptionBottom - height;
    }
    return (kFaceScreen - height) / 2;
}

}  // namespace

const char* FaceName(FaceId id) { return kNames[static_cast<int>(ClampFace(id))]; }

FaceId FaceForFacts(const FaceFacts& facts) {
    if (facts.crisis) {
        return FaceId::kCalm;
    }
    if (facts.intro) {
        return FaceId::kGreet;
    }
    if (facts.setup) {
        return FaceId::kPair;
    }
    switch (facts.state) {
        case kFaceStateStarting:
        case kFaceStateWifiConfiguring:
        case kFaceStateConnecting:
        case kFaceStateRecovering:
        case kFaceStateUpgrading:
        case kFaceStateActivating:
            return FaceId::kConnect;
        case kFaceStateListening:
            return facts.user_spoke ? FaceId::kThink : FaceId::kListen;
        case kFaceStateSpeaking:
            return FaceId::kSpeak;
        case kFaceStateAudioTesting:
            return FaceId::kListen;
        default:
            return FaceId::kCalm;
    }
}

FaceRect FaceBounds(bool captioned) {
    FaceRect box;
    box.x0 = OriginX();
    box.y0 = OriginY(captioned);
    box.x1 = box.x0 + kCols * kPitch;
    box.y1 = box.y0 + kRows * kPitch;
    return box;
}

void PaintDotFace(uint16_t* buffer, int width, int height, FaceId id, bool captioned) {
    if (buffer == nullptr || width <= 0 || height <= 0) {
        return;
    }
    std::memset(buffer, 0, static_cast<std::size_t>(width) * static_cast<std::size_t>(height) *
                               sizeof(uint16_t));
    const Glyph& glyph = kGlyphs[static_cast<int>(ClampFace(id))];
    const int origin_x = OriginX();
    const int origin_y = OriginY(captioned);
    for (int row = 0; row < kRows; ++row) {
        const uint16_t bits = glyph.row[row];
        for (int col = 0; col < kCols; ++col) {
            if ((bits & (1u << col)) == 0) {
                continue;
            }
            const int x0 = origin_x + col * kPitch + (kPitch - kDot) / 2;
            const int y0 = origin_y + row * kPitch + (kPitch - kDot) / 2;
            for (int dy = 0; dy < kDot; ++dy) {
                const int y = y0 + dy;
                if (y < 0 || y >= height) {
                    continue;
                }
                uint16_t* line = buffer + static_cast<std::size_t>(y) * static_cast<std::size_t>(width);
                for (int dx = 0; dx < kDot; ++dx) {
                    const int x = x0 + dx;
                    if (x < 0 || x >= width) {
                        continue;
                    }
                    line[x] = kFaceYellow;
                }
            }
        }
    }
}

}  // namespace memoria
