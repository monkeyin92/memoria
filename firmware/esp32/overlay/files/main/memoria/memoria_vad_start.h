#pragma once

#include <atomic>
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

// Latest uplink frame RMS, relative full scale, written by the Opus encode task
// before the packet reaches the protocol. One writer, one reader.
inline std::atomic<float> uplink_frame_rms{0.0f};

// The AFE reports the start edge on its first speaking frame, and that frame is
// much quieter than the rest of the utterance for weak speech (field log:
// 0.0007..0.0019 at the edge, 0.0031..0.0088 on the frames that followed). A
// start below the floor therefore holds the loudest frame for 250 ms and
// decides on that peak instead of rejecting the whole utterance on frame one.
class VadStartGate {
public:
    static constexpr uint64_t kPeakWindowMs = 250;

    VadEdgeAction Edge(bool epoch_open, bool speaking, float rms, float floor, uint64_t now_ms) {
        if (awaiting_ && !speaking) {
            awaiting_ = false;
            return VadEdgeAction::kSuppressStart;
        }
        if (speaking == epoch_open) {
            return VadEdgeAction::kIgnore;
        }
        if (!speaking) {
            return VadEdgeAction::kReport;
        }
        if (rms >= floor) {
            awaiting_ = false;
            peak_ = rms;
            return VadEdgeAction::kReport;
        }
        if (!awaiting_) {
            awaiting_ = true;
            started_ms_ = now_ms;
            peak_ = rms;
        } else if (rms > peak_) {
            peak_ = rms;
        }
        return VadEdgeAction::kAwaitPeak;
    }

    VadEdgeAction Frame(float rms, float floor, uint64_t now_ms) {
        if (!awaiting_) {
            return VadEdgeAction::kIgnore;
        }
        if (rms > peak_) {
            peak_ = rms;
        }
        if (peak_ >= floor) {
            awaiting_ = false;
            return VadEdgeAction::kReport;
        }
        if (now_ms - started_ms_ >= kPeakWindowMs) {
            awaiting_ = false;
            return VadEdgeAction::kSuppressStart;
        }
        return VadEdgeAction::kAwaitPeak;
    }

    bool awaiting() const { return awaiting_; }

    float peak() const { return peak_; }

    void Reset() {
        awaiting_ = false;
        started_ms_ = 0;
        peak_ = 0.0f;
    }

private:
    bool awaiting_ = false;
    uint64_t started_ms_ = 0;
    float peak_ = 0.0f;
};

}  // namespace memoria
