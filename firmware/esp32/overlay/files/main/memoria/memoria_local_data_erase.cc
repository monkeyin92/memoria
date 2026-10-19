#include "memoria_local_data_erase.h"

#include <esp_err.h>
#include <esp_log.h>
#include <nvs.h>

#include "settings.h"
#include "ssid_manager.h"

namespace memoria {
namespace {

constexpr const char* kTag = "MemoriaLocalDataErase";

// Keys to erase from specific NVS namespaces. Each is identified by (namespace, key).
// MUST NOT include activation_ctr (replay prevention), memoria_identity (device identity),
// or any non-child-related keys.
struct EraseKey {
    const char* namespace_name;
    const char* key;
};

constexpr EraseKey kEraseKeys[] = {
    // memoria_ui: wake mode and mascot companion settings
    {"memoria_ui", "wake_mode"},
    {"memoria_ui", "companion"},

    // memoria_power: quiet hours and Wi-Fi hold time
    {"memoria_power", "quiet_start"},
    {"memoria_power", "quiet_end"},
    {"memoria_power", "wifi_hold_ms"},

    // memoria: wake word configuration
    {"memoria", "wake_word_id"},
    {"memoria", "wake_word_cmd"},
    {"memoria", "wake_word_disp"},
};

}  // namespace

bool EraseLocalChildData() {
    bool success = true;

    // Erase specific child-related keys from NVS namespaces first
    // Using nvs_erase_key to preserve non-child-related keys in the same namespaces
    for (const auto& erase_key : kEraseKeys) {
        nvs_handle_t handle;
        esp_err_t result = nvs_open(erase_key.namespace_name, NVS_READWRITE, &handle);

        if (result == ESP_ERR_NVS_NOT_FOUND) {
            ESP_LOGD(kTag, "NVS namespace '%s' not found (skipping)", erase_key.namespace_name);
            continue;
        }

        if (result != ESP_OK) {
            ESP_LOGW(kTag, "Failed to open NVS namespace '%s': %s",
                     erase_key.namespace_name, esp_err_to_name(result));
            success = false;
            continue;
        }

        esp_err_t erase_result = nvs_erase_key(handle, erase_key.key);

        // ESP_ERR_NVS_NOT_FOUND is not an error; the key simply wasn't there
        if (erase_result != ESP_OK && erase_result != ESP_ERR_NVS_NOT_FOUND) {
            ESP_LOGW(kTag, "Failed to erase NVS key '%s' in namespace '%s': %s",
                     erase_key.key, erase_key.namespace_name,
                     esp_err_to_name(erase_result));
            nvs_close(handle);
            success = false;
            continue;
        }

        esp_err_t commit_result = nvs_commit(handle);
        if (commit_result != ESP_OK) {
            ESP_LOGW(kTag, "Failed to commit erase of key '%s' in namespace '%s': %s",
                     erase_key.key, erase_key.namespace_name,
                     esp_err_to_name(commit_result));
            nvs_close(handle);
            success = false;
            continue;
        }

        nvs_close(handle);
        ESP_LOGI(kTag, "Erased NVS key '%s' in namespace '%s'",
                 erase_key.key, erase_key.namespace_name);
    }

    // Erase Wi-Fi credentials LAST, only after all NVS keys succeeded
    // The Clear() method erases all saved SSIDs and passwords from NVS "wifi" namespace.
    if (!SsidManager::GetInstance().Clear()) {
        ESP_LOGW(kTag, "Failed to clear Wi-Fi credentials");
        success = false;
    } else {
        ESP_LOGI(kTag, "Erased Wi-Fi SSID/password credentials");
    }

    if (success) {
        ESP_LOGI(kTag, "All child-related local data erased successfully");
    } else {
        ESP_LOGW(kTag, "Local data erase completed with errors");
    }
    return success;
}

}  // namespace memoria
