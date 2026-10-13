#pragma once

// Guardian quiet-hours window evaluation for on-device night taps.
// Same [start, end) semantics as services/agent minor_session_limits
// (may wrap past midnight; start == end means no quiet window).
// Host-compilable.

#include <cstdint>
#include <cstring>

namespace memoria {

struct QuietHoursWindow {
    // Minutes from 00:00, 0..1439. Equal values mean "no quiet hours".
    std::uint16_t start_minute = 0;
    std::uint16_t end_minute = 0;

    bool active() const { return start_minute != end_minute; }
};

inline bool ParseClockToMinute(const char* hhmm, std::uint16_t* out_minute) {
    if (hhmm == nullptr || out_minute == nullptr) {
        return false;
    }
    if (std::strlen(hhmm) != 5 || hhmm[2] != ':') {
        return false;
    }
    if (hhmm[0] < '0' || hhmm[0] > '9' || hhmm[1] < '0' || hhmm[1] > '9' ||
        hhmm[3] < '0' || hhmm[3] > '9' || hhmm[4] < '0' || hhmm[4] > '9') {
        return false;
    }
    const int hours = (hhmm[0] - '0') * 10 + (hhmm[1] - '0');
    const int minutes = (hhmm[3] - '0') * 10 + (hhmm[4] - '0');
    if (hours > 23 || minutes > 59) {
        return false;
    }
    *out_minute = static_cast<std::uint16_t>(hours * 60 + minutes);
    return true;
}

inline bool ParseQuietHours(const char* start_hhmm, const char* end_hhmm, QuietHoursWindow* out) {
    if (out == nullptr) {
        return false;
    }
    QuietHoursWindow window;
    if (!ParseClockToMinute(start_hhmm, &window.start_minute) ||
        !ParseClockToMinute(end_hhmm, &window.end_minute)) {
        return false;
    }
    *out = window;
    return true;
}

inline bool InQuietHours(std::uint16_t now_minute, QuietHoursWindow window) {
    if (!window.active()) {
        return false;
    }
    if (window.start_minute < window.end_minute) {
        return now_minute >= window.start_minute && now_minute < window.end_minute;
    }
    return now_minute >= window.start_minute || now_minute < window.end_minute;
}

}  // namespace memoria
