#ifndef MEMORIA_MASCOT_SCENE_H
#define MEMORIA_MASCOT_SCENE_H

#include "memoria_mascot_pack.h"
#include "memoria_mascot_raster.h"

#include <cstdint>

namespace memoria {

// The companion scene on the 360x360 round LCD: a soft gradient backdrop in
// the companion's colours, the plush mascot with a ground shadow, and a state
// ring on the rim. The scene decides the mascot's behaviour (breathing,
// blinking, lip flaps that follow the reply's audio, mood changes with a
// squash-and-pop, hops when patted, idle moments, falling asleep) and renders
// it into an RGB565 framebuffer, reporting only the rectangles that changed.
//
// Text (status, subtitles, the binding QR card) stays in LVGL on top of the
// framebuffer. While the device is still getting online the scene can leave
// the lower band free for that text (SetCaptioned), so no line ever sits on
// top of the mascot. Like memoria_face.cc this file has no ESP-IDF or LVGL
// dependency; scripts/preview_memoria_mascot.py renders it on the host.

enum class ScenePhase : uint8_t {
    kIntro,       // boot animation, entered with StartIntro()
    kSetup,       // unbound: the binding QR card covers the centre
    kWifiConfig,  // hotspot provisioning
    kConnecting,  // network, activation or media session opening
    kIdle,
    kListening,
    kThinking,  // the user finished speaking, the reply has not started
    kSpeaking,
    kError,
};

enum class SceneMood : uint8_t {
    kNeutral,
    kHappy,
    kSad,
    kSurprised,
    kThinking,
    kLoving,
    kAngry,
};

// Server expression names (media edge screen.expression) plus the legacy
// aliases the upstream firmware passes. Unknown names return false.
bool SceneMoodFromName(const char* name, SceneMood* mood);

// Where a bench build's frames spend their time (TODOLIST M-6). Sums of microseconds over the renders that
// were sampled (every kProfileEvery-th while a clock is set), so the PC divides by `sampled`. The scene
// has no clock of its own; the bench build hands it one (SetProfileClock) and the product build never does.
struct RenderProfile {
    uint64_t renders = 0;      // Render() calls made while a clock was set
    uint64_t sampled = 0;      // of those, the ones timed below
    uint64_t total_us = 0;     // the whole Render()
    uint64_t actor_us = 0;     // UpdateActor + Compute (+ the intro tables)
    uint64_t rect_us = 0;      // all RedrawRect calls
    uint64_t touch_us = 0;     // SpriteTouch (the span of the last and the new placement)
    uint64_t rows = 0;         // rows composed
    uint64_t copy_in_us = 0;   // the backdrop copied into the line buffer
    uint64_t shadow_us = 0;
    uint64_t sprite_us = 0;
    uint64_t ring_us = 0;      // the state ring, and the boot overlay while it runs
    uint64_t copy_out_us = 0;  // the line written back to the framebuffer
};
using MascotClockFn = uint32_t (*)();  // microseconds, free running, wraps at 2^32
constexpr uint32_t kProfileEvery = 8;

class MascotScene {
public:
    static constexpr int kSize = 360;
    static constexpr int kMaxDirty = 6;
    // Screen row the mascot's feet stand on.
    static constexpr int kFootY = 298;
    // Captioned layout: the mascot shrinks onto a higher ground row and the
    // rows from kCaptionTextY down belong to the text (two to three lines of
    // the 28 px LVGL font still fit inside the round panel there).
    static constexpr int kCaptionFootY = 212;
    static constexpr int kCaptionScaleQ = 164;  // 0.64 in 8.8 fixed point
    static constexpr int kCaptionTextY = 226;
    static constexpr uint32_t kCaptionMs = 450;  // layout transition
    // Boot animation milestones, in ms after StartIntro().
    static constexpr uint32_t kIntroChromeMs = 3300;  // status text may appear
    static constexpr uint32_t kIntroDoneMs = 5200;    // live phases take over
    // Idle time before the mascot dozes off.
    static constexpr uint32_t kSleepAfterMs = 3 * 60 * 1000;

    // framebuffer: kSize * kSize RGB565 pixels, owned by the caller.
    MascotScene(uint16_t* framebuffer, MascotAllocFn alloc, MascotFreeFn free);
    ~MascotScene();
    MascotScene(const MascotScene&) = delete;
    MascotScene& operator=(const MascotScene&) = delete;

    bool Init();

    // The pack stays owned by the caller and must outlive its use here.
    // A companion change swaps with a pop; nullptr shows the backdrop only.
    void SetPack(MascotPack* pack, uint32_t now_ms);
    void SetBrand(const BrandMark* mark) { brand_ = mark; }
    void Seed(uint32_t seed) { rng_ = seed != 0 ? seed : 0x9E3779B9u; }

