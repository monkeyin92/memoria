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
// Breathing and speech are squashes about the feet: taller means narrower, by this share of the stretch.
constexpr float kPlushRatio = 0.7f;
// Speech lifts the body by up to this much (share of its height) at full level.
constexpr float kSpeakPulse = 0.014f;
// Audio level followers (TODOLIST M-1), ms. Levels come as 20 ms blocks; the mouth follows syllables,
// the glow and the body follow phrases.
constexpr float kMouthAttackMs = 15.0f;
constexpr float kMouthReleaseMs = 45.0f;
constexpr float kBodyAttackMs = 40.0f;
constexpr float kBodyReleaseMs = 160.0f;
constexpr float kVoiceReleaseMs = 200.0f;
// No blocks for this long means nothing is playing (a stalled queue), not a quiet passage.
constexpr uint32_t kStallMs = 80;
// The mouth opens on a rise out of a valley and shuts on a fall from a crest (a fixed threshold would
// hold it open through whole phrases: the TTS stays within 15 % of its crest inside one), but never
// flickers faster than this.
constexpr float kMouthOpenFloor = 70.0f;
constexpr float kMouthShutFloor = 45.0f;
constexpr uint32_t kMouthMinOpenMs = 80;
constexpr uint32_t kMouthMinClosedMs = 60;
// The glow ring is only redrawn this often (it is a 59k px redraw, and 8 ms steps of a soft glow are
// invisible).
constexpr uint32_t kGlowHoldMs = 80;

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

inline uint16_t Blend(uint16_t fg, uint16_t bg, uint32_t alpha) { return Blend565(fg, bg, alpha); }

