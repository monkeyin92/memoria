#include "memoria_activation_client.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string_view>
#include <sys/time.h>
#include <vector>

#include "board.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "http.h"
#include "settings.h"
#include "sodium.h"

#include <cJSON.h>

namespace memoria {
namespace {

constexpr const char* kTag = "MemoriaActivation";
constexpr int kHttpTimeoutMs = 10000;
constexpr time_t kMinimumTrustedUnixTime = 1700000000;
constexpr time_t kMaximumManifestLifetimeSeconds = 31 * 24 * 60 * 60;

struct ScopedJson final {
    cJSON* value = nullptr;
    ~ScopedJson() { cJSON_Delete(value); }
};

std::string JoinUrl(const std::string& base, const std::string& path) {
    if (base.empty()) {
        return {};
    }
    if (base.back() == '/') {
        return base.substr(0, base.size() - 1) + path;
    }
    return base + path;
}

std::string JsonString(const char* value) {
    ScopedJson json{cJSON_CreateString(value == nullptr ? "" : value)};
    if (json.value == nullptr) {
        return {};
    }
    char* rendered = cJSON_PrintUnformatted(json.value);
    if (rendered == nullptr) {
        return {};
    }
    std::string result(rendered);
    cJSON_free(rendered);
    return result;
}

bool CanonicalJson(const cJSON* value,
                   std::string* output,
                   std::string_view excluded_a = {},
                   std::string_view excluded_b = {}) {
    if (value == nullptr || output == nullptr) {
        return false;
    }
    if (cJSON_IsObject(value)) {
        std::vector<const cJSON*> children;
        for (const cJSON* child = value->child; child != nullptr; child = child->next) {
            if (child->string == nullptr || child->string == excluded_a || child->string == excluded_b) {
                continue;
            }
            children.push_back(child);
        }
        std::sort(children.begin(), children.end(), [](const cJSON* left, const cJSON* right) {
            return std::strcmp(left->string, right->string) < 0;
        });
        output->push_back('{');
        for (size_t index = 0; index < children.size(); ++index) {
            if (index != 0) {
                output->push_back(',');
            }
            const std::string key = JsonString(children[index]->string);
            if (key.empty()) {
                return false;
            }
            output->append(key);
            output->push_back(':');
            // Exclusions apply only to this object. Nested objects have their own schema.
            if (!CanonicalJson(children[index], output)) {
                return false;
            }
        }
        output->push_back('}');
        return true;
    }
    if (cJSON_IsArray(value)) {
        output->push_back('[');
        size_t index = 0;
        for (const cJSON* child = value->child; child != nullptr; child = child->next, ++index) {
            if (index != 0) {
                output->push_back(',');
            }
            if (!CanonicalJson(child, output)) {
                return false;
            }
        }
        output->push_back(']');
        return true;
    }
    if (cJSON_IsString(value)) {
        const std::string rendered = JsonString(value->valuestring);
        if (rendered.empty()) {
            return false;
        }
        output->append(rendered);
        return true;
    }
    if (cJSON_IsNumber(value)) {
        if (value->valuedouble != static_cast<double>(value->valueint)) {
            return false;
        }
        output->append(std::to_string(value->valueint));
        return true;
    }
    if (cJSON_IsBool(value)) {
        output->append(cJSON_IsTrue(value) ? "true" : "false");
        return true;
    }
    if (cJSON_IsNull(value)) {
        output->append("null");
        return true;
    }
    return false;
}

bool RequireExactObjectKeys(const cJSON* object, const std::vector<std::string_view>& expected) {
    if (!cJSON_IsObject(object)) {
        return false;
    }
    size_t count = 0;
    for (const cJSON* child = object->child; child != nullptr; child = child->next) {
        if (child->string == nullptr ||
            std::find(expected.begin(), expected.end(), child->string) == expected.end()) {
            return false;
        }
        ++count;
    }
    return count == expected.size();
}

bool RequiredString(const cJSON* object, const char* key, std::string* output, size_t max_length) {
    const cJSON* value = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsString(value) || value->valuestring == nullptr) {
        return false;
    }
    const size_t length = std::strlen(value->valuestring);
    if (length == 0 || length > max_length) {
        return false;
    }
    if (output != nullptr) {
        *output = value->valuestring;
    }
    return true;
}

bool RequiredPositiveInt(const cJSON* object, const char* key, int32_t* output) {
    const cJSON* value = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsNumber(value) || value->valueint < 1 ||
        value->valuedouble != static_cast<double>(value->valueint)) {
        return false;
    }
    if (output != nullptr) {
        *output = value->valueint;
    }
    return true;
}

