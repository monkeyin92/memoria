#pragma once

#include <cstdint>

namespace memoria {

// Keeps the animation task from eating the device (TODOLIST M-6). The mascot scene asks for a frame every
// 40 ms in every lit state, so the breathing moves in sub-pixel steps; whether the chip can afford that
// depends on what a frame costs on the real panel, which is only known by measuring it. The pacer measures:
// it follows the time one drawn frame keeps the task busy (waiting for the display lock included, since
// that is LVGL still flushing the frame before) and stretches the interval in 10 ms steps while a frame
// takes more than 65 % of it. It gives the time back slowly, once frames have fit comfortably for about a
// second, so the cadence does not wobble around the limit.
//
// Integer arithmetic only, no ESP-IDF dependency: host-tested like the rest of the compositor.
class FramePacer final {
public:
    static constexpr uint32_t kBudgetPercent = 65;  // a frame may take this much of its interval
    static constexpr uint32_t kRelaxPercent = 55;   // and may be given back below this, at the shorter one
    static constexpr uint32_t kStepMs = 10;
    static constexpr uint32_t kMaxExtraMs = 120;
    static constexpr uint32_t kRelaxAfterCalls = 25;

    // A drawn frame kept the task busy for busy_us.
    void NoteDrawn(uint32_t busy_us) {
        if (!primed_) {
            busy_us_ = busy_us;
            primed_ = true;
            return;
        }
        // An average over about eight frames: one slow frame (a companion switch, a flash write) is not a
        // reason to drop the frame rate.
        busy_us_ = static_cast<uint32_t>(static_cast<int64_t>(busy_us_) +
                                         (static_cast<int64_t>(busy_us) - static_cast<int64_t>(busy_us_)) / 8);
    }

    // How long to wait before the next frame when the scene would like wanted_ms. Called once per loop.
    uint32_t NextIntervalMs(uint32_t wanted_ms) {
        const uint64_t busy = busy_us_;
        const uint64_t interval_us = static_cast<uint64_t>(wanted_ms + extra_ms_) * 1000u;
        if (busy * 100u > interval_us * kBudgetPercent) {
            if (extra_ms_ < kMaxExtraMs) {
                extra_ms_ += kStepMs;
            }
            calm_calls_ = 0;
        } else if (extra_ms_ > 0) {
            const uint64_t shorter_us = static_cast<uint64_t>(wanted_ms + extra_ms_ - kStepMs) * 1000u;
            if (busy * 100u < shorter_us * kRelaxPercent) {
                if (++calm_calls_ >= kRelaxAfterCalls) {
                    extra_ms_ -= kStepMs;
                    calm_calls_ = 0;
                }
            } else {
                calm_calls_ = 0;
            }
        }
        return wanted_ms + extra_ms_;
    }

    uint32_t extra_ms() const { return extra_ms_; }
    uint32_t busy_us() const { return busy_us_; }

private:
    bool primed_ = false;
    uint32_t busy_us_ = 0;
    uint32_t extra_ms_ = 0;
    uint32_t calm_calls_ = 0;
};

}  // namespace memoria
