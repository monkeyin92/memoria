#ifndef MEMORIA_FACE_H
#define MEMORIA_FACE_H

#include <cstddef>
#include <cstdint>

namespace memoria {

// Conversation face for the 360x360 round LCD (ST77916).
//
// Layout is normalised to the screen radius R: eyes at x=+-0.30 R, y=-0.20 R,
// half-width 0.205 R; a rice-grain nose at y=0.07 R; the mouth at y=0.27 R.
// Closed eyes keep the product crescent (dome minus a shallow cut). Open eyes
// are almond sclera with a punched-out pupil, not solid white discs. The mouth
// is the signature of this face, not a pair of Vector-style eyes.
//
// This header and memoria_face.cc have no ESP-IDF or LVGL dependency, so the
// exact firmware renderer can be compiled and checked on the host by
// scripts/preview_memoria_face.py and tests/test_memoria_face.py.

struct FaceEye {
    float center_x;     // screen-radius units, 0 = screen centre
    float center_y;
    float half_width;   // screen-radius units
    float openness;     // 0 = closed crescent, 1 = rest open amount
    float rotation_deg; // positive raises the outer corner of a crescent
    float scale;
    float squint;       // extra closed-eye trim, in half-width units
    float gaze_x;       // almond pupil offset, screen-radius units
    float gaze_y;
    float roundness;    // almond vertical radius as a fraction of half-width
    float pupil;        // pupil radius as a fraction of half-width
    bool almond;        // true: open eye is almond+pupil; blink closes to crescent
};

struct Face {
    FaceEye left;
    FaceEye right;
    uint8_t kind;  // index into kFaceEmotions / canonical expression table
};

// Canonical emotion names, in display order.
extern const char* const kFaceEmotions[];
extern const std::size_t kFaceEmotionCount;

// Map a server emotion name onto a face. Unknown names fall back to neutral.
// blink (0..1) closes almond eyes and is ignored by closed-eye faces.
Face FaceForEmotion(const char* emotion, float blink = 0.0f);

// True when both eyes are almond, i.e. when the face can blink.
bool FaceBlinks(const char* emotion);

// Render one frame of the white-on-black conversation face into RGB565.
void RenderFaceRgb565(uint16_t* buffer, int width, int height, const Face& face);

}  // namespace memoria

#endif  // MEMORIA_FACE_H
