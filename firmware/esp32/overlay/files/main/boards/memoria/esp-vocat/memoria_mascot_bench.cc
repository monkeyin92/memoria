// Test-rig builds only (TODOLIST M-2): the serial screenshot and the status line. Without
// CONFIG_MEMORIA_BENCH_SERIAL (the product image, always) this file compiles to nothing, so a product binary
// has no code, no string and no marker from it. The QR card carries the binding payload, which is why the
// product firmware must not have an entry that puts the screen on the USB port.

#include "memoria_mascot_display.h"

#if CONFIG_MEMORIA_BENCH_SERIAL

#include "memoria_bench_snap.h"
#include "memoria_mascot_status.h"
#include "memoria_mascot_sampler_bench.h"

#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <fcntl.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>

// The one copy of this string says "this image is a bench image": the firmware publisher refuses an image that
// has it (firmware/esp32/scripts/publish_firmware_release.py) however it got there. The board logs it at boot,
// which is also what keeps the linker from discarding it.
extern "C" const char memoria_bench_build_marker[] = "MEMORIA_BENCH_BUILD=1;";

namespace {

constexpr char kTag[] = "MemoriaBench";

// The USB-Serial-JTAG console spins on its 64-byte FIFO until the host takes a packet (about one per
// millisecond), so a 560-character line costs some ten milliseconds of busy waiting, and a whole picture
// several seconds, on a task that has the lowest priority but the idle task's. A pause every few lines lets
// the idle task run and feed the task watchdog.
constexpr std::size_t kLinesPerPause = 8;

}  // namespace

void MemoriaMascotDisplay::BenchSendSnapshot() {
    using memoria::bench::kSnapLineCapacity;
    const uint32_t id = NowMs();
    const int64_t started_us = esp_timer_get_time();

    // The USB command channel itself stays read-only (memoria_usb_command.h); the picture goes out through its
    // own descriptor on the same console. The write path takes a lock per call, so each line lands whole even
    // when another task logs in between.
    const int fd = open("/dev/secondary", O_WRONLY);
    if (fd < 0) {
        ESP_LOGW(kTag, "snap id=%08lx not sent: console open errno=%d", static_cast<unsigned long>(id), errno);
        return;
    }

    // LVGL draws the active screen (backdrop frame and every text layer on it) into a buffer of its own; the
    // display lock is held for that and not a moment longer, so the animation resumes while the picture is sent.
    lv_draw_buf_t* picture = nullptr;
    const bool locked = Lock(1000);
    if (locked) {
        picture = lv_snapshot_take(lv_screen_active(), LV_COLOR_FORMAT_RGB565);
        Unlock();
    }

    std::size_t lines = 0;
    bool wire_ok = true;
    auto emit = [&](const char* line, std::size_t length) {
        if (!wire_ok) {
            return;
        }
        char framed[kSnapLineCapacity + 1];
        std::memcpy(framed, line, length);
        framed[length] = '\n';
        if (write(fd, framed, length + 1) < 0) {
            wire_ok = false;  // no host is attached: nothing more can arrive
            return;
        }
        if (++lines % kLinesPerPause == 0) {
            vTaskDelay(1);
        }
    };

    bool sent = false;
    if (!locked) {
        char failure[kSnapLineCapacity];
        const std::size_t length = memoria::bench::SnapFailLine(failure, sizeof(failure), id, "display_busy");
        if (length > 0) {
            emit(failure, length);
        }
    } else if (picture == nullptr || picture->header.cf != LV_COLOR_FORMAT_RGB565) {
        char failure[kSnapLineCapacity];
        const std::size_t length = memoria::bench::SnapFailLine(
            failure, sizeof(failure), id, picture == nullptr ? "no_picture" : "bad_format");
        if (length > 0) {
            emit(failure, length);
        }
    } else {
        sent = memoria::bench::StreamPicture(static_cast<const uint8_t*>(picture->data), picture->header.stride,
                                             picture->header.w, picture->header.h, id, emit);
    }
    const unsigned width = picture != nullptr ? picture->header.w : 0;
    const unsigned height = picture != nullptr ? picture->header.h : 0;
    if (picture != nullptr) {
        lv_draw_buf_destroy(picture);
    }
    close(fd);
    // The stack figure is what the 12 KB of the USB command task must keep covering: the renderer's depth.
    ESP_LOGI(kTag, "snap id=%08lx sent=%d wire=%s lines=%u picture=%ux%u ms=%lld stack_free=%u",
             static_cast<unsigned long>(id), sent ? 1 : 0, wire_ok ? "ok" : "lost", static_cast<unsigned>(lines),
             width, height, static_cast<long long>((esp_timer_get_time() - started_us) / 1000),
             static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr)));
}

void MemoriaMascotDisplay::BenchLogStatus(const memoria::BenchStatus& status) {
    // Only the animation task calls this, so the buffer can live in .bss instead of on its small stack.
    static char text[memoria::kBenchStatusCapacity];
    memoria::BenchStatus line = status;
    line.heap_free = static_cast<uint32_t>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    line.psram_free = static_cast<uint32_t>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    line.anim_stack_free = static_cast<uint32_t>(uxTaskGetStackHighWaterMark(nullptr));
    if (memoria::BenchStatusLine(text, sizeof(text), line) == 0) {
        ESP_LOGW(kTag, "status line did not fit");
        return;
    }
    ESP_LOGI(kTag, "%s", text);
}