float Follow(float env, float target, float dt_ms, float attack_ms, float release_ms) {
    const float tau = target > env ? attack_ms : release_ms;
    return env + (target - env) * (1.0f - std::exp(-dt_ms / tau));
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

void MascotScene::SetCaptioned(bool captioned, uint32_t now_ms) {
    if (captioned == caption_target_) {
        return;
    }
    // Reverse from wherever the running transition has got to.
    caption_from_ = caption_mix(now_ms) / 255.0f;
    caption_target_ = captioned;
    caption_since_ms_ = now_ms;
}

uint8_t MascotScene::caption_mix(uint32_t now_ms) const {
    const float target = caption_target_ ? 1.0f : 0.0f;
    const float u = Smooth(Phase01(now_ms, caption_since_ms_, caption_since_ms_ + kCaptionMs));
    return static_cast<uint8_t>(std::lround(255.0f * (caption_from_ + (target - caption_from_) * u)));
}

uint32_t MascotScene::FrameIntervalMs(uint32_t now_ms) const {
    const bool moving = motion_ != Motion::kNone || now_ms < wobble_until_ms_ ||
                        pending_pose_ != MascotFrame::kCount ||
                        now_ms - caption_since_ms_ < kCaptionMs;
    if (intro_active(now_ms) || moving) {
        return 40;
    }
    switch (phase_) {
        case ScenePhase::kSetup:
        case ScenePhase::kWifiConfig:
            return 80;  // a slow glow, and a small captioned companion
        case ScenePhase::kIdle:
            // The breathing is sub-pixel now, so at 25 fps every frame moves; a dozing companion
            // breathes once in five seconds and does not need it.
            return sleeping_ ? 80 : 40;
        default:
            return 40;  // the comet sweeps a visible distance per frame, the mouth follows 20 ms audio
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

    // Lip flaps follow what the speaker plays (SetOutputLevel); nothing playing, the mouth stays shut.
    mouth_open_ = phase_ == ScenePhase::kSpeaking && out_open_;
}

void MascotScene::SetOutputLevel(uint8_t valley, uint8_t peak, uint32_t blocks, uint32_t now_ms) {
    LevelFollower& f = out_;
    float dt = f.polled ? static_cast<float>(now_ms - f.last_ms) : 40.0f;
    dt = dt < 1.0f ? 1.0f : (dt > 200.0f ? 200.0f : dt);
    f.polled = true;
    f.last_ms = now_ms;
    if (blocks > 0) {
        f.audio_ms = now_ms;
        const float hi = static_cast<float>(peak);
        const float lo = valley < peak ? static_cast<float>(valley) : hi;
        // The quietest block first, then the loudest: a syllable boundary inside the frame shows up as
        // a dip the detector can see, whatever the frame rate.
        f.fast = Follow(f.fast, lo, dt * 0.5f, kMouthAttackMs, kMouthReleaseMs);
        StepMouth(f.fast, now_ms);
        f.fast = Follow(f.fast, hi, dt * 0.5f, kMouthAttackMs, kMouthReleaseMs);
        StepMouth(f.fast, now_ms);
        f.slow = Follow(f.slow, hi, dt, kBodyAttackMs, kBodyReleaseMs);
    } else if (now_ms - f.audio_ms >= kStallMs) {
        f.fast = Follow(f.fast, 0.0f, dt, kMouthAttackMs, kMouthReleaseMs);
        StepMouth(f.fast, now_ms);
        f.slow = Follow(f.slow, 0.0f, dt, kBodyAttackMs, kBodyReleaseMs);
    }
}

void MascotScene::StepMouth(float env, uint32_t now_ms) {
    const uint32_t since = now_ms - out_toggle_ms_;
    if (out_open_) {
        out_ext_ = env > out_ext_ ? env : out_ext_;
        const float drop = 0.15f * out_ext_ > 12.0f ? 0.15f * out_ext_ : 12.0f;
        if ((env <= kMouthShutFloor || env < out_ext_ - drop) && since >= kMouthMinOpenMs) {
            out_open_ = false;
            out_ext_ = env;
            out_toggle_ms_ = now_ms;
        }
    } else {
        out_ext_ = env < out_ext_ ? env : out_ext_;
        const float base = out_ext_ > kMouthOpenFloor ? out_ext_ : kMouthOpenFloor;
        const float rise = 0.12f * base > 12.0f ? 0.12f * base : 12.0f;
        if (env >= kMouthOpenFloor && env > out_ext_ + rise && since >= kMouthMinClosedMs) {
            out_open_ = true;
            out_ext_ = env;
            out_toggle_ms_ = now_ms;
        }
    }
}

void MascotScene::SetInputLevel(uint8_t peak, uint32_t blocks, uint32_t now_ms) {
    LevelFollower& f = in_;
    float dt = f.polled ? static_cast<float>(now_ms - f.last_ms) : 40.0f;
    dt = dt < 1.0f ? 1.0f : (dt > 200.0f ? 200.0f : dt);
    f.polled = true;
    f.last_ms = now_ms;
    if (blocks > 0) {
        f.audio_ms = now_ms;
        f.slow = Follow(f.slow, static_cast<float>(peak), dt, kBodyAttackMs, kVoiceReleaseMs);
    } else if (now_ms - f.audio_ms >= kStallMs) {
        f.slow = Follow(f.slow, 0.0f, dt, kBodyAttackMs, kVoiceReleaseMs);
    }
}

int MascotScene::HeldGlow(int level, uint32_t now_ms) {
    if (glow_valid_ && now_ms - glow_ms_ < kGlowHoldMs) {
        return glow_level_;
    }
    glow_valid_ = true;
    glow_ms_ = now_ms;
    glow_level_ = level;
    return level;
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
    int glow = 0;
    switch (phase_) {
        case ScenePhase::kListening: {
            // A calm pulse that brightens toward full with the child's voice.
            p.ring_mode = 1;
            const float calm = 120.0f + 70.0f * (0.5f + 0.5f * std::sin(t_s * 2.0f * kPi / 1.4f));
            glow = static_cast<int>(calm + (255.0f - calm) * (in_.slow / 255.0f));
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
            // The glow rides the phrase and dims while nothing plays.
            p.ring_mode = 1;
            glow = static_cast<int>(64.0f + 72.0f * (out_.slow / 255.0f));
            break;
        case ScenePhase::kSetup:
        case ScenePhase::kWifiConfig:
            p.ring_mode = 1;
            glow = static_cast<int>(80 + 90 * (0.5f + 0.5f * std::sin(t_s * 2.0f * kPi / 3.0f)));
            break;
        default:
            break;
    }
    if (intro_on) {
        p.ring_mode = 0;
    }
    if (p.ring_mode == 1) {
        p.ring_level = HeldGlow(glow & ~7, now_ms);  // 32 intensity steps are plenty for a glow
    } else {
        glow_valid_ = false;
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

    // Body motion. The mascot is one bitmap, so all it can do is move and squash. Breathing squashes it
    // about the feet (the head rises, the feet stay planted, taller means narrower) instead of sliding the
    // whole picture up and down, and a slow sideways weight shift keeps it from standing still between
    // breaths. Every effect below adds to the scale, so a pop or a hop starts from the breath it
    // interrupts rather than from a rest pose.
    float dx = 0.0f;
    float dy = 0.0f;
    float sx = 1.0f;
    float sy = 1.0f;
    float breathe = 0.012f;  // stretch of the body's height at the top of a breath
    float breathe_period = 3.6f;
    float sway_px = 1.2f;
    float sway_period = 7.3f;
    switch (phase_) {
        case ScenePhase::kListening:
            breathe = 0.008f;
            breathe_period = 2.4f;
            sway_px = 1.0f;
            sway_period = 6.1f;
            break;
        case ScenePhase::kThinking:
        case ScenePhase::kConnecting:
            breathe = 0.008f;
            breathe_period = 3.0f;
            sway_px = 0.0f;
            dx = 5.0f * std::sin(t_s * 2.0f * kPi / 2.8f);
            break;
        case ScenePhase::kSpeaking: {
            breathe = 0.006f;
            breathe_period = 3.0f;
            sway_px = 0.8f;
            sway_period = 5.9f;
            // The body lifts with the voice, a squash about the feet like the breath.
            const float lift_with_voice = kSpeakPulse * (out_.slow / 255.0f);
            sy += lift_with_voice;
            sx -= kPlushRatio * lift_with_voice;
            break;
        }
        case ScenePhase::kIdle:
            if (sleeping_) {
                breathe = 0.010f;
                breathe_period = 5.2f;
                sway_px = 0.0f;
            }
            break;
        default:
            break;
    }
    const float breath = breathe * std::sin(t_s * 2.0f * kPi / breathe_period);
    sy += breath;
    sx -= kPlushRatio * breath;
    dx += sway_px * std::sin(t_s * 2.0f * kPi / sway_period + 1.3f);
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
            sy += 0.06f * (1.0f - u);
            sx -= 0.04f * (1.0f - u);
        } else {
            const float v = Phase01(intro_t, kRiseStart + kRiseMs, kRiseStart + kRiseMs + 220);
            const float squash = std::sin(v * kPi) * 0.06f;
            sy -= squash;
            sx += squash * 0.8f;
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
        sy += s;
        sx -= s * 0.7f;
    } else if (motion_ == Motion::kHop) {
        if (mt < 90) {
            const float u = mt / 90.0f;
            sy -= 0.10f * u;
            sx += 0.07f * u;
        } else if (mt < 350) {
            const float u = (mt - 90) / 260.0f;
            dy -= 24.0f * std::sin(u * kPi);
            sy += 0.05f * std::sin(u * kPi);
            sx -= 0.035f * std::sin(u * kPi);
        } else {
            const float u = Clamp01((mt - 350) / 170.0f);
            const float squash = std::sin(u * kPi) * 0.08f;
            sy -= squash;
            sx += squash * 0.8f;
        }
    }

    // The shadow stays on the ground and shrinks while the mascot is airborne.
    const float lift = Clamp01(-dy / 30.0f);

    // Captioned layout: the whole body, its motion included, shrinks about
    // the feet onto a higher ground row. The boot entrance keeps its stage.
    float ground = static_cast<float>(kFootY);
    float k = 1.0f;
    if (!intro_on) {
        const float c = caption_mix(now_ms) / 255.0f;
        k = 1.0f - c * (1.0f - kCaptionScaleQ / 256.0f);
        ground -= c * static_cast<float>(kFootY - kCaptionFootY);
        dx *= k;
        dy *= k;
        sx *= k;
        sy *= k;
    }

    p.sprite = true;
    p.frame = frame;
    // Position to 1/16 px and scale to 1/4096: far finer than a pixel, so the sampler sees every bit of
    // the motion, and still coarse enough that an unmoving mascot compares equal frame to frame.
    p.xf.ax_q = static_cast<int32_t>(std::lround((kCenter + dx) * 16.0f));
    p.xf.ay_q = static_cast<int32_t>(std::lround((ground + dy) * 16.0f));
    p.xf.sx_q = static_cast<int32_t>(std::lround(sx * kScaleOne));
    p.xf.sy_q = static_cast<int32_t>(std::lround(sy * kScaleOne));
    p.sprite_box =
        Clip(SpriteBounds(*sprite, pack_->canvas_w() / 2, pack_->foot_y(), p.xf), Screen());

    const float rx = pack_->foot_half_w() * 1.35f * k * (1.0f - 0.3f * lift);
    const float ry = rx / 6.0f + 2.0f;
    const float cx = kCenter + dx * 0.4f;
    const float cy = ground + 2.0f;
    p.shadow_rx_q = static_cast<int>(std::lround(rx * 16.0f));
    p.shadow_cx_q = static_cast<int>(std::lround(cx * 16.0f));
    p.shadow_cy_q = static_cast<int>(std::lround(cy * 16.0f));
    p.shadow_alpha = static_cast<int>(72.0f * (1.0f - 0.5f * lift));
    p.shadow_box = Clip(SceneRect{static_cast<int>(std::floor(cx - rx)) - 1,
                                  static_cast<int>(std::floor(cy - ry)) - 1,
                                  static_cast<int>(std::ceil(cx + rx)) + 1,
                                  static_cast<int>(std::ceil(cy + ry)) + 1},
                        Screen());
    return p;
}

void MascotScene::DrawShadow(const SceneRect& clip, const Placement& p) {
    if (!p.sprite || p.shadow_rx_q <= 0) {
        return;
    }
    const SceneRect area = Clip(clip, p.shadow_box);
    if (Empty(area)) {
        return;
    }
    const uint16_t colour = To565(theme_.ink);
    const float cx = static_cast<float>(p.shadow_cx_q) / 16.0f;
    const float cy = static_cast<float>(p.shadow_cy_q) / 16.0f;
    const float rx = static_cast<float>(p.shadow_rx_q) / 16.0f;
    const float ry = rx / 6.0f + 2.0f;
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
    for (int y = area.y0; y < area.y1; ++y) {
        BlendSpriteRow(*s, cx, fy, p.xf, y, area.x0, area.x1, RowPtr(y));
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

bool MascotScene::SpriteTouch(const Placement& p, int y, int* lo, int* hi) const {
    int l = kSize;
    int h = 0;
    if (p.sprite && pack_ != nullptr) {
        const MascotSprite* s = pack_->Frame(p.frame);
        const SceneRect& box = p.sprite_box;
        int xs = 0;
        int xe = 0;
        if (s != nullptr && y >= box.y0 && y < box.y1 &&
            SpriteRowSpan(*s, pack_->canvas_w() / 2, pack_->foot_y(), p.xf, y, box.x0, box.x1, &xs, &xe)) {
            l = xs;
            h = xe;
        }
    }
    if (p.sprite && p.shadow_rx_q > 0 && y >= p.shadow_box.y0 && y < p.shadow_box.y1 &&
        p.shadow_box.x1 > p.shadow_box.x0) {
        l = p.shadow_box.x0 < l ? p.shadow_box.x0 : l;
        h = p.shadow_box.x1 > h ? p.shadow_box.x1 : h;
    }
    *lo = l;
    *hi = h;
    return h > l;
}

void MascotScene::RedrawRect(const SceneRect& rect, const Placement& p, uint32_t now_ms,
                             bool skip_hole, bool narrow_request) {
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
    const bool narrow = narrow_request && !intro_on;
    uint16_t line[kSize];
    auto compose = [&](int y, int xa, int xb) {
        if (xb <= xa) {
            return;
        }
        const std::size_t bytes = static_cast<std::size_t>(xb - xa) * 2;
        const std::size_t offset = static_cast<std::size_t>(y) * kSize;
        const bool timed = profiling_ && clock_ != nullptr;
        const uint32_t t0 = timed ? clock_() : 0;
        std::memcpy(line + xa, bg_ + offset + xa, bytes);
        line_ = line;
        line_y_ = y;
        const SceneRect row{xa, y, xb, y + 1};
        const uint32_t t1 = timed ? clock_() : 0;
        DrawShadow(row, p);
        const uint32_t t2 = timed ? clock_() : 0;
        DrawSprite(row, p);
        const uint32_t t3 = timed ? clock_() : 0;
        DrawRing(row, p);
        if (intro_on) {
            DrawIntro(row, now_ms);
        }
        const uint32_t t4 = timed ? clock_() : 0;
        line_y_ = -1;
        std::memcpy(fb_ + offset + xa, line + xa, bytes);
        composed_px_ += static_cast<uint32_t>(xb - xa);
        if (timed) {
            const uint32_t t5 = clock_();
            profile_.rows += 1;
            profile_.copy_in_us += t1 - t0;
            profile_.shadow_us += t2 - t1;
            profile_.sprite_us += t3 - t2;
            profile_.ring_us += t4 - t3;
            profile_.copy_out_us += t5 - t4;
        }
    };
    for (int y = r.y0; y < r.y1; ++y) {
        int x0 = r.x0 > kCenter - circle_hw_[y] ? r.x0 : kCenter - circle_hw_[y];
        int x1 = r.x1 < kCenter + circle_hw_[y] ? r.x1 : kCenter + circle_hw_[y];
        if (!skip_hole) {
            // What this row's sprite and shadow cover now is what the next narrow redraw must also erase.
            int lo = 0;
            int hi = 0;
            const bool timed = profiling_ && clock_ != nullptr;
            const uint32_t tt = timed ? clock_() : 0;
            const bool covers = SpriteTouch(p, y, &lo, &hi);
            if (timed) {
                profile_.touch_us += clock_() - tt;
            }
            if (narrow) {
                const bool had = touch_hi_[y] > touch_lo_[y];
                int ulo = covers ? lo : kSize;
                int uhi = covers ? hi : 0;
                if (had) {
                    ulo = touch_lo_[y] < ulo ? touch_lo_[y] : ulo;
                    uhi = touch_hi_[y] > uhi ? touch_hi_[y] : uhi;
                }
                x0 = ulo > x0 ? ulo : x0;
                x1 = uhi < x1 ? uhi : x1;
            }
            touch_lo_[y] = static_cast<int16_t>(covers ? lo : 0);
            touch_hi_[y] = static_cast<int16_t>(covers ? hi : 0);
        }
        if (x1 <= x0) {
            continue;
        }
        // The columns inside the ring's inner circle are never lit by it: a ring-only redraw leaves them.
        const int hole = skip_hole ? ring_hole_[y] : 0;
        if (hole > 0) {
            compose(y, x0, x1 < kCenter - hole ? x1 : kCenter - hole);
            compose(y, x0 > kCenter + hole ? x0 : kCenter + hole, x1);
        } else {
            compose(y, x0, x1);
        }
    }
}

int MascotScene::Render(uint32_t now_ms, SceneRect* dirty, int max_dirty) {
    if (clock_ == nullptr) {
        profiling_ = false;
        return RenderImpl(now_ms, dirty, max_dirty);
    }
    ++profile_.renders;
    profiling_ = (profile_tick_++ % kProfileEvery) == 0;
    const uint32_t t0 = profiling_ ? clock_() : 0;
    const int count = RenderImpl(now_ms, dirty, max_dirty);
    if (profiling_) {
        profile_.total_us += clock_() - t0;
        ++profile_.sampled;
        profiling_ = false;
    }
    return count;
}

int MascotScene::RenderImpl(uint32_t now_ms, SceneRect* dirty, int max_dirty) {
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
    composed_px_ = 0;
    const uint32_t ta = profiling_ ? clock_() : 0;
    UpdateActor(now_ms);
    const Placement p = Compute(now_ms);
    if (intro_active(now_ms)) {
        PrepareIntro(now_ms);
    }
    if (profiling_) {
        profile_.actor_us += clock_() - ta;
    }

    int count = 0;
    const bool intro_changed = p.intro_step != last_.intro_step;
    if (full_redraw_ || (intro_changed && (p.intro_step >= 0 || last_.intro_step >= 0))) {
        full_redraw_ = false;
        dirty[0] = Screen();
        const uint32_t tf = profiling_ ? clock_() : 0;
        RedrawRect(dirty[0], p, now_ms);
        if (profiling_) {
            profile_.rect_us += clock_() - tf;
        }
        last_ = p;
        return 1;
    }
    const bool sprite_changed = p.sprite != last_.sprite || p.frame != last_.frame ||
                                p.xf != last_.xf || p.shadow_cx_q != last_.shadow_cx_q ||
                                p.shadow_cy_q != last_.shadow_cy_q ||
                                p.shadow_rx_q != last_.shadow_rx_q ||
                                p.shadow_alpha != last_.shadow_alpha;
    const bool ring_changed = p.ring_mode != last_.ring_mode || p.ring_level != last_.ring_level;
    SceneRect rects[kMaxDirty];
    bool ring_only[kMaxDirty] = {};
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
        const int strips = RingStrips(rects + count);
        for (int i = 0; i < strips; ++i) {
            ring_only[count + i] = true;
        }
        count += strips;
    }
    const bool overflow = count > max_dirty;
    if (overflow) {
        count = 1;
        rects[0] = Screen();
        ring_only[0] = false;
    }
    const uint32_t tr = profiling_ ? clock_() : 0;
    for (int i = 0; i < count; ++i) {
        RedrawRect(rects[i], p, now_ms, ring_only[i], !ring_only[i] && !overflow);
        dirty[i] = rects[i];
    }
    if (profiling_) {
        profile_.rect_us += clock_() - tr;
    }
    last_ = p;
    return count;
}

}  // namespace memoria
