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
            if (facts.listening_closed) {
                return FaceId::kCalm;
            }
            return facts.voice_ended ? FaceId::kThink : FaceId::kListen;
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

namespace {

// Measured from the baked art (full-size layout), since its generator is not in
// this repository; the host tests check them against the art. Per eye: the
// rectangle a blink repaints (the eye and its moved lid line), the one or two
// boxes whose whole spans are the eye (a cheek reaching into the rectangle's
// lower corner stays outside them), where the closed-calm lid line moves for
// this face, and the row above which a half-shut eye is hidden.
struct EyeRegion {
    FaceRect area;
    FaceRect upper;
    FaceRect lower;
    int dx;
    int dy;
    int lid_bottom;
};

struct FaceRegions {
    EyeRegion eyes[kFaceEyeCount];
};

const FaceRegions kRegions[kFaceCount] = {
    {{{{58, 144, 153, 222}, {60, 144, 148, 222}, {}, 0, 0, 175},
      {{207, 144, 302, 222}, {212, 144, 300, 222}, {}, 0, 0, 175}}},  // calm
    {{{{58, 119, 155, 211}, {58, 119, 153, 208}, {90, 208, 122, 211}, 2, -20, 155},
      {{205, 119, 302, 211}, {207, 119, 302, 208}, {238, 208, 270, 211}, -2, -20, 155}}},  // happy
    {{{{60, 134, 157, 230}, {60, 134, 155, 229}, {100, 229, 112, 230}, 4, -4, 171},
      {{203, 134, 300, 230}, {204, 134, 300, 229}, {247, 229, 259, 230}, -4, -4, 171}}},  // listen
    {{{{63, 135, 158, 218}, {63, 135, 155, 218}, {}, 5, -7, 168},
      {{203, 135, 298, 218}, {206, 135, 298, 218}, {}, -4, -7, 168}}},  // think
    {{{{56, 125, 151, 197}, {58, 125, 145, 197}, {}, -2, -21, 154},
      {{209, 125, 304, 197}, {215, 125, 302, 197}, {}, 2, -21, 154}}},  // speak
    {{{{63, 131, 159, 222}, {63, 131, 156, 220}, {100, 220, 121, 222}, 6, -9, 166},
      {{199, 131, 294, 222}, {203, 131, 294, 221}, {239, 221, 256, 222}, -8, -9, 166}}},  // surprised
    {{{{69, 153, 164, 219}, {75, 153, 155, 217}, {104, 217, 126, 219}, 11, 5, 180},
      {{197, 154, 292, 219}, {206, 154, 285, 217}, {234, 217, 255, 219}, -10, 6, 181}}},  // shy
    {{{{58, 142, 155, 214}, {58, 142, 153, 214}, {}, 2, -4, 171},
      {{205, 142, 302, 215}, {207, 142, 302, 215}, {}, -2, -4, 171}}},  // sleepy
    {{{{58, 145, 153, 223}, {58, 145, 149, 223}, {}, 0, 1, 176},
      {{208, 145, 303, 223}, {211, 145, 303, 223}, {}, 1, 1, 176}}},  // comfort
    {{{{53, 131, 152, 227}, {53, 131, 152, 225}, {90, 225, 110, 227}, -2, -7, 168},
      {{208, 131, 306, 227}, {208, 131, 306, 225}, {250, 225, 264, 227}, 1, -7, 168}}},  // wake
};

// The speaking face's mouth, and the two mouths it borrows, centred on it.
constexpr FaceRect kSpeakMouth = {150, 208, 210, 270};
constexpr FaceRect kCalmMouth = {153, 235, 207, 245};
constexpr int kCalmMouthDy = -1;
constexpr FaceRect kListenMouth = {158, 238, 202, 264};
constexpr int kListenMouthDy = -12;

bool Empty(const FaceRect& box) { return box.x0 >= box.x1 || box.y0 >= box.y1; }

bool Holds(const FaceRect& box, const FaceSpan& span) {
    return span.y >= box.y0 && span.y < box.y1 && span.x >= box.x0 && span.x + span.len <= box.x1;
}

void CopySpan(uint16_t* buffer, int width, const FaceRect& clip, int y, int x, int len, const uint16_t* src) {
    if (y < clip.y0 || y >= clip.y1) {
        return;
    }
    const int x0 = x > clip.x0 ? x : clip.x0;
    const int x1 = x + len < clip.x1 ? x + len : clip.x1;
    if (x1 <= x0) {
        return;
    }
    std::memcpy(buffer + static_cast<std::size_t>(y) * static_cast<std::size_t>(width) + x0, src + (x0 - x),
                static_cast<std::size_t>(x1 - x0) * sizeof(uint16_t));
}

// The spans of `source` that lie wholly in `from`, moved by (dx, dy).
void CopyMoved(uint16_t* buffer, int width, const FaceRect& clip, const FaceDescriptor& source,
               const FaceRect& from, int dx, int dy) {
    for (uint16_t i = 0; i < source.norm_count; ++i) {
        const FaceSpan& span = source.norm_spans[i];
        if (Holds(from, span)) {
            CopySpan(buffer, width, clip, span.y + dy, span.x + dx, span.len, kFacePixelData + span.offset);
        }
    }
}

}  // namespace

