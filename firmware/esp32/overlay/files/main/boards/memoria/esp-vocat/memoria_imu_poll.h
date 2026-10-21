#pragma once

#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>

namespace memoria {

struct ImuAccelSample {
    std::int16_t x = 0;
    std::int16_t y = 0;
    std::int16_t z = 0;
};

// Counted per device state so a bus fault is not averaged away by quiet
// states.
enum ImuStatState : int {
    kImuStatSpeaking = 0,
    kImuStatListening,
    kImuStatIdle,
    kImuStatOther,
    kImuStatCount,
};

inline int ImuStatStateFromDeviceState(int state) {
    // kDeviceStateIdle/Listening/Speaking; this header stays free of
    // upstream headers so host tests can drive it.
    switch (state) {
        case 3:
            return kImuStatIdle;
        case 6:
            return kImuStatListening;
        case 7:
            return kImuStatSpeaking;
        default:
            return kImuStatOther;
    }
}

inline const char* ImuStatStateName(int state) {
    switch (state) {
        case kImuStatSpeaking:
            return "speaking";
        case kImuStatListening:
            return "listening";
        case kImuStatIdle:
            return "idle";
        default:
            return "other";
    }
}

struct ImuStatCounters {
    std::uint32_t ok[kImuStatCount] = {};
    std::uint32_t fail[kImuStatCount] = {};
};

// One BMI270 accel frame per 20 ms. A failed I2C read drops that frame only:
// there is no retry and no extra wait, and the next frame reads normally.
// The delta baseline is cleared so the first sample after a gap cannot score
// against a stale sample. Host-tested.
class ImuPoller final {
public:
    static constexpr std::int64_t kStatsLogIntervalMs = 60000;

    template <typename ReadFn, typename ScoreFn>
    void PollFrameInState(int device_state, ReadFn read, ScoreFn on_score) {
        const int state = ImuStatStateFromDeviceState(device_state);
        ImuAccelSample cur;
        if (!read(cur)) {
            ++counters_.fail[state];
            has_prev_ = false;
            return;
        }
        ++counters_.ok[state];
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

    // Each log line covers one window, not the whole boot.
    void TakeStats(ImuStatCounters& out) {
        out = counters_;
        counters_ = ImuStatCounters{};
    }

    const ImuStatCounters& counters() const { return counters_; }
    bool has_prev() const { return has_prev_; }

private:
    ImuAccelSample prev_{};
    bool has_prev_ = false;
    ImuStatCounters counters_{};
    std::int64_t last_stats_ms_ = 0;
};

inline std::size_t FormatImuStatsLine(const ImuStatCounters& counters, char* buf,
                                      std::size_t size) {
    int written = std::snprintf(buf, size, "BMI2 stats");
    if (written < 0) {
        return 0;
    }
    std::size_t used = static_cast<std::size_t>(written);
    for (int state = 0; state < kImuStatCount && used < size; ++state) {
        const int n = std::snprintf(buf + used, size - used, " %s ok=%u fail=%u",
                                    ImuStatStateName(state),
                                    static_cast<unsigned>(counters.ok[state]),
                                    static_cast<unsigned>(counters.fail[state]));
        if (n < 0) {
            break;
        }
        used += static_cast<std::size_t>(n);
    }
    return used < size ? used : size - 1;
}

}  // namespace memoria