    void StartIntro(uint32_t now_ms);
    bool intro_active(uint32_t now_ms) const;
    void SetPhase(ScenePhase phase, uint32_t now_ms);
    ScenePhase phase() const { return phase_; }
    SceneMood mood() const { return mood_; }
    void SetMood(SceneMood mood, uint32_t now_ms);
    void Pat(uint32_t now_ms);
    void Shake(uint32_t now_ms);
    // How loud the audio is (TODOLIST M-1), polled by the display task once per frame from the audio tasks'
    // level taps (memoria_audio_level.h). `peak` and `valley` are the loudest and the quietest 20 ms block
    // since the previous call, 0..255 as RmsToLevel() scales them, and `blocks` is how many blocks that
    // was. Zero blocks is not silence: a stalled playback queue publishes nothing, and then the mouth
    // closes and the glow dims instead of acting out a reply that is not being heard.
    void SetOutputLevel(uint8_t valley, uint8_t peak, uint32_t blocks, uint32_t now_ms);
    // The microphone side, after echo cancellation: the glow while listening brightens with the voice.
    void SetInputLevel(uint8_t peak, uint32_t blocks, uint32_t now_ms);
    // Eases the mascot into (or out of) the captioned layout.
    void SetCaptioned(bool captioned, uint32_t now_ms);
    // How far the captioned layout has progressed, 0 (full) .. 255 (captioned).
    uint8_t caption_mix(uint32_t now_ms) const;
    bool sleeping() const { return sleeping_; }
    // Redraw the whole screen on the next Render (e.g. after LVGL lost it).
    void Invalidate() { full_redraw_ = true; }

    // Advance to now_ms and redraw everything that changed. Returns the number
    // of dirty rectangles written (0 when the frame is unchanged).
    int Render(uint32_t now_ms, SceneRect* dirty, int max_dirty);
    // How soon the next Render is worth doing: 25 fps whenever the screen is lit and the companion is
    // alive (the breathing and the glow move by fractions of a pixel per frame, so every frame is a
    // visible step), half that for the Wi-Fi and binding screens and for the dozing companion. The
    // display task may stretch this when frames cost more than it can afford (memoria_frame_pacer.h).
    uint32_t FrameIntervalMs(uint32_t now_ms) const;
    // Pixels composed (backdrop copied, layers blended, row written back) by the last Render. The dirty
    // rectangles overstate nothing and hide nothing but this: a ring-only redraw skips the middle.
    uint32_t last_composed_px() const { return composed_px_; }

    // Bench builds only: time the stages of every kProfileEvery-th Render with `clock`. nullptr switches it off.
    void SetProfileClock(MascotClockFn clock) { clock_ = clock; }
    const RenderProfile& profile() const { return profile_; }
    // Opacity for the LVGL text layer: hidden during the boot animation.
    uint8_t chrome_opa(uint32_t now_ms) const;
    const MascotTheme& theme() const { return theme_; }
    // The mascot frame drawn by the last Render (for logs and tests).
    MascotFrame last_frame() const { return last_.frame; }

private:
    enum class Motion : uint8_t { kNone, kPop, kHop, kRise };

    // Follows one audio level: a fast envelope for syllables (the mouth), a slow one for phrases (glow,
    // body).
    struct LevelFollower {
        float fast = 0.0f;
        float slow = 0.0f;
        uint32_t last_ms = 0;   // the previous poll
        uint32_t audio_ms = 0;  // the last poll that carried blocks
        bool polled = false;
    };

    struct Placement {
        bool sprite = false;
        MascotFrame frame = MascotFrame::kCount;
        // Where the mascot stands: feet centre in 1/16 px, scale in 1/4096, sampled at exactly that.
        SpriteTransform xf;
        SceneRect sprite_box;
        // The ground shadow, centre and radius in 1/16 px.
        int shadow_cx_q = 0;
        int shadow_cy_q = 0;
        int shadow_rx_q = 0;
        int shadow_alpha = 0;
        SceneRect shadow_box;
        int ring_mode = 0;  // 0 none, 1 glow, 2 comet
        int ring_level = 0;  // glow: 0..255 intensity; comet: head angle 0..255
        int intro_step = -1;  // quantised intro overlay state, -1 when done
    };

