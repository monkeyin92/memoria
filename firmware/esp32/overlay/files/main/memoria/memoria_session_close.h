#pragma once

#include <string>

namespace memoria {

// What a server session.close asks this board to do when the conversation
// ends without a reply. tts_unavailable reaches the device after both TTS
// engines failed before the turn ever went to air: play_line tells the board
// that no line was heard, so it must speak the existing offline clip.
// llm_unavailable arrives after the server already spoke that same clip, and
// replaying it would read the line twice.
enum class SessionCloseFallback {
    kNone,        // other reasons: the close itself is the only signal
    kMenuSilent,  // enter the offline menu, the line was already heard
    kMenuPlayLine,  // enter the offline menu and speak the offline clip first
};

inline SessionCloseFallback ClassifySessionCloseFallback(
    const std::string& reason, bool play_line) {
    if (reason == "llm_unavailable") {
        return SessionCloseFallback::kMenuSilent;
    }
    if (reason == "tts_unavailable") {
        return play_line ? SessionCloseFallback::kMenuPlayLine
                         : SessionCloseFallback::kMenuSilent;
    }
    return SessionCloseFallback::kNone;
}

}  // namespace memoria
