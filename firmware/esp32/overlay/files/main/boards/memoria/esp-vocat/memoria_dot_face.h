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
//
// The faces are baked art (memoria_dot_face_data.inc; its generator is not in
// this repository). What moves on top of them (share/硬件/屏幕表情方案-v0.2.md
// items 4 and 6.6) is drawn from that same art: a blink closes each eye to the
// approved closed-calm lid line, and the speaking face's mouth swaps between
// its own open mouth, the listening face's small mouth and the calm face's
// flat one. Those changes repaint only the eye and mouth rectangles.
//
// Inputs are device facts only (compliance opinion 2026-10-22, item D): the
// device state, whether the device's own voice detector heard the child stop,
// the crisis-playback flag, the reply's expression token, and how loud the
// speaker is playing. Nothing here reads what was said.

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
    // Listening, and the device's voice detector has heard the child stop
    // (its own vad.end): the reply is being worked out.
    bool voice_ended = false;
    // Listening, but the server closed the capture window without a turn: the
    // microphone waits for a tap, so the face rests instead of listening.
    bool listening_closed = false;
    bool intro = false;
    bool setup = false;
    char expression[kExpressionNameMax] = {};
    // Counts expressions received, so a new one is seen even when its name
    // repeats the last.
    uint32_t expression_seq = 0;
};

// Half-open: x0 <= x < x1, y0 <= y < y1.
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

// A blink, top down: kHalf hides the eye above the lid line and draws the
// line, kShut is the line alone (on calm, exactly the approved closed-calm art).
enum class EyeShape : uint8_t { kOpen, kHalf, kShut };

// The speaking face's mouth: kOpen is the face as drawn, kHalf the listening
// face's small round mouth, kShut the calm face's flat one. Other faces keep
// the mouth they are drawn with.
enum class MouthShape : uint8_t { kOpen, kHalf, kShut };

// Every input one painted frame depends on. The display compares frames to
// skip a repaint that would change nothing, so a new paint input must join
// this struct. Eye and mouth shapes apply to the full-size layout only; the
// captioned layout is drawn as is.
struct FaceFrame {
    FaceId face = FaceId::kCalm;
    bool captioned = false;
    EyeShape eyes = EyeShape::kOpen;
    MouthShape mouth = MouthShape::kOpen;
};

inline bool operator==(const FaceFrame& a, const FaceFrame& b) {
    return a.face == b.face && a.captioned == b.captioned && a.eyes == b.eyes && a.mouth == b.mouth;
}

inline bool operator!=(const FaceFrame& a, const FaceFrame& b) { return !(a == b); }

// Black panel, glowing multi-color dot matrix. `captioned` centers the face
// between the rim and the status band. The buffer is width * height RGB565, row-major.
void PaintDotFace(uint16_t* buffer, int width, int height, const FaceFrame& frame);

// The same picture, written only inside `clip`; pixels outside it are left as
// they are. Repainting FaceDirtyRects(shown, next) this way over a buffer that
// holds `shown` leaves exactly the full paint of `next`.
void PaintDotFace(uint16_t* buffer, int width, int height, const FaceFrame& frame, const FaceRect& clip);

// What a blink or a mouth change repaints on the full-size layout: each eye's
// rectangle (the eye and the lid line wherever this face puts it) and the
// speaking face's mouth. They stay below the AI badge (y >= 48).
constexpr int kFaceEyeCount = 2;
FaceRect FaceEyeRect(FaceId face, int eye);
// Empty (x0 == x1) for a face whose mouth does not move.
FaceRect FaceMouthRect(FaceId face);

// The rectangles to repaint to turn a panel showing `shown` into `next`, at
// most kFaceDirtyMax, written to `out`; returns how many. A new face or a
// caption change is the whole panel; a blink is the two eye rectangles; a
// mouth change is the mouth rectangle. 0 when the panels would be equal.
constexpr int kFaceDirtyMax = 3;
int FaceDirtyRects(const FaceFrame& shown, const FaceFrame& next, FaceRect out[kFaceDirtyMax]);

// How loud the speaker is, as the scene's mouth detector reads it from the
// playback level (MascotScene::SetOutputLevel): `open` on a syllable, `wide`
// when that syllable is as loud as the phrase around it.
struct SpeechMouth {
    bool open = false;
    bool wide = false;
};

// Timing of what moves on the face, ms.
// One blink: half, shut, half, open again; about 160 ms in all (spec 6.6:
// about 150 ms). Blinks are kBlinkGapMinMs..kBlinkGapMaxMs apart, at random,
// never doubled: spec 6.6 keeps two blinks at least 2 s apart.
constexpr uint32_t kBlinkHalfMs = 40;
constexpr uint32_t kBlinkShutMs = 80;
constexpr uint32_t kBlinkMs = 2 * kBlinkHalfMs + kBlinkShutMs;
constexpr uint32_t kBlinkGapMinMs = 2400;
constexpr uint32_t kBlinkGapMaxMs = 5600;
// A reply's expression reaches the panel shortly after its audio starts. For
// up to this long after speaking begins the face that was showing stays, so
// the panel does not show the plain speaking face and then jump to the
// expression.
constexpr uint32_t kSpeakEntryWaitMs = 200;
// After speaking ends the reply's face stays this long, mouth shut.
constexpr uint32_t kReplyHoldMs = 800;
// The mouth does not switch between half and open faster than this; shutting
// is never held back.
constexpr uint32_t kMouthShapeMinMs = 80;
// How often a frame is due while something moves.
constexpr uint32_t kFaceAnimationFrameMs = 40;

// Turns the facts of each frame into the frame to paint: the face
// FaceForFacts picks, held through the speaking entry wait and the reply hold,
// with the blink and the speaking mouth on it. Owned by the display task.
class FaceAnimator {
public:
    explicit FaceAnimator(uint32_t seed = 0x9E3779B9u) : rng_(seed != 0 ? seed : 0x9E3779B9u) {}

    FaceFrame Next(const FaceFacts& facts, bool captioned, SpeechMouth mouth, uint32_t now_ms);

    // True while a blink, the speaking entry wait or the reply hold is under
    // way: the next frame is due within kFaceAnimationFrameMs.
    bool animating() const { return blink_on_ || entry_wait_ || hold_on_; }

private:
    uint32_t Random();
    EyeShape Blink(bool allowed, uint32_t now_ms);
    MouthShape Mouth(SpeechMouth mouth, uint32_t now_ms);

    uint32_t rng_;
    bool started_ = false;
    FaceId last_face_ = FaceId::kCalm;
    bool was_speaking_ = false;
    // Speaking entry wait: the face held, and the expression count it waits to see change.
    bool entry_wait_ = false;
    uint32_t entry_since_ms_ = 0;
    uint32_t entry_seq_ = 0;
    FaceId entry_face_ = FaceId::kCalm;
    // Reply hold.
    bool hold_on_ = false;
    uint32_t hold_until_ms_ = 0;
    FaceId hold_face_ = FaceId::kCalm;
    // Blink schedule.
    bool blink_on_ = false;
    uint32_t blink_since_ms_ = 0;
    uint32_t next_blink_ms_ = 0;
    // The crisis round's slow blink is timed from the round's start.
    bool crisis_on_ = false;
    uint32_t crisis_since_ms_ = 0;
    // Speaking mouth.
    MouthShape mouth_ = MouthShape::kShut;
    uint32_t mouth_since_ms_ = 0;
};

}  // namespace memoria

#endif  // MEMORIA_DOT_FACE_H
