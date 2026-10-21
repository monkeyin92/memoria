#ifndef MEMORIA_PAT_H
#define MEMORIA_PAT_H

#include <cstdint>

// The board includes this header after its own headers, which pull in the
// ESP-IDF integer typedefs; the host harness compiles it alone, so it needs
// the names from <cstdint> either way.
using std::int64_t;
using std::uint8_t;

namespace memoria {

// Body pat, shake and screen tap on the ESP-VoCat. Host-compilable on purpose
// so the exact firmware classifier and the product's reaction to it can be
// tested without ESP-IDF.
//
// A body pat is a short accel impulse. The BMI270's own boot self-calibration
// also produces one, at about 4100, and every "Device pat detected" line in
// the 2026-10 October field logs is that pulse at 1.2 s uptime. The first
// seconds after the IMU task starts are therefore a quiet window; a pat the
// child makes later still counts at the same 3200 floor the bench equipment
// needed. Screen-tap rumble is cancelled by the CST816S mute window.
// Sustained shake is ignored and only makes the companion dizzy.
//
// Product (2026-10-21): while the companion speaks, a pat or a screen tap
// stops the current sentence and goes back to listening; the companion and a
// short local clip give feedback right away. A crisis script plays to its end
// and ignores both.

enum class PatKind : std::uint8_t {
    kNone = 0,
    kPat = 1,
    kShake = 2,
    kTouchRumble = 3,
};

// Why a high sample did not become a pat.
enum class PatIgnore : std::uint8_t {
    kNone = 0,      // a pat pulse, or an ordinary sample
    kBootQuiet,     // the BMI270's own start-up pulse, not the child
    kTouchMute,     // inside the window after a screen touch
    kCooldown,      // a pat already fired within kCooldownMs
};

// How the classifier took one 20 ms sample.
enum PatReaction : std::uint8_t {
    kPatReactionNone = 0,
    kPatReactionBootQuiet = 1,  // high, but inside the IMU's start-up window
    kPatReactionImpulse = 2,    // a high sample that starts or feeds a pat pulse
};

struct PatResult {
    PatKind kind = PatKind::kNone;
    int peak = 0;
    int streak = 0;
    PatIgnore ignore = PatIgnore::kNone;
};

struct PatDetector {
    // Sample-to-sample |dx|+|dy|+|dz| of BMI270 raw accel at 20 ms.
    static constexpr int kDeltaThreshold = 3200;
    // The BMI270's start-up self-calibration peaks at about 4100 and clears
    // within the first second. Every field "Device pat detected" line is that
    // pulse (score 4083-4141, 1.2 s uptime), so ignoring this window removes
    // the one spurious pat that is documented, without raising the floor a
    // real pat has to beat.
    static constexpr std::int64_t kBootQuietMs = 5000;
    static constexpr int kPulseMaxSamples = 6;      // <= 120 ms
    static constexpr int kConfirmDelaySamples = 3;  // wait for touch mute
    static constexpr std::int64_t kCooldownMs = 2500;

    void Reset() {
        high_streak_ = 0;
        peak_ = 0;
        pending_ticks_ = 0;
        pending_peak_ = 0;
        pending_streak_ = 0;
        shake_logged_ = false;
    }

