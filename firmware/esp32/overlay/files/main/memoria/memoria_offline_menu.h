#pragma once

// Offline story menu policy for Memoria ESP-VoCat.
// Host-compilable pure state machine. The board feeds taps, network edges,
// timers, and story-finished events; it plays the returned clip ids as
// "<id>.ogg" and starts/stops story_0N.ogg.

#include <cstddef>
#include <cstdint>

namespace memoria {

inline constexpr std::uint32_t kOfflineLongPressMs = 800;
inline constexpr std::uint32_t kOfflineReconnectTimeoutMs = 20 * 1000;
inline constexpr std::uint32_t kOfflineMenuIdleMs = 30 * 1000;
inline constexpr std::uint8_t kOfflineStoryCount = 3;

inline constexpr const char* kOfflineWakeNoNetwork = "offline-wake-no-network-v1";
inline constexpr const char* kOfflineWakeAskParent = "offline-wake-ask-parent-v1";
inline constexpr const char* kOfflineWakeStoryHint = "offline-wake-story-hint-v2";
inline constexpr const char* kOfflineMidDrop = "offline-mid-drop-v2";
inline constexpr const char* kOfflineMidAskParent = "offline-mid-ask-parent-v1";
inline constexpr const char* kOfflineMidRetry = "offline-mid-retry-v1";
inline constexpr const char* kOfflineReconnecting = "offline-reconnecting-v1";
inline constexpr const char* kOnlineRestored = "online-restored-v1";
inline constexpr const char* kOnlineRestoredChat = "online-restored-chat-v1";
inline constexpr const char* kOnlineRestoredInStory = "online-restored-in-story-v2";
inline constexpr const char* kOfflineMenuIntro = "offline-menu-intro-v2";
inline constexpr const char* kOfflineMenuHow = "offline-menu-how-v1";
inline constexpr const char* kOfflineMenuConfirmHow = "offline-menu-confirm-how-v2";
inline constexpr const char* kOfflineMenuItem01 = "offline-menu-item-01-v2";
inline constexpr const char* kOfflineMenuItem02 = "offline-menu-item-02-v2";
inline constexpr const char* kOfflineMenuItem03 = "offline-menu-item-03-v2";
inline constexpr const char* kOfflineMenuChoose = "offline-menu-choose-v2";
inline constexpr const char* kOfflineMenuStart = "offline-menu-start-v1";
inline constexpr const char* kOfflineMenuEnd = "offline-menu-end-v1";
inline constexpr const char* kOfflineMenuIdle = "offline-menu-idle-v1";

inline constexpr const char* kOfflineLineIds[] = {
    kOfflineWakeNoNetwork,    kOfflineWakeAskParent,   kOfflineWakeStoryHint,
    kOfflineMidDrop,          kOfflineMidAskParent,    kOfflineMidRetry,
    kOfflineReconnecting,     kOnlineRestored,         kOnlineRestoredChat,
    kOnlineRestoredInStory,   kOfflineMenuIntro,       kOfflineMenuHow,
    kOfflineMenuConfirmHow,   kOfflineMenuItem01,      kOfflineMenuItem02,
    kOfflineMenuItem03,       kOfflineMenuChoose,      kOfflineMenuStart,
    kOfflineMenuEnd,          kOfflineMenuIdle,
};
inline constexpr std::size_t kOfflineLineCount =
    sizeof(kOfflineLineIds) / sizeof(kOfflineLineIds[0]);

enum class OfflineMode : unsigned char {
    kInactive,
    kAwakeReconnecting,
    kStandbyOffline,
    kMidReconnecting,
    kMenu,
    kPlaying,
    kPendingOnlineAfterStory,
};

struct OfflineMenuOutput {
    static constexpr std::size_t kMaxClips = 8;
    const char* clips[kMaxClips] = {};
    std::size_t clip_count = 0;
    bool intercept = false;
    bool start_story = false;
    bool stop_story = false;
    std::uint8_t story_index = 0;
    bool enter_deep_sleep = false;
    bool retry_network = false;
    bool stop_retry_network = false;
};

inline bool OfflineIsLongPress(std::uint32_t hold_ms) {
    return hold_ms >= kOfflineLongPressMs;
}

inline const char* OfflineModeName(OfflineMode mode) {
    switch (mode) {
        case OfflineMode::kInactive:
            return "inactive";
        case OfflineMode::kAwakeReconnecting:
            return "awake_reconnecting";
        case OfflineMode::kStandbyOffline:
            return "standby_offline";
        case OfflineMode::kMidReconnecting:
            return "mid_reconnecting";
        case OfflineMode::kMenu:
            return "menu";
        case OfflineMode::kPlaying:
            return "playing";
        case OfflineMode::kPendingOnlineAfterStory:
            return "pending_online_after_story";
    }
    return "unknown";
}

inline const char* OfflineMenuItemClip(std::uint8_t item_1_based) {
    switch (item_1_based) {
        case 1:
            return kOfflineMenuItem01;
        case 2:
            return kOfflineMenuItem02;
        case 3:
            return kOfflineMenuItem03;
        default:
            return kOfflineMenuItem01;
    }
}

inline const char* OfflineStoryFile(std::uint8_t item_1_based) {
    switch (item_1_based) {
        case 1:
            return "story_01.ogg";
        case 2:
            return "story_02.ogg";
        case 3:
            return "story_03.ogg";
        default:
            return "story_01.ogg";
    }
}

class OfflineMenuMachine {
public:
    OfflineMode mode() const { return mode_; }
    std::uint8_t menu_item() const { return menu_item_; }
    bool retry_network() const { return retry_network_; }

