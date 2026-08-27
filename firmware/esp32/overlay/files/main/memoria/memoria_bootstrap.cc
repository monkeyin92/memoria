#include "memoria_bootstrap.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

#include "board.h"
#include "cJSON.h"
#include "display/lcd_display.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_wifi.h"
#include "http.h"
#include "protocomm.h"
#include "protocomm_ble.h"
#include "protocomm_security1.h"
#include "sodium.h"
#include "ssid_manager.h"
#include "settings.h"
#include "wifi_manager.h"

namespace memoria {
namespace {

constexpr const char* kTag = "MemoriaBootstrap";
constexpr const char* kQrPrefix = "memoria-bootstrap:v1:";
constexpr const char* kProvisioningVersion = "memoria-provisioning/1";
constexpr const char* kCapabilityManifestHash =
    "67ab4e8840637bd8df497bed6b13153d146a8fa790271eae59ac3a032345758b";
constexpr int kHttpTimeoutMs = 10000;
constexpr const char* kServiceUuidText = "21d53b8d-bd75-688a-b442-eb314a1e983d";
constexpr std::array<uint8_t, 16> kServiceUuid = {
    0x21, 0xd5, 0x3b, 0x8d, 0xbd, 0x75, 0x68, 0x8a,
    0xb4, 0x42, 0xeb, 0x31, 0x4a, 0x1e, 0x98, 0x3d,
};

constexpr uint16_t kUuidProtoVersion = 0xFF50;
constexpr uint16_t kUuidProtoSession = 0xFF51;
constexpr uint16_t kUuidProvisionScan = 0xFF52;
constexpr uint16_t kUuidProvisionConfig = 0xFF53;
constexpr uint16_t kUuidProvisionStatus = 0xFF54;
constexpr uint16_t kUuidBootstrapContext = 0xFF55;

struct ScopedJson final {
    cJSON* value = nullptr;
    ~ScopedJson() { cJSON_Delete(value); }
};

std::string Base64UrlEncode(const uint8_t* data, size_t size) {
    std::vector<char> encoded(
        sodium_base64_ENCODED_LEN(size, sodium_base64_VARIANT_URLSAFE_NO_PADDING));
    sodium_bin2base64(encoded.data(), encoded.size(), data, size,
                      sodium_base64_VARIANT_URLSAFE_NO_PADDING);
    return encoded.data();
}

std::string BuildBleName(const std::string& device_id) {
    std::string tail;
    for (auto it = device_id.rbegin(); it != device_id.rend() && tail.size() < 4; ++it) {
        const unsigned char character = static_cast<unsigned char>(*it);
        if ((character >= '0' && character <= '9') ||
            (character >= 'a' && character <= 'z') ||
            (character >= 'A' && character <= 'Z')) {
            tail.push_back(static_cast<char>(std::toupper(character)));
        }
    }
    std::reverse(tail.begin(), tail.end());
    return tail.size() == 4 ? "MEM-" + tail : std::string{};
}

bool WriteVarint(std::vector<uint8_t>* output, size_t value) {
    if (output == nullptr) {
        return false;
    }
    do {
        uint8_t byte = static_cast<uint8_t>(value & 0x7f);
        value >>= 7;
        output->push_back(static_cast<uint8_t>(byte | (value == 0 ? 0 : 0x80)));
    } while (value != 0);
    return true;
}

bool ReadVarint(const uint8_t* input, size_t input_size, size_t* offset, size_t* value) {
    if (input == nullptr || offset == nullptr || value == nullptr) {
        return false;
    }
    size_t result = 0;
    for (unsigned shift = 0; shift < sizeof(size_t) * 8 && *offset < input_size; shift += 7) {
        const uint8_t byte = input[(*offset)++];
        result |= static_cast<size_t>(byte & 0x7f) << shift;
        if ((byte & 0x80) == 0) {
            *value = result;
            return true;
        }
    }
    return false;
}

// Memoria provisioning messages intentionally use one bounded protobuf bytes
// field containing a strict JSON object.  This keeps the wire contract valid
// protobuf without introducing a second generated-code toolchain into the
// locked ESP-IDF build.
bool DecodeJsonEnvelope(const uint8_t* input, size_t input_size, std::string* json) {
    if (json == nullptr || input == nullptr || input_size < 2 || input[0] != 0x0a) {
        return false;
    }
    size_t offset = 1;
    size_t length = 0;
    if (!ReadVarint(input, input_size, &offset, &length) || length > 4096 ||
        offset + length != input_size) {
        return false;
    }
    json->assign(reinterpret_cast<const char*>(input + offset), length);
    return true;
}

esp_err_t EncodeJsonEnvelope(const std::string& json, uint8_t** outbuf, ssize_t* outlen) {
    if (outbuf == nullptr || outlen == nullptr || json.size() > 4096) {
        return ESP_ERR_INVALID_ARG;
    }
    std::vector<uint8_t> encoded;
    encoded.reserve(json.size() + 6);
    encoded.push_back(0x0a);
    WriteVarint(&encoded, json.size());
    encoded.insert(encoded.end(), json.begin(), json.end());
    uint8_t* result = static_cast<uint8_t*>(malloc(encoded.size()));
    if (result == nullptr) {
        return ESP_ERR_NO_MEM;
    }
    std::memcpy(result, encoded.data(), encoded.size());
    *outbuf = result;
    *outlen = static_cast<ssize_t>(encoded.size());
    return ESP_OK;
}

esp_err_t EncodeJsonObject(cJSON* root, uint8_t** outbuf, ssize_t* outlen) {
    if (root == nullptr) {
        return ESP_ERR_NO_MEM;
    }
    char* rendered = cJSON_PrintUnformatted(root);
    if (rendered == nullptr) {
        return ESP_ERR_NO_MEM;
    }
    const std::string json(rendered);
    cJSON_free(rendered);
    return EncodeJsonEnvelope(json, outbuf, outlen);
}

bool ExactKeys(const cJSON* object, std::initializer_list<const char*> expected) {
    if (!cJSON_IsObject(object)) {
        return false;
    }
    size_t count = 0;
    for (const cJSON* child = object->child; child != nullptr; child = child->next) {
        if (child->string == nullptr ||
            std::none_of(expected.begin(), expected.end(), [child](const char* key) {
                return std::strcmp(child->string, key) == 0;
            })) {
            return false;
        }
        ++count;
    }
    return count == expected.size();
}

bool ReadRequiredString(const cJSON* object,
                        const char* key,
                        size_t maximum,
                        std::string* output) {
    const cJSON* value = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsString(value) || value->valuestring == nullptr) {
        return false;
    }
    const size_t length = std::strlen(value->valuestring);
    if (length == 0 || length > maximum) {
        return false;
    }
    output->assign(value->valuestring, length);
    return true;
}

std::string JoinUrl(const std::string& base, const std::string& path) {
    if (base.empty()) {
        return {};
    }
    if (base.back() == '/') {
        return base.substr(0, base.size() - 1) + path;
    }
    return base + path;
}

bool Base64UrlDecode(const std::string& encoded, std::vector<uint8_t>* output) {
    if (output == nullptr || encoded.empty()) {
        return false;
    }
    output->assign(encoded.size(), 0);
    size_t actual_size = 0;
    const int result = sodium_base642bin(
        output->data(), output->size(), encoded.data(), encoded.size(), nullptr, &actual_size,
        nullptr, sodium_base64_VARIANT_URLSAFE_NO_PADDING);
    if (result != 0) {
        output->clear();
        return false;
    }
    output->resize(actual_size);
    return true;
}

std::string Sha256Hex(const uint8_t* data, size_t size) {
    std::array<uint8_t, crypto_hash_sha256_BYTES> digest{};
    if (data == nullptr || crypto_hash_sha256(digest.data(), data, size) != 0) {
        return {};
    }
    static constexpr char kHex[] = "0123456789abcdef";
    std::string result(digest.size() * 2, '0');
    for (size_t index = 0; index < digest.size(); ++index) {
        result[index * 2] = kHex[digest[index] >> 4];
        result[index * 2 + 1] = kHex[digest[index] & 0x0f];
    }
    sodium_memzero(digest.data(), digest.size());
    return result;
}

std::string HashBase64Url(const std::string& value) {
    std::vector<uint8_t> decoded;
    return Base64UrlDecode(value, &decoded) ? Sha256Hex(decoded.data(), decoded.size()) : std::string{};
}

bool HttpRequest(const std::string& method,
                 const std::string& url,
                 const std::string& body,
                 std::string* response) {
    auto network = Board::GetInstance().GetNetwork();
    if (network == nullptr) {
        return false;
    }
    auto http = network->CreateHttp(0);
    if (http == nullptr) {
        return false;
    }
    http->SetTimeout(kHttpTimeoutMs);
    http->SetHeader("Accept", "application/json");
    if (!body.empty()) {
        http->SetHeader("Content-Type", "application/json");
        http->SetContent(std::string(body));
    }
    if (!http->Open(method, url)) {
        ESP_LOGE(kTag, "Bootstrap HTTP transport failed, code=0x%x", http->GetLastError());
        return false;
    }
    const int status = http->GetStatusCode();
    if (status != 200) {
        ESP_LOGE(kTag, "Bootstrap HTTP rejected, status=%d", status);
        http->Close();
        return false;
    }
    if (response != nullptr) {
        *response = http->ReadAll();
    }
    http->Close();
    return true;
}

}  // namespace

