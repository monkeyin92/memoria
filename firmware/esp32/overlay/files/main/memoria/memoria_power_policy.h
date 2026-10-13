#pragma once

// Product power and wake policy for Memoria ESP-VoCat (tap wake only).
// Decision helpers are host-compilable. The NVS-backed registry lives in
// memoria_power_policy.cc.

#include <atomic>
#include <cstdint>
#include <cstring>

#include "memoria_quiet_hours.h"

namespace memoria {

enum class PowerPhase : unsigned char {
    kConversing,
    kWifiHold,
    kDeepSleep,
    kWaking,
    kNightSoothing,
};

enum class TapWakeAction : unsigned char {
    kIgnore,
    kStartConversation,
    kResumeFromHold,
    kNightSoothing,
};

struct PowerPolicy {
    static constexpr std::uint32_t kNoSpeechEndMs = 15 * 1000;
    static constexpr std::uint32_t kDefaultWifiHoldMs = 3 * 60 * 1000;
    static constexpr std::uint32_t kTapPromptDeadlineMs = 1000;
    static constexpr std::uint32_t kConversationReadyTargetMs = 5000;
    static constexpr std::uint32_t kTapDebounceMs = 250;
    static constexpr std::uint32_t kIdleScreenOffMs = 15 * 1000;
};

inline TapWakeAction TapWakeActionFor(PowerPhase phase, bool /*device_idle*/, bool in_quiet_hours) {
    switch (phase) {
        case PowerPhase::kDeepSleep:
            return in_quiet_hours ? TapWakeAction::kNightSoothing
                                  : TapWakeAction::kStartConversation;
        case PowerPhase::kWifiHold:
            return in_quiet_hours ? TapWakeAction::kNightSoothing
                                  : TapWakeAction::kResumeFromHold;
        case PowerPhase::kWaking:
        case PowerPhase::kNightSoothing:
        case PowerPhase::kConversing:
            return TapWakeAction::kIgnore;
    }
    return TapWakeAction::kIgnore;
}

inline bool MicWantedOpen(PowerPhase phase) {
    return phase == PowerPhase::kConversing || phase == PowerPhase::kWaking;
}

inline const char* PowerPhaseName(PowerPhase phase) {
    switch (phase) {
        case PowerPhase::kConversing:
            return "conversing";
        case PowerPhase::kWifiHold:
            return "wifi_hold";
        case PowerPhase::kDeepSleep:
            return "deep_sleep";
        case PowerPhase::kWaking:
            return "waking";
        case PowerPhase::kNightSoothing:
            return "night_soothing";
    }
    return "unknown";
}

inline const char* TapWakeActionName(TapWakeAction action) {
    switch (action) {
        case TapWakeAction::kIgnore:
            return "ignore";
        case TapWakeAction::kStartConversation:
            return "start";
        case TapWakeAction::kResumeFromHold:
            return "resume";
        case TapWakeAction::kNightSoothing:
            return "night";
    }
    return "unknown";
}

inline bool TapDebounceAllows(std::uint32_t now_ms, std::uint32_t last_accepted_ms,
                              std::uint32_t debounce_ms = PowerPolicy::kTapDebounceMs) {
    return last_accepted_ms == 0 || (now_ms - last_accepted_ms) >= debounce_ms;
}

class PowerPolicyRegistry {
public:
    static PowerPolicyRegistry& GetInstance();

    PowerPhase phase() const { return phase_.load(std::memory_order_relaxed); }
    void SetPhase(PowerPhase phase);

    std::uint32_t wifi_hold_ms() const {
        return wifi_hold_ms_.load(std::memory_order_relaxed);
    }

    void LoadFromNvs();
    bool ApplyQuietHours(const char* start_hhmm, const char* end_hhmm);
    QuietHoursWindow quiet_hours() const { return quiet_; }
    bool in_quiet_hours(std::uint16_t now_minute) const;

    std::uint32_t last_tap_ms() const { return last_tap_ms_.load(std::memory_order_relaxed); }
    bool NoteTap(std::uint32_t now_ms);

private:
    std::atomic<PowerPhase> phase_{PowerPhase::kWifiHold};
    std::atomic<std::uint32_t> wifi_hold_ms_{PowerPolicy::kDefaultWifiHoldMs};
    std::atomic<std::uint32_t> last_tap_ms_{0};
    QuietHoursWindow quiet_{};
};

}  // namespace memoria
