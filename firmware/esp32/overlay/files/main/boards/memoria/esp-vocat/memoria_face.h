#ifndef MEMORIA_FACE_H
#define MEMORIA_FACE_H

#include <cstddef>
#include <cstdint>

namespace memoria {

// Eyes-only face for the 360x360 round LCD (ST77916).
//
// The geometry is derived from the product reference photo and normalised to the
// screen radius R: eye half width 0.26 R, eye centres at +-0.35 R, and the dome
// centre on the horizontal centre line.  A closed eye is a dome (half disc)
// trimmed by a shallow cut arc, which reproduces the reference crescent; pushing
// that cut arc down opens the eye into a full disc.
//
// This header and memoria_face.cc deliberately have no ESP-IDF or LVGL
// dependency, so the exact firmware renderer can be compiled and checked on the
// host by scripts/preview_memoria_face.py and tests/test_memoria_face.py.

struct FaceEye {
    float center_x;     // screen-radius units, 0 = screen centre
    float center_y;     // dome centre (closed) / disc centre (open)
    float half_width;   // screen-radius units
    float openness;     // 0 = closed crescent, 1 = open disc
    float rotation_deg; // frame rotation, positive rotates the shape clockwise
    float scale;        // relative size
    float squint;       // extra closed-eye trim, in half-width units
};

struct Face {
    FaceEye left;
    FaceEye right;
};

// Canonical emotion names, in display order.
extern const char* const kFaceEmotions[];
extern const std::size_t kFaceEmotionCount;

// Map a server emotion name onto a face.  Unknown names fall back to neutral,
// which is the closed-eye reference face.  `blink` (0..1) closes open eyes and
// is ignored by closed-eye faces.
Face FaceForEmotion(const char* emotion, float blink = 0.0f);

// True when the emotion is drawn with open eyes, i.e. when it can blink.
bool FaceBlinks(const char* emotion);

// Render one frame of white eyes on a black screen into an RGB565 buffer.
void RenderFaceRgb565(uint16_t* buffer, int width, int height, const Face& face);

}  // namespace memoria

#endif  // MEMORIA_FACE_H
