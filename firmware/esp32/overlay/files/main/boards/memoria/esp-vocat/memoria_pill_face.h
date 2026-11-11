#ifndef MEMORIA_PILL_FACE_H
#define MEMORIA_PILL_FACE_H

#include "memoria_dot_face.h"

#include <cstdint>

namespace memoria {

// The robot has one face: two soft eyes, a small mouth and, when it feels
// something, blush. Different robot bodies will later bring their own face
// styles; this file is the default one. It is drawn from numbers, so the host
// tests render exactly what the panel shows.

constexpr uint32_t kPillBackgroundRgb = 0xF28AA8;
constexpr uint32_t kPillEyeRgb = 0x17151A;
constexpr uint32_t kPillBlushRgb = 0xE5587F;
constexpr uint32_t kPillHighlightRgb = 0xFFFFFF;

struct PillEye {
    int16_t cx = 0;
    int16_t cy = 0;
    uint16_t width = 0;
    uint16_t height = 0;

    bool operator==(const PillEye& other) const {
        return cx == other.cx && cy == other.cy && width == other.width && height == other.height;
    }
};

// kCapsule is the round-ended bar; kArcUp is a happy "^"; kArcDown a content or
// sleepy "u". Arcs ignore the eyelid: they are already closed.
enum class PillEyeStyle : uint8_t {
    kCapsule = 0,
    kArcUp,
    kArcDown,
};

// One complete visual frame. The display task owns the clock and the state
// mapping; this type contains only render data.
struct PillFaceFrame {
    uint32_t background_rgb = kPillBackgroundRgb;
    uint32_t eye_rgb = kPillEyeRgb;
    PillEye left;
    PillEye right;
    PillEyeStyle style = PillEyeStyle::kCapsule;
    // 0 = open, 255 = shut. A blink drives it directly (the animator already
    // steps it every 40 ms), so it is never eased.
    uint8_t lid = 0;
    // Strength of the cheek blush and of the eye highlight, 0..255.
    uint8_t blush = 0;
    uint8_t highlight = 255;
    // Mouth: a smile of `mouth_width` px, opened by `mouth_open` (0 = a closed
    // smile, 255 = widest). `mouth_width` 0 hides it.
    int16_t mouth_cx = 180;
    int16_t mouth_cy = 0;
    uint16_t mouth_width = 0;
    uint8_t mouth_open = 0;

    bool operator==(const PillFaceFrame& other) const {
        return background_rgb == other.background_rgb && eye_rgb == other.eye_rgb &&
               left == other.left && right == other.right && style == other.style &&
               lid == other.lid && blush == other.blush && highlight == other.highlight &&
               mouth_cx == other.mouth_cx && mouth_cy == other.mouth_cy &&
               mouth_width == other.mouth_width && mouth_open == other.mouth_open;
    }
    bool operator!=(const PillFaceFrame& other) const { return !(*this == other); }
};

// The look a state asks for. `lid` is the blink; `lift` raises the face to make
// room for a caption band; `speaking` says the device is talking (a reply that
// names a "thinking" or "listening" look still talks, so it keeps the speaking
// look); `mouth` is the voice level, 0 (silent) to 255, only used while speaking.
PillFaceFrame MakePillFaceFrame(FaceId face, uint8_t lid, uint8_t lift, bool speaking, uint8_t mouth);

// Eases from one look to the next so the eyes never jump. Blinks (`lid`) pass
// straight through; everything else glides for kPillGlideMs, the mouth faster.
constexpr uint32_t kPillGlideMs = 180;
constexpr uint32_t kPillMouthGlideMs = 80;

class PillFaceTween {
public:
    // The look to show at `now_ms` when the state wants `target`.
    const PillFaceFrame& Step(const PillFaceFrame& target, uint32_t now_ms);
    // True while a glide is under way: the next frame is due soon.
    bool animating() const { return animating_; }
    // Forget the current look; the next Step opens the eyes from a closed line,
    // which is how the robot wakes up.
    void Reset() { started_ = false; }

private:
    PillFaceFrame from_;
    PillFaceFrame to_;
    PillFaceFrame current_;
    uint32_t start_ms_ = 0;
    uint32_t duration_ms_ = 0;
    bool started_ = false;
    bool animating_ = false;
};

void PaintPillFace(uint16_t* buffer, int width, int height, const PillFaceFrame& frame);

}  // namespace memoria

#endif  // MEMORIA_PILL_FACE_H
