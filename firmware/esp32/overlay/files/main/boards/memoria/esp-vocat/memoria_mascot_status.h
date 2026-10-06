#ifndef MEMORIA_MASCOT_STATUS_H
#define MEMORIA_MASCOT_STATUS_H

// The `status` line of a bench build (TODOLIST M-2): what the mascot is doing right now and what its frames
// have cost since boot, to be read next to the serial screenshot. Only the bench code in
// memoria_mascot_bench.cc uses it, and that file is empty without CONFIG_MEMORIA_BENCH_SERIAL.
//
// The counters are cumulative since boot. The PC subtracts two lines to get a rate, so no window state lives
// on the robot, a status line that is lost costs nothing, and the one-line-a-minute `anim ...` receipt of the
// product image keeps its own windows. A 64-bit sum of microseconds or pixels does not wrap in any
// experiment's lifetime; the 32-bit ones (frames, drawn) wrap after about five years at 25 fps.
//
// Like the scene this header has no ESP-IDF or LVGL dependency: the host tests compile it against the real
// enums, so a phase, mood or frame added without a name stops the build (the static_asserts below), not a
// test run some weeks later.

#include "memoria_mascot_pack.h"
#include "memoria_mascot_scene.h"

#include <cstddef>
#include <cstdint>
#include <cstdio>

namespace memoria {

// Everything one status line reports. The animation task fills it: it is the only task that touches the scene,
// so nothing here is shared between tasks.
struct BenchStatus {
    uint32_t up_ms = 0;
    ScenePhase phase = ScenePhase::kIntro;
    SceneMood mood = SceneMood::kNeutral;
    MascotFrame frame = MascotFrame::kCount;  // the frame the last redraw drew; kCount before the first
    bool screen_off = false;                  // the panel is dark: nothing is being rendered
    bool sleeping = false;                    // the companion dozes (and the backlight is dimmed)
    bool captioned = false;                   // LVGL text sits on the caption band under the mascot
    // Since boot. A "frame" is one pass of the animation loop that looked at the scene, "drawn" one that
    // changed pixels.
    uint32_t frames = 0;
    uint32_t drawn = 0;
    uint64_t render_us = 0;       // time Render() took, summed over the drawn frames
    uint32_t render_max_us = 0;   // the longest single Render()
    uint64_t busy_us = 0;         // drawn frames' whole cost to the task: display-lock wait, render, invalidate
    uint64_t pixels = 0;          // dirty-rectangle pixels handed to LVGL
    uint64_t composed_px = 0;     // pixels the compositor actually produced
    uint32_t extra_ms = 0;        // what the frame pacer currently adds to the wanted interval
    // Memory, read when the line is made.
    uint32_t heap_free = 0;         // internal heap, bytes
    uint32_t psram_free = 0;        // external heap, bytes
    uint32_t anim_stack_free = 0;   // the animation task's lowest free stack so far, bytes
};

// Room for one status line, NUL included: the longest names and every counter at its maximum come to 381
// characters (the host test measures it).
constexpr std::size_t kBenchStatusCapacity = 512;

inline const char* ScenePhaseName(ScenePhase phase) {
    static const char* const kNames[] = {"intro",     "setup",     "wifi_config", "connecting", "idle",
                                         "listening", "thinking",  "speaking",    "error"};
    static_assert(sizeof(kNames) / sizeof(kNames[0]) == static_cast<std::size_t>(ScenePhase::kError) + 1,
                  "every ScenePhase needs a name in ScenePhaseName");
    const std::size_t index = static_cast<std::size_t>(phase);
    return index < sizeof(kNames) / sizeof(kNames[0]) ? kNames[index] : "unknown";
}

inline const char* SceneMoodName(SceneMood mood) {
    static const char* const kNames[] = {"neutral", "happy", "sad", "surprised", "thinking", "loving", "angry"};
    static_assert(sizeof(kNames) / sizeof(kNames[0]) == static_cast<std::size_t>(SceneMood::kAngry) + 1,
                  "every SceneMood needs a name in SceneMoodName");
    const std::size_t index = static_cast<std::size_t>(mood);
    return index < sizeof(kNames) / sizeof(kNames[0]) ? kNames[index] : "unknown";
}

// kCount (no frame drawn yet) reads "none".
inline const char* MascotFrameName(MascotFrame frame) {
    static const char* const kNames[] = {
        "default",         "happy",           "sad",        "surprised",       "thinking",
        "listening",       "sleepy",          "dizzy",      "greeting",        "default_blink",
        "sad_blink",       "surprised_blink", "thinking_blink", "listening_blink", "default_talk",
        "happy_talk",      "sad_talk",        "surprised_talk", "thinking_talk"};
    static_assert(sizeof(kNames) / sizeof(kNames[0]) == kMascotFrameCount,
                  "every MascotFrame needs a name in MascotFrameName");
    const std::size_t index = static_cast<std::size_t>(frame);
    return index < sizeof(kNames) / sizeof(kNames[0]) ? kNames[index] : "none";
}

// The status line (no terminator, no log prefix). Writes at most `capacity` bytes including the NUL and returns
// the line's length, or 0 when it does not fit.
inline std::size_t BenchStatusLine(char* out, std::size_t capacity, const BenchStatus& s) {
    if (out == nullptr || capacity == 0) {
        return 0;
    }
    const int written = std::snprintf(
        out, capacity,
        "status up_ms=%lu phase=%s mood=%s frame=%s screen_off=%d sleeping=%d captioned=%d "
        "frames=%lu drawn=%lu render_us=%llu render_max_us=%lu busy_us=%llu px=%llu composed_px=%llu "
        "extra_ms=%lu heap_free=%lu psram_free=%lu anim_stack_free=%lu",
        static_cast<unsigned long>(s.up_ms), ScenePhaseName(s.phase), SceneMoodName(s.mood),
        MascotFrameName(s.frame), s.screen_off ? 1 : 0, s.sleeping ? 1 : 0, s.captioned ? 1 : 0,
        static_cast<unsigned long>(s.frames), static_cast<unsigned long>(s.drawn),
        static_cast<unsigned long long>(s.render_us), static_cast<unsigned long>(s.render_max_us),
        static_cast<unsigned long long>(s.busy_us), static_cast<unsigned long long>(s.pixels),
        static_cast<unsigned long long>(s.composed_px), static_cast<unsigned long>(s.extra_ms),
        static_cast<unsigned long>(s.heap_free), static_cast<unsigned long>(s.psram_free),
        static_cast<unsigned long>(s.anim_stack_free));
    return written > 0 && static_cast<std::size_t>(written) < capacity ? static_cast<std::size_t>(written) : 0;
}

}  // namespace memoria

#endif  // MEMORIA_MASCOT_STATUS_H
