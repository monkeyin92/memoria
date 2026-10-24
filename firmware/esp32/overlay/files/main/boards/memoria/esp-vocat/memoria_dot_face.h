#ifndef MEMORIA_DOT_FACE_H
#define MEMORIA_DOT_FACE_H

#include <cstddef>
#include <cstdint>

namespace memoria {

// Expressive dot-matrix face for the 360x360 round panel.
//
// 10 expressions matching the approved design sheet:
// calm (平静), happy (开心), listen (倾听), think (思考), speak (说话),
// surprised (惊讶), shy (害羞), sleepy (犯困), comfort (关心), wake (打招呼).
// While the device speaks, the face follows the reply's expression, drawn
// with one of these faces. Every other state follows the device state.
// A crisis script (protected playback) is the calm face in every state, and
// the crisis round breathes with a slow blink (product decision 2026-10-23).

enum class FaceId : uint8_t {
    kCalm = 0,
    kHappy = 1,
    kListen = 2,
    kThink = 3,
    kSpeak = 4,
    kSurprised = 5,
    kShy = 6,
    kSleepy = 7,
    kComfort = 8,
    kWake = 9,
    // Aliases for compatibility
    kConnect = kWake,
    kGreet = kWake,
    kPair = kComfort,
};

constexpr int kFaceCount = 10;
constexpr int kFaceScreen = 360;
constexpr uint16_t kFaceYellow = 0xFFE0;

enum FaceDeviceState : int {
    kFaceStateUnknown = 0,
    kFaceStateStarting = 1,
    kFaceStateWifiConfiguring = 2,
    kFaceStateIdle = 3,
    kFaceStateConnecting = 4,
    kFaceStateRecovering = 5,
    kFaceStateListening = 6,
    kFaceStateSpeaking = 7,
    kFaceStateUpgrading = 8,
    kFaceStateActivating = 9,
    kFaceStateAudioTesting = 10,
    kFaceStateFatalError = 11,
};

// The reply's expression as the server named it (screen.expression), or empty
// when the server sent none. While speaking it picks among the faces.
// A crisis round ignores it.
constexpr int kExpressionNameMax = 16;

struct FaceFacts {
    int state = kFaceStateIdle;
    bool crisis = false;
    bool user_spoke = false;
    bool intro = false;
    bool setup = false;
    char expression[kExpressionNameMax] = {};
};

struct FaceRect {
    int x0 = 0;
    int y0 = 0;
    int x1 = 0;
    int y1 = 0;
};

// One blink of the crisis round: the eyes close for kCrisisBlinkClosedMs at
// the tail of every kCrisisBlinkPeriodMs window after the round began.
constexpr uint32_t kCrisisBlinkPeriodMs = 3200;
constexpr uint32_t kCrisisBlinkClosedMs = 320;

// True while the crisis-round blink holds the eyes shut.
bool CrisisEyesClosed(uint32_t elapsed_ms);

const char* FaceName(FaceId id);

// The expression name the facts carry, or empty when the server sent none or
// sent one that is not a short token. Owning this read here keeps the panel
// from parsing the server string itself.
const char* ReceivedExpression(const FaceFacts& facts);

// The face a reply expression draws while the device is speaking. A name the
// faces have a use for picks that face; no name at all, or one they have
// no use for, keeps the speaking face.
FaceId FaceForExpression(const char* expression);

FaceId FaceForFacts(const FaceFacts& facts);
FaceRect FaceBounds(bool captioned);

// Every input one painted frame depends on: the face id, the caption lift,
// and whether the slow crisis blink has the eyes shut. The display compares
// frames to skip a repaint that would change nothing, so a new paint input
// must join this struct.
struct FaceFrame {
    FaceId face = FaceId::kCalm;
    bool captioned = false;
    bool eyes_closed = false;
};

inline bool operator==(const FaceFrame& a, const FaceFrame& b) {
    return a.face == b.face && a.captioned == b.captioned && a.eyes_closed == b.eyes_closed;
}

inline bool operator!=(const FaceFrame& a, const FaceFrame& b) { return !(a == b); }

// Black panel, glowing multi-color dot matrix. `captioned` centers the face
// between the rim and the status band. The buffer is width * height RGB565, row-major.
void PaintDotFace(uint16_t* buffer, int width, int height, const FaceFrame& frame);

}  // namespace memoria

#endif  // MEMORIA_DOT_FACE_H
