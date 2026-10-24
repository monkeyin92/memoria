#include "memoria_dot_face.h"
#include "memoria_dot_face_data.inc"

#include <cstring>

namespace memoria {
namespace {

const char* const kNames[kFaceCount] = {
    "calm", "happy", "listen", "think", "speak",
    "surprised", "shy", "sleepy", "comfort", "wake",
};

static_assert(static_cast<int>(FaceId::kCalm) == 0, "name table starts at calm");
static_assert(static_cast<int>(FaceId::kWake) + 1 == kFaceCount, "ten faces");
static_assert(kCrisisBlinkClosedMs < kCrisisBlinkPeriodMs,
              "a slow blink closes for a fraction of its cycle");

FaceId ClampFace(FaceId id) {
    const auto index = static_cast<uint8_t>(id);
    return index < kFaceCount ? id : FaceId::kCalm;
}

}  // namespace

const char* FaceName(FaceId id) { return kNames[static_cast<int>(ClampFace(id))]; }

const char* ReceivedExpression(const FaceFacts& facts) {
    for (int i = 0; i < kExpressionNameMax; ++i) {
        const char character = facts.expression[i];
        if (character == '\0') {
            return facts.expression;
        }
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

struct ExpressionFace {
    const char* name;
    FaceId face;
};

constexpr ExpressionFace kExpressionFaces[] = {
    {"happy", FaceId::kHappy},
    {"caring", FaceId::kComfort},
    {"loving", FaceId::kComfort},
    {"neutral", FaceId::kSpeak},
    {"curious", FaceId::kThink},
    {"thinking", FaceId::kThink},
    {"surprised", FaceId::kSurprised},
    {"shy", FaceId::kShy},
    {"sleepy", FaceId::kSleepy},
    {"comfort", FaceId::kComfort},
    {"wake", FaceId::kWake},
    {"sad", FaceId::kComfort},
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
    if (expression != nullptr) {
        for (const ExpressionFace& entry : kExpressionFaces) {
            if (SameToken(expression, entry.name)) {
                return entry.face;
            }
        }
    }
    return FaceId::kSpeak;
}

FaceId FaceForFacts(const FaceFacts& facts) {
    if (facts.crisis) {
        return FaceId::kCalm;
    }
    if (facts.intro) {
        return FaceId::kWake;
    }
    if (facts.setup) {
        return FaceId::kWake;
    }
    switch (facts.state) {
        case kFaceStateStarting:
        case kFaceStateWifiConfiguring:
        case kFaceStateConnecting:
        case kFaceStateRecovering:
        case kFaceStateUpgrading:
        case kFaceStateActivating:
            return FaceId::kWake;
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
    box.x0 = 30;
    box.x1 = 330;
    if (captioned) {
        box.y0 = 20;
        box.y1 = 215;
    } else {
        box.y0 = 70;
        box.y1 = 290;
    }
    return box;
}

void PaintDotFace(uint16_t* buffer, int width, int height, const FaceFrame& frame) {
    if (buffer == nullptr || width <= 0 || height <= 0) {
        return;
    }
    std::memset(buffer, 0, static_cast<std::size_t>(width) * static_cast<std::size_t>(height) *
                               sizeof(uint16_t));

    const FaceDescriptor* desc = nullptr;
    if (frame.face == FaceId::kCalm && frame.eyes_closed) {
        desc = &kClosedCalmDescriptor;
    } else {
        const auto index = static_cast<uint8_t>(ClampFace(frame.face));
        desc = &kFaceDescriptors[index];
    }

    const FaceSpan* spans = frame.captioned ? desc->capt_spans : desc->norm_spans;
    const uint16_t count = frame.captioned ? desc->capt_count : desc->norm_count;

    for (uint16_t i = 0; i < count; ++i) {
        const FaceSpan& span = spans[i];
        if (span.y >= height) {
            continue;
        }
        if (span.x >= width) {
            continue;
        }
        const int available = width - span.x;
        const int copy_len = span.len < available ? span.len : available;
        if (copy_len <= 0) {
            continue;
        }
        uint16_t* dest = buffer + static_cast<std::size_t>(span.y) * static_cast<std::size_t>(width) + span.x;
        const uint16_t* src = kFacePixelData + span.offset;
        std::memcpy(dest, src, static_cast<std::size_t>(copy_len) * sizeof(uint16_t));
    }
}

}  // namespace memoria
