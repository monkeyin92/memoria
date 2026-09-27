#include "memoria_firmware_update.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>

#include "board.h"
#include "cJSON.h"
#include "esp_app_format.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "http.h"
#include "memoria_activation_client.h"
#include "memoria_firmware_release.h"
#include "sodium.h"

#define MEMORIA_FIRMWARE_STR2(x) #x
#define MEMORIA_FIRMWARE_STR(x) MEMORIA_FIRMWARE_STR2(x)

// The publisher looks for exactly one copy of this marker in the image it
// signs and refuses a mismatch with the release build. Logged at boot, which
// also keeps it from being garbage-collected by the linker.
extern "C" const char memoria_firmware_build_marker[] =
    "MEMORIA_FIRMWARE_BUILD=" MEMORIA_FIRMWARE_STR(MEMORIA_FIRMWARE_BUILD) ";";

namespace memoria {
namespace {

constexpr const char* kTag = "MemoriaFirmware";
constexpr const char* kSigningDomain = "memoria-firmware-release-v1\n";
constexpr int kHttpTimeoutMs = 15000;
constexpr size_t kChunkBytes = 4096;

struct ScopedJson final {
    cJSON* value = nullptr;
    ~ScopedJson() { cJSON_Delete(value); }
};

struct Release {
    uint32_t build = 0;
    uint32_t size = 0;
    std::string version;
    std::string sha256;
    std::array<uint8_t, crypto_sign_BYTES> signature{};
};

std::string JoinUrl(const std::string& base, const std::string& path) {
    if (base.empty()) {
        return {};
    }
    return base.back() == '/' ? base.substr(0, base.size() - 1) + path : base + path;
}

bool Charset(const std::string& value, const char* allowed_punctuation) {
    for (const char character : value) {
        const bool alnum = (character >= '0' && character <= '9') ||
                           (character >= 'a' && character <= 'z') ||
                           (character >= 'A' && character <= 'Z');
        if (!alnum && std::strchr(allowed_punctuation, character) == nullptr) {
            return false;
        }
    }
    return true;
}

bool ReadUint(const cJSON* object, const char* key, uint32_t minimum, uint32_t maximum,
              uint32_t* output) {
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsNumber(item) || !std::isfinite(item->valuedouble) ||
        item->valuedouble != std::floor(item->valuedouble) || item->valuedouble < minimum ||
        item->valuedouble > maximum) {
        return false;
    }
    *output = static_cast<uint32_t>(item->valuedouble);
    return true;
}

bool ReadString(const cJSON* object, const char* key, size_t minimum, size_t maximum,
                std::string* output) {
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsString(item) || item->valuestring == nullptr) {
        return false;
    }
    const size_t length = std::strlen(item->valuestring);
    if (length < minimum || length > maximum) {
        return false;
    }
    output->assign(item->valuestring, length);
    return true;
}

bool ParseRelease(const std::string& body, Release* release) {
    ScopedJson root{cJSON_ParseWithLength(body.data(), body.size())};
    if (!cJSON_IsObject(root.value)) {
        return false;
    }
    int keys = 0;
    for (const cJSON* child = root.value->child; child != nullptr; child = child->next) {
        ++keys;
    }
    uint32_t schema_version = 0;
    std::string board;
    std::string signature;
    if (keys != 7 || !ReadUint(root.value, "schema_version", 1, 1, &schema_version) ||
        !ReadString(root.value, "board", 1, 64, &board) || board != kFirmwareBoard ||
        !ReadUint(root.value, "build", 1, 0x7fffffff, &release->build) ||
        !ReadUint(root.value, "size", 1, 0x7fffffff, &release->size) ||
        !ReadString(root.value, "version", 1, 32, &release->version) ||
        !Charset(release->version, ".+-") ||
        !ReadString(root.value, "sha256", 64, 64, &release->sha256) ||
        !Charset(release->sha256, "") ||
        !ReadString(root.value, "signature", 86, 86, &signature) ||
        !Charset(signature, "-_")) {
        return false;
    }
    for (const char character : release->sha256) {
        if (character >= 'A' && character <= 'Z') {
            return false;  // lowercase hex only
        }
    }
    size_t decoded = 0;
    return sodium_base642bin(release->signature.data(), release->signature.size(),
                             signature.data(), signature.size(), nullptr, &decoded, nullptr,
                             sodium_base64_VARIANT_URLSAFE_NO_PADDING) == 0 &&
           decoded == release->signature.size();
}

