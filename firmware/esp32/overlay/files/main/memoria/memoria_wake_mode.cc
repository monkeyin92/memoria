#include "memoria_wake_mode.h"

#include <esp_log.h>

#include "settings.h"

namespace memoria {
namespace {

constexpr char kTag[] = "MemoriaWakeMode";
constexpr char kSettingsNamespace[] = "memoria_ui";
constexpr char kWakeModeKey[] = "wake_mode";

}  // namespace

WakeModeRegistry& WakeModeRegistry::GetInstance() {
    static WakeModeRegistry instance;
    return instance;
}

void WakeModeRegistry::LoadFromNvs() {
    Settings settings(kSettingsNamespace, false);
    const std::string stored = settings.GetString(kWakeModeKey, "");
    WakeMode loaded = WakeMode::kBoth;
    if (!ParseWakeMode(stored.c_str(), &loaded)) {
        ESP_LOGI(kTag, "no stored wake mode; keeping %s", WakeModeName(mode()));
        return;
    }
    mode_.store(loaded);
    ESP_LOGI(kTag, "wake mode from NVS: %s (keyword=%d tap=%d)", WakeModeName(loaded),
             KeywordWakeEnabled(loaded) ? 1 : 0, TapWakeEnabled(loaded) ? 1 : 0);
}

bool WakeModeRegistry::Apply(const std::string& wire) {
    WakeMode next = WakeMode::kBoth;
    if (!ParseWakeMode(wire.c_str(), &next)) {
        ESP_LOGW(kTag, "ignoring unknown wake mode from the server: %s", wire.c_str());
        return false;
    }
    const WakeMode previous = mode_.exchange(next);
    if (previous == next) {
        return false;
    }
    Settings settings(kSettingsNamespace, true);
    settings.SetString(kWakeModeKey, WakeModeName(next));
    ESP_LOGI(kTag, "wake mode %s -> %s (keyword=%d tap=%d)", WakeModeName(previous),
             WakeModeName(next), KeywordWakeEnabled(next) ? 1 : 0, TapWakeEnabled(next) ? 1 : 0);
    return true;
}

}  // namespace memoria
