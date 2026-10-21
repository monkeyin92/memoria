#pragma once

// Whether the reply now playing may be stopped by a local gesture.
//
// A fixed safety script (crisis-self-v1 and the two victim scripts) must play
// to its end. The board cannot tell those scripts apart from an ordinary reply
// (the script text never reaches the device), so the platform decides: a
// protected generation carries "interruptible": false in generation.started.
// Every other generation omits the field and stays interruptible.
//
// MemoriaProtocol parses the field and publishes it here, so the board's
// gesture handlers read one atomic instead of reaching into the protocol.
// Host-compilable.

#include <atomic>

namespace memoria {

enum class PlaybackStop : unsigned char {
    kAllowed = 0,
    kProtected = 1,  // a fixed safety script plays
};

class PlaybackStopRegistry {
public:
    static PlaybackStopRegistry& GetInstance() {
        static PlaybackStopRegistry instance;
        return instance;
    }

    void Set(PlaybackStop stop) { stop_.store(stop, std::memory_order_relaxed); }
    PlaybackStop stop() const { return stop_.load(std::memory_order_relaxed); }
    bool allowed() const { return stop() == PlaybackStop::kAllowed; }

private:
    std::atomic<PlaybackStop> stop_{PlaybackStop::kAllowed};
};

inline const char* PlaybackStopName(PlaybackStop stop) {
    return stop == PlaybackStop::kProtected ? "protected" : "allowed";
}

// generation.started's "interruptible". Absent or malformed means
// interruptible: a normal reply must stay stoppable even if the field is
// missing, and only an explicit false protects playback.
inline bool InterruptibleFromWire(bool field_present, bool field_is_bool,
                                  bool field_true) {
    return !field_present || !field_is_bool || field_true;
}

inline PlaybackStop PlaybackStopFromInterruptible(bool interruptible) {
    return interruptible ? PlaybackStop::kAllowed : PlaybackStop::kProtected;
}

}  // namespace memoria
