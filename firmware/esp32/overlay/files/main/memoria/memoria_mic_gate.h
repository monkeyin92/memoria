#pragma once

// Mic open/close decision from PowerPhase. Host-compilable. Firmware logs the
// policy edge at MemoriaMic; AudioService still drives EnableInput.

#include <cstdint>

#include "memoria_power_policy.h"

namespace memoria {

enum class MicGateAction : unsigned char { kNoChange, kOpen, kClose };

inline MicGateAction MicGateTransition(bool currently_open, PowerPhase phase) {
    const bool wanted = MicWantedOpen(phase);
    if (wanted == currently_open) {
        return MicGateAction::kNoChange;
    }
    return wanted ? MicGateAction::kOpen : MicGateAction::kClose;
}

inline const char* MicGateActionName(MicGateAction action) {
    switch (action) {
        case MicGateAction::kNoChange:
            return "noop";
        case MicGateAction::kOpen:
            return "open";
        case MicGateAction::kClose:
            return "close";
    }
    return "unknown";
}

inline constexpr char kMicGateLogTag[] = "MemoriaMic";

}  // namespace memoria