// Domain + the unsigned document with sorted keys and no whitespace; every
// value is restricted to ASCII without escapes, so this equals Python's
// json.dumps(sort_keys=True, separators=(",", ":")).
bool VerifyRelease(const Release& release) {
    char canonical[320];
    const int length = std::snprintf(
        canonical, sizeof(canonical),
        "%s{\"board\":\"%s\",\"build\":%lu,\"schema_version\":1,\"sha256\":\"%s\","
        "\"size\":%lu,\"version\":\"%s\"}",
        kSigningDomain, kFirmwareBoard, static_cast<unsigned long>(release.build),
        release.sha256.c_str(), static_cast<unsigned long>(release.size), release.version.c_str());
    if (length <= 0 || static_cast<size_t>(length) >= sizeof(canonical)) {
        return false;
    }
    return crypto_sign_verify_detached(release.signature.data(),
                                       reinterpret_cast<const unsigned char*>(canonical),
                                       static_cast<unsigned long long>(length),
                                       kFirmwareReleasePublicKey.data()) == 0;
}

std::unique_ptr<Http> OpenSignedGet(const DeviceIdentity& identity, const std::string& base,
                                    const std::string& path, const char* accept, int* status) {
    MemoriaActivationClient client(identity);
    const std::string signature = client.SignGetRequest(path);
    auto network = Board::GetInstance().GetNetwork();
    if (signature.empty() || network == nullptr) {
        return nullptr;
    }
    auto http = network->CreateHttp(0);
    if (http == nullptr) {
        return nullptr;
    }
    http->SetTimeout(kHttpTimeoutMs);
    http->SetHeader("Accept", accept);
    http->SetHeader("X-Device-Certificate-ID", identity.certificate_id());
    http->SetHeader("X-Device-Signature", signature);
    if (!http->Open("GET", JoinUrl(base, path))) {
        ESP_LOGW(kTag, "Firmware request transport failed, code=0x%x", http->GetLastError());
        return nullptr;
    }
    *status = http->GetStatusCode();
    return http;
}

std::string ToHex(const uint8_t* data, size_t size) {
    static constexpr char kHex[] = "0123456789abcdef";
    std::string result(size * 2, '0');
    for (size_t index = 0; index < size; ++index) {
        result[index * 2] = kHex[data[index] >> 4];
        result[index * 2 + 1] = kHex[data[index] & 0x0f];
    }
    return result;
}

bool Download(const DeviceIdentity& identity, const std::string& base, const Release& release,
              const std::function<bool()>& may_continue) {
    const esp_partition_t* slot = esp_ota_get_next_update_partition(nullptr);
    if (slot == nullptr || release.size > slot->size) {
        ESP_LOGE(kTag, "No OTA slot fits build %lu (%lu bytes)",
                 static_cast<unsigned long>(release.build), static_cast<unsigned long>(release.size));
        return false;
    }
    const std::string path = "/v1/devices/" + identity.device_id() + "/firmware-release/" +
                             std::to_string(release.build) + "/image";
    int status = 0;
    auto http = OpenSignedGet(identity, base, path, "application/octet-stream", &status);
    if (http == nullptr || status != 200) {
        ESP_LOGW(kTag, "Firmware image unavailable, status=%d", status);
        return false;
    }
    if (http->GetBodyLength() != release.size) {
        ESP_LOGE(kTag, "Firmware image length %u does not match the release",
                 static_cast<unsigned>(http->GetBodyLength()));
        http->Close();
        return false;
    }
    auto* buffer = static_cast<char*>(heap_caps_malloc(kChunkBytes, MALLOC_CAP_INTERNAL));
    esp_ota_handle_t handle = 0;
    // Sequential writes erase sector by sector instead of the whole slot up
    // front, so the idle wake-word pipeline is never starved by a long erase.
    if (buffer == nullptr ||
        esp_ota_begin(slot, OTA_WITH_SEQUENTIAL_WRITES, &handle) != ESP_OK) {
        ESP_LOGE(kTag, "Unable to open OTA slot %s", slot->label);
        heap_caps_free(buffer);
        http->Close();
        return false;
    }
    crypto_hash_sha256_state digest;
    crypto_hash_sha256_init(&digest);
    size_t total = 0;
    int reported = -1;
    bool ok = true;
    while (total < release.size) {
        if (may_continue && !may_continue()) {
            ESP_LOGI(kTag, "Firmware download paused at %u bytes", static_cast<unsigned>(total));
            ok = false;
            break;
        }
        const size_t wanted = std::min(kChunkBytes, static_cast<size_t>(release.size - total));
        const int read = http->Read(buffer, wanted);
        if (read <= 0) {
            ESP_LOGE(kTag, "Firmware download ended early at %u bytes", static_cast<unsigned>(total));
            ok = false;
            break;
        }
        crypto_hash_sha256_update(&digest, reinterpret_cast<const unsigned char*>(buffer), read);
        if (esp_ota_write(handle, buffer, read) != ESP_OK) {
            ESP_LOGE(kTag, "Firmware slot write failed");
            ok = false;
            break;
        }
        total += read;
        const int percent = static_cast<int>(total * 10 / release.size) * 10;
        if (percent != reported) {
            reported = percent;
            ESP_LOGI(kTag, "Firmware download %d%%", percent);
        }
    }
    http->Close();
    heap_caps_free(buffer);
    std::array<uint8_t, crypto_hash_sha256_BYTES> hash{};
    crypto_hash_sha256_final(&digest, hash.data());
    if (!ok || total != release.size) {
        esp_ota_abort(handle);
        return false;
    }
    if (ToHex(hash.data(), hash.size()) != release.sha256) {
        ESP_LOGE(kTag, "Firmware image hash does not match the signed release");
        esp_ota_abort(handle);
        return false;
    }
    if (esp_ota_end(handle) != ESP_OK) {
        ESP_LOGE(kTag, "Firmware image failed validation");
        return false;
    }
    esp_app_desc_t description{};
    if (esp_ota_get_partition_description(slot, &description) != ESP_OK ||
        std::strcmp(description.project_name, "memoria") != 0) {
        ESP_LOGE(kTag, "Firmware image is not a Memoria application");
        return false;
    }
    if (esp_ota_set_boot_partition(slot) != ESP_OK) {
        ESP_LOGE(kTag, "Unable to select the new firmware slot");
        return false;
    }
    ESP_LOGI(kTag, "Firmware build %lu (%s) staged in %s",
             static_cast<unsigned long>(release.build), release.version.c_str(), slot->label);
    return true;
}

}  // namespace

