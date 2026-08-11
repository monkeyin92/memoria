#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <string>

#include "esp_err.h"

namespace memoria {

class DeviceIdentity final {
public:
    static constexpr size_t kEd25519SeedBytes = 32;
    static constexpr size_t kEd25519PublicKeyBytes = 32;
    static constexpr size_t kEd25519SignatureBytes = 64;

    DeviceIdentity() = default;
    ~DeviceIdentity();

    DeviceIdentity(const DeviceIdentity&) = delete;
    DeviceIdentity& operator=(const DeviceIdentity&) = delete;

    // Load the complete, immutable identity from the dedicated NVS partition.
    // A partial identity is rejected and leaves this object unloaded.
    esp_err_t Load();

    bool loaded() const { return loaded_; }
    const std::string& device_id() const { return device_id_; }
    const std::string& certificate_id() const { return certificate_id_; }
    const std::string& client_id() const { return client_id_; }
    const std::string& control_api_url() const { return control_api_url_; }
    const std::array<uint8_t, kEd25519PublicKeyBytes>& activation_public_key() const {
        return activation_public_key_;
    }

    // Sign an arbitrary detached message with the seed-derived Ed25519 key.
    // The seed and derived secret key are never returned or logged.
    esp_err_t SignDetached(const uint8_t* message,
                           size_t message_size,
                           uint8_t* signature,
                           size_t signature_capacity) const;

    void Clear();

private:
    static constexpr const char* kPartitionLabel = "memoria_identity";
    static constexpr const char* kNamespace = "device";

    bool loaded_ = false;
    std::string device_id_;
    std::string certificate_id_;
    std::string client_id_;
    std::string control_api_url_;
    std::array<uint8_t, kEd25519PublicKeyBytes> activation_public_key_{};
    std::array<uint8_t, 64> signing_secret_key_{};
};

}  // namespace memoria
