#ifndef MEMORIA_PILL_FACE_H
#define MEMORIA_PILL_FACE_H

#include "memoria_dot_face.h"

#include <cstdint>

namespace memoria {

constexpr uint32_t kPillBackgroundRgb = 0xF28AA8;
constexpr uint32_t kPillEyeRgb = 0x17151A;

struct PillEye {
    int16_t cx = 0;
    int16_t cy = 0;
    uint16_t width = 0;
    uint16_t height = 0;
};

enum class PillExpression : uint8_t {
    kNeutral = 0,
    kNarrow,
    kWide,
    kSquint,
};

// One complete visual frame for the experimental state-driven face. The display
// task owns the clock and state mapping; this type contains only render data.
struct PillFaceFrame {
    uint32_t background_rgb = kPillBackgroundRgb;
    uint32_t eye_rgb = kPillEyeRgb;
    PillEye left;
    PillEye right;
    bool eyes_closed = false;
};

// Non-speaking faces keep one fixed expression. Speaking alone advances
// between fixed expression frames at coarse time boundaries; it never moves
// or scales the eyes continuously.
PillExpression PillExpressionFor(FaceId face, uint32_t now_ms);

PillFaceFrame MakePillFaceFrame(FaceId face, bool eyes_closed, uint8_t lift, uint32_t now_ms);

void PaintPillFace(uint16_t* buffer, int width, int height, const PillFaceFrame& frame);

}  // namespace memoria

#endif  // MEMORIA_PILL_FACE_H
