#pragma once

// Tap-only wake for Memoria ESP-VoCat. Voice keyword wake is removed from the
// product. The wire string remains `button` so older control-plane rows and
// display-profile polls stay compatible; any other value is rejected and the
// device keeps tap-only behaviour.
//
// Host-compilable. NVS registry lives in memoria_wake_mode.cc.

#include <atomic>
#include <cstring>
#include <string>

#include "memoria_power_policy.h"

namespace memoria {

enum class WakeMode : unsigned char { kTapOnly };

inline bool ParseWakeMode(const char* text, WakeMode* mode) {
    if (text == nullptr || mode == nullptr) {
        return false;
    }
    // Legacy keyword modes coerce to tap-only so a rolling OTA cannot stick a
    // device on a removed wake path.
    if (std::strcmp(text, "button") == 0 || std::strcmp(text, "keyword") == 0 ||
        std::strcmp(text, "button_or_keyword") == 0) {
        *mode = WakeMode::kTapOnly;
        return true;
    }
    return false;
}

inline const char* WakeModeName(WakeMode) { return "button"; }

inline bool KeywordWakeEnabled(WakeMode) { return false; }
inline bool TapWakeEnabled(WakeMode) { return true; }

// Kept name used by USB and board call sites; maps onto the power-policy action.
using TapAction = TapWakeAction;

inline TapWakeAction TapActionFor(WakeMode, bool device_idle) {
    // Pre-phase call sites: idle starts a conversation; busy ignores.
    return device_idle ? TapWakeAction::kStartConversation : TapWakeAction::kIgnore;
}

class WakeModeRegistry {
public:
    static WakeModeRegistry& GetInstance();

    WakeMode mode() const { return mode_.load(std::memory_order_relaxed); }
    bool keyword_enabled() const { return false; }
    bool tap_enabled() const { return true; }

    void LoadFromNvs();
    // Apply the server's answer. Always stores tap-only. True when NVS wrote.
    bool Apply(const std::string& wire);

private:
    std::atomic<WakeMode> mode_{WakeMode::kTapOnly};
};

}  // namespace memoria
