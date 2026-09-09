#ifndef MEMORIA_PAT_H
#define MEMORIA_PAT_H

#include <cstdint>

namespace memoria {

// BMI270 body-pat detector. Host-compilable on purpose so the exact firmware
// state machine can be tested without ESP-IDF.
//
// A body pat is a short accel impulse. Screen-tap rumble is cancelled by the
// CST816S mute window. Sustained shake is ignored. None of these start chat.

enum class PatKind : std::uint8_t {
    kNone = 0,
    kPat = 1,
    kShake = 2,
    kTouchRumble = 3,
};

struct PatResult {
    PatKind kind = PatKind::kNone;
    int peak = 0;
    int streak = 0;
};

struct PatDetector {
    // Sample-to-sample |dx|+|dy|+|dz| of BMI270 raw accel at 20 ms.
    // The 6000 / 2-sample / quiet-floor detector missed real body pats.
    // The 4000 any-sample detector fired on screen rumble and shakes.
    static constexpr int kDeltaThreshold = 3200;
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

    PatResult Observe(int score, bool muted, std::int64_t now_ms) {
        PatResult result;

        if (muted) {
            const bool cancel_pat = pending_ticks_ > 0 ||
                                    (high_streak_ >= 1 && high_streak_ <= kPulseMaxSamples);
            const int cancelled_peak = pending_peak_ > 0 ? pending_peak_ : peak_;
            const int cancelled_streak = pending_streak_ > 0 ? pending_streak_ : high_streak_;
            Reset();
            if (cancel_pat && cancelled_peak > 0) {
                result.kind = PatKind::kTouchRumble;
                result.peak = cancelled_peak;
                result.streak = cancelled_streak;
            }
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
            }
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

}  // namespace memoria

#endif  // MEMORIA_PAT_H