MemoriaBootstrap& MemoriaBootstrap::GetInstance() {
    static MemoriaBootstrap instance;
    return instance;
}

MemoriaBootstrap::~MemoriaBootstrap() {
    Stop();
}

esp_err_t MemoriaBootstrap::BuildBootstrapQr() {
    if (sodium_init() < 0) {
        return ESP_FAIL;
    }
    esp_fill_random(nonce_bytes_.data(), nonce_bytes_.size());
    std::array<uint8_t, 16> pop_bytes{};
    esp_fill_random(pop_bytes.data(), pop_bytes.size());
    bootstrap_nonce_ = Base64UrlEncode(nonce_bytes_.data(), nonce_bytes_.size());
    pop_ = Base64UrlEncode(pop_bytes.data(), pop_bytes.size());
    sodium_memzero(pop_bytes.data(), pop_bytes.size());
    ble_name_ = BuildBleName(identity_.device_id());
    if (bootstrap_nonce_.empty() || pop_.empty() || ble_name_.empty()) {
        return ESP_FAIL;
    }

    ScopedJson root{cJSON_CreateObject()};
    if (root.value == nullptr) {
        return ESP_ERR_NO_MEM;
    }
    // The Python onboarding authority requires lexicographically sorted keys
    // for this RFC 8785-compatible all-string/integer payload.
    cJSON_AddStringToObject(root.value, "ble_name", ble_name_.c_str());
    cJSON_AddStringToObject(root.value, "ble_service_uuid", kServiceUuidText);
    cJSON_AddStringToObject(root.value, "bootstrap_nonce", bootstrap_nonce_.c_str());
    cJSON_AddStringToObject(root.value, "certificate_id", identity_.certificate_id().c_str());
    cJSON_AddStringToObject(root.value, "device_id", identity_.device_id().c_str());
    cJSON_AddStringToObject(root.value, "firmware_version", esp_app_get_description()->version);
    cJSON_AddStringToObject(root.value, "pop", pop_.c_str());
    cJSON_AddStringToObject(root.value, "provisioning_protocol", kProvisioningVersion);
    cJSON_AddStringToObject(root.value, "typ", "memoria-device-bootstrap");
    cJSON_AddNumberToObject(root.value, "ver", 1);
    char* rendered = cJSON_PrintUnformatted(root.value);
    if (rendered == nullptr) {
        return ESP_ERR_NO_MEM;
    }
    const std::string canonical(rendered);
    cJSON_free(rendered);

    std::array<uint8_t, DeviceIdentity::kEd25519SignatureBytes> signature{};
    const esp_err_t signed_result = identity_.SignDetached(
        reinterpret_cast<const uint8_t*>(canonical.data()), canonical.size(),
        signature.data(), signature.size());
    if (signed_result != ESP_OK) {
        return signed_result;
    }
    qr_payload_ = std::string(kQrPrefix) +
                  Base64UrlEncode(reinterpret_cast<const uint8_t*>(canonical.data()),
                                  canonical.size()) +
                  "." + Base64UrlEncode(signature.data(), signature.size());
    sodium_memzero(signature.data(), signature.size());
    return qr_payload_.empty() ? ESP_FAIL : ESP_OK;
}

