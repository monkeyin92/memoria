#pragma once

// How the bound device may be woken, as the phone's wake-mode setting says.
// The wire strings are the server's device setting `wake_mode`, delivered with
// the idle display-profile poll:
//   "keyword"            the wake word only; a tap on the screen does nothing
//   "button"             a tap on the screen only; the wake word is off
//   "button_or_keyword"  both
// The stored value keeps the name "button" (the contract and the Edge validate
// it), but on this board it means the round screen. The BOOT key is always
// live regardless: it is the hard stop and the pairing entry.
//
// This header has no ESP-IDF dependency so the host tests compile the real
// policy; the NVS-backed registry lives in memoria_wake_mode.cc.

#include <atomic>
#include <cstring>
#include <string>

namespace memoria {

enum class WakeMode : unsigned char { kKeywordOnly, kTapOnly, kBoth };

// Unknown text is rejected so an old or confused server cannot flip the mode.
inline bool ParseWakeMode(const char* text, WakeMode* mode) {
    if (text == nullptr || mode == nullptr) {
        return false;
    }
    if (std::strcmp(text, "keyword") == 0) {
        *mode = WakeMode::kKeywordOnly;
        return true;
    }
    if (std::strcmp(text, "button") == 0) {
        *mode = WakeMode::kTapOnly;
        return true;
    }
    if (std::strcmp(text, "button_or_keyword") == 0) {
        *mode = WakeMode::kBoth;
        return true;
    }
    return false;
}

inline const char* WakeModeName(WakeMode mode) {
    switch (mode) {
        case WakeMode::kKeywordOnly:
            return "keyword";
        case WakeMode::kTapOnly:
            return "button";
        case WakeMode::kBoth:
            return "button_or_keyword";
    }
    return "button_or_keyword";
}

inline bool KeywordWakeEnabled(WakeMode mode) { return mode != WakeMode::kTapOnly; }
inline bool TapWakeEnabled(WakeMode mode) { return mode != WakeMode::kKeywordOnly; }

// A released tap on the round screen, once pairing and start-up (which the
// board handles first) are out of the way. Only a tap on an idle device in a
// mode that includes the screen starts a conversation. In every other state
// and mode it does nothing, so a tap can neither stop the robot talking nor
// send it back to standby.
enum class TapAction : unsigned char { kIgnore, kStartConversation };

inline TapAction TapActionFor(WakeMode mode, bool device_idle) {
    return device_idle && TapWakeEnabled(mode) ? TapAction::kStartConversation
                                                : TapAction::kIgnore;
}

class WakeModeRegistry {
public:
    static WakeModeRegistry& GetInstance();

    WakeMode mode() const { return mode_.load(std::memory_order_relaxed); }
    bool keyword_enabled() const { return KeywordWakeEnabled(mode()); }
    bool tap_enabled() const { return TapWakeEnabled(mode()); }

    // Restore the last applied mode (boot, before the first poll answers).
    void LoadFromNvs();
    // Apply the server's answer and remember it. True when the mode changed.
    bool Apply(const std::string& wire);

private:
    // Both ways on until told otherwise: the server's default, and what a
    // device that never received a mode should offer.
    std::atomic<WakeMode> mode_{WakeMode::kBoth};
};

}  // namespace memoria
