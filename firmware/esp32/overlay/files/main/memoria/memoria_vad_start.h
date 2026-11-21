#pragma once

#include <atomic>
#include <cmath>
#include <cstdint>

namespace memoria {

enum class VadEdgeAction {
    kIgnore,
    kSuppressStart,
    kAwaitPeak,
    kReport,
};

inline float VadStartRmsFloorFromMilli(int milli) {
    return static_cast<float>(milli) / 1000.0f;
}

// Tracks steady ambient idle RMS so sustained mid-band room noise (~-40 dBFS,
// field 2026-11-21 soak-1244: idle rms p50=0.0013/p90=0.0034, 21 false overlong
// VAD epochs at rms 0.0030..0.0053) lifts the start floor above the noise band
// without altering quiet-room weak speech (soak-1534: idle rms p50=0.0002/p90=0.0007,
// effective floor stays at the 0.0030 base).
class VadNoiseFloor {
public:
    static constexpr float kSampleCap = 0.0030f;
    static constexpr uint32_t kWarmupFrames = 25;  // 0.5 s exact mean removes cold-start zero bias
    static constexpr float kAlphaUp = 0.008f;      // ~2.5 s tau at 50 Hz: ignores brief transients
    static constexpr float kAlphaDown = 0.050f;    // ~0.4 s tau at 50 Hz: recovers fast when quiet
    static constexpr float kNoiseMultiplier = 3.5f;
    static constexpr float kMaxAdaptiveFloor = 0.0100f;

    void Observe(float rms) {
        if (!std::isfinite(rms) || rms < 0.0f) {
            return;
        }
        const float sample = rms > kSampleCap ? kSampleCap : rms;
        float alpha = sample > ambient_ ? kAlphaUp : kAlphaDown;
        if (samples_ < kWarmupFrames) {
            ++samples_;
            const float init_alpha = 1.0f / static_cast<float>(samples_);
            if (init_alpha > alpha) {
                alpha = init_alpha;
            }
        }
        ambient_ += alpha * (sample - ambient_);
    }

    float EffectiveFloor(float base_floor) const {
        if (!std::isfinite(base_floor) || base_floor <= 0.0f) {
            return 0.0f;
        }
        const float adapted = ambient_ * kNoiseMultiplier;
        const float max_floor = base_floor > kMaxAdaptiveFloor ? base_floor : kMaxAdaptiveFloor;
        if (adapted <= base_floor) {
            return base_floor;
        }
        return adapted > max_floor ? max_floor : adapted;
    }

    float ambient() const { return ambient_; }

    void Reset() {
        ambient_ = 0.0f;
        samples_ = 0;
    }

private:
    float ambient_ = 0.0f;
    uint32_t samples_ = 0;
};

// Latest uplink frame RMS, relative full scale, written by the Opus encode task
// before the packet reaches the protocol. One writer, one reader.
inline std::atomic<float> uplink_frame_rms{0.0f};
inline std::atomic<uint32_t> afe_vad_epoch{0};

inline uint32_t AdvanceAfeVadEpoch(bool playback_speech) {
    uint32_t previous = afe_vad_epoch.load();
    uint32_t next;
    do {
        next = ((previous & ~1U) + 2U) | static_cast<uint32_t>(playback_speech);
    } while (!afe_vad_epoch.compare_exchange_weak(previous, next));
    return next;
}

class PlaybackVadHandoff {
public:
    void Completed(bool exact) {
        *this = {};
        completed_ = exact;
    }

    bool Listening(uint32_t current, uint64_t now_ms) {
        const bool exact = completed_;
        Completed(false);
        listen_pending_ = exact;
        listening_ms_ = now_ms;
        return TryArm(current, now_ms);
    }

    bool TryArm(uint32_t current, uint64_t now_ms) {
        if (!listen_pending_) return false;
        if (now_ms - listening_ms_ > 250) {
            listen_pending_ = false;
        } else if (current & 1U) {
            listen_pending_ = false;
            epoch_ = current;
            return true;
        }
        return false;
    }