    PatResult Observe(int score, bool muted, std::int64_t now_ms, bool boot_quiet = false) {
        PatResult result;

        if (boot_quiet) {
            // The sensor's own start-up pulse. It must not arm the confirm
            // chain, or a pat right after boot would be swallowed by the
            // cooldown.
            Reset();
            if (score > kDeltaThreshold) {
                result.ignore = PatIgnore::kBootQuiet;
            }
            return result;
        }

        if (muted) {
            const int cancelled_peak = pending_peak_ > 0 ? pending_peak_ : peak_;
            const bool cancel_pat =
                pending_ticks_ > 0 || (high_streak_ >= 1 && high_streak_ <= kPulseMaxSamples);
            Reset();
            if (!cancel_pat) {
                result.ignore = PatIgnore::kTouchMute;
                return result;
            }
            result.kind = PatKind::kTouchRumble;
            result.peak = cancelled_peak;
            return result;
        }

        if (score > kDeltaThreshold) {
            high_streak_++;
            if (score > peak_) {
                peak_ = score;
            }
            if (high_streak_ > kPulseMaxSamples) {
                pending_ticks_ = 0;
                pending_peak_ = 0;
                pending_streak_ = 0;
                if (!shake_logged_) {
                    shake_logged_ = true;
                    result.kind = PatKind::kShake;
                    result.peak = peak_;
                    result.streak = high_streak_;
                }
                return result;
            }
            result.streak = high_streak_;
            return result;
        }

        if (high_streak_ >= 1 && high_streak_ <= kPulseMaxSamples) {
            pending_ticks_ = kConfirmDelaySamples;
            pending_peak_ = peak_;
            pending_streak_ = high_streak_;
        }
        high_streak_ = 0;
        peak_ = 0;
        shake_logged_ = false;

        if (pending_ticks_ > 0) {
            pending_ticks_--;
            if (pending_ticks_ == 0) {
                if (!has_pat_ || now_ms - last_pat_ms_ > kCooldownMs) {
                    has_pat_ = true;
                    last_pat_ms_ = now_ms;
                    result.kind = PatKind::kPat;
                    result.peak = pending_peak_;
                    result.streak = pending_streak_;
                } else {
                    result.ignore = PatIgnore::kCooldown;
                }
                pending_peak_ = 0;
                pending_streak_ = 0;
            }
        }
        return result;
    }

private:
    int high_streak_ = 0;
    int peak_ = 0;
    int pending_ticks_ = 0;
    int pending_peak_ = 0;
    int pending_streak_ = 0;
    bool shake_logged_ = false;
    bool has_pat_ = false;
    std::int64_t last_pat_ms_ = 0;
};

inline const char* PatEventName(PatKind kind) {
    switch (kind) {
        case PatKind::kPat:
            return "pat";
        case PatKind::kShake:
            return "shake";
        case PatKind::kTouchRumble:
            return "rumble";
        case PatKind::kNone:
        default:
            return "none";
    }
}

inline const char* PatIgnoreName(PatIgnore ignore) {
    switch (ignore) {
        case PatIgnore::kBootQuiet:
            return "boot_quiet";
        case PatIgnore::kTouchMute:
            return "touch_mute";
        case PatIgnore::kCooldown:
            return "cooldown";
        case PatIgnore::kNone:
        default:
            return "none";
    }
}

// One reason string for any sample the classifier did not turn into a pat.
inline const char* PatIgnoreReasonName(PatKind kind, PatIgnore ignore) {
    return kind == PatKind::kTouchRumble ? "touch_rumble" : PatIgnoreName(ignore);
}

inline const char* PatReactionName(int reaction) {
    switch (reaction) {
        case kPatReactionBootQuiet:
            return "boot_quiet";
        case kPatReactionImpulse:
            return "impulse";
        case kPatReactionNone:
        default:
            return "none";
    }
}

// What one sample means to the classifier. A start-up sample is not a pat.
inline int PatReactionFor(int score, bool boot_quiet = false) {
    if (score <= PatDetector::kDeltaThreshold) {
        return kPatReactionNone;
    }
    return boot_quiet ? kPatReactionBootQuiet : kPatReactionImpulse;
}

enum class PatGestureDecision : std::uint8_t {
    kNone = 0,
    kStopSpeaking = 1,        // stop the current sentence, companion listens
    kCrisisKeepPlayback = 2,  // a crisis script is playing: never interrupt
};

// DeviceState values, so this header stays independent of the firmware enum.
// The board static_asserts them against device_state.h at compile time.
enum PatDeviceState : int {
    kPatStateStarting = 1,
    kPatStateIdle = 3,
    kPatStateListening = 6,
    kPatStateSpeaking = 7,
};

struct PatGestureFacts {
    int state = kPatStateIdle;
    bool speaking_crisis_playback = false;
    bool is_pat = false;
};

inline PatGestureDecision PatGestureDecisionFor(const PatGestureFacts& facts) {
    if (!facts.is_pat) {
        return PatGestureDecision::kNone;
    }
    if (facts.speaking_crisis_playback) {
        return facts.state == kPatStateSpeaking ? PatGestureDecision::kCrisisKeepPlayback
                                                : PatGestureDecision::kNone;
    }
    return facts.state == kPatStateSpeaking || facts.state == kPatStateIdle
               ? PatGestureDecision::kStopSpeaking
               : PatGestureDecision::kNone;
}

inline const char* PatGestureDecisionName(PatGestureDecision decision) {
    switch (decision) {
        case PatGestureDecision::kStopSpeaking:
            return "stop_speaking";
        case PatGestureDecision::kCrisisKeepPlayback:
            return "crisis_keep_playback";
        case PatGestureDecision::kNone:
        default:
            return "none";
    }
}

// The touch task mutes the IMU while the screen is pressed, so a tap is
// decided on release. The board keeps its power-policy navigation (wake,
// offline menu, Wi-Fi setup); only a driven conversation element changes.
enum class ScreenTapDecision : std::uint8_t {
    kNone = 0,
    kTapNavigation = 1,  // power policy decides: wake, offline menu, Wi-Fi setup
    kStopSpeaking = 2,
    kCrisisKeepPlayback = 3,
};

struct TapGestureFacts {
    int state = kPatStateIdle;
    bool speaking_crisis_playback = false;
    bool wifi_configuring = false;
    bool offline_wants_tap = false;
};

inline ScreenTapDecision ScreenTapDecisionFor(const TapGestureFacts& facts) {
    if (facts.state == kPatStateSpeaking) {
        return facts.speaking_crisis_playback ? ScreenTapDecision::kCrisisKeepPlayback
                                              : ScreenTapDecision::kStopSpeaking;
    }
    if (facts.state == kPatStateIdle || facts.wifi_configuring ||
        facts.state == kPatStateStarting || facts.offline_wants_tap) {
        return ScreenTapDecision::kTapNavigation;
    }
    return ScreenTapDecision::kNone;
}

inline const char* ScreenTapDecisionName(ScreenTapDecision decision) {
    switch (decision) {
        case ScreenTapDecision::kTapNavigation:
            return "tap_navigation";
        case ScreenTapDecision::kStopSpeaking:
            return "stop_speaking";
        case ScreenTapDecision::kCrisisKeepPlayback:
            return "crisis_keep_playback";
        case ScreenTapDecision::kNone:
        default:
            return "none";
    }
}

}  // namespace memoria

#endif  // MEMORIA_PAT_H