void PaintDotFace(uint16_t* buffer, int width, int height, const FaceFrame& frame) {
    PaintDotFace(buffer, width, height, frame, FaceRect{0, 0, width, height});
}

void PaintDotFace(uint16_t* buffer, int width, int height, const FaceFrame& frame, const FaceRect& area) {
    if (buffer == nullptr || width <= 0 || height <= 0) {
        return;
    }
    FaceRect clip = area;
    clip.x0 = clip.x0 > 0 ? clip.x0 : 0;
    clip.y0 = clip.y0 > 0 ? clip.y0 : 0;
    clip.x1 = clip.x1 < width ? clip.x1 : width;
    clip.y1 = clip.y1 < height ? clip.y1 : height;
    if (Empty(clip)) {
        return;
    }
    for (int y = clip.y0; y < clip.y1; ++y) {
        std::memset(buffer + static_cast<std::size_t>(y) * static_cast<std::size_t>(width) + clip.x0, 0,
                    static_cast<std::size_t>(clip.x1 - clip.x0) * sizeof(uint16_t));
    }

    const FaceId face = ClampFace(frame.face);
    const auto index = static_cast<uint8_t>(face);
    const FaceDescriptor& desc = kFaceDescriptors[index];
    const EyeShape eyes = frame.captioned ? EyeShape::kOpen : frame.eyes;
    const MouthShape mouth = frame.captioned || face != FaceId::kSpeak ? MouthShape::kOpen : frame.mouth;
    const FaceRegions& regions = kRegions[index];

    const FaceSpan* spans = frame.captioned ? desc.capt_spans : desc.norm_spans;
    const uint16_t count = frame.captioned ? desc.capt_count : desc.norm_count;
    for (uint16_t i = 0; i < count; ++i) {
        const FaceSpan& span = spans[i];
        bool hidden = false;
        if (eyes != EyeShape::kOpen) {
            for (const EyeRegion& eye : regions.eyes) {
                if (Holds(eye.upper, span) || (!Empty(eye.lower) && Holds(eye.lower, span))) {
                    hidden = eyes == EyeShape::kShut || span.y < eye.lid_bottom;
                }
            }
        }
        if (mouth != MouthShape::kOpen && Holds(kSpeakMouth, span)) {
            hidden = true;
        }
        if (!hidden) {
            CopySpan(buffer, width, clip, span.y, span.x, span.len, kFacePixelData + span.offset);
        }
    }
    if (eyes != EyeShape::kOpen) {
        for (int e = 0; e < kFaceEyeCount; ++e) {
            const EyeRegion& eye = regions.eyes[e];
            CopyMoved(buffer, width, clip, kClosedCalmDescriptor, kRegions[0].eyes[e].area, eye.dx, eye.dy);
        }
    }
    if (mouth == MouthShape::kHalf) {
        CopyMoved(buffer, width, clip, kFaceDescriptors[static_cast<int>(FaceId::kListen)], kListenMouth, 0,
                  kListenMouthDy);
    } else if (mouth == MouthShape::kShut) {
        CopyMoved(buffer, width, clip, kFaceDescriptors[static_cast<int>(FaceId::kCalm)], kCalmMouth, 0,
                  kCalmMouthDy);
    }
}

FaceRect FaceEyeRect(FaceId face, int eye) {
    const int side = eye <= 0 ? 0 : kFaceEyeCount - 1;
    return kRegions[static_cast<uint8_t>(ClampFace(face))].eyes[side].area;
}

FaceRect FaceMouthRect(FaceId face) { return ClampFace(face) == FaceId::kSpeak ? kSpeakMouth : FaceRect{}; }

int FaceDirtyRects(const FaceFrame& shown, const FaceFrame& next, FaceRect out[kFaceDirtyMax]) {
    if (shown.face != next.face || shown.captioned != next.captioned) {
        out[0] = FaceRect{0, 0, kFaceScreen, kFaceScreen};
        return 1;
    }
    if (next.captioned) {
        return 0;
    }
    int count = 0;
    if (shown.eyes != next.eyes) {
        out[count++] = FaceEyeRect(next.face, 0);
        out[count++] = FaceEyeRect(next.face, 1);
    }
    if (shown.mouth != next.mouth && !Empty(FaceMouthRect(next.face))) {
        out[count++] = FaceMouthRect(next.face);
    }
    return count;
}

