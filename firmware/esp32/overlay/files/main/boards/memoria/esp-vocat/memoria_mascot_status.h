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
#include <cstring>

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

// Room for one profile line, NUL included (twelve 64-bit sums at their maximum come to 367 characters; the host
// test measures it).
constexpr std::size_t kBenchProfileCapacity = 448;

// The profile line that follows every status line (no terminator, no log prefix): where the sampled renders spent
// their time, in microseconds summed over `sampled` renders (memoria_mascot_scene.h, RenderProfile). Same
// contract as BenchStatusLine: at most `capacity` bytes including the NUL, the length, or 0 when it does not fit.
inline std::size_t BenchProfileLine(char* out, std::size_t capacity, const RenderProfile& p) {
    if (out == nullptr || capacity == 0) {
        return 0;
    }
    const int written = std::snprintf(
        out, capacity,
        "profile renders=%llu sampled=%llu total_us=%llu actor_us=%llu rect_us=%llu touch_us=%llu rows=%llu "
        "copy_in_us=%llu shadow_us=%llu sprite_us=%llu ring_us=%llu copy_out_us=%llu",
        static_cast<unsigned long long>(p.renders), static_cast<unsigned long long>(p.sampled),
        static_cast<unsigned long long>(p.total_us), static_cast<unsigned long long>(p.actor_us),
        static_cast<unsigned long long>(p.rect_us), static_cast<unsigned long long>(p.touch_us),
        static_cast<unsigned long long>(p.rows), static_cast<unsigned long long>(p.copy_in_us),
        static_cast<unsigned long long>(p.shadow_us), static_cast<unsigned long long>(p.sprite_us),
        static_cast<unsigned long long>(p.ring_us), static_cast<unsigned long long>(p.copy_out_us));
    return written > 0 && static_cast<std::size_t>(written) < capacity ? static_cast<std::size_t>(written) : 0;
}

// One FreeRTOS task's share of the CPU since boot, for the `tasks` line (TODOLIST M-6): the bench build samples
// uxTaskGetSystemState and the PC divides two lines' differences by the wall time between them. Comparing
// mascot_anim's CPU time with the wall time its frames take tells "preempted by audio" from "waiting on memory".
struct BenchTaskRow {
    char name[16] = {};
    int core = 0;          // 0, 1, or -1 for no affinity
    uint32_t priority = 0;
    uint64_t runtime_us = 0;
};

constexpr std::size_t kBenchTaskRows = 24;
// 24 rows of "<15 chars>:<core>:<prio>:<20 digits> " come to about 24 * 45, plus the head.
constexpr std::size_t kBenchTasksCapacity = 1280;

// Busiest first; the line keeps as many rows as fit.
inline void BenchSortTasks(BenchTaskRow* rows, std::size_t n) {
    for (std::size_t i = 1; i < n; ++i) {
        const BenchTaskRow key = rows[i];
        std::size_t j = i;
        while (j > 0 && rows[j - 1].runtime_us < key.runtime_us) {
            rows[j] = rows[j - 1];
            --j;
        }
        rows[j] = key;
    }
}

// `tasks count=<all tasks> up_us=<wall clock> <name>:<core>:<prio>:<runtime_us> ...` for the busiest tasks. A name
// has no spaces or colons in the line (they become '_'). Same contract as the other line builders: at most
// `capacity` bytes including the NUL, the length, or 0 when even the head does not fit; rows that do not fit
// are left out, never cut in half.
// Busiest first, so the line keeps the tasks that matter when there are more than kBenchTaskRows of them.
inline void SortBenchTasksByRuntime(BenchTaskRow* rows, std::size_t n) {
    for (std::size_t i = 1; i < n; ++i) {
        BenchTaskRow key = rows[i];
        std::size_t j = i;
        while (j > 0 && rows[j - 1].runtime_us < key.runtime_us) {
            rows[j] = rows[j - 1];
            --j;
        }
        rows[j] = key;
    }
}