    void Reset() {
        mode_ = OfflineMode::kInactive;
        menu_item_ = 1;
        retry_network_ = false;
        reconnect_deadline_ms_ = 0;
        menu_idle_deadline_ms_ = 0;
        from_menu_sleep_ = false;
    }

    // First processed tap after chip deep sleep (or menu-idle sleep). Wake only.
    OfflineMenuOutput OnDeepSleepWake(std::uint32_t now_ms, bool network_online) {
        OfflineMenuOutput out;
        out.intercept = true;
        if (network_online) {
            Reset();
            return out;
        }
        mode_ = OfflineMode::kAwakeReconnecting;
        retry_network_ = true;
        reconnect_deadline_ms_ = now_ms + kOfflineReconnectTimeoutMs;
        from_menu_sleep_ = false;
        out.retry_network = true;
        return out;
    }

    OfflineMenuOutput OnNetworkDown(std::uint32_t now_ms, bool in_conversation) {
        OfflineMenuOutput out;
        if (mode_ == OfflineMode::kPlaying || mode_ == OfflineMode::kPendingOnlineAfterStory ||
            mode_ == OfflineMode::kMenu || mode_ == OfflineMode::kMidReconnecting ||
            mode_ == OfflineMode::kAwakeReconnecting || mode_ == OfflineMode::kStandbyOffline) {
            // Already offline-handling; keep retrying.
            retry_network_ = true;
            out.retry_network = true;
            out.intercept = true;
            return out;
        }
        if (in_conversation) {
            mode_ = OfflineMode::kMidReconnecting;
            retry_network_ = true;
            reconnect_deadline_ms_ = now_ms + kOfflineReconnectTimeoutMs;
            Push(out, kOfflineMidDrop);
            Push(out, kOfflineMidAskParent);
            Push(out, kOfflineMidRetry);
            out.intercept = true;
            out.retry_network = true;
            return out;
        }
        // Idle/standby drop: silent background retry only.
        mode_ = OfflineMode::kStandbyOffline;
        retry_network_ = true;
        reconnect_deadline_ms_ = 0;
        out.intercept = true;
        out.retry_network = true;
        return out;
    }

    OfflineMenuOutput OnNetworkUp(std::uint32_t /*now_ms*/) {
        OfflineMenuOutput out;
        out.intercept = true;
        switch (mode_) {
            case OfflineMode::kPlaying:
                mode_ = OfflineMode::kPendingOnlineAfterStory;
                retry_network_ = false;
                out.stop_retry_network = true;
                return out;
            case OfflineMode::kPendingOnlineAfterStory:
                return out;
            case OfflineMode::kMenu:
            case OfflineMode::kMidReconnecting:
            case OfflineMode::kAwakeReconnecting:
            case OfflineMode::kStandbyOffline:
                Push(out, kOnlineRestored);
                Push(out, kOnlineRestoredChat);
                Reset();
                out.stop_retry_network = true;
                return out;
            case OfflineMode::kInactive:
                break;
        }
        return OfflineMenuOutput{};
    }

    OfflineMenuOutput OnTimer(std::uint32_t now_ms) {
        OfflineMenuOutput out;
        if (mode_ == OfflineMode::kAwakeReconnecting && reconnect_deadline_ms_ != 0 &&
            now_ms >= reconnect_deadline_ms_) {
            reconnect_deadline_ms_ = 0;
            AppendWakeThenMenu(out);
            // Board arms idle via NoteMenuActivity after the intro clips finish.
            menu_idle_deadline_ms_ = 0;
            out.intercept = true;
            out.retry_network = true;
            return out;
        }
        if (mode_ == OfflineMode::kMidReconnecting && reconnect_deadline_ms_ != 0 &&
            now_ms >= reconnect_deadline_ms_) {
            reconnect_deadline_ms_ = 0;
            AppendMenuEntry(out);
            menu_idle_deadline_ms_ = 0;
            out.intercept = true;
            out.retry_network = true;
            return out;
        }
        if (mode_ == OfflineMode::kMenu && menu_idle_deadline_ms_ != 0 &&
            now_ms >= menu_idle_deadline_ms_) {
            Push(out, kOfflineMenuIdle);
            from_menu_sleep_ = true;
            retry_network_ = false;
            mode_ = OfflineMode::kInactive;
            menu_idle_deadline_ms_ = 0;
            reconnect_deadline_ms_ = 0;
            out.intercept = true;
            out.enter_deep_sleep = true;
            out.stop_retry_network = true;
            return out;
        }
        return out;
    }

