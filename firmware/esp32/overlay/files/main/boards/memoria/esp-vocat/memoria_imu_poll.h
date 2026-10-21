#pragma once

#include <cstdint>
#include <cstdlib>

namespace memoria {

struct ImuAccelSample {
    std::int16_t x = 0;
    std::int16_t y = 0;
    std::int16_t z = 0;
};

// One BMI270 accel frame per 20 ms. A failed I2C read drops that frame only:
// there is no retry and no extra wait, and the next frame reads normally.
// The delta baseline is cleared so the first sample after a gap cannot score
// against a stale sample. Host-tested.
class ImuPoller final {
public:
    static constexpr std::int64_t kStatsLogIntervalMs = 60000;

    template <typename ReadFn, typename ScoreFn>
    void PollFrame(ReadFn read, ScoreFn on_score) {
        ImuAccelSample cur;
        if (!read(cur)) {
            ++fail_;
            has_prev_ = false;
            return;
        }
        ++ok_;
        if (!has_prev_) {
            prev_ = cur;
            has_prev_ = true;
            return;
        }
        const int score = std::abs(static_cast<int>(cur.x) - static_cast<int>(prev_.x)) +
                          std::abs(static_cast<int>(cur.y) - static_cast<int>(prev_.y)) +
                          std::abs(static_cast<int>(cur.z) - static_cast<int>(prev_.z));
        prev_ = cur;
        on_score(score);
    }

    bool StatsLogDue(std::int64_t now_ms) {
        if (now_ms - last_stats_ms_ < kStatsLogIntervalMs) {
            return false;
        }
        last_stats_ms_ = now_ms;
        return true;
    }

    std::uint32_t ok() const { return ok_; }
    std::uint32_t fail() const { return fail_; }
    bool has_prev() const { return has_prev_; }

private:
    ImuAccelSample prev_{};
    bool has_prev_ = false;
    std::uint32_t ok_ = 0;
    std::uint32_t fail_ = 0;
    std::int64_t last_stats_ms_ = 0;
};

}  // namespace memoria