std::string Base64UrlEncode(const uint8_t* data, size_t size) {
    std::vector<char> encoded(sodium_base64_ENCODED_LEN(size, sodium_base64_VARIANT_URLSAFE_NO_PADDING));
    sodium_bin2base64(encoded.data(), encoded.size(), data, size,
                      sodium_base64_VARIANT_URLSAFE_NO_PADDING);
    return encoded.data();
}

bool Base64UrlDecode(const std::string& encoded, uint8_t* output, size_t expected_size) {
    size_t actual_size = 0;
    return output != nullptr &&
           sodium_base642bin(output, expected_size, encoded.data(), encoded.size(), nullptr,
                             &actual_size, nullptr,
                             sodium_base64_VARIANT_URLSAFE_NO_PADDING) == 0 &&
           actual_size == expected_size;
}

std::string Sha256Hex(const std::string& input) {
    std::array<uint8_t, crypto_hash_sha256_BYTES> digest{};
    crypto_hash_sha256(digest.data(), reinterpret_cast<const uint8_t*>(input.data()), input.size());
    static constexpr char kHex[] = "0123456789abcdef";
    std::string output(digest.size() * 2, '0');
    for (size_t index = 0; index < digest.size(); ++index) {
        output[index * 2] = kHex[digest[index] >> 4];
        output[index * 2 + 1] = kHex[digest[index] & 0x0f];
    }
    sodium_memzero(digest.data(), digest.size());
    return output;
}

bool ConstantTimeEquals(const std::string& left, const std::string& right) {
    return left.size() == right.size() && sodium_memcmp(left.data(), right.data(), left.size()) == 0;
}

bool ParseRfc3339Utc(const std::string& value, time_t* timestamp) {
    if (timestamp == nullptr || value.size() < 20 || value.back() != 'Z') {
        return false;
    }
    std::string whole_seconds;
    if (value.size() == 20) {
        whole_seconds = value;
    } else {
        const size_t fractional_digits = value.size() - 21;
        if (value[19] != '.' || fractional_digits == 0 || fractional_digits > 9 ||
            !std::all_of(value.begin() + 20, value.end() - 1,
                         [](unsigned char character) { return character >= '0' && character <= '9'; })) {
            return false;
        }
        whole_seconds = value.substr(0, 19) + "Z";
    }
    std::tm parsed{};
    char* end = strptime(whole_seconds.c_str(), "%Y-%m-%dT%H:%M:%SZ", &parsed);
    if (end == nullptr || *end != '\0') {
        return false;
    }
    errno = 0;
    const time_t result = timegm(&parsed);
    if (result == static_cast<time_t>(-1) && errno != 0) {
        return false;
    }
    *timestamp = result;
    return true;
}

std::string FormatRfc3339Utc(time_t timestamp) {
    std::tm value{};
    gmtime_r(&timestamp, &value);
    std::array<char, 32> output{};
    if (std::strftime(output.data(), output.size(), "%Y-%m-%dT%H:%M:%SZ", &value) == 0) {
        return {};
    }
    return output.data();
}

bool HttpRequest(const std::string& method,
                 const std::string& url,
                 const std::vector<std::pair<std::string, std::string>>& headers,
                 std::string body,
                 std::string* response) {
    auto network = Board::GetInstance().GetNetwork();
    auto http = network->CreateHttp(0);
    if (http == nullptr) {
        return false;
    }
    http->SetTimeout(kHttpTimeoutMs);
    http->SetHeader("Accept", "application/json");
    for (const auto& [key, value] : headers) {
        http->SetHeader(key, value);
    }
    if (!body.empty()) {
        http->SetHeader("Content-Type", "application/json");
        http->SetContent(std::move(body));
    }
    if (!http->Open(method, url)) {
        ESP_LOGE(kTag, "Activation HTTP transport failed, code=0x%x", http->GetLastError());
        return false;
    }
    const int status = http->GetStatusCode();
    if (status != 200) {
        ESP_LOGE(kTag, "Activation HTTP rejected, status=%d", status);
        http->Close();
        return false;
    }
    if (response != nullptr) {
        *response = http->ReadAll();
    }
    http->Close();
    return true;
}

std::string SignedRequestHeader(const DeviceIdentity& identity, const std::string& payload) {
    std::array<uint8_t, DeviceIdentity::kEd25519SignatureBytes> signature{};
    if (identity.SignDetached(reinterpret_cast<const uint8_t*>(payload.data()), payload.size(),
                              signature.data(), signature.size()) != ESP_OK) {
        return {};
    }
    std::string encoded = Base64UrlEncode(signature.data(), signature.size());
    sodium_memzero(signature.data(), signature.size());
    return encoded;
}