esp_err_t MemoriaBootstrap::StartBle() {
    static protocomm_ble_name_uuid_t endpoints[] = {
        {"proto-ver", kUuidProtoVersion},
        {"proto-session", kUuidProtoSession},
        {"prov-scan", kUuidProvisionScan},
        {"prov-config", kUuidProvisionConfig},
        {"prov-status", kUuidProvisionStatus},
        {"memoria-bootstrap", kUuidBootstrapContext},
    };

    protocomm_ = protocomm_new();
    if (protocomm_ == nullptr) {
        return ESP_ERR_NO_MEM;
    }
    protocomm_ble_config_t config{};
    std::strncpy(config.device_name, ble_name_.c_str(), sizeof(config.device_name) - 1);
    std::copy(kServiceUuid.begin(), kServiceUuid.end(), config.service_uuid);
    config.nu_lookup_count = sizeof(endpoints) / sizeof(endpoints[0]);
    config.nu_lookup = endpoints;
    config.ble_notify = 0;
    config.keep_ble_on = 1;
    esp_err_t result = protocomm_ble_start(protocomm_, &config);
    if (result != ESP_OK) {
        return result;
    }

    protocomm_security1_params_t security_params = {
        .data = reinterpret_cast<const uint8_t*>(pop_.data()),
        .len = static_cast<uint16_t>(pop_.size()),
    };
    result = protocomm_set_security(protocomm_, "proto-session", &protocomm_security1,
                                    &security_params);
    if (result == ESP_OK) {
        result = protocomm_set_version(protocomm_, "proto-ver", kProvisioningVersion);
    }
    if (result == ESP_OK) {
        result = protocomm_add_endpoint(protocomm_, "prov-scan", HandleScan, this);
    }
    if (result == ESP_OK) {
        result = protocomm_add_endpoint(protocomm_, "prov-config", HandleConfig, this);
    }
    if (result == ESP_OK) {
        result = protocomm_add_endpoint(protocomm_, "prov-status", HandleStatus, this);
    }
    if (result == ESP_OK) {
        result = protocomm_add_endpoint(protocomm_, "memoria-bootstrap",
                                        HandleBootstrapContext, this);
    }
    return result;
}

