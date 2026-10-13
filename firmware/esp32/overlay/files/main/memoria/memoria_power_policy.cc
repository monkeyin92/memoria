#include "memoria_power_policy.h"

#include <esp_log.h>
#include <esp_timer.h>

#include "settings.h"

namespace memoria {
namespace {

constexpr char kTag[] = "MemoriaPower";
constexpr char kSettingsNamespace[] = "memoria_power";
constexpr char kWifiHoldMsKey[] = "wifi_hold_ms";
constexpr char kQuietStartKey[] = "quiet_start";
constexpr char kQuietEndKey[] = "quiet_end";

}  // namespace

PowerPolicyRegistry& PowerPolicyRegistry::GetInstance() {
    static PowerPolicyRegistry instance;
    return instance;
}

void PowerPolicyRegistry::SetPhase(PowerPhase phase) {
    const PowerPhase previous = phase_.exchange(phase);
    if (previous != phase) {
        ESP_LOGI(kTag, "phase %s -> %s", PowerPhaseName(previous), PowerPhaseName(phase));
        // MicWantedOpen is the product gate; AudioCodec still performs the I2S
        // close. Log the policy edge at MemoriaMic so demos can grep one tag.
        const bool was_open = MicWantedOpen(previous);
        const bool now_open = MicWantedOpen(phase);
        if (was_open != now_open) {
            ESP_LOGI("MemoriaMic", "mic %s ts_ms=%lld reason=phase_%s",
                     now_open ? "open" : "close",
                     static_cast<long long>(esp_timer_get_time() / 1000),
                     PowerPhaseName(phase));
        }
    }
}

void PowerPolicyRegistry::LoadFromNvs() {
    Settings settings(kSettingsNamespace, false);
    const int hold = settings.GetInt(kWifiHoldMsKey, static_cast<int>(PowerPolicy::kDefaultWifiHoldMs));
    if (hold >= 30 * 1000 && hold <= 30 * 60 * 1000) {
        wifi_hold_ms_.store(static_cast<std::uint32_t>(hold));
    }
    const std::string start = settings.GetString(kQuietStartKey, "");
    const std::string end = settings.GetString(kQuietEndKey, "");
    QuietHoursWindow window;
    if (ParseQuietHours(start.c_str(), end.c_str(), &window)) {
        quiet_ = window;
        ESP_LOGI(kTag, "quiet hours from NVS: %s-%s hold_ms=%u", start.c_str(), end.c_str(),
                 wifi_hold_ms());
    } else {
        quiet_ = QuietHoursWindow{};
        ESP_LOGI(kTag, "no quiet hours stored; hold_ms=%u", wifi_hold_ms());
    }
}

bool PowerPolicyRegistry::ApplyQuietHours(const char* start_hhmm, const char* end_hhmm) {
    if (start_hhmm == nullptr || end_hhmm == nullptr || start_hhmm[0] == '\0') {
        quiet_ = QuietHoursWindow{};
        Settings settings(kSettingsNamespace, true);
        settings.SetString(kQuietStartKey, "");
        settings.SetString(kQuietEndKey, "");
        return true;
    }
    QuietHoursWindow window;
    if (!ParseQuietHours(start_hhmm, end_hhmm, &window)) {
        ESP_LOGW(kTag, "ignoring bad quiet hours %s-%s", start_hhmm, end_hhmm);
        return false;
    }
    quiet_ = window;
    Settings settings(kSettingsNamespace, true);
    settings.SetString(kQuietStartKey, start_hhmm);
    settings.SetString(kQuietEndKey, end_hhmm);
    ESP_LOGI(kTag, "quiet hours -> %s-%s", start_hhmm, end_hhmm);
    return true;
}

bool PowerPolicyRegistry::in_quiet_hours(std::uint16_t now_minute) const {
    return InQuietHours(now_minute, quiet_);
}

bool PowerPolicyRegistry::NoteTap(std::uint32_t now_ms) {
    if (!TapDebounceAllows(now_ms, last_tap_ms())) {
        return false;
    }
    last_tap_ms_.store(now_ms);
    return true;
}

}  // namespace memoria