uint32_t FaceAnimator::Random() {
    rng_ ^= rng_ << 13;
    rng_ ^= rng_ >> 17;
    rng_ ^= rng_ << 5;
    return rng_;
}

EyeShape FaceAnimator::Blink(bool allowed, uint32_t now_ms) {
    if (!allowed) {
        blink_on_ = false;
        if (static_cast<int32_t>(next_blink_ms_ - (now_ms + kBlinkGapMinMs)) < 0) {
            next_blink_ms_ = now_ms + kBlinkGapMinMs;
        }
        return EyeShape::kOpen;
    }
    if (!blink_on_ && static_cast<int32_t>(now_ms - next_blink_ms_) >= 0) {
        blink_on_ = true;
        blink_since_ms_ = now_ms;
    }
    if (!blink_on_) {
        return EyeShape::kOpen;
    }
    const uint32_t t = now_ms - blink_since_ms_;
    if (t < kBlinkHalfMs) {
        return EyeShape::kHalf;
    }
    if (t < kBlinkHalfMs + kBlinkShutMs) {
        return EyeShape::kShut;
    }
    if (t < kBlinkMs) {
        return EyeShape::kHalf;
    }
    blink_on_ = false;
    next_blink_ms_ = now_ms + kBlinkGapMinMs + Random() % (kBlinkGapMaxMs - kBlinkGapMinMs + 1);
    return EyeShape::kOpen;
}

MouthShape FaceAnimator::Mouth(SpeechMouth speech, uint32_t now_ms) {
    const MouthShape want = !speech.open ? MouthShape::kShut : (speech.wide ? MouthShape::kOpen : MouthShape::kHalf);
    if (want != mouth_) {
        const bool reshape = want != MouthShape::kShut && mouth_ != MouthShape::kShut;
        if (!reshape || now_ms - mouth_since_ms_ >= kMouthShapeMinMs) {
            mouth_ = want;
            mouth_since_ms_ = now_ms;
        }
    }
    return mouth_;
}

FaceFrame FaceAnimator::Next(const FaceFacts& facts, bool captioned, SpeechMouth speech, uint32_t now_ms) {
    FaceId face = FaceForFacts(facts);
    const bool speaking = facts.state == kFaceStateSpeaking;
    if (!started_) {
        started_ = true;
        last_face_ = face;
        next_blink_ms_ = now_ms + kBlinkGapMinMs + Random() % (kBlinkGapMaxMs - kBlinkGapMinMs + 1);
    }
    if (speaking && !was_speaking_) {
        entry_wait_ = !facts.crisis;
        entry_since_ms_ = now_ms;
        entry_seq_ = facts.expression_seq;
        entry_face_ = last_face_;
        hold_on_ = false;
    } else if (!speaking && was_speaking_) {
        hold_on_ = true;
        hold_until_ms_ = now_ms + kReplyHoldMs;
        hold_face_ = last_face_;
    }
    was_speaking_ = speaking;
    if (entry_wait_) {
        if (!speaking || facts.crisis || facts.expression_seq != entry_seq_ ||
            now_ms - entry_since_ms_ >= kSpeakEntryWaitMs) {
            entry_wait_ = false;
        } else {
            face = entry_face_;
        }
    }
    if (hold_on_) {
        if (speaking || facts.intro || facts.setup || static_cast<int32_t>(now_ms - hold_until_ms_) >= 0) {
            hold_on_ = false;
        } else {
            face = hold_face_;
        }
    }

    FaceFrame frame;
    frame.face = face;
    frame.captioned = captioned;
    if (facts.crisis) {
        if (!crisis_on_) {
            crisis_on_ = true;
            crisis_since_ms_ = now_ms;
        }
        frame.eyes = CrisisEyesClosed(now_ms - crisis_since_ms_) ? EyeShape::kShut : EyeShape::kOpen;
        Blink(false, now_ms);
    } else {
        crisis_on_ = false;
        const bool conversing = facts.state == kFaceStateIdle || facts.state == kFaceStateListening || speaking;
        frame.eyes = Blink(conversing && !captioned && !facts.intro && !facts.setup, now_ms);
    }
    if (face == FaceId::kSpeak && !captioned && speaking && !entry_wait_) {
        frame.mouth = Mouth(speech, now_ms);
    } else {
        mouth_ = MouthShape::kShut;
        frame.mouth = face == FaceId::kSpeak && !captioned ? MouthShape::kShut : MouthShape::kOpen;
    }
    last_face_ = face;
    return frame;
}

}  // namespace memoria