    int RenderImpl(uint32_t now_ms, SceneRect* dirty, int max_dirty);
    uint32_t Random();
    uint32_t RandomRange(uint32_t lo, uint32_t hi);
    MascotFrame PoseForMood(SceneMood mood) const;
    MascotFrame DesiredPose(uint32_t now_ms) const;
    void UpdateActor(uint32_t now_ms);
    void StartMotion(Motion motion, uint32_t now_ms);
    Placement Compute(uint32_t now_ms);
    // One step of the mouth detector on the fast output envelope.
    void StepMouth(float env, uint32_t now_ms);
    // The glow level for this frame: held for kGlowHoldMs so the ring is not redrawn at the frame rate.
    int HeldGlow(int level, uint32_t now_ms);
    void BuildBackground();
    // skip_hole: a ring-only redraw leaves the middle of the screen (inside the ring) alone.
    // span: recompose only the columns the sprite and shadow covered in the last frame or cover now
    // (everything else in the rectangle is already right); never with the boot animation.
    // Returns the bounding box of the pixels actually written (empty when the call wrote nothing), which
    // is never wider than the requested rectangle: Render hands this to the display as the invalidate
    // area so LVGL re-blits what the scene composed and nothing more.
    SceneRect RedrawRect(const SceneRect& rect, const Placement& p, uint32_t now_ms,
                         bool skip_hole = false, bool span = false);
    // The columns [*lo, *hi) of screen row y that the sprite or its shadow can change under p.
    bool SpriteTouch(const Placement& p, int y, int* lo, int* hi) const;
    void DrawSprite(const SceneRect& clip, const Placement& p);
    void DrawShadow(const SceneRect& clip, const Placement& p);
    void DrawRing(const SceneRect& clip, const Placement& p);
    void DrawIntro(const SceneRect& clip, uint32_t now_ms);
    void PrepareIntro(uint32_t now_ms);
    void PrepareComet();
    // Row being composed in RedrawRect's line buffer, else the framebuffer.
    uint16_t* RowPtr(int y) { return y == line_y_ ? line_ : fb_ + static_cast<std::size_t>(y) * kSize; }
    void DrawBrand(const SceneRect& clip, uint8_t opa, int dy);

    uint16_t* fb_;
    MascotAllocFn alloc_;
    MascotFreeFn free_;
    uint16_t* bg_ = nullptr;
    uint8_t* ring_weight_ = nullptr;  // 0 outside the rim band
    uint8_t* ring_angle_ = nullptr;   // 0..255 clockwise from 12 o'clock
    uint8_t* radius_ = nullptr;       // distance from centre, px (clamped 255)
    int16_t ring_hole_[kSize] = {};   // per row: half-width inside the ring
    int16_t circle_hw_[kSize] = {};   // per row: half-width of the visible panel
    int16_t touch_lo_[kSize] = {};    // per row: columns the last drawn sprite and shadow covered
    int16_t touch_hi_[kSize] = {};
    uint16_t orb_r5_[256] = {};  // boot orb light per radius, 8.8 fixed point
    uint16_t orb_g6_[256] = {};
    uint16_t orb_b5_[256] = {};
    uint8_t mask_lut_[256] = {};
    uint8_t comet_level_[256] = {};  // comet ring by angle behind the head
    uint16_t comet_colour_[256] = {};
    uint16_t* line_ = nullptr;
    int line_y_ = -1;
    MascotPack* pack_ = nullptr;
    const BrandMark* brand_ = nullptr;
    MascotTheme theme_;
    uint32_t rng_ = 0x9E3779B9u;
    bool full_redraw_ = true;

    ScenePhase phase_ = ScenePhase::kConnecting;
    uint32_t phase_since_ms_ = 0;
    uint32_t intro_start_ms_ = 0;
    bool intro_ = false;
    SceneMood mood_ = SceneMood::kNeutral;
    uint32_t mood_hold_until_ms_ = 0;

    MascotFrame pose_ = MascotFrame::kDefault;
    MascotFrame pending_pose_ = MascotFrame::kCount;
    Motion motion_ = Motion::kNone;
    uint32_t motion_start_ms_ = 0;
    MascotFrame override_pose_ = MascotFrame::kCount;
    uint32_t override_until_ms_ = 0;
    uint32_t wobble_until_ms_ = 0;

    bool caption_target_ = false;
    float caption_from_ = 0.0f;
    uint32_t caption_since_ms_ = 0;

    uint32_t next_blink_ms_ = 0;
    uint32_t blink_until_ms_ = 0;
    bool double_blink_ = false;
    bool mouth_open_ = false;  // out_open_ while speaking
    uint32_t next_idle_action_ms_ = 0;

    // Audio levels (SetOutputLevel / SetInputLevel). out_open_ is the mouth detector's state, out_ext_
    // the crest (while open) or valley (while shut) it is measuring from.
    LevelFollower out_;
    LevelFollower in_;
    bool out_open_ = false;
    float out_ext_ = 0.0f;
    uint32_t out_toggle_ms_ = 0;
    // The glow ring's held level (HeldGlow).
    int glow_level_ = 0;
    uint32_t glow_ms_ = 0;
    bool glow_valid_ = false;
    uint32_t composed_px_ = 0;
    MascotClockFn clock_ = nullptr;
    RenderProfile profile_;
    bool profiling_ = false;  // this Render is one of the sampled ones
    uint32_t profile_tick_ = 0;
    uint32_t last_activity_ms_ = 0;
    bool sleeping_ = false;
    bool actor_started_ = false;

    Placement last_;
};

}  // namespace memoria

#endif  // MEMORIA_MASCOT_SCENE_H