inline std::size_t BenchTasksLine(char* out, std::size_t capacity, const BenchTaskRow* rows, std::size_t n,
                                  std::size_t task_count, uint64_t up_us) {
    if (out == nullptr || capacity == 0) {
        return 0;
    }
    int head = std::snprintf(out, capacity, "tasks count=%llu up_us=%llu", static_cast<unsigned long long>(task_count),
                             static_cast<unsigned long long>(up_us));
    if (head <= 0 || static_cast<std::size_t>(head) >= capacity) {
        out[0] = '\0';
        return 0;
    }
    std::size_t length = static_cast<std::size_t>(head);
    for (std::size_t i = 0; i < n && i < kBenchTaskRows; ++i) {
        char name[16];
        std::size_t k = 0;
        for (; k < sizeof(name) - 1 && rows[i].name[k] != '\0'; ++k) {
            const char c = rows[i].name[k];
            name[k] = (c == ' ' || c == ':') ? '_' : c;
        }
        name[k] = '\0';
        char item[80];
        const int item_length = std::snprintf(item, sizeof(item), " %s:%d:%lu:%llu", name, rows[i].core,
                                              static_cast<unsigned long>(rows[i].priority),
                                              static_cast<unsigned long long>(rows[i].runtime_us));
        if (item_length <= 0 || length + static_cast<std::size_t>(item_length) >= capacity) {
            break;
        }
        std::memcpy(out + length, item, static_cast<std::size_t>(item_length) + 1);
        length += static_cast<std::size_t>(item_length);
    }
    return length;
}

// Where taskLVGL's time goes (TODOLIST M-6), counted by hooks on LVGL's own events in the bench image, all
// cumulative since boot, microseconds of wall clock. Four spans per refresh: the whole refresh (REFR_START to
// REFR_READY), the flush callbacks inside it (FLUSH_START to FLUSH_FINISH: the byte swap and the hand-over to the
// panel), and the waits for the previous flush to finish (FLUSH_WAIT_START to FLUSH_WAIT_FINISH). Nothing in
// upstream sets a flush-wait callback, so a single-buffered LVGL spins in `while (flushing)` on the task's own
// CPU: the wait is suspected to be counted as taskLVGL run time. What is left of the refresh once flush and wait
// are taken out is rendering (object tree, background image, blend into the 20-row buffer).
struct LvglStats {
    uint64_t refreshes = 0;    // refresh cycles that drew something (REFR_START..REFR_READY with a flush inside)
    uint64_t refresh_us = 0;
    uint64_t flushes = 0;      // flush callbacks, one per chunk
    uint64_t flush_us = 0;
    uint64_t flush_px = 0;     // pixels handed to those callbacks
    uint64_t waits = 0;        // flush-wait spans
    uint64_t wait_us = 0;
};

// Worst case: seven 64-bit sums come to about 170 characters.
constexpr std::size_t kBenchLvglCapacity = 256;

// `disp refreshes=.. refresh_us=.. flushes=.. flush_us=.. flush_px=.. waits=.. wait_us=..` (no terminator, no log
// prefix). Same contract as the other line builders: at most `capacity` bytes including the NUL, the length, or 0
// when it does not fit.
inline std::size_t BenchLvglLine(char* out, std::size_t capacity, const LvglStats& s) {
    if (out == nullptr || capacity == 0) {
        return 0;
    }
    const int written = std::snprintf(
        out, capacity,
        "disp refreshes=%llu refresh_us=%llu flushes=%llu flush_us=%llu flush_px=%llu waits=%llu wait_us=%llu",
        static_cast<unsigned long long>(s.refreshes), static_cast<unsigned long long>(s.refresh_us),
        static_cast<unsigned long long>(s.flushes), static_cast<unsigned long long>(s.flush_us),
        static_cast<unsigned long long>(s.flush_px), static_cast<unsigned long long>(s.waits),
        static_cast<unsigned long long>(s.wait_us));
    return written > 0 && static_cast<std::size_t>(written) < capacity ? static_cast<std::size_t>(written) : 0;
}

}  // namespace memoria

#endif  // MEMORIA_MASCOT_STATUS_H
