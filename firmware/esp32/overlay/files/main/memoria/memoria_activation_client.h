#pragma once

#include <cstdint>
#include <string>

#include "device_identity.h"
#include "esp_err.h"

namespace memoria {

struct ActivationProfile {
    std::string activation_id;
    int32_t activation_version = 0;
    std::string binding_id;
    int32_t binding_version = 0;
    std::string control_api_url;
    std::string device_media_url;
};

// Which companion the bound account picked, for the device screen.
struct DisplayProfile {
    std::string companion_id;
    std::string display_version;  // opaque; changes when the companion changes
};

// Fetches, verifies and acknowledges the server-signed Activation Manifest.
// The device private key never leaves DeviceIdentity and no bearer material is logged.
class MemoriaActivationClient final {
public:
    explicit MemoriaActivationClient(const DeviceIdentity& identity) : identity_(identity) {}

    esp_err_t Activate(ActivationProfile* profile);

    // Device-signed GET /v1/devices/{id}/display-profile (same request
    // signature as the manifest). ESP_ERR_INVALID_STATE when unbound.
    esp_err_t FetchDisplayProfile(const std::string& control_api_url, DisplayProfile* profile);

private:
    const DeviceIdentity& identity_;
};

}  // namespace memoria