// Where the CPU went since boot, per task (TODOLIST M-6): the PC subtracts two lines and divides by the wall time
// between them. uxTaskGetSystemState stops the scheduler for a moment, so this runs once per status request only.
void MemoriaMascotDisplay::BenchLogTasks() {
    static char text[memoria::kBenchTasksCapacity];
    const UBaseType_t count = uxTaskGetNumberOfTasks();
    auto* states = static_cast<TaskStatus_t*>(heap_caps_malloc(sizeof(TaskStatus_t) * (count + 4), MALLOC_CAP_8BIT));
    if (states == nullptr) {
        ESP_LOGW(kTag, "tasks line: no memory");
        return;
    }
    uint32_t total = 0;
    const UBaseType_t got = uxTaskGetSystemState(states, count + 4, &total);
    static memoria::BenchTaskRow rows[64];
    std::size_t n = 0;
    for (UBaseType_t i = 0; i < got && n < 64; ++i) {
        memoria::BenchTaskRow& row = rows[n++];
        std::memset(&row, 0, sizeof(row));
        std::strncpy(row.name, states[i].pcTaskName, sizeof(row.name) - 1);
        const BaseType_t core = xTaskGetCoreID(states[i].xHandle);  // TaskStatus_t::xCoreID needs a Kconfig option
        row.core = core == tskNO_AFFINITY ? -1 : static_cast<int>(core);
        row.priority = states[i].uxCurrentPriority;
        row.runtime_us = states[i].ulRunTimeCounter;
    }
    heap_caps_free(states);
    memoria::SortBenchTasksByRuntime(rows, n);
    if (memoria::BenchTasksLine(text, sizeof(text), rows, n, got, static_cast<uint64_t>(esp_timer_get_time())) == 0) {
        ESP_LOGW(kTag, "tasks line did not fit");
        return;
    }
    ESP_LOGI(kTag, "%s", text);
}

void MemoriaMascotDisplay::BenchLogProfile(const memoria::RenderProfile& profile) {
    static char text[memoria::kBenchProfileCapacity];
    if (memoria::BenchProfileLine(text, sizeof(text), profile) == 0) {
        ESP_LOGW(kTag, "profile line did not fit");
        return;
    }
    ESP_LOGI(kTag, "%s", text);
}

namespace {

uint32_t SamplerBenchClock() { return static_cast<uint32_t>(esp_timer_get_time()); }

void* BenchAlloc(std::size_t bytes, bool psram) {
    return heap_caps_malloc(bytes, (psram ? MALLOC_CAP_SPIRAM : MALLOC_CAP_INTERNAL) | MALLOC_CAP_8BIT);
}

// One variant: the best of three timed passes (another task running in between only ever makes a pass slower).
// A sprite that cannot be allocated reports zero pixels, so the line says "not measured" rather than a wrong number.
memoria::SamplerBenchRow RunSamplerBenchVariant(const memoria::SamplerBenchVariant& v) {
    memoria::SamplerBenchRow result;
    const std::size_t pixels = static_cast<std::size_t>(v.w) * static_cast<std::size_t>(v.h);
    auto* rgb = static_cast<uint16_t*>(BenchAlloc(pixels * sizeof(uint16_t), v.sprite_psram));
    auto* alpha = static_cast<uint8_t*>(BenchAlloc(pixels, v.sprite_psram));
    auto* span = static_cast<uint16_t*>(BenchAlloc(static_cast<std::size_t>(v.h) * 2 * sizeof(uint16_t), v.sprite_psram));
    auto* row = static_cast<uint16_t*>(BenchAlloc(memoria::kBenchCanvas * sizeof(uint16_t), v.row_psram));
    if (rgb != nullptr && alpha != nullptr && span != nullptr && row != nullptr) {
        memoria::MascotSprite sprite;
        sprite.w = v.w;
        sprite.h = v.h;
        sprite.rgb = rgb;
        sprite.alpha = alpha;
        sprite.span = span;
        memoria::FillBenchSprite(&sprite, v.soft);
        for (int pass = 0; pass < 3; ++pass) {
            uint64_t px = 0;
            const uint32_t us = memoria::SamplerBenchPass(sprite, row, v.reps, SamplerBenchClock, &px);
            if (result.px == 0 || us < result.us) {
                result.us = us;
                result.px = px;
            }
            vTaskDelay(1);  // let the idle task feed the watchdog between passes
        }
    } else {
        ESP_LOGW(kTag, "sampler bench %s: no memory", v.name);
    }
    heap_caps_free(rgb);
    heap_caps_free(alpha);
    heap_caps_free(span);
    heap_caps_free(row);
    return result;
}

// Twice, 20 s apart, so the log shows whether the numbers repeat. Runs above the animation and LVGL tasks on their
// core, so for the second or so it takes nothing else there competes with it; the audio tasks are idle at that point.
void SamplerBenchTask(void*) {
    for (int round = 0; round < 2; ++round) {
        vTaskDelay(pdMS_TO_TICKS(20000));
        memoria::SamplerBenchRow rows[memoria::kSamplerBenchCount];
        for (std::size_t i = 0; i < memoria::kSamplerBenchCount; ++i) {
            rows[i] = RunSamplerBenchVariant(memoria::kSamplerBenchVariants[i]);
        }
        char text[memoria::kSamplerBenchLineCapacity];
        if (memoria::SamplerBenchLine(text, sizeof(text), rows) == 0) {
            ESP_LOGW(kTag, "sampler bench line did not fit");
        } else {
            ESP_LOGI(kTag, "%s round=%d", text, round);
        }
    }
    vTaskDelete(nullptr);
}

}  // namespace

void MemoriaMascotDisplay::BenchStartSamplerBench() {
    if (xTaskCreatePinnedToCore(&SamplerBenchTask, "sampler_bench", 4096, nullptr, 4, nullptr, 1) != pdPASS) {
        ESP_LOGW(kTag, "sampler bench task not started");
    }
}

#endif  // CONFIG_MEMORIA_BENCH_SERIAL
