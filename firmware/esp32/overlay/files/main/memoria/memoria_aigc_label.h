#ifndef MEMORIA_AIGC_LABEL_H
#define MEMORIA_AIGC_LABEL_H

#include <cstdint>

namespace memoria {

// Explicit AIGC label for live dialogue (GB 45438-2025 / art.4).
// Primary: persistent 「AI」 corner badge while a conversation is active,
// including stuck → offline menu. Fallback: short end-of-turn tone only when
// the badge cannot render, gated by kAigcTurnToneEnabled (default off).

inline constexpr bool kAigcTurnToneEnabled = false;

// Basename files for spoken notices (ids live in memoria_offline_menu.h).
inline constexpr const char* kAigcNoticeStoryFile = "aigc_story_intro.ogg";
inline constexpr const char* kAigcNoticeBedtimeFile = "aigc_soothe_intro.ogg";

// 360x360 round panel. Every badge corner must stay within radius-8 of centre.
inline constexpr int kAigcBadgePanelSize = 360;
inline constexpr int kAigcBadgePanelRadius = 180;
inline constexpr int kAigcBadgeEdgeMarginPx = 8;
inline constexpr int kAigcBadgeMaxCornerRadius = kAigcBadgePanelRadius - kAigcBadgeEdgeMarginPx;

// ~1 o'clock, hugging the inner edge, clear of eyes/mouth. Sized for ~20px "AI".
inline constexpr int kAigcBadgeX = 228;
inline constexpr int kAigcBadgeY = 40;
inline constexpr int kAigcBadgeW = 50;
inline constexpr int kAigcBadgeH = 30;
inline constexpr int kAigcBadgeFontPx = 20;
inline constexpr int kAigcBadgeCornerRadius = 12;

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

inline constexpr std::int64_t AigcBadgeCornerDistSq(int x, int y) {
    const std::int64_t dx = static_cast<std::int64_t>(x) - kAigcBadgePanelSize / 2;
    const std::int64_t dy = static_cast<std::int64_t>(y) - kAigcBadgePanelSize / 2;
    return dx * dx + dy * dy;
}

// True when the layout rectangle's four corners sit inside the safe circle.
inline constexpr bool AigcBadgeCornersInsideSafeCircle(int x0, int y0, int w, int h) {
    const std::int64_t limit =
        static_cast<std::int64_t>(kAigcBadgeMaxCornerRadius) * kAigcBadgeMaxCornerRadius;
    const int x1 = x0 + w;
    const int y1 = y0 + h;
    return AigcBadgeCornerDistSq(x0, y0) <= limit && AigcBadgeCornerDistSq(x1, y0) <= limit &&
           AigcBadgeCornerDistSq(x0, y1) <= limit && AigcBadgeCornerDistSq(x1, y1) <= limit;
}

inline constexpr bool AigcBadgeLayoutInsideSafeCircle() {
    return AigcBadgeCornersInsideSafeCircle(kAigcBadgeX, kAigcBadgeY, kAigcBadgeW, kAigcBadgeH);
}

static_assert(AigcBadgeLayoutInsideSafeCircle(),
              "AIGC badge layout must keep every corner within radius-8 of the panel centre");

}  // namespace memoria

#endif
