#ifndef MEMORIA_AIGC_LABEL_H
#define MEMORIA_AIGC_LABEL_H

#include <cstdint>

namespace memoria {

// Explicit AIGC label for live dialogue (GB 45438-2025 / art.4).
// Primary: persistent 「AI」 corner badge while a conversation is active.
// Fallback: short end-of-turn tone only when the badge cannot render, gated
// by kAigcTurnToneEnabled (default off). No per-session spoken notice.

inline constexpr bool kAigcTurnToneEnabled = false;

// Basename files for spoken notices (ids live in memoria_offline_menu.h).
inline constexpr const char* kAigcNoticeStoryFile = "aigc-notice-story.ogg";
inline constexpr const char* kAigcNoticeBedtimeFile = "aigc-notice-bedtime.ogg";

enum class AigcExplicitMode : std::uint8_t {
    kNone = 0,
    kBadge = 1,
    kTurnTone = 2,
};

inline AigcExplicitMode AigcExplicitModeFor(bool conversation_active, bool badge_can_render) {
    if (!conversation_active) {
        return AigcExplicitMode::kNone;
    }
    if (badge_can_render) {
        return AigcExplicitMode::kBadge;
    }
    return kAigcTurnToneEnabled ? AigcExplicitMode::kTurnTone : AigcExplicitMode::kNone;
}

inline bool AigcBadgeVisible(AigcExplicitMode mode) {
    return mode == AigcExplicitMode::kBadge;
}

inline bool AigcTurnToneWanted(AigcExplicitMode mode) {
    return mode == AigcExplicitMode::kTurnTone;
}

}  // namespace memoria

#endif