bool VerifyManifest(const DeviceIdentity& identity,
                    const cJSON* manifest,
                    ActivationProfile* profile,
                    std::string* config_hash,
                    std::string* expires_at) {
    static const std::vector<std::string_view> kManifestKeys = {
        "schema_version", "activation_id", "activation_version", "device_id", "binding_id",
        "binding_version", "persona_assignment_id", "service_profile_version",
        "policy_bundle_version", "runtime_profile_version", "locale", "timezone", "display",
        "endpoints", "config_hash", "issued_at", "expires_at", "signature"};
    if (profile == nullptr || config_hash == nullptr || expires_at == nullptr ||
        !RequireExactObjectKeys(manifest, kManifestKeys)) {
        return false;
    }
    const cJSON* schema_version = cJSON_GetObjectItemCaseSensitive(manifest, "schema_version");
    if (!cJSON_IsNumber(schema_version) || schema_version->valueint != 1) {
        return false;
    }
    std::string device_id;
    std::string issued_at;
    std::string signature_b64;
    if (!RequiredString(manifest, "activation_id", &profile->activation_id, 128) ||
        !RequiredPositiveInt(manifest, "activation_version", &profile->activation_version) ||
        !RequiredString(manifest, "device_id", &device_id, 128) ||
        device_id != identity.device_id() ||
        !RequiredString(manifest, "binding_id", &profile->binding_id, 128) ||
        !RequiredPositiveInt(manifest, "binding_version", &profile->binding_version) ||
        !RequiredString(manifest, "config_hash", config_hash, 64) || config_hash->size() != 64 ||
        !RequiredString(manifest, "issued_at", &issued_at, 64) ||
        !RequiredString(manifest, "expires_at", expires_at, 64) ||
        !RequiredString(manifest, "signature", &signature_b64, 128)) {
        return false;
    }
    const cJSON* display = cJSON_GetObjectItemCaseSensitive(manifest, "display");
    const cJSON* endpoints = cJSON_GetObjectItemCaseSensitive(manifest, "endpoints");
    if (!RequireExactObjectKeys(display, {"robot_name", "primary_subject_display_name"}) ||
        !RequireExactObjectKeys(endpoints, {"control_api", "device_media"}) ||
        !RequiredString(display, "robot_name", nullptr, 128) ||
        !RequiredString(display, "primary_subject_display_name", nullptr, 128) ||
        !RequiredString(endpoints, "control_api", &profile->control_api_url, 512) ||
        !RequiredString(endpoints, "device_media", &profile->device_media_url, 512)) {
        return false;
    }

    std::string unsigned_manifest;
    if (!CanonicalJson(manifest, &unsigned_manifest, "signature")) {
        return false;
    }
    std::array<uint8_t, DeviceIdentity::kEd25519SignatureBytes> signature{};
    if (!Base64UrlDecode(signature_b64, signature.data(), signature.size()) ||
        crypto_sign_verify_detached(signature.data(),
                                    reinterpret_cast<const uint8_t*>(unsigned_manifest.data()),
                                    unsigned_manifest.size(),
                                    identity.activation_public_key().data()) != 0) {
        sodium_memzero(signature.data(), signature.size());
        return false;
    }
    sodium_memzero(signature.data(), signature.size());

    std::string base_manifest;
    if (!CanonicalJson(manifest, &base_manifest, "config_hash", "signature") ||
        !ConstantTimeEquals(Sha256Hex(base_manifest), *config_hash)) {
        return false;
    }

    time_t issued = 0;
    time_t expires = 0;
    if (!ParseRfc3339Utc(issued_at, &issued) || !ParseRfc3339Utc(*expires_at, &expires) ||
        expires <= issued || expires - issued > kMaximumManifestLifetimeSeconds) {
        return false;
    }
    time_t now = time(nullptr);
    if (now < kMinimumTrustedUnixTime) {
        timeval trusted_time{.tv_sec = issued, .tv_usec = 0};
        if (settimeofday(&trusted_time, nullptr) != 0) {
            return false;
        }
        now = issued;
    }
    return now >= issued - 300 && now < expires;
}

std::string BuildManifestRequestPayload(const DeviceIdentity& identity) {
    cJSON* root = cJSON_CreateObject();
    if (root == nullptr) {
        return {};
    }
    // Add in canonical order so this also remains easy to compare with backend fixtures.
    cJSON_AddStringToObject(root, "certificate_id", identity.certificate_id().c_str());
    cJSON_AddStringToObject(root, "device_id", identity.device_id().c_str());
    cJSON_AddStringToObject(root, "method", "GET");
    const std::string path = "/v1/devices/" + identity.device_id() + "/activation-manifest";
    cJSON_AddStringToObject(root, "path", path.c_str());
    std::string payload;
    CanonicalJson(root, &payload);
    cJSON_Delete(root);
    return payload;
}

