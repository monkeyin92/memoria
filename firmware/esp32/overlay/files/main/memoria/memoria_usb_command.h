#pragma once

// A debug command line on the robot's USB-Serial-JTAG port (firmware build 20).
//
// Until build 19 the USB port only printed logs, so the computer-driven tests could wake the robot only by
// playing the wake word through the Mac's speaker, and the wake word false-triggers (TODOLIST N-1). One
// command now does what a tap on the round screen does:
//
//   wake\n    start a conversation on an idle device, under the same gate as a screen tap
//
// A product image accepts nothing else: the port is reachable only with the cable plugged in (no network
// entry), the command cannot stop, abort or reconfigure anything, and a device that is not idle, is pairing
// or is starting up ignores it and says why in the log.
//
// A bench build (CONFIG_MEMORIA_BENCH_SERIAL, TODOLIST M-2; never published, see memoria_bench_snap.h) also
// answers two read-only requests, so a computer can look at the round screen without anyone standing at the
// robot:
//
//   snap\n    stream a screenshot of the display as SNAP log lines
//   status\n  log one line of key=value state (frames drawn, render cost, device state, memory)
//
// Neither changes anything on the robot. They are compiled in only under that switch: a product image has
// no such enumerators, no such verbs and no code behind them.
//
// This header has no ESP-IDF dependency so the host tests compile the real parser, line assembler and wake
// policy (in both configurations); the task that reads the port and calls the application lives in the
// board file.

#include <cstddef>
#include <cstdint>
#include <cstring>

// ESP-IDF generates sdkconfig.h with the option defined as 1 when it is on and not at all when it is off;
// the host tests pass -DCONFIG_MEMORIA_BENCH_SERIAL=0/1 instead.
#if __has_include("sdkconfig.h")
#include "sdkconfig.h"
#endif
#ifndef CONFIG_MEMORIA_BENCH_SERIAL
#define CONFIG_MEMORIA_BENCH_SERIAL 0
#endif

#include "memoria_wake_mode.h"

namespace memoria {

// Stack of the task that reads the port (the board file starts it). A product image only assembles lines and
// calls the application; a bench image also takes the screenshot on that task, and LVGL's software renderer
// is the deepest call chain on the robot.
constexpr std::size_t kUsbTaskStackBytes = CONFIG_MEMORIA_BENCH_SERIAL ? 12 * 1024 : 4 * 1024;

enum class UsbCommand : unsigned char {
    kUnknown,
    kWake,
#if CONFIG_MEMORIA_BENCH_SERIAL
    kSnap,
    kStatus,
#endif
};

namespace usb_command_detail {

template <std::size_t N>
inline bool IsVerb(const char* text, std::size_t length, const char (&verb)[N]) {
    return length == N - 1 && std::memcmp(text, verb, N - 1) == 0;
}

}  // namespace usb_command_detail

// One complete line without its terminator. Blanks around the verb are ignored; the verb itself is exact
// (lower case, nothing after it), so a stray terminal, a pasted log line or line noise cannot wake the robot.
inline UsbCommand ParseUsbCommand(const char* line, std::size_t length) {
    if (line == nullptr) {
        return UsbCommand::kUnknown;
    }
    std::size_t begin = 0;
    std::size_t end = length;
    while (begin < end && (line[begin] == ' ' || line[begin] == '\t')) {
        ++begin;
    }
    while (end > begin && (line[end - 1] == ' ' || line[end - 1] == '\t')) {
        --end;
    }
    const char* verb = line + begin;
    const std::size_t verb_length = end - begin;
    if (usb_command_detail::IsVerb(verb, verb_length, "wake")) {
        return UsbCommand::kWake;
    }
#if CONFIG_MEMORIA_BENCH_SERIAL
    if (usb_command_detail::IsVerb(verb, verb_length, "snap")) {
        return UsbCommand::kSnap;
    }
    if (usb_command_detail::IsVerb(verb, verb_length, "status")) {
        return UsbCommand::kStatus;
    }
#endif
    return UsbCommand::kUnknown;
}

enum class UsbLineEvent : unsigned char {
    kNone,     // nothing complete yet, or an empty line (CR LF arrives as two terminators)
    kWake,     // a complete line that is the wake command
    kUnknown,  // a complete line that is not a command
    kTooLong,  // a line longer than kMaxLine just ended; it was dropped whole
#if CONFIG_MEMORIA_BENCH_SERIAL
    kSnap,    // bench builds only: a complete `snap` line
    kStatus,  // bench builds only: a complete `status` line
#endif
};

// Turns the bytes read from the port into lines. A line is ended by CR or LF. One that does not fit is
// dropped up to its terminator, so the line after it starts clean; a half-written line older than
// kStaleMs is dropped too, so a "wak" left in the FIFO cannot be completed by an "e\n" much later.
class UsbLineAssembler {
public:
    static constexpr std::size_t kMaxLine = 32;
    static constexpr std::uint32_t kStaleMs = 2000;