    bool Frame(bool eligible, uint64_t produced_ms, uint64_t now_ms, float rms, float floor) {
        if (epoch_ == 0) return false;
        if (!eligible || now_ms - listening_ms_ > 5000) {
            Completed(false);
            return false;
        }
        if (produced_ms < listening_ms_ + 250 || produced_ms > now_ms || now_ms - produced_ms > 250) {
            first_loud_ = false;
            return false;
        }
        if (rms < floor) {
            quiet_ = true;
            first_loud_ = false;
        } else if (quiet_) {
            if (first_loud_ && produced_ms - first_loud_ms_ <= 40) {
                Completed(false);
                return true;
            }
            first_loud_ms_ = produced_ms;
            first_loud_ = true;
        }
        return false;
    }

    uint32_t epoch() const { return epoch_; }
    bool pending() const { return listen_pending_; }

private:
    bool completed_ = false;
    bool listen_pending_ = false;
    bool quiet_ = false;
    bool first_loud_ = false;
    uint32_t epoch_ = 0;
    uint64_t listening_ms_ = 0;
    uint64_t first_loud_ms_ = 0;
};

// The AFE reports the start edge on its first speaking frame, and that frame is
// much quieter than the rest of the utterance for weak speech (field log:
// 0.0007..0.0019 at the edge, 0.0031..0.0088 on the frames that followed). A
// start below the floor therefore holds the loudest frame for 250 ms and
// decides on that peak instead of rejecting the whole utterance on frame one.
class VadStartGate {
public:
    static constexpr uint64_t kPeakWindowMs = 250;
    static constexpr uint64_t kMaxRecoveryAgeMs = 5000;

    VadEdgeAction Edge(bool epoch_open, bool speaking, float rms, float floor, uint64_t now_ms) {
        if (!speaking && recovering()) {
            Reset();
        }
        if (awaiting() && !speaking) {
            phase_ = Phase::kIdle;
            return VadEdgeAction::kSuppressStart;
        }
        if (speaking == epoch_open) {
            return VadEdgeAction::kIgnore;
        }
        if (!speaking) {
            return VadEdgeAction::kReport;
        }
        if (rms >= floor) {
            phase_ = Phase::kIdle;
            peak_ = rms;
            return VadEdgeAction::kReport;
        }
        if (!awaiting()) {
            phase_ = Phase::kPeakWindow;
            started_ms_ = now_ms;
            peak_ = rms;
        } else if (rms > peak_) {
            peak_ = rms;
        }
        return VadEdgeAction::kAwaitPeak;
    }

    VadEdgeAction Frame(float rms, float floor, uint64_t now_ms, bool may_recover) {
        if (recovering()) {
            if (!may_recover || now_ms - started_ms_ > kMaxRecoveryAgeMs) {
                Reset();
                return VadEdgeAction::kIgnore;
            }
            if (rms < floor) {
                phase_ = Phase::kWaitForSpeech;
            } else if (phase_ == Phase::kFirstLoudFrame) {
                phase_ = Phase::kIdle;
                peak_ = rms > peak_ ? rms : peak_;
                return VadEdgeAction::kReport;
            } else {
                phase_ = Phase::kFirstLoudFrame;
                peak_ = rms;
            }
            return VadEdgeAction::kIgnore;
        }
        if (!awaiting()) {
            return VadEdgeAction::kIgnore;
        }
        if (rms > peak_) {
            peak_ = rms;
        }
        if (peak_ >= floor) {
            phase_ = Phase::kIdle;
            return VadEdgeAction::kReport;
        }
        if (now_ms - started_ms_ >= kPeakWindowMs) {
            phase_ = may_recover ? Phase::kWaitForSpeech : Phase::kIdle;
            return VadEdgeAction::kSuppressStart;
        }
        return VadEdgeAction::kAwaitPeak;
    }

    bool awaiting() const { return phase_ == Phase::kPeakWindow; }
    bool recovering() const {
        return phase_ == Phase::kWaitForSpeech || phase_ == Phase::kFirstLoudFrame;
    }

    float peak() const { return peak_; }

    void Reset() {
        phase_ = Phase::kIdle;
        started_ms_ = 0;
        peak_ = 0.0f;
    }

private:
    enum class Phase { kIdle, kPeakWindow, kWaitForSpeech, kFirstLoudFrame };
    Phase phase_ = Phase::kIdle;
    uint64_t started_ms_ = 0;
    float peak_ = 0.0f;
};

}  // namespace memoria