std::string BuildAckPayload(const DeviceIdentity& identity,
                            const ActivationProfile& profile,
                            const std::string& config_hash,
                            int32_t monotonic_counter,
                            const std::string& applied_at,
                            bool include_signature) {
    cJSON* root = cJSON_CreateObject();
    if (root == nullptr) {
        return {};
    }
    cJSON_AddNumberToObject(root, "activation_version", profile.activation_version);
    cJSON_AddStringToObject(root, "applied_at", applied_at.c_str());
    cJSON_AddStringToObject(root, "binding_id", profile.binding_id.c_str());
    cJSON_AddNumberToObject(root, "binding_version", profile.binding_version);
    cJSON_AddStringToObject(root, "certificate_id", identity.certificate_id().c_str());
    cJSON_AddStringToObject(root, "config_hash", config_hash.c_str());
    cJSON_AddStringToObject(root, "device_id", identity.device_id().c_str());
    cJSON_AddStringToObject(root, "firmware_version", esp_app_get_description()->version);
    cJSON_AddNumberToObject(root, "monotonic_counter", monotonic_counter);
    if (include_signature) {
        std::string unsigned_payload;
        CanonicalJson(root, &unsigned_payload);
        const std::string signature = SignedRequestHeader(identity, unsigned_payload);
        if (signature.empty()) {
            cJSON_Delete(root);
            return {};
        }
        cJSON_AddStringToObject(root, "signature", signature.c_str());
    }
    char* rendered = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (rendered == nullptr) {
        return {};
    }
    std::string result(rendered);
    cJSON_free(rendered);
    return result;
}

}  // namespace

esp_err_t MemoriaActivationClient::Activate(ActivationProfile* profile) {
    if (profile == nullptr || !identity_.loaded()) {
        return ESP_ERR_INVALID_ARG;
    }
    const std::string request_payload = BuildManifestRequestPayload(identity_);
    const std::string request_signature = SignedRequestHeader(identity_, request_payload);
    if (request_payload.empty() || request_signature.empty()) {
        return ESP_FAIL;
    }
    const std::string path = "/v1/devices/" + identity_.device_id() + "/activation-manifest";
    std::string response;
    if (!HttpRequest("GET", JoinUrl(identity_.control_api_url(), path),
                     {{"X-Device-Certificate-ID", identity_.certificate_id()},
                      {"X-Device-Signature", request_signature}},
                     {}, &response)) {
        return ESP_FAIL;
    }
    ScopedJson manifest{cJSON_ParseWithLength(response.data(), response.size())};
    std::string config_hash;
    std::string expires_at;
    if (manifest.value == nullptr ||
        !VerifyManifest(identity_, manifest.value, profile, &config_hash, &expires_at)) {
        ESP_LOGE(kTag, "Activation Manifest validation failed");
        return ESP_ERR_INVALID_RESPONSE;
    }

    Settings runtime("memoria_runtime", true);
    const int32_t acknowledged_version = runtime.GetInt("activation_v", 0);
    if (acknowledged_version >= profile->activation_version) {
        ESP_LOGI(kTag, "Activation Manifest verified, version=%ld",
                 static_cast<long>(profile->activation_version));
        return ESP_OK;
    }
    // Bootstrap/online proof consumes counter 1 before activation. The same
    // runtime namespace will own that counter once firmware-side bootstrap is
    // enabled; the lab identity therefore starts Activation ACK at 2.
    const int32_t previous_counter = runtime.GetInt("activation_ctr", 1);
    const int32_t counter = std::max<int32_t>(1, previous_counter + 1);
    const std::string applied_at = FormatRfc3339Utc(time(nullptr));
    const std::string ack = BuildAckPayload(identity_, *profile, config_hash, counter, applied_at, true);
    if (applied_at.empty() || ack.empty() ||
        !HttpRequest("POST",
                     JoinUrl(profile->control_api_url,
                             "/v1/devices/" + identity_.device_id() + "/activation-ack"),
                     {}, ack, nullptr)) {
        return ESP_FAIL;
    }
    runtime.SetInt("activation_ctr", counter);
    runtime.SetInt("activation_v", profile->activation_version);
    ESP_LOGI(kTag, "Activation acknowledged, version=%ld",
             static_cast<long>(profile->activation_version));
    return ESP_OK;
}

}  // namespace memoria
