#ifndef MEMORIA_MASCOT_DISPLAY_H
#define MEMORIA_MASCOT_DISPLAY_H

#include "display/lcd_display.h"
#include "lvgl_image.h"
#include "memoria_mascot_pack.h"
#include "memoria_mascot_scene.h"

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include <atomic>
#include <functional>
#include <memory>
#include <string>

class Backlight;

// LCD display for the Memoria ESP-VoCat: the 360x360 round panel shows the
// account's companion mascot (memoria_mascot_scene.h) as a living character,
// with the boot animation, state ring and companion switching. LVGL keeps
// only the text on top: a quiet status line, notification and subtitle pills,
// and the binding QR card. While the device is getting online (network scan,
// Wi-Fi setup, activation) the scene is captioned: the mascot moves up and the
// text sits on its own band below it, never on the mascot. The QR card is a
// screen of its own; no other text is drawn over it.
class MemoriaMascotDisplay : public SpiLcdDisplay {
public:
    MemoriaMascotDisplay(esp_lcd_panel_io_handle_t panel_io, esp_lcd_panel_handle_t panel,
                         int width, int height, int offset_x, int offset_y, bool mirror_x,
                         bool mirror_y, bool swap_xy);
    ~MemoriaMascotDisplay() override;

    void SetupUI() override;
    void SetEmotion(const char* emotion) override;
    void SetStatus(const char* status) override;
    void ShowNotification(const char* notification, int duration_ms = 3000) override;
    void ShowNotification(const std::string& notification, int duration_ms = 3000) override;
    void SetChatMessage(const char* role, const char* content) override;
    void SetTheme(Theme* theme) override;
    bool ShowQrCode(const std::string& payload, const char* caption) override;
    void ClearQrCode() override;

    // Switch to another companion (persisted, applied by the animation task).
    // Unknown ids are ignored.
    void SetCompanion(const char* companion_id);
    // Body gestures from the IMU.
    void Pat();
    void Shake();
    // Called once from the animation task after the first frame is on screen,
    // so the backlight can fade in over a finished picture, never over noise.
    void SetOnFirstFrame(std::function<void()> callback) { on_first_frame_ = std::move(callback); }
    // Used to dim the panel while the companion sleeps.
    void SetBacklight(Backlight* backlight) { backlight_ = backlight; }

    // False when the scene could not be allocated; the board then lights the
    // panel immediately and the upstream widgets stay in charge.
    bool animated() const { return scene_ != nullptr && frame_image_ != nullptr; }

    static bool IsKnownCompanion(const char* companion_id);

private:
    static constexpr uint32_t kFrameMs = 40;  // fastest frame pace

    static void AnimationTask(void* arg);
    void AnimationLoop();
    memoria::ScenePhase CurrentPhase(uint32_t now_ms);
    bool LoadCompanion(const std::string& id, std::unique_ptr<memoria::MascotPack>* out);
    void ApplyChrome();                 // caller holds the display lock
    void ApplyChromeOpacity(uint8_t opa);  // caller holds the display lock
    bool WantsCaption(uint32_t now_ms);
    void StyleQrCard();                 // caller holds the display lock
    static uint32_t NowMs();

    uint16_t* framebuffer_ = nullptr;
    std::unique_ptr<LvglAllocatedImage> frame_image_;
    std::unique_ptr<memoria::MascotScene> scene_;
    std::unique_ptr<memoria::MascotPack> pack_;
    memoria::BrandMark brand_;
    TaskHandle_t task_ = nullptr;
    std::atomic<bool> running_{false};
    std::function<void()> on_first_frame_;
    Backlight* backlight_ = nullptr;
    bool dimmed_ = false;
    uint8_t chrome_opa_ = 0;
    uint8_t text_opa_applied_ = 0;
    bool caption_layout_ = false;  // LVGL text is on the caption band
    uint8_t caption_mix_ = 0;
    int32_t bottom_bar_height_ = 0;
    lv_obj_t* qr_card_ = nullptr;
    lv_obj_t* qr_title_ = nullptr;

    // Inputs from other tasks, consumed by the animation task.
    SemaphoreHandle_t input_mutex_ = nullptr;
    std::string companion_id_ = "starlight";
    std::string pending_companion_;
    memoria::SceneMood pending_mood_ = memoria::SceneMood::kNeutral;
    bool mood_pending_ = false;
    bool pat_pending_ = false;
    bool shake_pending_ = false;
    std::atomic<uint32_t> error_until_ms_{0};
    std::atomic<bool> user_spoke_{false};
    std::atomic<bool> qr_visible_{false};
};

#endif  // MEMORIA_MASCOT_DISPLAY_H