esp_err_t MemoriaBootstrap::Start(LcdDisplay* display) {
    if (display == nullptr || !display->IsSetupUICalled()) {
        return ESP_ERR_INVALID_STATE;
    }
    if (active_) {
        return display->ShowQrCode(qr_payload_, "微信扫码绑定") ? ESP_OK : ESP_FAIL;
    }
    display_ = display;
    esp_err_t result = identity_.Load();
    if (result == ESP_OK) {
        result = BuildBootstrapQr();
    }
    if (result == ESP_OK) {
        result = StartBle();
    }
    if (result == ESP_OK && !display_->ShowQrCode(qr_payload_, "微信扫码绑定")) {
        result = ESP_FAIL;
    }
    if (result != ESP_OK) {
        ESP_LOGE(kTag, "Bootstrap start failed: %s", esp_err_to_name(result));
        Stop();
        return result;
    }
    active_ = true;
    ESP_LOGI(kTag, "Nearby bootstrap ready, device=%s, BLE=%s",
             identity_.device_id().c_str(), ble_name_.c_str());
    return ESP_OK;
}

void MemoriaBootstrap::Stop() {
    active_ = false;
    if (online_proof_task_ != nullptr && online_proof_task_ != xTaskGetCurrentTaskHandle()) {
        vTaskDelete(online_proof_task_);
        online_proof_task_ = nullptr;
    }
    if (display_ != nullptr) {
        display_->ClearQrCode();
    }
    if (protocomm_ != nullptr) {
        protocomm_ble_stop(protocomm_);
        protocomm_delete(protocomm_);
        protocomm_ = nullptr;
    }
    display_ = nullptr;
    identity_.Clear();
    qr_payload_.clear();
    bootstrap_nonce_.clear();
    pop_.clear();
    onboarding_session_id_.clear();
    mobile_nonce_.clear();
    sodium_memzero(nonce_bytes_.data(), nonce_bytes_.size());
}

