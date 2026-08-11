#include "device_identity.h"

#include <algorithm>
#include <utility>

#include "nvs.h"
#include "nvs_flash.h"
#include "sodium.h"

namespace memoria {
namespace {

constexpr const char* kDeviceIdKey = "device_id";
constexpr const char* kCertificateIdKey = "certificate_id";
constexpr const char* kClientIdKey = "client_id";
constexpr const char* kSigningSeedKey = "ed25519_seed";
constexpr const char* kActivationPublicKeyKey = "activation_pk";
constexpr const char* kControlApiUrlKey = "control_api_url";

constexpr size_t kIdMaxBytes = 127;
constexpr size_t kUrlMaxBytes = 255;

bool IsVisibleAscii(const std::string& value) {
    if (value.empty()) {
        return false;
    }
    return std::all_of(value.begin(), value.end(), [](unsigned char character) {
        return character >= 0x21 && character <= 0x7e;
    });
}

bool IsControlApiUrl(const std::string& value) {
    if (!IsVisibleAscii(value)) {
        return false;
    }
    const bool https = value.rfind("https://", 0) == 0;
    const bool http = value.rfind("http://", 0) == 0;
    if (!https && !http) {
        return false;
    }
    const size_t authority_start = value.find("://") + 3;
    return authority_start < value.size() && value.find('/', authority_start) != authority_start;
}

esp_err_t ReadStrictString(nvs_handle_t handle,
                           const char* key,
                           size_t max_bytes,
                           std::string* output) {
    if (output == nullptr) {
        return ESP_ERR_INVALID_ARG;
    }

    size_t required = 0;
    esp_err_t err = nvs_get_str(handle, key, nullptr, &required);
    if (err != ESP_OK) {
        return err;
    }
    if (required < 2 || required > max_bytes + 1) {
        return ESP_ERR_NVS_INVALID_LENGTH;
    }

    std::string value(required, '\0');
    size_t actual = required;
    err = nvs_get_str(handle, key, value.data(), &actual);
    if (err != ESP_OK) {
        return err;
    }
    if (actual != required || value.back() != '\0') {
        return ESP_ERR_NVS_INVALID_LENGTH;
    }
    value.pop_back();
    if (!IsVisibleAscii(value)) {
        return ESP_ERR_INVALID_ARG;
    }
    *output = std::move(value);
    return ESP_OK;
}

esp_err_t ReadFixedBlob(nvs_handle_t handle,
                        const char* key,
                        uint8_t* output,
                        size_t expected_size) {
    if (output == nullptr || expected_size == 0) {
        return ESP_ERR_INVALID_ARG;
    }
    size_t actual = 0;
    esp_err_t err = nvs_get_blob(handle, key, nullptr, &actual);
    if (err != ESP_OK) {
        return err;
    }
    if (actual != expected_size) {
        return ESP_ERR_NVS_INVALID_LENGTH;
    }
    return nvs_get_blob(handle, key, output, &actual);
}

bool IsAllZero(const uint8_t* value, size_t size) {
    return std::all_of(value, value + size, [](uint8_t byte) { return byte == 0; });
}

}  // namespace

DeviceIdentity::~DeviceIdentity() {
    Clear();
}

void DeviceIdentity::Clear() {
    loaded_ = false;
    device_id_.clear();
    certificate_id_.clear();
    client_id_.clear();
    control_api_url_.clear();
    std::fill(activation_public_key_.begin(), activation_public_key_.end(), 0);
    sodium_memzero(signing_secret_key_.data(), signing_secret_key_.size());
}

esp_err_t DeviceIdentity::Load() {
    Clear();

    if (sodium_init() < 0) {
        return ESP_FAIL;
    }

    std::array<uint8_t, kEd25519SeedBytes> signing_seed{};
    std::array<uint8_t, kEd25519PublicKeyBytes> derived_public_key{};
    nvs_handle_t handle = 0;
    bool opened = false;
    esp_err_t result = nvs_flash_init_partition(kPartitionLabel);
    if (result == ESP_OK) {
        result = nvs_open_from_partition(kPartitionLabel, kNamespace, NVS_READONLY, &handle);
        opened = result == ESP_OK;
    }
    if (result == ESP_OK) {
        result = ReadStrictString(handle, kDeviceIdKey, kIdMaxBytes, &device_id_);
    }
    if (result == ESP_OK) {
        result = ReadStrictString(handle, kCertificateIdKey, kIdMaxBytes, &certificate_id_);
    }
    if (result == ESP_OK) {
        result = ReadStrictString(handle, kClientIdKey, kIdMaxBytes, &client_id_);
    }
    if (result == ESP_OK) {
        result = ReadFixedBlob(handle, kSigningSeedKey, signing_seed.data(), signing_seed.size());
    }
    if (result == ESP_OK) {
        result = ReadFixedBlob(handle,
                               kActivationPublicKeyKey,
                               activation_public_key_.data(),
                               activation_public_key_.size());
    }
    if (result == ESP_OK) {
        result = ReadStrictString(handle, kControlApiUrlKey, kUrlMaxBytes, &control_api_url_);
        if (result == ESP_OK && !IsControlApiUrl(control_api_url_)) {
            result = ESP_ERR_INVALID_ARG;
        }
    }
    if (opened) {
        nvs_close(handle);
    }

    if (result == ESP_OK && IsAllZero(activation_public_key_.data(), activation_public_key_.size())) {
        result = ESP_ERR_INVALID_ARG;
    }
    if (result == ESP_OK &&
        crypto_sign_seed_keypair(derived_public_key.data(),
                                 signing_secret_key_.data(),
                                 signing_seed.data()) != 0) {
        result = ESP_FAIL;
    }
    sodium_memzero(signing_seed.data(), signing_seed.size());
    sodium_memzero(derived_public_key.data(), derived_public_key.size());

    if (result != ESP_OK) {
        Clear();
        return result;
    }
    loaded_ = true;
    return ESP_OK;
}

esp_err_t DeviceIdentity::SignDetached(const uint8_t* message,
                                       size_t message_size,
                                       uint8_t* signature,
                                       size_t signature_capacity) const {
    if (!loaded_) {
        return ESP_ERR_INVALID_STATE;
    }
    if ((message == nullptr && message_size != 0) || signature == nullptr) {
        return ESP_ERR_INVALID_ARG;
    }
    if (signature_capacity < kEd25519SignatureBytes) {
        return ESP_ERR_INVALID_SIZE;
    }

    unsigned long long signature_size = 0;
    const int result = crypto_sign_detached(signature,
                                            &signature_size,
                                            message,
                                            static_cast<unsigned long long>(message_size),
                                            signing_secret_key_.data());
    if (result != 0 || signature_size != kEd25519SignatureBytes) {
        std::fill(signature, signature + kEd25519SignatureBytes, 0);
        return ESP_FAIL;
    }
    return ESP_OK;
}

}  // namespace memoria
