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
    WakeMode loaded = WakeMode::kTapOnly;
    if (!ParseWakeMode(stored.c_str(), &loaded)) {
        ESP_LOGI(kTag, "no stored wake mode; tap-only");
        return;
    }
    mode_.store(WakeMode::kTapOnly);
    if (stored != "button") {
        Settings writable(kSettingsNamespace, true);
        writable.SetString(kWakeModeKey, "button");
        ESP_LOGI(kTag, "wake mode from NVS coerced %s -> button (tap-only)", stored.c_str());
        return;
    }
    ESP_LOGI(kTag, "wake mode from NVS: button (tap-only)");
}

bool WakeModeRegistry::Apply(const std::string& wire) {
    WakeMode next = WakeMode::kTapOnly;
    if (!ParseWakeMode(wire.c_str(), &next)) {
        ESP_LOGW(kTag, "ignoring unknown wake mode from the server: %s", wire.c_str());
        return false;
    }
    mode_.store(WakeMode::kTapOnly);
    Settings settings(kSettingsNamespace, true);
    const std::string previous = settings.GetString(kWakeModeKey, "");
    if (previous == "button") {
        return false;
    }
    settings.SetString(kWakeModeKey, "button");
    ESP_LOGI(kTag, "wake mode %s -> button (tap-only)", previous.empty() ? "(none)" : previous.c_str());
    return true;
}

}  // namespace memoria