void MemoriaFirmwareUpdate::ConfirmRunningImage() {
    const esp_partition_t* running = esp_ota_get_running_partition();
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    ESP_LOGI(kTag, "%s slot=%s", memoria_firmware_build_marker,
             running != nullptr ? running->label : "?");
    if (running == nullptr || esp_ota_get_state_partition(running, &state) != ESP_OK ||
        state != ESP_OTA_IMG_PENDING_VERIFY) {
        return;
    }
    if (esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) {
        ESP_LOGI(kTag, "Firmware build %lu confirmed", static_cast<unsigned long>(kFirmwareBuild));
    }
}

FirmwareCheck MemoriaFirmwareUpdate::CheckAndStage(const DeviceIdentity& identity,
                                          const std::string& control_api_url,
                                          const std::function<bool()>& may_continue) {
    if (control_api_url.empty() || sodium_init() < 0) {
        return FirmwareCheck::kNothingNew;
    }
    const std::string path = "/v1/devices/" + identity.device_id() + "/firmware-release";
    int status = 0;
    auto http = OpenSignedGet(identity, control_api_url, path, "application/json", &status);
    if (http == nullptr) {
        return FirmwareCheck::kRetryLater;
    }
    if (status == 204) {
        http->Close();
        return FirmwareCheck::kNothingNew;
    }
    if (status != 200) {
        ESP_LOGW(kTag, "Firmware release check rejected, status=%d", status);
        http->Close();
        return FirmwareCheck::kRetryLater;
    }
    const std::string body = http->ReadAll();
    http->Close();
    Release release;
    if (!ParseRelease(body, &release)) {
        ESP_LOGE(kTag, "Firmware release document is malformed");
        return FirmwareCheck::kNothingNew;
    }
    if (release.build <= kFirmwareBuild) {
        return FirmwareCheck::kNothingNew;
    }
    if (!VerifyRelease(release)) {
        ESP_LOGE(kTag, "Firmware release signature does not verify; ignoring build %lu",
                 static_cast<unsigned long>(release.build));
        return FirmwareCheck::kNothingNew;
    }
    ESP_LOGI(kTag, "Firmware build %lu (%s) available, running %lu",
             static_cast<unsigned long>(release.build), release.version.c_str(),
             static_cast<unsigned long>(kFirmwareBuild));
    return Download(identity, control_api_url, release, may_continue) ? FirmwareCheck::kStaged
                                                                        : FirmwareCheck::kRetryLater;
}

}  // namespace memoria
