#include "memoria_display_hooks.h"

#include <atomic>

namespace memoria {

namespace {
std::atomic<CompanionSink> g_companion_sink{nullptr};
}  // namespace

void SetCompanionSink(CompanionSink sink) { g_companion_sink.store(sink); }

void PublishCompanion(const char* companion_id) {
    const CompanionSink sink = g_companion_sink.load();
    if (sink != nullptr && companion_id != nullptr) {
        sink(companion_id);
    }
}

}  // namespace memoria
