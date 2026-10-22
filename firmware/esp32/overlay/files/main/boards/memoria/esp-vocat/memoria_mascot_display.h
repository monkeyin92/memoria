#ifndef MEMORIA_MASCOT_DISPLAY_H
#define MEMORIA_MASCOT_DISPLAY_H

#include "display/lcd_display.h"
#include "lvgl_image.h"
#include "memoria_dot_face.h"
#include "memoria_mascot_pack.h"
#include "memoria_mascot_scene.h"
#include "sdkconfig.h"
#if CONFIG_MEMORIA_BENCH_SERIAL
#include "memoria_mascot_status.h"

// Defined in memoria_mascot_bench.cc; present in test-rig images only.
extern "C" const char memoria_bench_build_marker[];
#endif

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#include <atomic>
#include <functional>
#include <memory>
#include <string>

class Backlight;

// LCD display for the Memoria ESP-VoCat. The 360x360 panel shows the yellow
// dot face from memoria_dot_face.h. The mascot scene still times the boot
// beat, the caption band, and the chrome. While the dot face is on screen the
// scene does not draw. LVGL keeps the text and the binding QR card. A server
// emotion name never selects a face. While the device is getting online the
// face lifts and the status text sits on the band below it. The QR card is a
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
    void SetOfflineMenuActive(bool active);
    // Body gestures from the IMU.
    void Pat();
    void Shake();
    // Called once from the animation task after the first frame is on screen,
    // so the backlight can fade in over a finished picture, never over noise.
    void SetOnFirstFrame(std::function<void()> callback) { on_first_frame_ = std::move(callback); }
    // Board wires WifiHold start when idle darkens the panel.
    void SetOnScreenOff(std::function<void()> callback) { on_screen_off_ = std::move(callback); }
    // Used to dim the panel while the companion sleeps.
    void SetBacklight(Backlight* backlight) { backlight_ = backlight; }

    // False when the scene could not be allocated; the board then lights the
    // panel immediately and the upstream widgets stay in charge.
    bool animated() const { return scene_ != nullptr && frame_image_ != nullptr; }

    static bool IsKnownCompanion(const char* companion_id);

#if CONFIG_MEMORIA_BENCH_SERIAL
    // Test-rig builds only (TODOLIST M-2, memoria_mascot_bench.cc); a product image has neither of these.
    // The whole screen, LVGL text layer included, as SNAP lines on the USB port (memoria_bench_snap.h). It
    // runs on the calling task, the USB command task, whose stack is sized for the software renderer; the
    // display lock is held only while LVGL draws the picture, not while it is sent.
    void BenchSendSnapshot();
    // Asks the animation task for one status line (memoria_mascot_status.h) with its counters; answered within
    // a frame, 100 ms at the slowest (the dark-panel poll).
    void BenchRequestStatus() { bench_status_requested_.store(true); }
#endif

private:
    static constexpr uint32_t kFrameMs = 40;  // fastest frame pace
    // Matches PowerPolicy::kIdleScreenOffMs / kNoSpeechEndMs (15 s).
    static constexpr uint32_t kIdleScreenOffMs = 15 * 1000;
    static constexpr uint32_t kScreenOffPollMs = 100;  // how fast a dark panel notices a wake

    static void AnimationTask(void* arg);
    void AnimationLoop();
    memoria::ScenePhase CurrentPhase(uint32_t now_ms);
    // No conversation, no screen: the panel goes dark kIdleScreenOffMs after the
    // companion settles into the idle phase and lights again the moment anything
    // else happens (tap, BOOT, pairing, an error). Nothing is rendered while it
    // is dark; the panel keeps its last frame. Entering dark notifies the board
    // so WifiHold can start.
    void UpdateIdleScreen(memoria::ScenePhase phase, uint32_t now_ms);
    void ApplyChrome();                 // caller holds the display lock
    void ApplyChromeOpacity(uint8_t opa);  // caller holds the display lock
    bool WantsCaption(uint32_t now_ms);
    void StyleQrCard();                 // caller holds the display lock
    static uint32_t NowMs();
#if CONFIG_MEMORIA_BENCH_SERIAL
    // Logs the status line; called by the animation task with its own counters.
    static void BenchLogStatus(const memoria::BenchStatus& status);
    // Logs the profile line that follows it: where the sampled renders spent their time.
    static void BenchLogProfile(const memoria::RenderProfile& profile);
    static void BenchLogTasks();
    // The disp line: where LVGL's refreshes spend their time (hooks on its events, installed once).
    void BenchHookLvgl();
    static void BenchLogLvgl();
    // One-shot sampler microbenchmark 20 s after boot (memoria_mascot_sampler_bench.h).
    static void BenchStartSamplerBench();
#endif

    uint16_t* framebuffer_ = nullptr;
    std::unique_ptr<LvglAllocatedImage> frame_image_;
    std::unique_ptr<memoria::MascotScene> scene_;
    memoria::BrandMark brand_;
    TaskHandle_t task_ = nullptr;
    std::atomic<bool> running_{false};
    std::function<void()> on_first_frame_;
    std::function<void()> on_screen_off_;
    Backlight* backlight_ = nullptr;
    bool dimmed_ = false;
    bool screen_off_ = false;
    bool idle_ = false;           // the companion is in the idle phase
    uint32_t idle_since_ms_ = 0;  // when it settled there (valid while idle_)
    uint8_t chrome_opa_ = 0;
    uint8_t text_opa_applied_ = 0;
    bool caption_layout_ = false;  // LVGL text is on the caption band
    uint8_t caption_mix_ = 0;
    int32_t bottom_bar_height_ = 0;
    lv_obj_t* qr_card_ = nullptr;
    lv_obj_t* qr_title_ = nullptr;
    std::atomic<bool> offline_menu_active_{false};

    // Inputs from other tasks, consumed by the animation task.
    SemaphoreHandle_t input_mutex_ = nullptr;
    std::string companion_id_ = "starlight";
    std::string pending_companion_;
    memoria::FaceId shown_face_ = static_cast<memoria::FaceId>(0xFF);
    bool pat_pending_ = false;
    bool shake_pending_ = false;
    std::atomic<uint32_t> error_until_ms_{0};
    std::atomic<bool> user_spoke_{false};
    std::atomic<bool> qr_visible_{false};
#if CONFIG_MEMORIA_BENCH_SERIAL
    std::atomic<bool> bench_status_requested_{false};
#endif
};

#endif  // MEMORIA_MASCOT_DISPLAY_H
