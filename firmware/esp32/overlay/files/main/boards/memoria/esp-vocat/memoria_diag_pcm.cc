// Test-rig builds only (TODOLIST N-10): the device side of the PCM journal in memoria_diag_pcm.h. Without
// CONFIG_MEMORIA_BENCH_SERIAL (the product image, always) this file compiles to nothing.

#include "sdkconfig.h"

#if CONFIG_MEMORIA_BENCH_SERIAL

#include "memoria_diag_pcm.h"

#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_timer.h>
#include <fcntl.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <unistd.h>

#include <cerrno>

namespace memoria {
namespace diag {
namespace {

constexpr char kTag[] = "MemoriaDiagPcm";
// What the rest of the firmware keeps after the arena is taken: the display, the decoder and the AFE allocate
// from PSRAM while a capture runs.
constexpr std::size_t kPsramReserveBytes = 1024 * 1024;
// See kLinesPerPause in memoria_mascot_bench.cc: the console busy-waits, the idle task must still run.
constexpr std::size_t kLinesPerPause = 8;

constinit Recorder g_recorder;

std::uint32_t NowMs() { return static_cast<std::uint32_t>(esp_timer_get_time() / 1000); }

void LogFail(std::uint32_t id, const char* reason) {
    char line[kPcmdLineCapacity];
    if (PcmdFailLine(line, sizeof(line), id, reason) > 0) {
        ESP_LOGI(kTag, "%s", line);
    }
}

}  // namespace

void RecordFeed(const std::int16_t* interleaved, std::size_t frames, int channels, std::uint8_t flags) {
    g_recorder.RecordFeed(interleaved, frames, channels, flags, esp_timer_get_time());
}

void RecordPostAfe(const std::int16_t* pcm, std::size_t samples, std::uint8_t flags) {
    g_recorder.RecordPostAfe(pcm, samples, flags, esp_timer_get_time());
}

void RecordPlay(const std::int16_t* pcm, std::size_t samples, int sample_rate, std::uint8_t flags) {
    g_recorder.RecordPlay(pcm, samples, sample_rate, flags, esp_timer_get_time());
}

void RecordRawTdm(const std::int16_t* interleaved, std::size_t frames, int channels) {
    g_recorder.RecordRawTdm(interleaved, frames, channels, esp_timer_get_time());
}

void Arm() {
    const std::uint32_t id = NowMs();
    // The arena is taken on the first arm and kept until reboot: a bench image that never records costs nothing.
    if (!g_recorder.attached()) {
        if (heap_caps_get_free_size(MALLOC_CAP_SPIRAM) >= kArenaBytes + kPsramReserveBytes) {
            g_recorder.Attach(static_cast<std::uint8_t*>(heap_caps_malloc(kArenaBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)));
        }
    }
    const char* refused = g_recorder.Arm(id, esp_timer_get_time());
    if (refused != nullptr) {
        LogFail(g_recorder.state() == State::kIdle ? id : g_recorder.id(), refused);
        return;
    }
    char line[kPcmdLineCapacity];
    if (PcmdArmedLine(line, sizeof(line), id, static_cast<std::uint32_t>(heap_caps_get_free_size(MALLOC_CAP_SPIRAM))) > 0) {
        ESP_LOGI(kTag, "%s", line);
    }
}

void Tick() {
    if (!g_recorder.Tick(esp_timer_get_time())) {
        return;
    }
    char line[kPcmdLineCapacity];
    if (PcmdSealedLine(line, sizeof(line), g_recorder.View()) > 0) {
        ESP_LOGI(kTag, "%s", line);
    }
}

void Dump() {
    const char* refused = g_recorder.BeginDump();
    if (refused != nullptr) {
        LogFail(g_recorder.state() == State::kIdle ? NowMs() : g_recorder.id(), refused);
        return;
    }
    const std::uint32_t id = g_recorder.id();
    const std::int64_t started_us = esp_timer_get_time();
    const int fd = open("/dev/secondary", O_WRONLY);
    if (fd < 0) {
        g_recorder.EndDump();
        LogFail(id, "console");
        return;
    }
    std::size_t lines = 0;
    bool wire_ok = true;
    auto emit = [&](const char* line, std::size_t length) {
        if (!wire_ok) {
            return;
        }
        char framed[kPcmdLineCapacity + 1];
        std::memcpy(framed, line, length);
        framed[length] = '\n';
        if (write(fd, framed, length + 1) < 0) {
            wire_ok = false;
            return;
        }
        if (++lines % kLinesPerPause == 0) {
            vTaskDelay(1);
        }
    };
    const bool sent = StreamJournal(g_recorder.View(), id, emit);
    close(fd);
    g_recorder.EndDump();
    ESP_LOGI(kTag, "pcmdump id=%08lx sent=%d wire=%s lines=%u ms=%lld stack_free=%u", static_cast<unsigned long>(id),
             sent ? 1 : 0, wire_ok ? "ok" : "lost", static_cast<unsigned>(lines),
             static_cast<long long>((esp_timer_get_time() - started_us) / 1000),
             static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr)));
}

}  // namespace diag
}  // namespace memoria

#endif  // CONFIG_MEMORIA_BENCH_SERIAL
