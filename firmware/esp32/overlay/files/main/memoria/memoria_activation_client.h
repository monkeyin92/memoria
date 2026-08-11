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

// Fetches, verifies and acknowledges the server-signed Activation Manifest.
// The device private key never leaves DeviceIdentity and no bearer material is logged.
class MemoriaActivationClient final {
public:
    explicit MemoriaActivationClient(const DeviceIdentity& identity) : identity_(identity) {}

    esp_err_t Activate(ActivationProfile* profile);

private:
    const DeviceIdentity& identity_;
};

}  // namespace memoria
