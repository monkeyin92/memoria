#ifndef MEMORIA_AIGC_LABEL_H
#define MEMORIA_AIGC_LABEL_H

#include <cstdint>

namespace memoria {

// Explicit AIGC label for live dialogue (GB 45438-2025 / art.4).
// Primary: persistent 「AI」 badge while the companion is on screen, idle and
// in conversation, including stuck → offline menu. Fallback: short end-of-turn
// tone only when the badge cannot render, gated by kAigcTurnToneEnabled
// (default off).

inline constexpr bool kAigcTurnToneEnabled = false;

// Basename files for spoken notices (ids live in memoria_offline_menu.h).
inline constexpr const char* kAigcNoticeStoryFile = "aigc_story_intro.ogg";
inline constexpr const char* kAigcNoticeBedtimeFile = "aigc_soothe_intro.ogg";

enum class AigcPlaybackStart : std::uint8_t {
    kNewStory = 0,  // long press starts a story from the top
    kResume = 1,    // paused story continues from where it stopped
    kReplay = 2,    // single-title loop restarts the same story
    kJumpIn = 3,    // playback cut into from another screen or channel
};

// Every start of a story or bedtime clip plays the notice first: a dimmed
// screen is all the label these paths have (GB 45438-2025 5.2/5.3).
inline constexpr const char* AigcNoticeFileFor(AigcPlaybackStart start) {
    switch (start) {
        case AigcPlaybackStart::kNewStory:
        case AigcPlaybackStart::kResume:
        case AigcPlaybackStart::kReplay:
        case AigcPlaybackStart::kJumpIn:
            return kAigcNoticeStoryFile;
    }
    return kAigcNoticeStoryFile;
}

inline constexpr const char* AigcBedtimeNoticeFile() { return kAigcNoticeBedtimeFile; }

// 360x360 round panel. Every badge corner stays at least 8 px inside the rim.
inline constexpr int kAigcBadgePanelSize = 360;
inline constexpr int kAigcBadgePanelRadius = 180;
inline constexpr int kAigcBadgeEdgeMarginPx = 8;
inline constexpr int kAigcBadgeMaxCornerRadius = kAigcBadgePanelRadius - kAigcBadgeEdgeMarginPx;

// Top-centre, inside the circle. 16 px "AI" with 6 px horizontal and 2 px
// vertical padding; the mascot stands below it (MascotScene::kRestFootY).
inline constexpr int kAigcBadgeX = 163;
inline constexpr int kAigcBadgeY = 10;
inline constexpr int kAigcBadgeW = 34;
inline constexpr int kAigcBadgeH = 22;
inline constexpr int kAigcBadgeFontPx = 16;
inline constexpr int kAigcBadgePadHorPx = 6;
inline constexpr int kAigcBadgePadVerPx = 2;
inline constexpr int kAigcBadgeCornerRadius = 8;

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
              "AIGC badge layout must keep every corner 8 px inside the panel rim");

}  // namespace memoria

#endif
