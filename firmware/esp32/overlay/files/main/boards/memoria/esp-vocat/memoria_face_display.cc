#include "memoria_face_display.h"

#include "display/lvgl_display/lvgl_theme.h"

#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_random.h>

#include <cstring>

#define TAG "MemoriaFaceDisplay"

namespace {
constexpr uint32_t kBlinkTickUs = 30 * 1000;  // 30 ms per blink frame
constexpr uint32_t kBlinkCooldownMinTicks = 130;
constexpr uint32_t kBlinkCooldownSpreadTicks = 100;
}  // namespace

MemoriaFaceDisplay::MemoriaFaceDisplay(esp_lcd_panel_io_handle_t panel_io,
                                       esp_lcd_panel_handle_t panel, int width, int height,
                                       int offset_x, int offset_y, bool mirror_x, bool mirror_y,
                                       bool swap_xy)
    : SpiLcdDisplay(panel_io, panel, width, height, offset_x, offset_y, mirror_x, mirror_y,
                    swap_xy) {
    const size_t bytes = static_cast<size_t>(width_) * height_ * sizeof(uint16_t);
    face_buffer_ = static_cast<uint16_t*>(
        heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (face_buffer_ == nullptr) {
        face_buffer_ = static_cast<uint16_t*>(heap_caps_malloc(bytes, MALLOC_CAP_8BIT));
    }
    if (face_buffer_ == nullptr) {
        ESP_LOGE(TAG, "face buffer allocation failed (%u bytes)", static_cast<unsigned>(bytes));
        return;
    }
    // Render the idle face before the buffer becomes visible so no garbage is
    // ever shown.
    memoria::RenderFaceRgb565(face_buffer_, width_, height_,
                              memoria::FaceForEmotion("neutral", 0.0f));
    face_image_ = std::make_unique<LvglAllocatedImage>(face_buffer_, bytes, width_, height_,
                                                       width_ * static_cast<int>(sizeof(uint16_t)),
                                                       LV_COLOR_FORMAT_RGB565);

    const esp_timer_create_args_t timer_args = {
        .callback = &MemoriaFaceDisplay::BlinkTimerCallback,
        .arg = this,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "memoria_face_blink",
        .skip_unhandled_events = true,
    };
    if (esp_timer_create(&timer_args, &blink_timer_) != ESP_OK) {
        blink_timer_ = nullptr;
        ESP_LOGW(TAG, "blink timer unavailable");
    }
}

MemoriaFaceDisplay::~MemoriaFaceDisplay() {
    if (blink_timer_ != nullptr) {
        esp_timer_stop(blink_timer_);
        esp_timer_delete(blink_timer_);
        blink_timer_ = nullptr;
    }
    // Detach the face buffer from LVGL before it is freed.
    if (Lock(1000)) {
        if (container_ != nullptr && face_image_ != nullptr) {
            lv_obj_set_style_bg_image_src(container_, nullptr, 0);
        }
        Unlock();
    }
    face_image_.reset();
    face_buffer_ = nullptr;
}

void MemoriaFaceDisplay::SetupUI() {
    SpiLcdDisplay::SetupUI();

    // The face is white on black, so pin the board to the dark theme: the light
    // theme would draw black status text onto the black screen.
    auto* dark_theme = LvglThemeManager::GetInstance().GetTheme("dark");
    if (dark_theme != nullptr && current_theme_ != dark_theme) {
        SetTheme(dark_theme);  // our override re-applies the face chrome
    } else {
        if (!Lock(1000)) {
            return;
        }
        ApplyFaceChrome();
        Unlock();
    }

    if (!Lock(1000)) {
        return;
    }
    RenderFace(0.0f);
    UpdateBlinkTimer();
    Unlock();
}

void MemoriaFaceDisplay::SetEmotion(const char* emotion) {
    if (emotion == nullptr || emotion[0] == '\0') {
        return;
    }
    if (face_image_ == nullptr) {
        // No face buffer: keep the upstream colour-emoji behaviour.
        LcdDisplay::SetEmotion(emotion);
        return;
    }
    if (!Lock(1000)) {
        return;
    }
    if (std::strcmp(emotion, emotion_) == 0) {
        Unlock();
        return;
    }
    std::strncpy(emotion_, emotion, sizeof(emotion_) - 1);
    emotion_[sizeof(emotion_) - 1] = '\0';
    // Serial receipt for the expression acceptance run: one line per change.
    ESP_LOGI(TAG, "emotion=%s open_eyes=%d", emotion_, memoria::FaceBlinks(emotion_) ? 1 : 0);
    blink_frame_ = -1;
    blink_cooldown_ = kBlinkCooldownMinTicks +
                      static_cast<int>(esp_random() % kBlinkCooldownSpreadTicks);
    RenderFace(0.0f);
    UpdateBlinkTimer();
    Unlock();
}

void MemoriaFaceDisplay::SetTheme(Theme* theme) {
    LcdDisplay::SetTheme(theme);
    if (!Lock(1000)) {
        return;
    }
    ApplyFaceChrome();
    Unlock();
}

void MemoriaFaceDisplay::ApplyFaceChrome() {
    if (container_ == nullptr || face_image_ == nullptr) {
        return;
    }
    // The face is the screen background; everything else stays transparent on
    // top of it so subtitles and status text remain readable.
    lv_obj_set_style_bg_color(container_, lv_color_black(), 0);
    lv_obj_set_style_bg_image_src(container_, face_image_->image_dsc(), 0);
    lv_obj_set_style_bg_image_opa(container_, LV_OPA_COVER, 0);
    if (content_ != nullptr) {
        lv_obj_set_style_bg_opa(content_, LV_OPA_TRANSP, 0);
    }
    if (top_bar_ != nullptr) {
        lv_obj_set_style_bg_color(top_bar_, lv_color_black(), 0);
        lv_obj_set_style_bg_opa(top_bar_, LV_OPA_TRANSP, 0);
    }
    // The upstream emoji image/label are replaced by the face layer.
    if (emoji_image_ != nullptr) {
        lv_obj_add_flag(emoji_image_, LV_OBJ_FLAG_HIDDEN);
    }
    if (emoji_label_ != nullptr) {
        lv_obj_add_flag(emoji_label_, LV_OBJ_FLAG_HIDDEN);
    }
    lv_obj_invalidate(container_);
}

void MemoriaFaceDisplay::RenderFace(float blink) {
    if (face_buffer_ == nullptr || container_ == nullptr) {
        return;
    }
    memoria::RenderFaceRgb565(face_buffer_, width_, height_,
                              memoria::FaceForEmotion(emotion_, blink));
    lv_obj_invalidate(container_);
}

void MemoriaFaceDisplay::UpdateBlinkTimer() {
    if (blink_timer_ == nullptr) {
        return;
    }
    if (!memoria::FaceBlinks(emotion_)) {
        esp_timer_stop(blink_timer_);
        blink_frame_ = -1;
        return;
    }
    if (!esp_timer_is_active(blink_timer_)) {
        esp_timer_start_periodic(blink_timer_, kBlinkTickUs);
    }
}

void MemoriaFaceDisplay::BlinkTimerCallback(void* arg) {
    auto* display = static_cast<MemoriaFaceDisplay*>(arg);
    if (display == nullptr || display->face_buffer_ == nullptr) {
        return;
    }
    if (!display->Lock(500)) {
        return;
    }
    if (display->blink_frame_ < 0) {
        if (--display->blink_cooldown_ <= 0) {
            display->blink_frame_ = 0;
        }
    }
    if (display->blink_frame_ >= 0) {
        display->RenderFace(kBlinkFrames[display->blink_frame_]);
        if (++display->blink_frame_ >= kBlinkFrameCount) {
            display->blink_frame_ = -1;
            display->blink_cooldown_ = kBlinkCooldownMinTicks +
                                       static_cast<int>(esp_random() % kBlinkCooldownSpreadTicks);
        }
    }
    display->Unlock();
}
