// The firmware builds for size (-Os); the per-pixel compositor below is the
// one hot loop worth optimising for speed.
#if defined(__GNUC__) && !defined(__clang__)
#pragma GCC optimize("O2")
#endif

#include "memoria_mascot_scene.h"

#include <cmath>
#include <cstring>

namespace memoria {

namespace {

constexpr int kSize = MascotScene::kSize;
constexpr int kCenter = kSize / 2;
constexpr int kRingInner = 148;  // the state ring glows from here to the rim
constexpr int kRingOuter = 180;
// Comet ring (thinking / connecting), in 1/256 turns.
constexpr uint32_t kCometTail = 110;
constexpr uint32_t kCometHead = 22;
constexpr uint32_t kCometLead = 5;
constexpr uint32_t kCometBase = 34;
constexpr float kPi = 3.14159265f;

// Motion envelopes, ms.
constexpr uint32_t kPopMs = 300;
constexpr uint32_t kPopSwapMs = 90;
constexpr uint32_t kHopMs = 520;
constexpr uint32_t kRiseMs = 700;
constexpr uint32_t kBlinkMs = 120;

// Boot animation timeline, ms after StartIntro().
constexpr uint32_t kOrbStart = 250;
constexpr uint32_t kOrbFull = 1100;
constexpr uint32_t kRevealStart = 950;
constexpr uint32_t kRevealEnd = 1750;
constexpr uint32_t kBrandIn = 1350;
constexpr uint32_t kBrandHold = 1750;
constexpr uint32_t kBrandOut = 2450;
constexpr uint32_t kBrandGone = 2800;
constexpr uint32_t kRiseStart = 2550;
constexpr uint32_t kGreetStart = 3650;
constexpr uint32_t kGreetEnd = 5000;

float Clamp01(float v) { return v < 0.0f ? 0.0f : (v > 1.0f ? 1.0f : v); }
float Smooth(float t) {
    t = Clamp01(t);
    return t * t * (3.0f - 2.0f * t);
}
float EaseOutBack(float t) {
    t = Clamp01(t);
    const float c1 = 1.5f;
    const float c3 = c1 + 1.0f;
    const float u = t - 1.0f;
    return 1.0f + c3 * u * u * u + c1 * u * u;
}
float Phase01(uint32_t now, uint32_t start, uint32_t end) {
    if (now <= start) {
        return 0.0f;
    }
    if (now >= end) {
        return 1.0f;
    }
    return static_cast<float>(now - start) / static_cast<float>(end - start);
}

uint16_t To565(uint32_t rgb) {
    return static_cast<uint16_t>(((rgb >> 8) & 0xF800) | ((rgb >> 5) & 0x07E0) |
                                 ((rgb >> 3) & 0x001F));
}

// Blend fg over bg with alpha 0..255 in RGB565 (5-bit alpha precision).
#if defined(__GNUC__)
__attribute__((always_inline))
#endif
inline uint16_t Blend(uint16_t fg, uint16_t bg, uint32_t alpha) {
    if (alpha >= 250) {
        return fg;
    }
    if (alpha < 4) {
        return bg;
    }
    const uint32_t a = (alpha + 4) >> 3;  // 0..32
    uint32_t f = (fg | (static_cast<uint32_t>(fg) << 16)) & 0x07E0F81Fu;
    uint32_t b = (bg | (static_cast<uint32_t>(bg) << 16)) & 0x07E0F81Fu;
    uint32_t r = ((((f - b) * a) >> 5) + b) & 0x07E0F81Fu;
    return static_cast<uint16_t>(r | (r >> 16));
}

SceneRect Union(const SceneRect& a, const SceneRect& b) {
    if (a.x1 <= a.x0 || a.y1 <= a.y0) {
        return b;
    }
    if (b.x1 <= b.x0 || b.y1 <= b.y0) {
        return a;
    }
    SceneRect r;
    r.x0 = a.x0 < b.x0 ? a.x0 : b.x0;
    r.y0 = a.y0 < b.y0 ? a.y0 : b.y0;
    r.x1 = a.x1 > b.x1 ? a.x1 : b.x1;
    r.y1 = a.y1 > b.y1 ? a.y1 : b.y1;
    return r;
}

SceneRect Clip(const SceneRect& a, const SceneRect& b) {
    SceneRect r;
    r.x0 = a.x0 > b.x0 ? a.x0 : b.x0;
    r.y0 = a.y0 > b.y0 ? a.y0 : b.y0;
    r.x1 = a.x1 < b.x1 ? a.x1 : b.x1;
    r.y1 = a.y1 < b.y1 ? a.y1 : b.y1;
    if (r.x1 < r.x0) {
        r.x1 = r.x0;
    }
    if (r.y1 < r.y0) {
        r.y1 = r.y0;
    }
    return r;
}

bool Empty(const SceneRect& r) { return r.x1 <= r.x0 || r.y1 <= r.y0; }

SceneRect Screen() {
    SceneRect r;
    r.x1 = kSize;
    r.y1 = kSize;
    return r;
}

// Four rectangles covering the rim band: everything outside the square
// inscribed in the ring's inner circle. The ring can then animate without
// redrawing the middle of the mascot.
int RingStrips(SceneRect* out) {
    constexpr int band = kCenter - (kRingInner * 7071) / 10000 + 1;
    out[0] = SceneRect{0, 0, kSize, band};
    out[1] = SceneRect{0, kSize - band, kSize, kSize};
    out[2] = SceneRect{0, band, band, kSize - band};
    out[3] = SceneRect{kSize - band, band, kSize, kSize - band};
    return 4;
}

}  // namespace

bool SceneMoodFromName(const char* name, SceneMood* mood) {
    if (name == nullptr || mood == nullptr) {
        return false;
    }
    struct Alias {
        const char* name;
        SceneMood mood;
    };
    static const Alias kAliases[] = {
        {"neutral", SceneMood::kNeutral},   {"idle", SceneMood::kNeutral},
        {"speaking", SceneMood::kNeutral},  {"happy", SceneMood::kHappy},
        {"laughing", SceneMood::kHappy},    {"funny", SceneMood::kHappy},
        {"cool", SceneMood::kHappy},        {"winking", SceneMood::kHappy},
        {"wink", SceneMood::kHappy},        {"loving", SceneMood::kLoving},
        {"caring", SceneMood::kLoving},     {"kissy", SceneMood::kLoving},
        {"sad", SceneMood::kSad},           {"crying", SceneMood::kSad},
        {"embarrassed", SceneMood::kSad},   {"surprised", SceneMood::kSurprised},
        {"shocked", SceneMood::kSurprised}, {"thinking", SceneMood::kThinking},
        {"curious", SceneMood::kThinking},  {"confused", SceneMood::kThinking},
        {"angry", SceneMood::kAngry},
    };
    for (const auto& alias : kAliases) {
        if (std::strcmp(alias.name, name) == 0) {
            *mood = alias.mood;
            return true;
        }
    }
    return false;
}

MascotScene::MascotScene(uint16_t* framebuffer, MascotAllocFn alloc, MascotFreeFn free)
    : fb_(framebuffer), alloc_(alloc), free_(free) {}

MascotScene::~MascotScene() {
    if (bg_ != nullptr) {
        free_(bg_);
    }
    if (ring_weight_ != nullptr) {
        free_(ring_weight_);
    }
    if (ring_angle_ != nullptr) {
        free_(ring_angle_);
    }
    if (radius_ != nullptr) {
        free_(radius_);
    }
}

bool MascotScene::Init() {
    const std::size_t pixels = static_cast<std::size_t>(kSize) * kSize;
    bg_ = static_cast<uint16_t*>(alloc_(pixels * 2));
    ring_weight_ = static_cast<uint8_t*>(alloc_(pixels));
    ring_angle_ = static_cast<uint8_t*>(alloc_(pixels));
    radius_ = static_cast<uint8_t*>(alloc_(pixels));
    if (fb_ == nullptr || bg_ == nullptr || ring_weight_ == nullptr || ring_angle_ == nullptr ||
        radius_ == nullptr) {
        return false;
    }
    for (int y = 0; y < kSize; ++y) {
        for (int x = 0; x < kSize; ++x) {
            const float dx = x + 0.5f - kCenter;
            const float dy = y + 0.5f - kCenter;
            const float r = std::sqrt(dx * dx + dy * dy);
            const std::size_t i = static_cast<std::size_t>(y) * kSize + x;
            radius_[i] = static_cast<uint8_t>(r > 255.0f ? 255.0f : r);
            float w = 0.0f;
            if (r >= kRingInner && r < kRingOuter + 2) {
                const float t = Clamp01((r - kRingInner) / (kRingOuter - kRingInner));
                w = t * std::sqrt(t);
            }
            ring_weight_[i] = static_cast<uint8_t>(w * 255.0f);
            float angle = std::atan2(dx, -dy) / (2.0f * kPi);  // 0 at 12 o'clock, clockwise
            if (angle < 0.0f) {
                angle += 1.0f;
            }
            ring_angle_[i] = static_cast<uint8_t>(static_cast<int>(angle * 256.0f) & 0xFF);
        }
    }
    for (int y = 0; y < kSize; ++y) {
        const float dy = y + 0.5f - kCenter;
        // One pixel past the edge keeps the panel's anti-aliased rim covered.
        circle_hw_[y] = static_cast<int16_t>(std::ceil(std::sqrt(kCenter * kCenter - dy * dy)) + 1);
        const float inner = static_cast<float>(kRingInner) - 1.0f;
        ring_hole_[y] = dy * dy < inner * inner
                            ? static_cast<int16_t>(std::sqrt(inner * inner - dy * dy))
                            : static_cast<int16_t>(0);
    }
    BuildBackground();
    full_redraw_ = true;
    return true;
}

void MascotScene::BuildBackground() {
    PrepareComet();
    if (bg_ == nullptr) {
        return;
    }
    // Radial wash from a lightened centre slightly above the middle to the
    // companion's soft tone, with a faint rim vignette. Ordered dither keeps
    // the RGB565 gradient free of bands.
    static const uint8_t kBayer[4][4] = {{0, 8, 2, 10}, {12, 4, 14, 6}, {3, 11, 1, 9}, {15, 7, 13, 5}};
    const float ir = (theme_.bg_inner >> 16) & 0xFF;
    const float ig = (theme_.bg_inner >> 8) & 0xFF;
    const float ib = theme_.bg_inner & 0xFF;
    const float orr = (theme_.bg_outer >> 16) & 0xFF;
    const float og = (theme_.bg_outer >> 8) & 0xFF;
    const float ob = theme_.bg_outer & 0xFF;
    for (int y = 0; y < kSize; ++y) {
        for (int x = 0; x < kSize; ++x) {
            const float dx = x + 0.5f - kCenter;
            const float dy = y + 0.5f - (kCenter - 26);
            const float d = std::sqrt(dx * dx + dy * dy) / 215.0f;
            const float t = Smooth(d);
            const float rim = radius_[static_cast<std::size_t>(y) * kSize + x];
            const float vignette = rim > 150.0f ? 1.0f - 0.07f * Clamp01((rim - 150.0f) / 30.0f) : 1.0f;
            const float dither = (kBayer[y & 3][x & 3] + 0.5f) / 16.0f;
            const float r = (ir + (orr - ir) * t) * vignette;
            const float g = (ig + (og - ig) * t) * vignette;
            const float b = (ib + (ob - ib) * t) * vignette;
            const int r5 = static_cast<int>(r / 255.0f * 31.0f + dither);
            const int g6 = static_cast<int>(g / 255.0f * 63.0f + dither);
            const int b5 = static_cast<int>(b / 255.0f * 31.0f + dither);
            bg_[static_cast<std::size_t>(y) * kSize + x] = static_cast<uint16_t>(
                ((r5 > 31 ? 31 : r5) << 11) | ((g6 > 63 ? 63 : g6) << 5) | (b5 > 31 ? 31 : b5));
        }
    }
}

uint32_t MascotScene::Random() {
    rng_ ^= rng_ << 13;
    rng_ ^= rng_ >> 17;
    rng_ ^= rng_ << 5;
    return rng_;
}

uint32_t MascotScene::RandomRange(uint32_t lo, uint32_t hi) {
    return hi <= lo ? lo : lo + Random() % (hi - lo);
}

void MascotScene::SetPack(MascotPack* pack, uint32_t now_ms) {
    const bool had_pack = pack_ != nullptr;
    pack_ = pack != nullptr && pack->loaded() ? pack : nullptr;
    theme_ = pack_ != nullptr ? pack_->theme() : MascotTheme{};
    BuildBackground();
    full_redraw_ = true;
    if (had_pack && pack_ != nullptr && !intro_active(now_ms)) {
        // A new companion arrives with a hop and a wave.
        override_pose_ = MascotFrame::kGreeting;
        override_until_ms_ = now_ms + 1800;
        StartMotion(Motion::kHop, now_ms);
        pose_ = MascotFrame::kGreeting;
    }
}

void MascotScene::StartIntro(uint32_t now_ms) {
    intro_ = true;
    intro_start_ms_ = now_ms;
    phase_ = ScenePhase::kIntro;
    phase_since_ms_ = now_ms;
    pose_ = MascotFrame::kDefault;
    motion_ = Motion::kNone;
    full_redraw_ = true;
}

bool MascotScene::intro_active(uint32_t now_ms) const {
    return intro_ && now_ms - intro_start_ms_ < kIntroDoneMs;
}

uint8_t MascotScene::chrome_opa(uint32_t now_ms) const {
    if (!intro_) {
        return 255;
    }
    const uint32_t t = now_ms - intro_start_ms_;
    if (t >= kIntroDoneMs) {
        return 255;
    }
    return static_cast<uint8_t>(255.0f * Phase01(t, kIntroChromeMs, kIntroChromeMs + 400));
}

uint32_t MascotScene::FrameIntervalMs(uint32_t now_ms) const {
    const bool moving = motion_ != Motion::kNone || now_ms < wobble_until_ms_ ||
                        pending_pose_ != MascotFrame::kCount;
    if (intro_active(now_ms) || moving) {
        return 40;
    }
    switch (phase_) {
        case ScenePhase::kThinking:
        case ScenePhase::kConnecting:
            return 40;  // the comet sweeps a visible distance every frame
        case ScenePhase::kSpeaking:
            return 50;  // lip beats are 70 ms and up
        case ScenePhase::kListening:
            return 60;
        default:
            return 80;
    }
}

void MascotScene::SetPhase(ScenePhase phase, uint32_t now_ms) {
    if (phase == ScenePhase::kIntro || phase == phase_) {
        return;
    }
    if (phase_ == ScenePhase::kSpeaking && phase == ScenePhase::kIdle) {
        // Keep the reply's mood on the face for a moment after speaking.
        mood_hold_until_ms_ = now_ms + 2200;
    }
    if (phase != ScenePhase::kSpeaking) {
        mouth_open_ = false;
    }
    phase_ = phase;
    phase_since_ms_ = now_ms;
    last_activity_ms_ = now_ms;
    sleeping_ = false;
    if (phase == ScenePhase::kError) {
        wobble_until_ms_ = now_ms + 2600;
    }
}

void MascotScene::SetMood(SceneMood mood, uint32_t now_ms) {
    mood_ = mood;
    last_activity_ms_ = now_ms;
    sleeping_ = false;
    if (phase_ == ScenePhase::kIdle) {
        mood_hold_until_ms_ = now_ms + 2200;
    }
}

void MascotScene::Pat(uint32_t now_ms) {
    last_activity_ms_ = now_ms;
    sleeping_ = false;
    override_pose_ = MascotFrame::kHappy;
    override_until_ms_ = now_ms + 1800;
    StartMotion(Motion::kHop, now_ms);
    pose_ = MascotFrame::kHappy;
}

void MascotScene::Shake(uint32_t now_ms) {
    last_activity_ms_ = now_ms;
    sleeping_ = false;
    override_pose_ = MascotFrame::kDizzy;
    override_until_ms_ = now_ms + 2400;
    wobble_until_ms_ = now_ms + 2400;
}

MascotFrame MascotScene::PoseForMood(SceneMood mood) const {
    switch (mood) {
        case SceneMood::kHappy:
        case SceneMood::kLoving:
            return MascotFrame::kHappy;
        case SceneMood::kSad:
        case SceneMood::kAngry:
            return MascotFrame::kSad;
        case SceneMood::kSurprised:
            return MascotFrame::kSurprised;
        case SceneMood::kThinking:
            return MascotFrame::kThinking;
        case SceneMood::kNeutral:
        default:
            return MascotFrame::kDefault;
    }
}

MascotFrame MascotScene::DesiredPose(uint32_t now_ms) const {
    if (intro_active(now_ms)) {
        // The boot entrance follows its own timeline whatever the device does.
        const uint32_t t = now_ms - intro_start_ms_;
        return t >= kGreetStart && t < kGreetEnd ? MascotFrame::kGreeting : MascotFrame::kDefault;
    }
    if (override_pose_ != MascotFrame::kCount && now_ms < override_until_ms_) {
        return override_pose_;
    }
    switch (phase_) {
        case ScenePhase::kIntro:
            return MascotFrame::kDefault;
        case ScenePhase::kSetup:
            return MascotFrame::kGreeting;
        case ScenePhase::kWifiConfig:
            return MascotFrame::kListening;
        case ScenePhase::kConnecting:
        case ScenePhase::kThinking:
            return MascotFrame::kThinking;
        case ScenePhase::kListening:
            return MascotFrame::kListening;
        case ScenePhase::kSpeaking:
            return PoseForMood(mood_);
        case ScenePhase::kError:
            return MascotFrame::kDizzy;
        case ScenePhase::kIdle:
        default:
            if (sleeping_) {
                return MascotFrame::kSleepy;
            }
            if (now_ms < mood_hold_until_ms_) {
                return PoseForMood(mood_);
            }
            return MascotFrame::kDefault;
    }
}

void MascotScene::StartMotion(Motion motion, uint32_t now_ms) {
    motion_ = motion;
    motion_start_ms_ = now_ms;
}

void MascotScene::UpdateActor(uint32_t now_ms) {
    if (!actor_started_) {
        actor_started_ = true;
        next_blink_ms_ = now_ms + RandomRange(1800, 4200);
        next_idle_action_ms_ = now_ms + RandomRange(18000, 32000);
        last_activity_ms_ = now_ms;
    }
    if (override_pose_ != MascotFrame::kCount && now_ms >= override_until_ms_) {
        override_pose_ = MascotFrame::kCount;
    }
    if (motion_ == Motion::kPop && now_ms - motion_start_ms_ >= kPopMs) {
        motion_ = Motion::kNone;
    } else if (motion_ == Motion::kHop && now_ms - motion_start_ms_ >= kHopMs) {
        motion_ = Motion::kNone;
    } else if (motion_ == Motion::kRise && now_ms - motion_start_ms_ >= kRiseMs + 200) {
        motion_ = Motion::kNone;
    }

    // Idle life: doze off after a while, and now and then do something small.
    if (phase_ == ScenePhase::kIdle && !intro_active(now_ms)) {
        if (!sleeping_ && now_ms - last_activity_ms_ >= kSleepAfterMs) {
            sleeping_ = true;
        }
        if (!sleeping_ && now_ms >= next_idle_action_ms_ && motion_ == Motion::kNone &&
            now_ms >= mood_hold_until_ms_) {
            next_idle_action_ms_ = now_ms + RandomRange(20000, 38000);
            const uint32_t pick = Random() % 100;
            if (pick < 35) {
                next_blink_ms_ = now_ms;  // a double blink
                double_blink_ = true;
            } else if (pick < 60) {
                override_pose_ = MascotFrame::kHappy;
                override_until_ms_ = now_ms + 1600;
                StartMotion(Motion::kHop, now_ms);
                pose_ = MascotFrame::kHappy;
            } else if (pick < 85) {
                override_pose_ = MascotFrame::kThinking;
                override_until_ms_ = now_ms + 1500;
            } else {
                override_pose_ = MascotFrame::kGreeting;
                override_until_ms_ = now_ms + 1600;
                StartMotion(Motion::kHop, now_ms);
                pose_ = MascotFrame::kGreeting;
            }
        }
    }

    // Pose changes go through a squash-and-pop; the swap happens at the
    // bottom of the squash so the new pose springs up.
    const MascotFrame desired = DesiredPose(now_ms);
    if (motion_ == Motion::kPop && pending_pose_ != MascotFrame::kCount &&
        now_ms - motion_start_ms_ >= kPopSwapMs) {
        pose_ = pending_pose_;
        pending_pose_ = MascotFrame::kCount;
    }
    if (desired != pose_ && pending_pose_ != desired) {
        if (motion_ == Motion::kHop || motion_ == Motion::kRise) {
            pose_ = desired;
        } else {
            pending_pose_ = desired;
            StartMotion(Motion::kPop, now_ms);
        }
    }

    // Blinks.
    if (blink_until_ms_ != 0 && now_ms >= blink_until_ms_) {
        blink_until_ms_ = 0;
        if (double_blink_) {
            double_blink_ = false;
            next_blink_ms_ = now_ms + 140;
        } else {
            next_blink_ms_ = now_ms + RandomRange(2400, 5600);
            double_blink_ = Random() % 5 == 0;
        }
    }
    if (blink_until_ms_ == 0 && now_ms >= next_blink_ms_) {
        blink_until_ms_ = now_ms + kBlinkMs;
    }

    // Lip flaps while speaking: short open/closed beats with breath pauses.
    if (phase_ == ScenePhase::kSpeaking) {
        if (now_ms >= next_mouth_ms_) {
            mouth_open_ = !mouth_open_;
            if (mouth_open_) {
                next_mouth_ms_ = now_ms + RandomRange(90, 180);
            } else {
                next_mouth_ms_ = now_ms + (Random() % 7 == 0 ? RandomRange(260, 460)
                                                              : RandomRange(70, 150));
            }
        }
    } else {
        mouth_open_ = false;
    }
}

MascotScene::Placement MascotScene::Compute(uint32_t now_ms) {
    Placement p;
    const uint32_t intro_t = intro_ ? now_ms - intro_start_ms_ : 0;
    const bool intro_on = intro_active(now_ms);
    if (intro_on) {
        // Quantise the overlay so a static stretch of the intro is not redrawn.
        if (intro_t < kRevealEnd + 40 || (intro_t >= kBrandIn && intro_t < kBrandGone + 40)) {
            p.intro_step = static_cast<int>(intro_t / 16);
        }
    }

    // Ring.
    const float t_s = now_ms / 1000.0f;
    switch (phase_) {
        case ScenePhase::kListening: {
            p.ring_mode = 1;
            p.ring_level = static_cast<int>(150 + 90 * (0.5f + 0.5f * std::sin(t_s * 2.0f * kPi / 1.4f)));
            break;
        }
        case ScenePhase::kThinking:
        case ScenePhase::kConnecting: {
            p.ring_mode = 2;
            const float rev = phase_ == ScenePhase::kThinking ? 1.1f : 1.6f;
            p.ring_level = static_cast<int>(std::fmod(t_s / rev, 1.0f) * 256.0f) & 0xFF;
            break;
        }
        case ScenePhase::kSpeaking:
            p.ring_mode = 1;
            p.ring_level = mouth_open_ ? 120 : 70;
            break;
        case ScenePhase::kSetup:
        case ScenePhase::kWifiConfig:
            p.ring_mode = 1;
            p.ring_level = static_cast<int>(80 + 90 * (0.5f + 0.5f * std::sin(t_s * 2.0f * kPi / 3.0f)));
            break;
        default:
            break;
    }
    if (intro_on) {
        p.ring_mode = 0;
    }
    if (p.ring_mode == 1) {
        p.ring_level &= ~7;  // 32 intensity steps are plenty for a glow
    }

    if (pack_ == nullptr || phase_ == ScenePhase::kSetup) {
        return p;
    }
    if (intro_on && intro_t < kRiseStart) {
        return p;
    }

    // Choose the frame: talk beats and blinks ride on the current pose.
    MascotFrame frame = pose_;
    const bool blinking = blink_until_ms_ != 0 && now_ms < blink_until_ms_;
    if (phase_ == ScenePhase::kSpeaking && mouth_open_ && TalkFrameFor(pose_) != MascotFrame::kCount) {
        frame = TalkFrameFor(pose_);
    } else if (blinking && BlinkFrameFor(pose_) != MascotFrame::kCount && motion_ != Motion::kPop) {
        frame = BlinkFrameFor(pose_);
    }
    const MascotSprite* sprite = pack_->Frame(frame);
    if (sprite == nullptr) {
        return p;
    }

    // Body motion.
    float dx = 0.0f;
    float dy = 0.0f;
    float sx = 1.0f;
    float sy = 1.0f;
    float breathe_amp = 3.0f;
    float breathe_period = 3.6f;
    switch (phase_) {
        case ScenePhase::kListening:
            breathe_amp = 2.0f;
            breathe_period = 2.4f;
            break;
        case ScenePhase::kThinking:
        case ScenePhase::kConnecting:
            breathe_amp = 2.0f;
            breathe_period = 3.0f;
            dx = 5.0f * std::sin(t_s * 2.0f * kPi / 2.8f);
            break;
        case ScenePhase::kSpeaking:
            breathe_amp = 1.5f;
            breathe_period = 3.0f;
            dy -= mouth_open_ ? 2.0f : 0.0f;
            break;
        case ScenePhase::kIdle:
            if (sleeping_) {
                breathe_amp = 2.5f;
                breathe_period = 5.2f;
            }
            break;
        default:
            break;
    }
    dy += breathe_amp * std::sin(t_s * 2.0f * kPi / breathe_period);
    if (now_ms < wobble_until_ms_) {
        const float fade = Clamp01((wobble_until_ms_ - now_ms) / 600.0f);
        dx += 6.0f * fade * std::sin(t_s * 2.0f * kPi / 0.45f);
    }

    const uint32_t mt = now_ms - motion_start_ms_;
    if (intro_on && intro_t >= kRiseStart) {
        // The boot entrance: rise from below the rim with a little overshoot,
        // then land with a squash.
        const float u = Phase01(intro_t, kRiseStart, kRiseStart + kRiseMs);
        dy += (1.0f - EaseOutBack(u)) * 230.0f;
        if (u < 1.0f) {
            sy = 1.0f + 0.06f * (1.0f - u);
            sx = 1.0f - 0.04f * (1.0f - u);
        } else {
            const float v = Phase01(intro_t, kRiseStart + kRiseMs, kRiseStart + kRiseMs + 220);
            const float squash = std::sin(v * kPi) * 0.06f;
            sy = 1.0f - squash;
            sx = 1.0f + squash * 0.8f;
        }
        if (intro_t >= kGreetStart && intro_t < kGreetStart + kHopMs) {
            const float h = static_cast<float>(intro_t - kGreetStart) / kHopMs;
            dy -= 14.0f * std::sin(Clamp01((h - 0.15f) / 0.7f) * kPi);
        }
    } else if (motion_ == Motion::kPop) {
        const float u = static_cast<float>(mt) / kPopMs;
        float s;
        if (mt < kPopSwapMs) {
            s = -0.07f * std::sin(u * kPi / (2.0f * kPopSwapMs / kPopMs));
        } else {
            const float v = static_cast<float>(mt - kPopSwapMs) / (kPopMs - kPopSwapMs);
            s = -0.07f * std::cos(v * kPi * 0.5f) + 0.05f * std::sin(v * kPi) * (1.0f - v);
        }
        sy = 1.0f + s;
        sx = 1.0f - s * 0.7f;
    } else if (motion_ == Motion::kHop) {
        if (mt < 90) {
            const float u = mt / 90.0f;
            sy = 1.0f - 0.10f * u;
            sx = 1.0f + 0.07f * u;
        } else if (mt < 350) {
            const float u = (mt - 90) / 260.0f;
            dy -= 24.0f * std::sin(u * kPi);
            sy = 1.0f + 0.05f * std::sin(u * kPi);
            sx = 1.0f - 0.035f * std::sin(u * kPi);
        } else {
            const float u = Clamp01((mt - 350) / 170.0f);
            const float squash = std::sin(u * kPi) * 0.08f;
            sy = 1.0f - squash;
            sx = 1.0f + squash * 0.8f;
        }
    }

    p.sprite = true;
    p.frame = frame;
    p.ax = kCenter + static_cast<int>(std::lround(dx));
    p.ay = kFootY + static_cast<int>(std::lround(dy));
    p.sx_q = static_cast<int>(std::lround(sx * 256.0f));
    p.sy_q = static_cast<int>(std::lround(sy * 256.0f));
    const int cx = pack_->canvas_w() / 2;
    const int fy = pack_->foot_y();
    p.sprite_box.x0 = p.ax + ((sprite->x - cx) * p.sx_q >> 8) - 1;
    p.sprite_box.x1 = p.ax + ((sprite->x + sprite->w - cx) * p.sx_q >> 8) + 2;
    p.sprite_box.y0 = p.ay + ((sprite->y - fy) * p.sy_q >> 8) - 1;
    p.sprite_box.y1 = p.ay + ((sprite->y + sprite->h - fy) * p.sy_q >> 8) + 2;
    p.sprite_box = Clip(p.sprite_box, Screen());

    // The shadow stays on the ground and shrinks while the mascot is airborne.
    const float lift = Clamp01((kFootY - p.ay) / 30.0f);
    p.shadow_rx = static_cast<int>(pack_->foot_half_w() * 1.35f * (1.0f - 0.3f * lift));
    p.shadow_alpha = static_cast<int>(72.0f * (1.0f - 0.5f * lift));
    const int sry = p.shadow_rx / 6 + 2;
    const int scx = kCenter + static_cast<int>(dx * 0.4f);
    p.shadow_box = Clip(SceneRect{scx - p.shadow_rx - 1, kFootY + 2 - sry - 1, scx + p.shadow_rx + 2,
                                  kFootY + 2 + sry + 2},
                        Screen());
    return p;
}

void MascotScene::DrawShadow(const SceneRect& clip, const Placement& p) {
    if (!p.sprite || p.shadow_rx <= 0) {
        return;
    }
    const SceneRect area = Clip(clip, p.shadow_box);
    if (Empty(area)) {
        return;
    }
    const uint16_t colour = To565(theme_.ink);
    const float cx = (p.shadow_box.x0 + p.shadow_box.x1) * 0.5f;
    const float cy = kFootY + 2.0f;
    const float rx = static_cast<float>(p.shadow_rx);
    const float ry = static_cast<float>(p.shadow_rx / 6 + 2);
    for (int y = area.y0; y < area.y1; ++y) {
        const float ny = (y + 0.5f - cy) / ry;
        uint16_t* row = RowPtr(y);
        for (int x = area.x0; x < area.x1; ++x) {
            const float nx = (x + 0.5f - cx) / rx;
            const float q = nx * nx + ny * ny;
            if (q >= 1.0f) {
                continue;
            }
            const float f = (1.0f - q) * (1.0f - q);
            row[x] = Blend(colour, row[x], static_cast<uint32_t>(p.shadow_alpha * f));
        }
    }
}

void MascotScene::DrawSprite(const SceneRect& clip, const Placement& p) {
    if (!p.sprite || pack_ == nullptr) {
        return;
    }
    const MascotSprite* s = pack_->Frame(p.frame);
    if (s == nullptr) {
        return;
    }
    const SceneRect area = Clip(clip, p.sprite_box);
    if (Empty(area)) {
        return;
    }
    const int cx = pack_->canvas_w() / 2;
    const int fy = pack_->foot_y();
    if (p.sx_q == 256 && p.sy_q == 256) {
        // Unscaled: a straight alpha blit.
        const int ox = p.ax - cx + s->x;  // screen x of sprite column 0
        const int oy = p.ay - fy + s->y;
        for (int y = area.y0; y < area.y1; ++y) {
            const int sy = y - oy;
            if (sy < 0 || sy >= s->h) {
                continue;
            }
            const uint16_t* src = s->rgb + static_cast<std::size_t>(sy) * s->w;
            const uint8_t* alpha = s->alpha + static_cast<std::size_t>(sy) * s->w;
            uint16_t* row = RowPtr(y);
            // Only the opaque span of this sprite row can change a pixel.
            const int span0 = ox + s->span[sy * 2];
            const int span1 = ox + s->span[sy * 2 + 1];
            int x0 = area.x0 > span0 ? area.x0 : span0;
            int x1 = area.x1 < span1 ? area.x1 : span1;
            for (int x = x0; x < x1; ++x) {
                const uint8_t a = alpha[x - ox];
                if (a != 0) {
                    row[x] = Blend(src[x - ox], row[x], a);
                }
            }
        }
        return;
    }
    // Scaled about the feet: nearest-neighbour inverse mapping, 16.16 fixed point.
    const int32_t inv_x = static_cast<int32_t>((256LL << 16) / p.sx_q);
    const int32_t inv_y = static_cast<int32_t>((256LL << 16) / p.sy_q);
    for (int y = area.y0; y < area.y1; ++y) {
        const int32_t cyq = (y - p.ay) * inv_y;  // canvas offset from the feet, 16.16
        const int canvas_y = fy + (cyq >> 16);
        const int sy = canvas_y - s->y;
        if (sy < 0 || sy >= s->h) {
            continue;
        }
        const uint16_t* src = s->rgb + static_cast<std::size_t>(sy) * s->w;
        const uint8_t* alpha = s->alpha + static_cast<std::size_t>(sy) * s->w;
        uint16_t* row = RowPtr(y);
        const int span0 = s->span[sy * 2];
        const int span1 = s->span[sy * 2 + 1];
        int32_t cxq = (area.x0 - p.ax) * inv_x;
        for (int x = area.x0; x < area.x1; ++x, cxq += inv_x) {
            const int sx = cx + (cxq >> 16) - s->x;
            if (sx < span0 || sx >= span1) {
                continue;
            }
            const uint8_t a = alpha[sx];
            if (a != 0) {
                row[x] = Blend(src[sx], row[x], a);
            }
        }
    }
}

void MascotScene::DrawRing(const SceneRect& clip, const Placement& p) {
    if (p.ring_mode == 0) {
        return;
    }
    const uint16_t colour = To565(theme_.primary);
    for (int y = clip.y0; y < clip.y1; ++y) {
        const std::size_t base = static_cast<std::size_t>(y) * kSize;
        uint16_t* row = RowPtr(y);
        // Columns [kCenter - hole, kCenter + hole) of this row are inside the
        // ring's inner circle and never lit.
        const int hole = ring_hole_[y];
        for (int x = clip.x0; x < clip.x1; ++x) {
            if (x == kCenter - hole && hole > 0) {
                x = kCenter + hole - 1;
                continue;
            }
            const uint32_t w = ring_weight_[base + x];
            if (w == 0) {
                continue;
            }
            if (p.ring_mode == 1) {
                row[x] = Blend(colour, row[x], (w * static_cast<uint32_t>(p.ring_level)) >> 8);
            } else {
                const uint8_t behind = static_cast<uint8_t>(p.ring_level - ring_angle_[base + x]);
                row[x] = Blend(comet_colour_[behind], row[x], (w * comet_level_[behind]) >> 8);
            }
        }
    }
}

void MascotScene::PrepareComet() {
    // Comet: a warm head at ring_level with a tail fading counter-clockwise
    // and a soft leading edge, tabulated by angular distance behind the head.
    const uint16_t colour = To565(theme_.primary);
    const uint16_t head = To565(theme_.accent);
    for (uint32_t behind = 0; behind < 256; ++behind) {
        uint32_t level = kCometBase;
        uint16_t c = colour;
        if (behind < kCometTail) {
            level = kCometBase + ((kCometTail - behind) * (255 - kCometBase)) / kCometTail;
            if (behind < kCometHead) {
                c = Blend(head, colour, 255 * (kCometHead - behind) / kCometHead);
            }
        } else if (behind > 256 - kCometLead) {
            const uint32_t ahead = 256 - behind;  // 1..kCometLead
            level = kCometBase + (255 - kCometBase) * (kCometLead - ahead) / kCometLead;
            c = head;
        }
        comet_level_[behind] = static_cast<uint8_t>(level);
        comet_colour_[behind] = c;
    }
}

void MascotScene::DrawBrand(const SceneRect& clip, uint8_t opa, int dy) {
    if (brand_ == nullptr || brand_->alpha == nullptr || opa == 0) {
        return;
    }
    const int x0 = kCenter - brand_->w / 2;
    const int y0 = kCenter - brand_->h / 2 - 6 + dy;
    const SceneRect box = Clip(clip, SceneRect{x0, y0, x0 + brand_->w, y0 + brand_->h});
    const uint16_t colour = To565(theme_.ink);
    for (int y = box.y0; y < box.y1; ++y) {
        const uint8_t* src = brand_->alpha + static_cast<std::size_t>(y - y0) * brand_->w;
        uint16_t* row = RowPtr(y);
        for (int x = box.x0; x < box.x1; ++x) {
            const uint32_t a = (src[x - x0] * static_cast<uint32_t>(opa)) >> 8;
            if (a != 0) {
                row[x] = Blend(colour, row[x], a);
            }
        }
    }
}

void MascotScene::DrawIntro(const SceneRect& clip, uint32_t now_ms) {
    const uint32_t t = now_ms - intro_start_ms_;
    // Wordmark over the revealed backdrop.
    if (t >= kBrandIn && t < kBrandGone) {
        float opa;
        if (t < kBrandHold) {
            opa = Smooth(Phase01(t, kBrandIn, kBrandHold));
        } else if (t < kBrandOut) {
            opa = 1.0f;
        } else {
            opa = 1.0f - Smooth(Phase01(t, kBrandOut, kBrandGone));
        }
        const int drift = static_cast<int>(8.0f * (1.0f - Smooth(Phase01(t, kBrandIn, kBrandHold)))) -
                          static_cast<int>(10.0f * Smooth(Phase01(t, kBrandOut, kBrandGone)));
        DrawBrand(clip, static_cast<uint8_t>(opa * 255.0f), drift);
    }
    if (t >= kRevealEnd) {
        return;
    }
    // Darkness with a warm orb of light that opens into the scene; the
    // per-radius tables come from PrepareIntro(). The orb glows on black,
    // where RGB565 steps would show as rings, so it is dithered down.
    const uint8_t* mask_lut = mask_lut_;
    static const uint8_t kBayer[4][4] = {{0, 8, 2, 10}, {12, 4, 14, 6}, {3, 11, 1, 9}, {15, 7, 13, 5}};
    for (int y = clip.y0; y < clip.y1; ++y) {
        const std::size_t base = static_cast<std::size_t>(y) * kSize;
        uint16_t* row = RowPtr(y);
        const uint8_t* dither = kBayer[y & 3];
        for (int x = clip.x0; x < clip.x1; ++x) {
            const uint8_t r = radius_[base + x];
            const uint8_t m = mask_lut[r];
            if (m >= 250) {
                continue;  // fully revealed
            }
            const uint32_t d = dither[x & 3] * 16u;
            const uint16_t outside = static_cast<uint16_t>(((orb_r5_[r] + d) >> 8) << 11 |
                                                           ((orb_g6_[r] + d) >> 8) << 5 |
                                                           ((orb_b5_[r] + d) >> 8));
            row[x] = Blend(row[x], outside, m);
        }
    }
}

void MascotScene::PrepareIntro(uint32_t now_ms) {
    const uint32_t t = now_ms - intro_start_ms_;
    const float orb = Smooth(Phase01(t, kOrbStart, kOrbFull));
    const float orb_r = 18.0f + 70.0f * orb;
    const float reveal = Smooth(Phase01(t, kRevealStart, kRevealEnd));
    const float reveal_r = reveal * 280.0f;
    constexpr float kEdge = 36.0f;
    const float orb_gain = orb * (1.0f - 0.6f * reveal);
    // Warm light #FFF6EA shaded by the orb, as RGB565 channels in 8.8 fixed
    // point so DrawIntro only adds the dither and shifts.
    for (int r = 0; r < 256; ++r) {
        const float q = r / orb_r;
        const uint32_t o = static_cast<uint32_t>(255.0f * orb_gain * std::exp(-q * q * 2.2f));
        orb_r5_[r] = static_cast<uint16_t>(0xFFu * o * 31 / 255);
        orb_g6_[r] = static_cast<uint16_t>(0xF6u * o * 63 / 255);
        orb_b5_[r] = static_cast<uint16_t>(0xEAu * o * 31 / 255);
        mask_lut_[r] = static_cast<uint8_t>(255.0f * Clamp01((reveal_r - r) / kEdge + 0.5f));
    }
}

void MascotScene::RedrawRect(const SceneRect& rect, const Placement& p, uint32_t now_ms) {
    const SceneRect r = Clip(rect, Screen());
    if (Empty(r)) {
        return;
    }
    // Compose one row at a time in a small line buffer (internal RAM on the
    // device) so each framebuffer pixel in PSRAM is read once from the
    // backdrop and written once, whatever the number of layers.
    // Pixels outside the round panel are never seen, so each row stops at
    // the circle.
    const bool intro_on = intro_active(now_ms);
    uint16_t line[kSize];
    for (int y = r.y0; y < r.y1; ++y) {
        const int x0 = r.x0 > kCenter - circle_hw_[y] ? r.x0 : kCenter - circle_hw_[y];
        const int x1 = r.x1 < kCenter + circle_hw_[y] ? r.x1 : kCenter + circle_hw_[y];
        if (x1 <= x0) {
            continue;
        }
        const std::size_t bytes = static_cast<std::size_t>(x1 - x0) * 2;
        const std::size_t offset = static_cast<std::size_t>(y) * kSize;
        std::memcpy(line + x0, bg_ + offset + x0, bytes);
        line_ = line;
        line_y_ = y;
        const SceneRect row{x0, y, x1, y + 1};
        DrawShadow(row, p);
        DrawSprite(row, p);
        DrawRing(row, p);
        if (intro_on) {
            DrawIntro(row, now_ms);
        }
        line_y_ = -1;
        std::memcpy(fb_ + offset + x0, line + x0, bytes);
    }
}

int MascotScene::Render(uint32_t now_ms, SceneRect* dirty, int max_dirty) {
    if (fb_ == nullptr || bg_ == nullptr || dirty == nullptr || max_dirty <= 0) {
        return 0;
    }
    if (intro_ && !intro_active(now_ms) && phase_ == ScenePhase::kIntro) {
        // The boot animation ended before any live phase was reported.
        phase_ = ScenePhase::kConnecting;
        phase_since_ms_ = now_ms;
    }
    if (intro_ && !intro_active(now_ms)) {
        intro_ = false;
        full_redraw_ = true;
    }
    UpdateActor(now_ms);
    const Placement p = Compute(now_ms);
    if (intro_active(now_ms)) {
        PrepareIntro(now_ms);
    }

    int count = 0;
    const bool intro_changed = p.intro_step != last_.intro_step;
    if (full_redraw_ || (intro_changed && (p.intro_step >= 0 || last_.intro_step >= 0))) {
        full_redraw_ = false;
        dirty[0] = Screen();
        RedrawRect(dirty[0], p, now_ms);
        last_ = p;
        return 1;
    }
    const bool sprite_changed = p.sprite != last_.sprite || p.frame != last_.frame ||
                                p.ax != last_.ax || p.ay != last_.ay || p.sx_q != last_.sx_q ||
                                p.sy_q != last_.sy_q || p.shadow_rx != last_.shadow_rx ||
                                p.shadow_alpha != last_.shadow_alpha;
    const bool ring_changed = p.ring_mode != last_.ring_mode || p.ring_level != last_.ring_level;
    SceneRect rects[kMaxDirty];
    if (sprite_changed) {
        SceneRect box = Union(Union(last_.sprite_box, last_.shadow_box),
                              Union(p.sprite_box, p.shadow_box));
        if (!p.sprite && !last_.sprite) {
            box = SceneRect{};
        }
        if (!Empty(box)) {
            rects[count++] = box;
        }
    }
    if (ring_changed) {
        count += RingStrips(rects + count);
    }
    if (count > max_dirty) {
        count = 1;
        rects[0] = Screen();
    }
    for (int i = 0; i < count; ++i) {
        RedrawRect(rects[i], p, now_ms);
        dirty[i] = rects[i];
    }
    last_ = p;
    return count;
}

}  // namespace memoria
