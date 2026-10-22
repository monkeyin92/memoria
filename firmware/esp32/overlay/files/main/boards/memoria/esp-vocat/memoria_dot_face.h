#ifndef MEMORIA_DOT_FACE_H
#define MEMORIA_DOT_FACE_H

#include <cstddef>
#include <cstdint>

namespace memoria {

// Yellow dot-matrix face for the 360x360 round panel.
//
// Seven faces, and only these seven. Scared and angry are not in the set.
// The face follows device state. It does not read a server emotion name.
// A crisis script (protected playback) is the calm face in every state.
//
// Integer values match the patched DeviceState enum. The display static_asserts
// them against device_state.h. This header stays free of ESP-IDF and LVGL.

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
constexpr int kFaceBadgeClearY = 40;

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

struct FaceFacts {
    int state = kFaceStateIdle;
    bool crisis = false;
    bool user_spoke = false;
    bool intro = false;
    bool setup = false;
};

struct FaceRect {
    int x0 = 0;
    int y0 = 0;
    int x1 = 0;
    int y1 = 0;
};

const char* FaceName(FaceId id);
FaceId FaceForFacts(const FaceFacts& facts);
FaceRect FaceBounds(bool captioned);

// Black panel, yellow squares, no frame. `captioned` lifts the face above the
// status band. The buffer is width * height RGB565, row-major.
void PaintDotFace(uint16_t* buffer, int width, int height, FaceId id, bool captioned);

}  // namespace memoria

#endif  // MEMORIA_DOT_FACE_H