    OfflineMenuOutput OnStoryFinished(std::uint32_t now_ms) {
        OfflineMenuOutput out;
        out.intercept = true;
        if (mode_ == OfflineMode::kPendingOnlineAfterStory) {
            Push(out, kOnlineRestoredInStory);
            Reset();
            out.stop_retry_network = true;
            return out;
        }
        if (mode_ == OfflineMode::kPlaying) {
            Push(out, kOfflineMenuEnd);
            mode_ = OfflineMode::kMenu;
            ArmMenuIdle(now_ms);
            return out;
        }
        return OfflineMenuOutput{};
    }

    // Quiet hours: caller must handle night soothe and not call this.
    // Online idle: caller uses TapWakeActionFor; this returns intercept=false.
    OfflineMenuOutput OnTap(std::uint32_t now_ms, std::uint32_t hold_ms, bool network_online,
                            bool device_idle, bool in_conversation) {
        OfflineMenuOutput out;
        const bool long_press = OfflineIsLongPress(hold_ms);

        if (mode_ == OfflineMode::kInactive) {
            if (network_online || !device_idle) {
                return out;
            }
            // Idle, offline, no active offline mode: wake lines then menu.
            AppendWakeThenMenu(out);
            menu_idle_deadline_ms_ = 0;
            out.intercept = true;
            out.retry_network = true;
            retry_network_ = true;
            return out;
        }

        out.intercept = true;

        if (mode_ == OfflineMode::kAwakeReconnecting || mode_ == OfflineMode::kMidReconnecting) {
            if (network_online) {
                return OnNetworkUp(now_ms);
            }
            Push(out, kOfflineReconnecting);
            return out;
        }

        if (mode_ == OfflineMode::kStandbyOffline) {
            if (network_online) {
                return OnNetworkUp(now_ms);
            }
            AppendWakeThenMenu(out);
            menu_idle_deadline_ms_ = 0;
            out.retry_network = true;
            return out;
        }

        if (mode_ == OfflineMode::kMenu) {
            if (long_press) {
                Push(out, kOfflineMenuStart);
                out.start_story = true;
                out.story_index = menu_item_;
                mode_ = OfflineMode::kPlaying;
                menu_idle_deadline_ms_ = 0;
                return out;
            }
            menu_item_ = static_cast<std::uint8_t>((menu_item_ % kOfflineStoryCount) + 1);
            Push(out, OfflineMenuItemClip(menu_item_));
            Push(out, kOfflineMenuChoose);
            ArmMenuIdle(now_ms);
            return out;
        }

        if (mode_ == OfflineMode::kPlaying || mode_ == OfflineMode::kPendingOnlineAfterStory) {
            if (!long_press) {
                return out;
            }
            out.stop_story = true;
            mode_ = OfflineMode::kMenu;
            Push(out, OfflineMenuItemClip(menu_item_));
            Push(out, kOfflineMenuChoose);
            ArmMenuIdle(now_ms);
            return out;
        }

        (void)in_conversation;
        return out;
    }

    bool ConsumesTap() const {
        return mode_ != OfflineMode::kInactive;
    }

    bool from_menu_sleep() const { return from_menu_sleep_; }
    void ClearMenuSleepFlag() { from_menu_sleep_ = false; }

private:
    void Push(OfflineMenuOutput& out, const char* clip) {
        if (out.clip_count < OfflineMenuOutput::kMaxClips) {
            out.clips[out.clip_count++] = clip;
        }
    }

    void ArmMenuIdle(std::uint32_t now_ms) {
        menu_idle_deadline_ms_ = now_ms + kOfflineMenuIdleMs;
    }

    void EnterMenuItem1(std::uint32_t /*now_ms*/) {
        mode_ = OfflineMode::kMenu;
        menu_item_ = 1;
    }

    void AppendMenuEntry(OfflineMenuOutput& out) {
        Push(out, kOfflineMenuIntro);
        Push(out, kOfflineMenuHow);
        Push(out, kOfflineMenuConfirmHow);
        Push(out, kOfflineMenuItem01);
        Push(out, kOfflineMenuChoose);
        EnterMenuItem1(0);
        // Caller supplies now_ms via OnTimer; arm idle from there after play.
        // Approximate: board should call NoteMenuActivity after clips.
        menu_idle_deadline_ms_ = 0;
    }

    void AppendWakeThenMenu(OfflineMenuOutput& out) {
        Push(out, kOfflineWakeNoNetwork);
        Push(out, kOfflineWakeAskParent);
        Push(out, kOfflineWakeStoryHint);
        AppendMenuEntry(out);
    }

public:
    // Board calls after entering menu so the 30s idle starts after speech.
    void NoteMenuActivity(std::uint32_t now_ms) {
        if (mode_ == OfflineMode::kMenu) {
            ArmMenuIdle(now_ms);
        }
    }

private:
    OfflineMode mode_ = OfflineMode::kInactive;
    std::uint8_t menu_item_ = 1;
    bool retry_network_ = false;
    std::uint32_t reconnect_deadline_ms_ = 0;
    std::uint32_t menu_idle_deadline_ms_ = 0;
    bool from_menu_sleep_ = false;
};

}  // namespace memoria
