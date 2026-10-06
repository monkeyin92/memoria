// Test-rig builds only (TODOLIST M-2): the serial screenshot and the status line. Without
// CONFIG_MEMORIA_BENCH_SERIAL (the product image, always) this file compiles to nothing, so a product binary
// has no code, no string and no marker from it. The QR card carries the binding payload, which is why the
// product firmware must not have an entry that puts the screen on the USB port.

#include "memoria_mascot_display.h"

#if CONFIG_MEMORIA_BENCH_SERIAL

#include "memoria_bench_snap.h"
#include "memoria_mascot_status.h"

#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_timer.h>
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

#endif  // CONFIG_MEMORIA_BENCH_SERIAL
