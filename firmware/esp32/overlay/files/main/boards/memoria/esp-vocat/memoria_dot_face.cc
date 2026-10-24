#include "memoria_dot_face.h"

#include <cstring>

namespace memoria {
namespace {

constexpr int kCols = 13;
constexpr int kRows = 11;
constexpr int kPitch = 16;
constexpr int kDot = 10;
constexpr int kRim = 8;
constexpr int kCaptionLimit = 216;

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
    // Rows this glyph hides while the crisis-round blink closes the eyes. Only
    // the calm face blinks today; 0 keeps every other glyph intact.
    uint8_t eye_rows;
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
    }, 0x06},
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
    }, 0x00},
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
    }, 0x00},
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
    }, 0x00},
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
    }, 0x00},
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
    }, 0x00},
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
    }, 0x00},
};

const char* const kNames[kFaceCount] = {
    "calm", "listen", "think", "speak", "connect", "greet", "pair",
};

static_assert(static_cast<int>(FaceId::kCalm) == 0, "name table starts at calm");
static_assert(static_cast<int>(FaceId::kPair) + 1 == kFaceCount, "seven faces");
static_assert(kCrisisBlinkClosedMs < kCrisisBlinkPeriodMs,
              "a slow blink closes for a fraction of its cycle");

FaceId ClampFace(FaceId id) {
    const auto index = static_cast<uint8_t>(id);
    return index < kFaceCount ? id : FaceId::kCalm;
}

int OriginX() { return (kFaceScreen - kCols * kPitch) / 2; }

int OriginY(bool captioned) {
    const int height = kRows * kPitch;
    if (captioned) {
        return kRim + (kCaptionLimit - kRim - height) / 2;
    }
    return (kFaceScreen - height) / 2;
}

}  // namespace

const char* FaceName(FaceId id) { return kNames[static_cast<int>(ClampFace(id))]; }

const char* ReceivedExpression(const FaceFacts& facts) {
    for (int i = 0; i < kExpressionNameMax; ++i) {
        const char character = facts.expression[i];
        if (character == '\0') {
            return facts.expression;
        }
        // A server value with a character outside a short token, or one that
        // fills the buffer without a terminator, is treated as no expression.
        const bool letter = (character >= 'a' && character <= 'z') || character == '_';
        if (!letter) {
            return "";
        }
    }
    return "";
}

bool CrisisEyesClosed(uint32_t elapsed_ms) {
    return elapsed_ms % kCrisisBlinkPeriodMs >= kCrisisBlinkPeriodMs - kCrisisBlinkClosedMs;
}

namespace {

// The reply expressions the seven faces can show, and the face each one draws.
// Names the server sends and the media edge does not fold are both listed, so
// the panel reads either spelling. Anything else stays the calm face.
struct ExpressionFace {
    const char* name;
    FaceId face;
};

constexpr ExpressionFace kExpressionFaces[] = {
    {"happy", FaceId::kSpeak},
    {"caring", FaceId::kSpeak},
    {"loving", FaceId::kSpeak},
    {"curious", FaceId::kThink},
    {"thinking", FaceId::kThink},
    {"sad", FaceId::kCalm},
    {"surprised", FaceId::kGreet},
    {"neutral", FaceId::kCalm},
};

bool SameToken(const char* value, const char* name) {
    for (int i = 0; i < kExpressionNameMax; ++i) {
        if (value[i] != name[i]) {
            return false;
        }
        if (value[i] == '\0') {
            return true;
        }
    }
    return false;
}

}  // namespace

FaceId FaceForExpression(const char* expression) {
    // No reply expression yet: the device is simply speaking, so it keeps the
    // speaking face rather than dropping to calm between replies.
    if (expression == nullptr || expression[0] == '\0') {
        return FaceId::kSpeak;
    }
    for (const ExpressionFace& entry : kExpressionFaces) {
        if (SameToken(expression, entry.name)) {
            return entry.face;
        }
    }
    return FaceId::kCalm;
}

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
            return FaceForExpression(ReceivedExpression(facts));
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

void PaintDotFace(uint16_t* buffer, int width, int height, const FaceFrame& frame) {
    if (buffer == nullptr || width <= 0 || height <= 0) {
        return;
    }
    std::memset(buffer, 0, static_cast<std::size_t>(width) * static_cast<std::size_t>(height) *
                               sizeof(uint16_t));
    const Glyph& glyph = kGlyphs[static_cast<int>(ClampFace(frame.face))];
    const int origin_x = OriginX();
    const int origin_y = OriginY(frame.captioned);
    for (int row = 0; row < kRows; ++row) {
        if (frame.eyes_closed && ((glyph.eye_rows >> row) & 1u) != 0) {
            continue;
        }
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
