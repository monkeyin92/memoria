#ifndef MEMORIA_DOT_FACE_H
#define MEMORIA_DOT_FACE_H

#include <cstddef>
#include <cstdint>

namespace memoria {

// Yellow dot-matrix face for the 360x360 round panel.
//
// Seven faces, and only these seven. Scared and angry are not in the set.
// While the device speaks, the face follows the reply's expression, drawn
// with one of these seven. Every other state follows the device state.
// A crisis script (protected playback) is the calm face in every state, and
// the crisis round breathes with a slow blink (product decision 2026-10-23).

enum class FaceId : uint8_t {
    kCalm = 0,
    kListen,
    kThink,
    kSpeak,
    kConnect,
    kGreet,
    kPair,
};

constexpr int kFaceCount = 7;
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
// when the server sent none. While speaking it picks among the seven faces.
// It never adds one, and a crisis round ignores it.
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

// The face a reply expression draws while the device is speaking. No name at
// all keeps the speaking face, and a name the seven faces have no reading for
// is the calm face.
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

// Black panel, yellow squares, no frame. `captioned` centers the glyph between
// the rim and the status band. The buffer is width * height RGB565, row-major.
void PaintDotFace(uint16_t* buffer, int width, int height, const FaceFrame& frame);

}  // namespace memoria

#endif  // MEMORIA_DOT_FACE_H