esp_err_t MemoriaBootstrap::StartOnlineProofTask() {
    if (online_proof_task_ != nullptr) {
        return ESP_OK;
    }
    const BaseType_t created = xTaskCreate(
        &MemoriaBootstrap::OnlineProofTask, "memoria_proof", 8192, this, 4, &online_proof_task_);
    return created == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}

void MemoriaBootstrap::OnlineProofTask(void* context) {
    auto* bootstrap = static_cast<MemoriaBootstrap*>(context);
    if (bootstrap != nullptr && !bootstrap->RunOnlineProof()) {
        ESP_LOGW(kTag, "Nearby device online proof did not complete");
    }
    if (bootstrap != nullptr) {
        bootstrap->online_proof_task_ = nullptr;
    }
    vTaskDelete(nullptr);
}

bool MemoriaBootstrap::RunOnlineProof() {
    // The nearby nonce is delivered before Wi-Fi credentials are written. Wait
    // here so the proof is emitted only after the board has a real route.
    for (int attempt = 0; attempt < 120; ++attempt) {
        if (WifiManager::GetInstance().IsConnected()) {
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    auto& wifi = WifiManager::GetInstance();
    if (!wifi.IsConnected() || wifi.GetIpAddress().empty() || onboarding_session_id_.empty() ||
        mobile_nonce_.empty() || bootstrap_nonce_.empty()) {
        return false;
    }

    const std::string challenge_path = "/v1/device-bootstrap/" + onboarding_session_id_ + "/challenge";
    const std::string challenge_body = "{\"certificate_id\":\"" + identity_.certificate_id() +
                                       "\",\"device_id\":\"" + identity_.device_id() + "\"}";
    std::string challenge_response;
    if (!HttpRequest("POST", JoinUrl(identity_.control_api_url(), challenge_path), challenge_body,
                     &challenge_response)) {
        return false;
    }
    ScopedJson challenge{cJSON_ParseWithLength(challenge_response.data(), challenge_response.size())};
    std::string challenge_id;
    std::string challenge_nonce;
    if (!ReadRequiredString(challenge.value, "challenge_id", 128, &challenge_id) ||
        !ReadRequiredString(challenge.value, "nonce", 128, &challenge_nonce)) {
        return false;
    }
    std::vector<uint8_t> decoded_challenge_nonce;
    if (!Base64UrlDecode(challenge_nonce, &decoded_challenge_nonce) ||
        decoded_challenge_nonce.size() != 32) {
        return false;
    }
    const std::string bootstrap_nonce_hash = HashBase64Url(bootstrap_nonce_);
    const std::string mobile_nonce_hash = HashBase64Url(mobile_nonce_);
    if (bootstrap_nonce_hash.empty() || mobile_nonce_hash.empty()) {
        return false;
    }

    Settings runtime("memoria_runtime", true);
    const int32_t previous_counter = runtime.GetInt("activation_ctr", 0);
    const int32_t counter = std::max<int32_t>(1, previous_counter + 1);
    ScopedJson unsigned_proof{cJSON_CreateObject()};
    if (unsigned_proof.value == nullptr) {
        return false;
    }
    cJSON_AddStringToObject(unsigned_proof.value, "bootstrap_nonce_hash", bootstrap_nonce_hash.c_str());
    cJSON_AddStringToObject(unsigned_proof.value, "capability_manifest_hash", kCapabilityManifestHash);
    cJSON_AddStringToObject(unsigned_proof.value, "certificate_id", identity_.certificate_id().c_str());
    cJSON_AddStringToObject(unsigned_proof.value, "challenge_id", challenge_id.c_str());
    cJSON_AddStringToObject(unsigned_proof.value, "challenge_nonce", challenge_nonce.c_str());
    cJSON_AddStringToObject(unsigned_proof.value, "device_id", identity_.device_id().c_str());
    cJSON_AddNumberToObject(unsigned_proof.value, "firmware_security_version", 1);
    cJSON_AddStringToObject(unsigned_proof.value, "firmware_version", esp_app_get_description()->version);
    cJSON_AddStringToObject(unsigned_proof.value, "mobile_nonce_hash", mobile_nonce_hash.c_str());
    cJSON_AddNumberToObject(unsigned_proof.value, "monotonic_counter", counter);
    cJSON* network_result = cJSON_AddObjectToObject(unsigned_proof.value, "network_result");
    if (network_result == nullptr) {
        return false;
    }
    cJSON_AddBoolToObject(network_result, "dns_ready", true);
    cJSON_AddBoolToObject(network_result, "got_ip", true);
    cJSON_AddBoolToObject(network_result, "tls_ready", true);
    char* unsigned_rendered = cJSON_PrintUnformatted(unsigned_proof.value);
    if (unsigned_rendered == nullptr) {
        return false;
    }
    const std::string unsigned_payload(unsigned_rendered);
    cJSON_free(unsigned_rendered);
    std::array<uint8_t, DeviceIdentity::kEd25519SignatureBytes> signature{};
    if (identity_.SignDetached(reinterpret_cast<const uint8_t*>(unsigned_payload.data()),
                               unsigned_payload.size(), signature.data(), signature.size()) != ESP_OK) {
        return false;
    }
    const std::string signature_b64 = Base64UrlEncode(signature.data(), signature.size());
    sodium_memzero(signature.data(), signature.size());
    if (signature_b64.empty()) {
        return false;
    }
    cJSON_AddStringToObject(unsigned_proof.value, "signature", signature_b64.c_str());
    char* proof_rendered = cJSON_PrintUnformatted(unsigned_proof.value);
    if (proof_rendered == nullptr) {
        return false;
    }
    const std::string proof(proof_rendered);
    cJSON_free(proof_rendered);
    std::string proof_response;
    const std::string proof_path = "/v1/device-bootstrap/" + onboarding_session_id_ + "/online-proof";
    if (!HttpRequest("POST", JoinUrl(identity_.control_api_url(), proof_path), proof, &proof_response)) {
        return false;
    }
    runtime.SetInt("activation_ctr", counter);
    ESP_LOGI(kTag, "Nearby device online proof accepted, counter=%ld", static_cast<long>(counter));
    return true;
}

esp_err_t MemoriaBootstrap::HandleScan(uint32_t,
                                       const uint8_t*,
                                       ssize_t,
                                       uint8_t** outbuf,
                                       ssize_t* outlen,
    void*) {
    ScopedJson root{cJSON_CreateObject()};
    if (root.value == nullptr) {
        return ESP_ERR_NO_MEM;
    }
    cJSON* networks = cJSON_AddArrayToObject(root.value, "networks");
    if (networks == nullptr) {
        return ESP_ERR_NO_MEM;
    }
    uint16_t count = 0;
    if (esp_wifi_scan_start(nullptr, true) == ESP_OK &&
        esp_wifi_scan_get_ap_num(&count) == ESP_OK && count > 0) {
        count = std::min<uint16_t>(count, 20);
        std::vector<wifi_ap_record_t> records(count);
        if (esp_wifi_scan_get_ap_records(&count, records.data()) == ESP_OK) {
            for (uint16_t index = 0; index < count; ++index) {
                if (records[index].ssid[0] == '\0') {
                    continue;
                }
                cJSON* network = cJSON_CreateObject();
                cJSON_AddNumberToObject(network, "rssi", records[index].rssi);
                cJSON_AddBoolToObject(network, "secure",
                                      records[index].authmode != WIFI_AUTH_OPEN);
                cJSON_AddStringToObject(
                    network, "ssid", reinterpret_cast<const char*>(records[index].ssid));
                cJSON_AddItemToArray(networks, network);
            }
        }
    }
    return EncodeJsonObject(root.value, outbuf, outlen);
}

esp_err_t MemoriaBootstrap::HandleConfig(uint32_t,
                                         const uint8_t* inbuf,
                                         ssize_t inlen,
                                         uint8_t** outbuf,
                                         ssize_t* outlen,
                                         void*) {
    std::string json;
    if (inlen < 0 || !DecodeJsonEnvelope(inbuf, static_cast<size_t>(inlen), &json)) {
        return ESP_ERR_INVALID_ARG;
    }
    ScopedJson root{cJSON_ParseWithLength(json.data(), json.size())};
    std::string ssid;
    std::string password;
    if (!ExactKeys(root.value, {"password", "ssid"}) ||
        !ReadRequiredString(root.value, "ssid", 32, &ssid)) {
        return ESP_ERR_INVALID_ARG;
    }
    const cJSON* password_value = cJSON_GetObjectItemCaseSensitive(root.value, "password");
    if (!cJSON_IsString(password_value) || password_value->valuestring == nullptr ||
        std::strlen(password_value->valuestring) > 64) {
        return ESP_ERR_INVALID_ARG;
    }
    password = password_value->valuestring;
    SsidManager::GetInstance().AddSsid(ssid, password);
    std::fill(password.begin(), password.end(), '\0');
    if (WifiManager::GetInstance().IsInitialized()) {
        WifiManager::GetInstance().StopConfigAp();
        WifiManager::GetInstance().StopStation();
        WifiManager::GetInstance().StartStation();
    }
    return EncodeJsonEnvelope("{\"accepted\":true}", outbuf, outlen);
}

esp_err_t MemoriaBootstrap::HandleStatus(uint32_t,
                                         const uint8_t*,
                                         ssize_t,
                                         uint8_t** outbuf,
                                         ssize_t* outlen,
                                         void*) {
    auto& wifi = WifiManager::GetInstance();
    ScopedJson root{cJSON_CreateObject()};
    cJSON_AddBoolToObject(root.value, "connected", wifi.IsConnected());
    cJSON_AddStringToObject(root.value, "ip", wifi.GetIpAddress().c_str());
    cJSON_AddStringToObject(root.value, "ssid", wifi.GetSsid().c_str());
    return EncodeJsonObject(root.value, outbuf, outlen);
}

esp_err_t MemoriaBootstrap::HandleBootstrapContext(uint32_t,
                                                   const uint8_t* inbuf,
                                                   ssize_t inlen,
                                                   uint8_t** outbuf,
                                                   ssize_t* outlen,
                                                   void* context) {
    auto* bootstrap = static_cast<MemoriaBootstrap*>(context);
    std::string json;
    if (bootstrap == nullptr || inlen < 0 ||
        !DecodeJsonEnvelope(inbuf, static_cast<size_t>(inlen), &json)) {
        return ESP_ERR_INVALID_ARG;
    }
    ScopedJson root{cJSON_ParseWithLength(json.data(), json.size())};
    std::string onboarding_session_id;
    std::string mobile_nonce;
    if (!ExactKeys(root.value, {"mobile_nonce", "onboarding_session_id"}) ||
        !ReadRequiredString(root.value, "onboarding_session_id", 128,
                            &onboarding_session_id) ||
        !ReadRequiredString(root.value, "mobile_nonce", 128, &mobile_nonce)) {
        return ESP_ERR_INVALID_ARG;
    }
    bootstrap->onboarding_session_id_ = std::move(onboarding_session_id);
    bootstrap->mobile_nonce_ = std::move(mobile_nonce);
    if (bootstrap->StartOnlineProofTask() != ESP_OK) {
        return ESP_ERR_NO_MEM;
    }
    return EncodeJsonEnvelope("{\"accepted\":true}", outbuf, outlen);
}

}  // namespace memoria
