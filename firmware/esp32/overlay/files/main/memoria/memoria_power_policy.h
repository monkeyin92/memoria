#pragma once

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

enum class TapDevice : unsigned char {
    kStarting,
    kIdle,
    kBusy,
    kListeningClosed,
};

enum class TapWakeAction : unsigned char {
    kIgnore,
    kStartConversation,
    kNightSoothing,
    kOfflineMenu,
    kWifiConfig,
    kResumeListening,
};

struct TapFacts {
    TapDevice device = TapDevice::kBusy;
    bool in_quiet_hours = false;
    bool offline_wants_tap = false;
    bool online = false;
};

struct PowerPolicy {
    static constexpr std::uint32_t kNoSpeechEndMs = 15 * 1000;
    static constexpr std::uint32_t kDefaultWifiHoldMs = 3 * 60 * 1000;
    static constexpr std::uint32_t kTapPromptDeadlineMs = 1000;
    static constexpr std::uint32_t kConversationReadyTargetMs = 5000;
    static constexpr std::uint32_t kTapDebounceMs = 250;
    static constexpr std::uint32_t kIdleScreenOffMs = 15 * 1000;
};

inline TapWakeAction TapWakeActionFor(TapFacts facts) {
    if (facts.online) {
        return TapWakeAction::kIgnore;
    }
    if (facts.device == TapDevice::kStarting) {
        return facts.offline_wants_tap ? TapWakeAction::kOfflineMenu : TapWakeAction::kWifiConfig;
    }
    if (facts.device == TapDevice::kListeningClosed) {
        return TapWakeAction::kResumeListening;
    }
    if (facts.device == TapDevice::kIdle && facts.in_quiet_hours) {
        return TapWakeAction::kNightSoothing;
    }
    if (facts.offline_wants_tap) {
        return TapWakeAction::kOfflineMenu;
    }
    if (facts.device == TapDevice::kIdle) {
        return TapWakeAction::kStartConversation;
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
        case TapWakeAction::kNightSoothing:
            return "night";
        case TapWakeAction::kOfflineMenu:
            return "offline";
        case TapWakeAction::kWifiConfig:
            return "wifi_config";
        case TapWakeAction::kResumeListening:
            return "resume";
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