    UsbLineEvent Feed(std::uint8_t byte, std::uint32_t now_ms) {
        // Unsigned subtraction: correct across the 32-bit millisecond counter wrapping.
        if (length_ > 0 && now_ms - started_ms_ > kStaleMs) {
            Reset();
        }
        if (byte == '\r' || byte == '\n') {
            return EndLine();
        }
        if (overflow_) {
            return UsbLineEvent::kNone;
        }
        if (length_ == kMaxLine) {
            overflow_ = true;
            return UsbLineEvent::kNone;
        }
        if (length_ == 0) {
            started_ms_ = now_ms;
        }
        line_[length_++] = static_cast<char>(byte);
        return UsbLineEvent::kNone;
    }

    // Bytes held for a line that has not ended yet (tests).
    std::size_t pending() const { return length_; }

private:
    UsbLineEvent EndLine() {
        const bool overflowed = overflow_;
        const std::size_t length = length_;
        const UsbCommand command = ParseUsbCommand(line_, length);
        Reset();
        if (overflowed) {
            return UsbLineEvent::kTooLong;
        }
        if (length == 0) {
            return UsbLineEvent::kNone;
        }
        switch (command) {
            case UsbCommand::kWake:
                return UsbLineEvent::kWake;
#if CONFIG_MEMORIA_BENCH_SERIAL
            case UsbCommand::kSnap:
                return UsbLineEvent::kSnap;
            case UsbCommand::kStatus:
                return UsbLineEvent::kStatus;
#endif
            case UsbCommand::kUnknown:
                break;
        }
        return UsbLineEvent::kUnknown;
    }

    void Reset() {
        length_ = 0;
        overflow_ = false;
    }

    char line_[kMaxLine] = {};
    std::size_t length_ = 0;
    std::uint32_t started_ms_ = 0;
    bool overflow_ = false;
};

// What the wake command does to a device that is not pairing or starting up (the board handles those first,
// as it does for a tap). It follows the screen tap's gate exactly: only an idle device in a mode that
// includes the screen starts a conversation, so the command can never stop the robot talking or send it back
// to standby.
enum class UsbWakeDecision : unsigned char { kStartConversation, kIgnoreNotIdle, kIgnoreWakeMode };

inline UsbWakeDecision UsbWakeDecisionFor(WakeMode mode, bool device_idle) {
    if (TapActionFor(mode, device_idle) == TapAction::kStartConversation) {
        return UsbWakeDecision::kStartConversation;
    }
    return device_idle ? UsbWakeDecision::kIgnoreWakeMode : UsbWakeDecision::kIgnoreNotIdle;
}

inline const char* UsbWakeDecisionName(UsbWakeDecision decision) {
    switch (decision) {
        case UsbWakeDecision::kStartConversation:
            return "start";
        case UsbWakeDecision::kIgnoreNotIdle:
            return "not_idle";
        case UsbWakeDecision::kIgnoreWakeMode:
            return "wake_mode";
    }
    return "unknown";
}

}  // namespace memoria
