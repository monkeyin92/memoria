#ifndef MEMORIA_FACE_DISPLAY_H
#define MEMORIA_FACE_DISPLAY_H

#include "display/lcd_display.h"
#include "lvgl_image.h"
#include "memoria_face.h"

#include <memory>

// LCD display for the Memoria ESP-VoCat: the 360x360 round panel shows the
// eyes-only face from memoria_face.h (white eyes on black) instead of the small
// colour emoji glyphs, and the board is pinned to the dark theme so the white
// face and the white status text stay legible.
class MemoriaFaceDisplay : public SpiLcdDisplay {
public:
    MemoriaFaceDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                       int width, int height, int offset_x, int offset_y, bool mirror_x,
                       bool mirror_y, bool swap_xy);
    ~MemoriaFaceDisplay() override;

    void SetupUI() override;
    void SetEmotion(const char* emotion) override;
    void SetTheme(Theme* theme) override;

private:
    static constexpr float kBlinkFrames[5] = {0.0f, 0.55f, 1.0f, 0.55f, 0.0f};
    static constexpr int kBlinkFrameCount = 5;

    void ApplyFaceChrome();  // caller holds the display lock
    void RenderFace(float blink);  // caller holds the display lock
    void UpdateBlinkTimer();       // caller holds the display lock
    static void BlinkTimerCallback(void* arg);

    std::unique_ptr<LvglAllocatedImage> face_image_;
    uint16_t* face_buffer_ = nullptr;
    char emotion_[64] = "neutral";
    esp_timer_handle_t blink_timer_ = nullptr;
    int blink_frame_ = -1;  // -1 = waiting for the next blink
    int blink_cooldown_ = 150;
};

#endif  // MEMORIA_FACE_DISPLAY_H
