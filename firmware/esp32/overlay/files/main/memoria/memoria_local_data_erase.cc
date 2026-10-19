#include "memoria_local_data_erase.h"

#include <esp_err.h>
#include <esp_log.h>
#include <nvs.h>

#include "settings.h"
#include "wifi_provisioning/wifi_config.h"

namespace memoria {
namespace {

constexpr const char* kTag = "MemoriaLocalDataErase";

// NVS namespaces to erase
constexpr const char* kEraseNamespaces[] = {
    "memoria_runtime",   // activation_v, activation_ctr
    "memoria_ui",        // wake_mode, companion
    "memoria_power",     // quiet_start, quiet_end, wifi_hold_ms
    "memoria",           // wake_word_id, wake_word_cmd, wake_word_disp
};

}  // namespace

bool EraseLocalChildData() {
    bool success = true;

    // Erase Wi-Fi SSID and password via SDK API
    wifi_config_t wifi_config;
    wifi_config.sta.ssid[0] = '\0';
    wifi_config.sta.password[0] = '\0';
    wifi_config.sta.bssid_set = false;
    esp_err_t wifi_result =
        esp_wifi_set_config(WIFI_IF_STA, &wifi_config);
    if (wifi_result != ESP_OK) {
        ESP_LOGW(kTag, "Failed to erase Wi-Fi config: %s", esp_err_to_name(wifi_result));
        success = false;
    } else {
        ESP_LOGI(kTag, "Erased Wi-Fi SSID and password");
    }

    // Erase each NVS namespace related to child settings
    for (const char* namespace_name : kEraseNamespaces) {
        nvs_handle_t handle;
        esp_err_t result = nvs_open(namespace_name, NVS_READWRITE, &handle);
        if (result == ESP_ERR_NVS_NOT_FOUND) {
            ESP_LOGD(kTag, "NVS namespace '%s' not found (not erasing)", namespace_name);
            continue;
        }
        if (result != ESP_OK) {
            ESP_LOGW(kTag, "Failed to open NVS namespace '%s': %s", namespace_name,
                     esp_err_to_name(result));
            success = false;
            continue;
        }

        esp_err_t erase_result = nvs_erase_all(handle);
        if (erase_result != ESP_OK) {
            ESP_LOGW(kTag, "Failed to erase NVS namespace '%s': %s", namespace_name,
                     esp_err_to_name(erase_result));
            nvs_close(handle);
            success = false;
            continue;
        }

        esp_err_t commit_result = nvs_commit(handle);
        if (commit_result != ESP_OK) {
            ESP_LOGW(kTag, "Failed to commit erase of NVS namespace '%s': %s", namespace_name,
                     esp_err_to_name(commit_result));
            nvs_close(handle);
            success = false;
            continue;
        }

        nvs_close(handle);
        ESP_LOGI(kTag, "Erased NVS namespace '%s'", namespace_name);
    }

    if (success) {
        ESP_LOGI(kTag, "All local child data erased successfully");
    } else {
        ESP_LOGW(kTag, "Local data erase completed with errors");
    }
    return success;
}

}  // namespace memoria
