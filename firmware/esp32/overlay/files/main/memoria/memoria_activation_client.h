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
    // Always tap-only on this product (`button`). Empty when the server omitted
    // it: the device keeps tap-only behaviour.
    std::string wake_mode;
    // Guardian quiet hours HH:MM, additive. Empty when absent.
    std::string quiet_hours_start;
    std::string quiet_hours_end;
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

    // X-Device-Signature value for a device-signed GET of `path` (the
    // manifest's request object with this path). Empty on failure.
    std::string SignGetRequest(const std::string& path) const;

    // Best-effort failure detail for logs and the error page. The upstream
    // Http interface has no separate TLS error accessor; GetLastError() on a
    // failed Open() is the only transport/TLS code this overlay can see, and
    // it is 0 when the failure happened after Open() succeeded. The HTTP
    // status is 0 when no response status was reached.
    int last_http_status() const { return last_http_status_; }
    int last_transport_error() const { return last_transport_error_; }

private:
    const DeviceIdentity& identity_;
    int last_http_status_ = 0;
    int last_transport_error_ = 0;
};

}  // namespace memoria
