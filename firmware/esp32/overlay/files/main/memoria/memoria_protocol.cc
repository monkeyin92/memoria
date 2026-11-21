#include "memoria_protocol.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <string_view>
#include <utility>
#include <vector>

#include "board.h"
#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_random.h"
#include "sdkconfig.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "esp_tls.h"
#include "display/lcd_display.h"
#include "http.h"
#include "memoria_activation_error_page.h"
#include "memoria_playback_stop.h"
#include "memoria_activation_error_text.h"
#include "memoria_activation_retry_policy.h"
#include "memoria_audio_frame.h"
#include "application.h"
#include "memoria_bootstrap.h"
#include "memoria_display_hooks.h"
#include "memoria_firmware_update.h"
#include "memoria_offline_menu.h"
#include "memoria_session_close.h"
#include "memoria_stop_keyword.h"
#include "memoria_vad_start.h"
#include "memoria_wake_mode.h"
#include "memoria_wake_word.h"
#include "memoria_ws_tls_guard.h"
#include "network_interface.h"
#include "settings.h"
#include "sodium.h"
#include "web_socket.h"

#include <cerrno>
#include <new>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>

namespace memoria {
namespace {

constexpr const char* kTag = "MemoriaProtocol";
constexpr int kHttpTimeoutMs = 10000;
constexpr int kSessionReadyTimeoutMs = 10000;
constexpr int64_t kTransportPingIntervalUs = 30LL * 1000LL * 1000LL;
constexpr int64_t kTransportPongTimeoutUs = 10LL * 1000LL * 1000LL;
// The phone's companion pick reaches an idle device within one poll.
constexpr uint32_t kDisplayProfilePollMs = 20000;
// A bound board whose display profile is unavailable rechecks the manifest
// at most this often.
constexpr uint64_t kReleaseRecheckMs = 10 * 60 * 1000;
// Firmware updates: first look shortly after the board settles idle, then a
// few times a day; a paused or failed download is retried sooner.
constexpr uint64_t kFirstFirmwareCheckMs = 30 * 1000;
constexpr uint64_t kFirmwareCheckMs = 6ULL * 60 * 60 * 1000;
constexpr uint64_t kFirmwareRetryMs = 15 * 60 * 1000;
constexpr uint32_t kUplinkSampleRate = 16000;
constexpr uint32_t kDownlinkSampleRate24k = 24000;
constexpr uint32_t kDownlinkSampleRate16k = 16000;
constexpr uint32_t kFrameMs = 20;
// One heartbeat per second of uplink frames with no VAD line, so an open
// microphone whose VAD never fires leaves an rms trace in the serial log.
constexpr uint32_t kVadQuietLogFrames = 1000 / kFrameMs;
// The Memoria AFE configuration keeps 900 ms of quiet audio before emitting
// VAD end. Report the estimated last voiced sample instead of the callback
// time; otherwise Voice Core waits for ASR text to cover silence that cannot
// contain words and eventually discards a valid final.
constexpr uint64_t kAfeVadHangoverSamples =
    static_cast<uint64_t>(kUplinkSampleRate) * 900 / 1000;
// AFE is the normal endpoint authority. This absolute sample-clock fence only
// prevents one bad/noisy capture from holding a signed media session open
// indefinitely; ordinary turns must still end through the AFE VAD callback.
constexpr uint64_t kMaxVadSpeechSamples = static_cast<uint64_t>(kUplinkSampleRate) * 20;

ActivationAttemptOutcome ActivationOutcomeFor(esp_err_t result) {
    if (result == ESP_OK) {
        return ActivationAttemptOutcome::kOk;
    }
    // Activate() maps a manifest 409 to INVALID_STATE after clearing the
    // activation marker; the parent still has to scan the nearby QR.
    if (result == ESP_ERR_INVALID_STATE) {
        return ActivationAttemptOutcome::kServerUnbound;
    }
    return ActivationAttemptOutcome::kFailed;
}

// Read after Activate(): a 409 clears activation_v itself before returning, so
// a bound board released by the server becomes unbound here too.
bool ActivationAwaitingBinding() {
    Settings runtime("memoria_runtime", true);
    return runtime.GetInt("activation_v", 0) == 0 ||
           MemoriaBootstrap::GetInstance().active();
}

std::string ActivationFailureDetail(esp_err_t result,
                                    const MemoriaActivationClient& client) {
    const uint32_t value =
        (static_cast<uint32_t>(result) * 2654435761u) ^
        (static_cast<uint32_t>(client.last_http_status()) * 40503u) ^
        static_cast<uint32_t>(client.last_transport_error());
    char support_code[7];
    std::snprintf(support_code, sizeof(support_code), "%04X",
                  static_cast<unsigned>(value & 0xFFFFu));
    char detail[160];
    std::snprintf(detail, sizeof(detail), kActivationNetworkErrorDetailFormat, support_code);
    return detail;
}

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

std::string Base64UrlEncode(const uint8_t* data, size_t size) {
    std::vector<char> output(sodium_base64_ENCODED_LEN(size, sodium_base64_VARIANT_URLSAFE_NO_PADDING));
    sodium_bin2base64(output.data(), output.size(), data, size,
                      sodium_base64_VARIANT_URLSAFE_NO_PADDING);
    return output.data();
}

std::string JsonString(const char* value) {
    ScopedJson string{cJSON_CreateString(value == nullptr ? "" : value)};
    if (string.value == nullptr) {
        return {};
    }
    char* rendered = cJSON_PrintUnformatted(string.value);
    if (rendered == nullptr) {
        return {};
    }
    std::string result(rendered);
    cJSON_free(rendered);
    return result;
}

// The media proof schema is intentionally flat. Building it directly in sorted
// key order avoids a second JSON authority on device.
std::string MediaProofPayload(const DeviceIdentity& identity,
                              const std::string& challenge_id,
                              const std::string& nonce,
                              const std::string& issued_at) {
    return std::string("{\"certificate_id\":") + JsonString(identity.certificate_id().c_str()) +
           ",\"challenge_id\":" + JsonString(challenge_id.c_str()) +
           ",\"client_id\":" + JsonString(identity.client_id().c_str()) +
           ",\"device_id\":" + JsonString(identity.device_id().c_str()) +
           ",\"issued_at\":" + JsonString(issued_at.c_str()) +
           ",\"nonce\":" + JsonString(nonce.c_str()) +
           ",\"type\":\"memoria-device-media-auth\",\"version\":1}";
}

bool HttpJson(const std::string& url,
              const std::vector<std::pair<std::string, std::string>>& headers,
              std::string body,
              std::string* response,
              int* status_code = nullptr) {
    if (status_code != nullptr) {
        *status_code = 0;
    }
    auto http = Board::GetInstance().GetNetwork()->CreateHttp(0);
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
    if (!http->Open("POST", url)) {
        ESP_LOGE(kTag, "Media HTTP transport failed, code=0x%x", http->GetLastError());
        return false;
    }
    const int status = http->GetStatusCode();
    if (status_code != nullptr) {
        *status_code = status;
    }
    if (status != 200) {
        // The caller may inspect a bounded, structured error to distinguish a
        // rolling-upgrade schema rejection from authentication/runtime
        // failures. Never log this body: validation errors can echo inputs.
        if (response != nullptr) {
            *response = http->ReadAll();
        }
        ESP_LOGE(kTag, "Media HTTP rejected, status=%d", status);
        http->Close();
        return false;
    }
    if (response != nullptr) {
        *response = http->ReadAll();
    }
    http->Close();
    return true;
}

enum class LegacyProtocolSchemaRejection {
    kNone,
    kAdvertisementOnly,
    kAdvertisementAndResume,
};

LegacyProtocolSchemaRejection DetectLegacyProtocolSchemaRejection(
    const std::string& response) {
    ScopedJson root{cJSON_ParseWithLength(response.data(), response.size())};
    if (root.value == nullptr) {
        return LegacyProtocolSchemaRejection::kNone;
    }
    const cJSON* detail = cJSON_GetObjectItemCaseSensitive(root.value, "detail");
    if (!cJSON_IsArray(detail)) {
        return LegacyProtocolSchemaRejection::kNone;
    }
    const int error_count = cJSON_GetArraySize(detail);
    if (error_count < 1 || error_count > 2) {
        return LegacyProtocolSchemaRejection::kNone;
    }
    bool advertisement_rejected = false;
    bool resume_rejected = false;
    for (int index = 0; index < error_count; ++index) {
        const cJSON* error = cJSON_GetArrayItem(detail, index);
        const cJSON* type = cJSON_GetObjectItemCaseSensitive(error, "type");
        const cJSON* location = cJSON_GetObjectItemCaseSensitive(error, "loc");
        if (!cJSON_IsString(type) || type->valuestring == nullptr ||
            std::string_view(type->valuestring) != "extra_forbidden" ||
            !cJSON_IsArray(location) || cJSON_GetArraySize(location) != 2) {
            return LegacyProtocolSchemaRejection::kNone;
        }
        const cJSON* scope = cJSON_GetArrayItem(location, 0);
        const cJSON* field = cJSON_GetArrayItem(location, 1);
        if (!cJSON_IsString(scope) || scope->valuestring == nullptr ||
            std::string_view(scope->valuestring) != "body" || !cJSON_IsString(field) ||
            field->valuestring == nullptr) {
            return LegacyProtocolSchemaRejection::kNone;
        }
        const std::string_view field_name(field->valuestring);
        if (field_name == "supported_protocol_versions" && !advertisement_rejected) {
            advertisement_rejected = true;
        } else if (field_name == "resume_session_id" && !resume_rejected) {
            resume_rejected = true;
        } else {
            return LegacyProtocolSchemaRejection::kNone;
        }
    }
    if (advertisement_rejected && resume_rejected) {
        return LegacyProtocolSchemaRejection::kAdvertisementAndResume;
    }
    if (advertisement_rejected) {
        return LegacyProtocolSchemaRejection::kAdvertisementOnly;
    }
    return LegacyProtocolSchemaRejection::kNone;
}

bool GetString(const cJSON* object, const char* key, std::string* output, size_t max_length = 4096) {
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsString(item) || item->valuestring == nullptr) {
        return false;
    }
    const size_t size = std::strlen(item->valuestring);
    if (size == 0 || size > max_length) {
        return false;
    }
    if (output != nullptr) {
        *output = item->valuestring;
    }
    return true;
}

bool IsFiniteInteger(double value) {
    return std::isfinite(value) && std::floor(value) == value;
}

bool GetPositiveUint32(const cJSON* object, const char* key, uint32_t* output) {
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsNumber(item)) {
        return false;
    }
    const double value = item->valuedouble;
    if (!IsFiniteInteger(value) || value < 1.0 || value > static_cast<double>(UINT32_MAX)) {
        return false;
    }
    if (output != nullptr) {
        *output = static_cast<uint32_t>(value);
    }
    return true;
}

bool GetUint32(const cJSON* object, const char* key, uint32_t* output) {
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsNumber(item)) {
        return false;
    }
    const double value = item->valuedouble;
    if (!IsFiniteInteger(value) || value < 0.0 || value > static_cast<double>(UINT32_MAX)) {
        return false;
    }
    if (output != nullptr) {
        *output = static_cast<uint32_t>(value);
    }
    return true;
}

bool GetUint64(const cJSON* object, const char* key, uint64_t* output) {
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsNumber(item)) {
        return false;
    }
    const double value = item->valuedouble;
    // cJSON stores JSON numbers as double. UINT64_MAX rounds to 2^64 in that
    // representation, so use the exclusive 2^64 bound before converting.
    if (!IsFiniteInteger(value) || value < 0.0 || value >= std::ldexp(1.0, 64)) {
        return false;
    }
    if (output != nullptr) {
        *output = static_cast<uint64_t>(value);
    }
    return true;
}

bool GetBool(const cJSON* object, const char* key, bool* output) {
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsBool(item)) {
        return false;
    }
    if (output != nullptr) {
        *output = cJSON_IsTrue(item);
    }
    return true;
}

uint64_t DeviceMonotonicMs() {
    return static_cast<uint64_t>(esp_timer_get_time() / 1000);
}

std::string RenderJson(cJSON* root) {
    char* rendered = cJSON_PrintUnformatted(root);
    if (rendered == nullptr) {
        return {};
    }
    std::string result(rendered);
    cJSON_free(rendered);
    return result;
}

float ConfiguredVadStartRmsFloor() {
    return VadStartRmsFloorFromMilli(CONFIG_MEMORIA_VAD_START_RMS_FLOOR_MILLI);
}

void LogVadStartSuppressed(float rms, float peak, float floor) {
    ESP_LOGW(kTag, "vad.start suppressed rms=%.4f peak=%.4f floor=%.4f",
             static_cast<double>(rms), static_cast<double>(peak), static_cast<double>(floor));
}

}  // namespace

namespace {
MemoriaProtocol* g_active_protocol = nullptr;
std::function<void(bool)> g_listening_closed_handler;
std::atomic<bool> g_listening_closed{false};
std::function<void(bool)> g_voice_activity_handler;
}  // namespace

void SetVoiceActivityHandler(std::function<void(bool speaking)> handler) {
    g_voice_activity_handler = std::move(handler);
}

void SetListeningClosedHandler(std::function<void(bool closed)> handler) {
    g_listening_closed_handler = std::move(handler);
}

bool IsListeningClosed() {
    return g_listening_closed.load(std::memory_order_relaxed);
}

bool SendListeningResume() {
    if (g_active_protocol == nullptr) {
        return false;
    }
    return g_active_protocol->SendListeningResume();
}

namespace {
bool IsConnectedStandby() {
    const auto state = Application::GetInstance().GetDeviceState();
    return state == kDeviceStateIdle ||
           (state == kDeviceStateListening && g_listening_closed.load(std::memory_order_relaxed));
}

void NotifyListeningClosed(bool closed) {
    g_listening_closed.store(closed, std::memory_order_relaxed);
    if (g_listening_closed_handler) {
        g_listening_closed_handler(closed);
    }
}

void NotifyVoiceActivity(bool speaking) {
    if (g_voice_activity_handler) {
        g_voice_activity_handler(speaking);
    }
}
}  // namespace

MemoriaProtocol::MemoriaProtocol() {
    g_active_protocol = this;
    event_group_ = xEventGroupCreate();
    server_sample_rate_ = kDownlinkSampleRate24k;
    server_frame_duration_ = kFrameMs;
    static bool logged_vad_start_floor = false;
    if (!logged_vad_start_floor) {
        logged_vad_start_floor = true;
        ESP_LOGI(kTag, "vad.start rms floor=%.4f",
                 static_cast<double>(ConfiguredVadStartRmsFloor()));
    }
}

MemoriaProtocol::~MemoriaProtocol() {
    if (g_active_protocol == this) {
        g_active_protocol = nullptr;
    }
    // Application owns the AudioService captured by this callback and its
    // lifetime can end before Protocol member destruction.
    on_local_flush_requested_ = nullptr;
    on_audio_channel_closed_ = nullptr;
    on_disconnected_ = nullptr;
    on_transport_action_ready_ = nullptr;
    if (activation_retry_task_ != nullptr &&
        activation_retry_task_ != xTaskGetCurrentTaskHandle()) {
        vTaskDelete(activation_retry_task_);
        activation_retry_task_ = nullptr;
    }
    if (display_profile_task_ != nullptr &&
        display_profile_task_ != xTaskGetCurrentTaskHandle()) {
        vTaskDelete(display_profile_task_);
        display_profile_task_ = nullptr;
    }
    CloseAudioChannel(false);
    // Application must retain the whole protocol until PrepareForDestruction succeeds.
    // A timeout is never permission to destroy callback owners.
    ReapRetiredWebSockets();
    configASSERT(retired_websockets_.empty());
    if (event_group_ != nullptr) {
        vEventGroupDelete(event_group_);
    }
}

bool MemoriaProtocol::Start() {
    error_occurred_ = false;
    const esp_err_t identity_result = identity_.Load();
    if (identity_result != ESP_OK) {
        ESP_LOGE(kTag, "Device identity unavailable, code=%s", esp_err_to_name(identity_result));
        SetError("设备身份不可用");
        return false;
    }
    MemoriaActivationClient activation_client(identity_);
    const esp_err_t activation_result = activation_client.Activate(&activation_);
    if (activation_result == ESP_OK || activation_result == ESP_ERR_INVALID_STATE) {
        // Control API answered this image (bound or not): an update that got
        // this far is good; until then a reset rolls back to the old slot.
        MemoriaFirmwareUpdate::ConfirmRunningImage();
    }
    if (activation_result != ESP_OK) {
        const ActivationAttemptOutcome outcome = ActivationOutcomeFor(activation_result);
        if (ActivationRetryPolicy::ShouldRetryOnBoot(ActivationAwaitingBinding(), outcome)) {
            auto* display = dynamic_cast<LcdDisplay*>(Board::GetInstance().GetDisplay());
            if (display != nullptr &&
                MemoriaBootstrap::GetInstance().Start(display) != ESP_OK) {
                ESP_LOGE(kTag, "Unable to keep nearby bootstrap available; activation retry continues");
            }
            ESP_LOGW(kTag,
                     "Activation failed while unbound, code=%s (%d) http_status=%d transport_err=0x%x internal_free=%u internal_largest=%u",
                     esp_err_to_name(activation_result),
                     static_cast<int>(activation_result),
                     activation_client.last_http_status(),
                     activation_client.last_transport_error(),
                     static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                     static_cast<unsigned>(
                         heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)));
            StartActivationRetry();
            ESP_LOGW(kTag, "Device is unbound; nearby bootstrap remains available, activation retry started");
            return false;
        }
        ESP_LOGE(kTag, "Memoria activation failed, code=%s", esp_err_to_name(activation_result));
        SetError("Memoria 激活失败");
        return false;
    }
    // A successful retry may be the first activation after nearby bootstrap.
    // Clear its BLE/QR surface before advertising normal conversation state.
    MemoriaBootstrap::GetInstance().Stop();
    StartDisplayProfilePoll();
    if (on_connected_ != nullptr) {
        on_connected_();
    }
    return true;
}

void MemoriaProtocol::StartActivationRetry() {
    if (activation_retry_task_ != nullptr) {
        return;
    }
    const BaseType_t created = xTaskCreate(
        &MemoriaProtocol::ActivationRetryTask, "memoria_activation_retry", 8192, this, 2,
        &activation_retry_task_);
    if (created != pdPASS) {
        activation_retry_task_ = nullptr;
        ESP_LOGE(kTag, "Unable to start activation retry task");
    }
}

void MemoriaProtocol::ActivationRetryTask(void* context) {
    auto* protocol = static_cast<MemoriaProtocol*>(context);
    if (protocol != nullptr) {
        protocol->RunActivationRetry();
        protocol->activation_retry_task_ = nullptr;
    }
    vTaskDelete(nullptr);
}

void MemoriaProtocol::RunActivationRetry() {
    // Binding is committed by the Mini Program after online-proof acceptance,
    // so the first activation attempt can legitimately precede the manifest.
    // Keep the retry on its own task; never block Application's main loop on
    // the 10-second HTTPS timeout while the phone completes binding.
    //
    // Start() already logged the first failure, so the schedule begins here at
    // failure 1 and waits 5 s, 10 s, then 20 s forever.
    uint32_t failures = 1;
    bool error_page_shown = false;
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(ActivationRetryPolicy::DelayMsForFailureCount(failures)));

        MemoriaActivationClient activation_client(identity_);
        const esp_err_t result = activation_client.Activate(&activation_);
        if (result == ESP_OK) {
            // Application's activation task gave up when the first attempt
            // found the board unbound, so nothing here would move it out of
            // "activating": the screen stayed on 连接中, the audio engine
            // never started and the idle-only display-profile poll never ran.
            // Restart into the bound boot path instead (activation_v is now
            // stored): it brings the audio engine up without the BLE stack
            // ever having shared internal RAM with it.
            MemoriaBootstrap::GetInstance().Stop();
            ESP_LOGW(kTag, "Activation completed after nearby bootstrap; restarting into the bound boot path");
            vTaskDelay(pdMS_TO_TICKS(500));
            esp_restart();
        }

        ++failures;
        const ActivationAttemptOutcome outcome = ActivationOutcomeFor(result);
        ESP_LOGW(kTag, "Activation retry pending, code=%s (%d) http_status=%d transport_err=0x%x internal_free=%u internal_largest=%u",
                 esp_err_to_name(result), static_cast<int>(result),
                 activation_client.last_http_status(), activation_client.last_transport_error(),
                 static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
                 static_cast<unsigned>(
                     heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)));

        auto* display = dynamic_cast<LcdDisplay*>(Board::GetInstance().GetDisplay());
        if (error_page_shown && display != nullptr &&
            ActivationRetryPolicy::ShouldRestoreQr(outcome)) {
            // Create the QR before deleting the page so the screen never goes
            // blank, and leave the page up if the QR cannot be allocated.
            if (MemoriaBootstrap::GetInstance().Start(display) == ESP_OK) {
                HideActivationNetworkErrorPage(display);
                error_page_shown = false;
                ESP_LOGW(kTag, "Server reports the board unbound; QR restored");
            }
        }
        if (display != nullptr &&
            ActivationRetryPolicy::ShouldShowErrorPage(failures, outcome)) {
            if (!error_page_shown) {
                // Free the QR canvas before the next TLS attempt and replace it
                // with the product's network-error page.
                display->ClearQrCode();
                MemoriaBootstrap::GetInstance().Stop();
            }
            if (ShowActivationNetworkErrorPage(
                    display, ActivationFailureDetail(result, activation_client).c_str())) {
                error_page_shown = true;
            }
        }
    }
}

void MemoriaProtocol::StartDisplayProfilePoll() {
    if (display_profile_task_ != nullptr) {
        return;
    }
    const BaseType_t created = xTaskCreate(&MemoriaProtocol::DisplayProfileTask,
                                           "memoria_display_profile", 8192, this, 2,
                                           &display_profile_task_);
    if (created != pdPASS) {
        display_profile_task_ = nullptr;
        ESP_LOGE(kTag, "Unable to start display profile poll");
    }
}

void MemoriaProtocol::DisplayProfileTask(void* context) {
    auto* protocol = static_cast<MemoriaProtocol*>(context);
    std::string applied_version;
    bool polled = false;
    uint64_t next_release_check_ms = 0;
    uint64_t next_firmware_check_ms = DeviceMonotonicMs() + kFirstFirmwareCheckMs;
    while (protocol != nullptr) {
        // Only while idle: a conversation must never share the radio with a
        // cosmetic poll, and the next idle moment is soon enough.
        if (IsConnectedStandby()) {
            polled = true;
            const std::string base = !protocol->activation_.control_api_url.empty()
                                         ? protocol->activation_.control_api_url
                                         : protocol->identity_.control_api_url();
            MemoriaActivationClient client(protocol->identity_);
            DisplayProfile profile;
            const esp_err_t result = client.FetchDisplayProfile(base, &profile);
            if (result == ESP_OK && !profile.wake_mode.empty() &&
                WakeModeRegistry::GetInstance().Apply(profile.wake_mode)) {
                // Tap-only product: keep idle mic/wake arming consistent.
                Application::GetInstance().RefreshWakeMode();
            }
            if (result == ESP_OK) {
                PowerPolicyRegistry::GetInstance().ApplyQuietHours(
                    profile.quiet_hours_start.c_str(), profile.quiet_hours_end.c_str());
            }
            if (result == ESP_OK && profile.display_version != applied_version) {
                applied_version = profile.display_version;
                ESP_LOGI(kTag, "Display profile companion=%s version=%s",
                         profile.companion_id.c_str(), profile.display_version.c_str());
                PublishCompanion(profile.companion_id.c_str());
            } else if (result == ESP_ERR_INVALID_STATE &&
                       DeviceMonotonicMs() >= next_release_check_ms) {
                // 409: possibly released by the phone. Without a check the
                // board would keep its old binding until a reboot and never
                // offer the QR the phone needs to bind it again.
                if (protocol->ConfirmServerRelease()) {
                    // Restart into the unbound boot path: QR and BLE come up
                    // before the audio engine, which cannot share internal
                    // RAM with the BLE stack once it runs.
                    ESP_LOGW(kTag, "Device released by the server; restarting into nearby bootstrap");
                    vTaskDelay(pdMS_TO_TICKS(500));
                    esp_restart();
                }
                next_release_check_ms = DeviceMonotonicMs() + kReleaseRecheckMs;
            } else if (result != ESP_OK && result != ESP_ERR_INVALID_STATE) {
                ESP_LOGW(kTag, "Display profile poll failed, code=%s", esp_err_to_name(result));
            }
            if (result == ESP_OK && MemoriaFirmwareUpdate::AcceptRequest(profile.firmware_request_id)) {
                next_firmware_check_ms = 0;
            }
            if (DeviceMonotonicMs() >= next_firmware_check_ms) {
                protocol->CheckFirmwareUpdate(base, &next_firmware_check_ms);
            }
        }
        // Right after activation the device is not idle yet; check back soon.
        vTaskDelay(pdMS_TO_TICKS(polled ? kDisplayProfilePollMs : 3000));
    }
    vTaskDelete(nullptr);
}

void MemoriaProtocol::CheckFirmwareUpdate(const std::string& base, uint64_t* next_check_ms) {
    const auto idle = []() {
        return IsConnectedStandby();
    };
    const FirmwareCheck check = MemoriaFirmwareUpdate::CheckAndStage(identity_, base, idle);
    MemoriaActivationClient report_client(identity_);
    DisplayProfile report_profile;
    report_client.FetchDisplayProfile(base, &report_profile);
    if (check != FirmwareCheck::kStaged) {
        *next_check_ms =
            DeviceMonotonicMs() + (check == FirmwareCheck::kRetryLater ? kFirmwareRetryMs : kFirmwareCheckMs);
        return;
    }
    // Never cut a conversation short: restart at the next quiet moment.
    while (!idle()) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    ESP_LOGW(kTag, "Restarting into the new firmware");
    Board::GetInstance().GetDisplay()->ShowNotification("正在更新，马上回来", 3000);
    vTaskDelay(pdMS_TO_TICKS(1500));
    esp_restart();
}

bool MemoriaProtocol::ConfirmServerRelease() {
    // Activate() answers ESP_ERR_INVALID_STATE only for a manifest 409 and
    // then clears activation_v itself (activation_ctr is kept).
    MemoriaActivationClient client(identity_);
    ActivationProfile probe;
    const esp_err_t result = client.Activate(&probe);
    if (result == ESP_ERR_INVALID_STATE) {
        return true;
    }
    ESP_LOGW(kTag, "Display profile unavailable but activation manifest answers %s; keeping binding",
             esp_err_to_name(result));
    return false;
}

bool MemoriaProtocol::CreateMediaSession(MediaSession* session) {
    if (session == nullptr || !identity_.loaded() || activation_.control_api_url.empty()) {
        return false;
    }
    const std::string base = activation_.control_api_url;
    const std::string device_path = "/v1/devices/" + identity_.device_id();
    std::string challenge_response;
    if (!HttpJson(JoinUrl(base, device_path + "/media-challenge"),
                  {{"X-Device-Certificate-ID", identity_.certificate_id()},
                   {"X-Client-ID", identity_.client_id()}},
                  "{}", &challenge_response)) {
        return false;
    }
    ScopedJson challenge{cJSON_ParseWithLength(challenge_response.data(), challenge_response.size())};
    std::string challenge_id;
    std::string nonce;
    std::string issued_at;
    std::string response_device_id;
    std::string response_certificate_id;
    std::string response_client_id;
    std::string algorithm;
    if (challenge.value == nullptr ||
        !GetString(challenge.value, "challenge_id", &challenge_id, 128) ||
        !GetString(challenge.value, "nonce", &nonce, 128) || nonce.size() != 43 ||
        !GetString(challenge.value, "issued_at", &issued_at, 64) ||
        !GetString(challenge.value, "device_id", &response_device_id, 128) ||
        !GetString(challenge.value, "certificate_id", &response_certificate_id, 128) ||
        !GetString(challenge.value, "client_id", &response_client_id, 128) ||
        !GetString(challenge.value, "algorithm", &algorithm, 32) || algorithm != "Ed25519" ||
        response_device_id != identity_.device_id() ||
        response_certificate_id != identity_.certificate_id() ||
        response_client_id != identity_.client_id()) {
        ESP_LOGE(kTag, "Media challenge response is invalid");
        return false;
    }
    const std::string proof = MediaProofPayload(identity_, challenge_id, nonce, issued_at);
    std::array<uint8_t, DeviceIdentity::kEd25519SignatureBytes> signature{};
    if (proof.empty() ||
        identity_.SignDetached(reinterpret_cast<const uint8_t*>(proof.data()), proof.size(),
                               signature.data(), signature.size()) != ESP_OK) {
        return false;
    }
    const std::string signature_b64 = Base64UrlEncode(signature.data(), signature.size());
    sodium_memzero(signature.data(), signature.size());

    ScopedJson request{cJSON_CreateObject()};
    if (request.value == nullptr) {
        return false;
    }
    cJSON_AddStringToObject(request.value, "certificate_id", identity_.certificate_id().c_str());
    cJSON_AddStringToObject(request.value, "challenge_id", challenge_id.c_str());
    cJSON_AddStringToObject(request.value, "nonce", nonce.c_str());
    cJSON_AddStringToObject(request.value, "signature", signature_b64.c_str());
    cJSON_AddStringToObject(request.value, "client_id", identity_.client_id().c_str());
    // Negotiation advertisement: this firmware speaks v2 (direct Edge, which
    // is v2-only) first and falls back to v1 (legacy livekit_compat
    // gateway). The Control plane treats a missing/empty list as [1] for old
    // firmware and only routes to the direct Edge when 2 is explicitly
    // advertised here.
    cJSON* supported_versions = cJSON_CreateArray();
    if (supported_versions == nullptr) {
        return false;
    }
    cJSON_AddItemToArray(supported_versions, cJSON_CreateNumber(kProtocolVersionV2));
    cJSON_AddItemToArray(supported_versions, cJSON_CreateNumber(kProtocolVersionV1));
    cJSON_AddItemToObject(request.value, "supported_protocol_versions", supported_versions);
    bool resume_requested = false;
    std::string requested_session_id;
    uint32_t requested_after_epoch = 0;
    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        resume_requested = !terminal_session_close_ && !resumable_session_id_.empty() &&
                           resumable_stream_epoch_ != 0;
        requested_session_id = resumable_session_id_;
        requested_after_epoch = resumable_stream_epoch_;
    }
    if (resume_requested) {
        cJSON_AddStringToObject(request.value, "resume_session_id",
                                requested_session_id.c_str());
    }
    const std::string session_url = JoinUrl(base, device_path + "/media-sessions");
    std::string session_response;
    int session_status = 0;
    bool session_created =
        HttpJson(session_url, {}, RenderJson(request.value), &session_response, &session_status);
    const LegacyProtocolSchemaRejection legacy_rejection =
        session_status == 422 ? DetectLegacyProtocolSchemaRejection(session_response)
                              : LegacyProtocolSchemaRejection::kNone;
    const bool legacy_new_session =
        !resume_requested &&
        legacy_rejection == LegacyProtocolSchemaRejection::kAdvertisementOnly;
    bool legacy_resume_rollback =
        resume_requested &&
        legacy_rejection == LegacyProtocolSchemaRejection::kAdvertisementAndResume;
    if (!session_created && legacy_resume_rollback) {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        // Do not let a delayed 422 override a terminal button close or a newer
        // resume candidate. Only the exact snapshot advertised by this request
        // may be abandoned for a fresh v1 Session during a server rollback.
        legacy_resume_rollback =
            !terminal_session_close_ && resumable_session_id_ == requested_session_id &&
            resumable_stream_epoch_ == requested_after_epoch;
        if (legacy_resume_rollback) {
            resumable_session_id_.clear();
            resumable_stream_epoch_ = 0;
            resume_requested = false;
            cJSON_DeleteItemFromObjectCaseSensitive(request.value, "resume_session_id");
            ESP_LOGW(kTag, "Control rollback requires a fresh legacy v1 media Session");
        }
    }
    if (!session_created && (legacy_new_session || legacy_resume_rollback)) {
        // Old Control releases use extra=forbid and predate capability
        // negotiation. FastAPI rejected the body before entering the handler,
        // so the signed one-time challenge is still unconsumed. Retry exactly
        // once without the advertisement (and, on a precise server rollback,
        // without the abandoned resume id); the old response is v1-only. Any
        // other 422/auth/runtime error remains fail-closed.
        ESP_LOGW(kTag, "Control API requires legacy v1 media negotiation");
        cJSON_DeleteItemFromObjectCaseSensitive(request.value, "supported_protocol_versions");
        session_response.clear();
        session_status = 0;
        session_created =
            HttpJson(session_url, {}, RenderJson(request.value), &session_response, &session_status);
    }
    if (!session_created) {
        return false;
    }
    ScopedJson response{cJSON_ParseWithLength(session_response.data(), session_response.size())};
    uint32_t protocol_version = 0;
    if (response.value == nullptr || !GetString(response.value, "session_id", &session->session_id, 128) ||
        !GetPositiveUint32(response.value, "stream_epoch", &session->stream_epoch) ||
        !GetString(response.value, "websocket_url", &session->websocket_url, 512) ||
        !GetString(response.value, "media_token", &session->media_token, 4096) ||
        !GetPositiveUint32(response.value, "protocol_version", &protocol_version) ||
        (protocol_version != kProtocolVersionV1 && protocol_version != kProtocolVersionV2)) {
        ESP_LOGE(kTag, "Media session response is invalid");
        return false;
    }
    session->protocol_version = protocol_version;
    ESP_LOGI(kTag, "Media session negotiated protocol v%u", protocol_version);
    if (resume_requested &&
        (protocol_version != kProtocolVersionV2 ||
         session->session_id != requested_session_id ||
         session->stream_epoch <= requested_after_epoch)) {
        ESP_LOGE(kTag,
                 "Media resume response violated same-Session/newer-epoch fence "
                 "(session_match=%d version=%u epoch=%u after=%u)",
                 session->session_id == requested_session_id, protocol_version,
                 session->stream_epoch, requested_after_epoch);
        return false;
    }
    const bool websocket_scheme = session->websocket_url.rfind("wss://", 0) == 0 ||
                                  session->websocket_url.rfind("ws://", 0) == 0;
    return websocket_scheme;
}

class MemoriaSslTransport final : public Tcp {
public:
    using PingHandler = std::function<void(const char*, size_t)>;

    explicit MemoriaSslTransport(PingHandler on_ping)
        : on_ping_(std::move(on_ping)) {}

    ~MemoriaSslTransport() override {
        Disconnect();
    }

    void OnStream(std::function<void(const std::string& data)> callback) override {
        stream_callback_ = callback;
        if (state_ != nullptr) {
            std::lock_guard<std::mutex> lock(state_->callback_mutex);
            if (!state_->stopping.load(std::memory_order_acquire)) {
                state_->stream_callback = std::move(callback);
            }
        }
    }

    void OnDisconnected(std::function<void()> callback) override {
        disconnect_callback_ = callback;
        if (state_ != nullptr) {
            std::lock_guard<std::mutex> lock(state_->callback_mutex);
            if (!state_->stopping.load(std::memory_order_acquire)) {
                state_->disconnect_callback = std::move(callback);
            }
        }
    }

    void SetDisconnectWaitTimeoutMs(uint32_t timeout_ms) {
        disconnect_wait_timeout_ms_ = timeout_ms;
    }

    // True only after Disconnect() has cleared SharedState's callbacks, no
    // previously copied stream/disconnect callback is in flight on any task,
    // and ssl_receive has exited (or the transport was never started).
    // Because upstream WebSocket::~WebSocket only calls tcp_->Disconnect() when
    // WebSocket::connected_ is true and destroys WebSocket members before tcp_,
    // MemoriaProtocol uses this predicate before resetting WebSocket so a raw
    // `this` captured by WebSocket::OnTcpData / OnDisconnected cannot be used
    // after free even if Disconnect() timed out or was called from ssl_receive.
    bool CanDestroyCallbackOwner() const {
        if (state_ == nullptr) {
            return true;
        }
        return state_->stopping.load(std::memory_order_acquire) &&
               state_->in_flight_callbacks.load(std::memory_order_acquire) == 0 &&
               state_->receive_task_exited.load(std::memory_order_acquire);
    }

    bool Connect(const std::string& host, int port) override {
        if (state_ != nullptr) {
            ESP_LOGE(kTag, "MemoriaSslTransport already initialized");
            return false;
        }
        auto state = std::make_shared<SharedState>(on_ping_);
        state->event_group = xEventGroupCreate();
        if (state->event_group == nullptr) {
            last_error_ = ESP_ERR_NO_MEM;
            ESP_LOGE(kTag, "Failed to create SSL event group");
            return false;
        }
        state->stream_callback = stream_callback_;
        state->disconnect_callback = disconnect_callback_;

        esp_tls_t* tls_client = esp_tls_init();
        if (tls_client == nullptr) {
            last_error_ = ESP_ERR_NO_MEM;
            ESP_LOGE(kTag, "Failed to initialize TLS");
            return false;
        }

        esp_tls_cfg_t cfg = {};
        cfg.crt_bundle_attach = esp_crt_bundle_attach;

        const int ret = esp_tls_conn_new_sync(host.c_str(), host.length(), port, &cfg, tls_client);
        if (ret != 1) {
            esp_tls_error_handle_t last_error;
            if (esp_tls_get_error_handle(tls_client, &last_error) == ESP_OK) {
                int error_code = 0;
                int error_flags = 0;
                const esp_err_t err =
                    esp_tls_get_and_clear_last_error(last_error, &error_code, &error_flags);
                last_error_ = err;
                ESP_LOGE(kTag, "Failed to connect to %s:%d, code=0x%x", host.c_str(), port, err);
            } else {
                last_error_ = -1;
            }
            esp_tls_conn_destroy(tls_client);
            return false;
        }

        state->tls_client = tls_client;
        int sockfd = -1;
        if (esp_tls_get_conn_sockfd(tls_client, &sockfd) == ESP_OK && sockfd >= 0) {
            // lwIP without LWIP_NETCONN_FULLDUPLEX does not wake an in-flight
            // blocking read() on shutdown() until SO_RCVTIMEO expires. A short
            // 200 ms receive timeout surfaces as ESP_TLS_ERR_SSL_WANT_READ and
            // lets ssl_receive observe Disconnect() promptly.
            struct timeval tv = {};
            tv.tv_sec = 0;
            tv.tv_usec = 200 * 1000;
            setsockopt(sockfd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
        }

        state->connected.store(true, std::memory_order_release);
        connected_ = true;
        xEventGroupClearBits(state->event_group, kReceiveTaskExitBit);

        auto* state_arg = new (std::nothrow) std::shared_ptr<SharedState>(state);
        if (state_arg == nullptr) {
            connected_ = false;
            last_error_ = ESP_ERR_NO_MEM;
            ESP_LOGE(kTag, "Failed to allocate ssl_receive task state");
            return false;
        }

        TaskHandle_t task_handle = nullptr;
        const BaseType_t task_ok = xTaskCreate(
            [](void* arg) {
                auto* holder = static_cast<std::shared_ptr<SharedState>*>(arg);
                std::shared_ptr<SharedState> task_state = std::move(*holder);
                delete holder;
                task_state->receive_task.store(
                    xTaskGetCurrentTaskHandle(), std::memory_order_release);
                ReceiveLoop(task_state);
                task_state->receive_task_exited.store(true, std::memory_order_release);
                xEventGroupSetBits(task_state->event_group, kReceiveTaskExitBit);
                task_state.reset();
                vTaskDelete(nullptr);
            },
            "ssl_receive", 4096, state_arg, 1, &task_handle);
        if (task_ok != pdPASS) {
            delete state_arg;
            connected_ = false;
            last_error_ = ESP_ERR_NO_MEM;
            ESP_LOGE(kTag, "Failed to create ssl_receive task");
            return false;
        }
        state->receive_task.store(task_handle, std::memory_order_release);
        state_ = std::move(state);
        return true;
    }

    void Disconnect() override {
        connected_ = false;
        stream_callback_ = nullptr;
        disconnect_callback_ = nullptr;
        std::shared_ptr<SharedState> state = state_;
        if (state == nullptr) {
            return;
        }
        state->stopping.store(true, std::memory_order_release);
        state->connected.store(false, std::memory_order_release);
        state->disconnect_notified.store(true, std::memory_order_release);

        std::function<void(const std::string&)> old_stream_cb;
        std::function<void()> old_disconnect_cb;
        {
            std::lock_guard<std::mutex> lock(state->callback_mutex);
            old_stream_cb = std::move(state->stream_callback);
            old_disconnect_cb = std::move(state->disconnect_callback);
        }

        ShutdownSocket(state->tls_client);

        const TaskHandle_t current_task = xTaskGetCurrentTaskHandle();
        const TaskHandle_t rx_task = state->receive_task.load(std::memory_order_acquire);
        if (rx_task != nullptr && current_task != rx_task) {
            const EventBits_t bits = xEventGroupWaitBits(
                state->event_group, kReceiveTaskExitBit, pdFALSE, pdFALSE,
                pdMS_TO_TICKS(disconnect_wait_timeout_ms_));
            if ((bits & kReceiveTaskExitBit) != 0) {
                std::lock_guard<std::mutex> write_lock(state->write_mutex);
                if (state->tls_client != nullptr) {
                    esp_tls_conn_destroy(state->tls_client);
                    state->tls_client = nullptr;
                }
            } else {
                ESP_LOGE(kTag,
                         "Failed to wait for ssl_receive task exit; deferring TLS cleanup");
            }
        }
        last_error_ = state->last_error.load(std::memory_order_relaxed);
        if (CanDestroyCallbackOwner()) {
            state_.reset();
        }
    }

    int Send(const std::string& data) override {
        std::shared_ptr<SharedState> state = state_;
        if (state == nullptr || !state->connected.load(std::memory_order_acquire)) {
            return -1;
        }
        int written = -1;
        {
            std::lock_guard<std::mutex> write_lock(state->write_mutex);
            if (!state->connected.load(std::memory_order_acquire) ||
                state->tls_client == nullptr) {
                return -1;
            }
            written = WriteBoundedTls(
                data.data(), data.size(),
                [&state](const char* ptr, size_t len) {
                    return esp_tls_conn_write(state->tls_client, ptr, len);
                },
                []() { return esp_timer_get_time(); },
                ESP_TLS_ERR_SSL_WANT_WRITE);
            if (written < 0) {
                state->last_error.store(written, std::memory_order_relaxed);
                last_error_ = written;
                ESP_LOGE(kTag, "Bounded TLS write failed: ret=%d errno=%d bytes=%u",
                         written, errno, static_cast<unsigned int>(data.size()));
                state->connected.store(false, std::memory_order_release);
                connected_ = false;
                ShutdownSocket(state->tls_client);
            }
        }
        if (written < 0) {
            NotifyDisconnectOnce(state);
        }
        return written;
    }

    bool SendRawFrame(const std::string& frame) {
        return Send(frame) >= 0;
    }

    int GetLastError() override {
        if (state_ != nullptr) {
            return state_->last_error.load(std::memory_order_relaxed);
        }
        return last_error_;
    }

private:
    static constexpr EventBits_t kReceiveTaskExitBit = BIT0;

    struct SharedState {
        explicit SharedState(PingHandler ping_handler)
            : on_ping(std::move(ping_handler)) {}

        ~SharedState() {
            if (tls_client != nullptr) {
                esp_tls_conn_destroy(tls_client);
                tls_client = nullptr;
            }
            if (event_group != nullptr) {
                vEventGroupDelete(event_group);
                event_group = nullptr;
            }
        }

        PingHandler on_ping;
        WebSocketPingInterceptor ping_interceptor;
        std::mutex write_mutex;
        std::mutex callback_mutex;
        std::function<void(const std::string&)> stream_callback;
        std::function<void()> disconnect_callback;
        esp_tls_t* tls_client = nullptr;
        EventGroupHandle_t event_group = nullptr;
        std::atomic<TaskHandle_t> receive_task{nullptr};
        std::atomic<uint32_t> in_flight_callbacks{0};
        std::atomic<bool> receive_task_exited{false};
        std::atomic<bool> connected{false};
        std::atomic<bool> stopping{false};
        std::atomic<bool> disconnect_notified{false};
        std::atomic<int> last_error{0};
    };

    struct CallbackFlightGuard {
        SharedState* state = nullptr;
        explicit CallbackFlightGuard(SharedState* s) : state(s) {}
        ~CallbackFlightGuard() {
            if (state != nullptr) {
                state->in_flight_callbacks.fetch_sub(1, std::memory_order_acq_rel);
            }
        }
    };

    static void ShutdownSocket(esp_tls_t* tls_client) {
        if (tls_client == nullptr) {
            return;
        }
        int sockfd = -1;
        if (esp_tls_get_conn_sockfd(tls_client, &sockfd) == ESP_OK && sockfd >= 0) {
            // Never close(sockfd) directly here: esp_tls_conn_destroy() owns
            // closing tls->sockfd, and closing early risks double-close / FD reuse.
            shutdown(sockfd, SHUT_RDWR);
        }
    }

    static void NotifyDisconnectOnce(const std::shared_ptr<SharedState>& state) {
        if (state == nullptr || state->stopping.load(std::memory_order_acquire)) {
            return;
        }
        if (state->disconnect_notified.exchange(true, std::memory_order_acq_rel)) {
            return;
        }
        std::function<void()> cb;
        {
            std::lock_guard<std::mutex> lock(state->callback_mutex);
            if (state->stopping.load(std::memory_order_acquire) || !state->disconnect_callback) {
                return;
            }
            cb = state->disconnect_callback;
            state->in_flight_callbacks.fetch_add(1, std::memory_order_acq_rel);
        }
        CallbackFlightGuard flight_guard(state.get());
        if (!state->stopping.load(std::memory_order_acquire)) {
            cb();
        }
    }

    static void ReceiveLoop(const std::shared_ptr<SharedState>& state) {
        std::string chunk;
        while (!state->stopping.load(std::memory_order_acquire) &&
               state->connected.load(std::memory_order_acquire)) {
            chunk.resize(1500);
            const int ret = esp_tls_conn_read(state->tls_client, chunk.data(), chunk.size());
            if (ret == ESP_TLS_ERR_SSL_WANT_READ) {
                continue;
            }
            if (ret <= 0) {
                if (ret < 0 && !state->stopping.load(std::memory_order_acquire)) {
                    state->last_error.store(ret, std::memory_order_relaxed);
                    ESP_LOGE(kTag, "SSL receive failed: %d", ret);
                }
                state->connected.store(false, std::memory_order_release);
                NotifyDisconnectOnce(state);
                break;
            }
            chunk.resize(static_cast<size_t>(ret));
            const bool ok = state->ping_interceptor.OnStreamChunk(
                chunk,
                [&state](const std::string& forwarded) {
                    std::function<void(const std::string&)> cb;
                    {
                        std::lock_guard<std::mutex> lock(state->callback_mutex);
                        if (state->stopping.load(std::memory_order_acquire) ||
                            !state->stream_callback) {
                            return;
                        }
                        cb = state->stream_callback;
                        state->in_flight_callbacks.fetch_add(1, std::memory_order_acq_rel);
                    }
                    CallbackFlightGuard flight_guard(state.get());
                    if (!state->stopping.load(std::memory_order_acquire)) {
                        cb(forwarded);
                    }
                },
                [&state](const char* payload, size_t len) {
                    if (state->on_ping && !state->stopping.load(std::memory_order_acquire)) {
                        state->on_ping(payload, len);
                    }
                });
            if (!ok) {
                ESP_LOGE(kTag, "WebSocket stream/frame validation failed in TLS guard");
                state->last_error.store(-1, std::memory_order_relaxed);
                state->connected.store(false, std::memory_order_release);
                ShutdownSocket(state->tls_client);
                NotifyDisconnectOnce(state);
                break;
            }
        }
    }

    PingHandler on_ping_;
    std::shared_ptr<SharedState> state_;
    uint32_t disconnect_wait_timeout_ms_ = 10000;
    int last_error_ = 0;
};

namespace {

class MemoriaMediaNetwork final : public NetworkInterface {
public:
    using SslFactory = std::function<std::unique_ptr<Tcp>()>;

    void Configure(NetworkInterface* delegate, SslFactory ssl_factory) {
        delegate_ = delegate;
        ssl_factory_ = std::move(ssl_factory);
    }

    std::unique_ptr<Http> CreateHttp(int connect_id) override {
        return delegate_ != nullptr ? delegate_->CreateHttp(connect_id) : nullptr;
    }

    std::unique_ptr<Tcp> CreateTcp(int connect_id) override {
        return delegate_ != nullptr ? delegate_->CreateTcp(connect_id) : nullptr;
    }

    std::unique_ptr<Tcp> CreateSsl(int connect_id) override {
        if (ssl_factory_) {
            return ssl_factory_();
        }
        return delegate_ != nullptr ? delegate_->CreateSsl(connect_id) : nullptr;
    }

    std::unique_ptr<Udp> CreateUdp(int connect_id) override {
        return delegate_ != nullptr ? delegate_->CreateUdp(connect_id) : nullptr;
    }

    std::unique_ptr<Mqtt> CreateMqtt(int connect_id) override {
        return delegate_ != nullptr ? delegate_->CreateMqtt(connect_id) : nullptr;
    }

    std::unique_ptr<WebSocket> CreateWebSocket(int connect_id) override {
        return std::make_unique<WebSocket>(this, connect_id);
    }

private:
    NetworkInterface* delegate_ = nullptr;
    SslFactory ssl_factory_;
};

MemoriaMediaNetwork g_media_network;

}  // namespace

bool MemoriaProtocol::PrepareForDestruction() {
    if (!shutting_down_.exchange(true, std::memory_order_acq_rel)) {
        ++websocket_attempt_id_;
    }
    std::unique_lock<std::recursive_mutex> lock(playback_state_mutex_, std::try_to_lock);
    if (!lock.owns_lock()) {
        return false;
    }
    if (websocket_ != nullptr) {
        retired_websockets_.emplace_back(std::move(websocket_), media_ssl_transport_);
        media_ssl_transport_ = nullptr;
    }
    for (auto& retired : retired_websockets_) {
        if (retired.second != nullptr) {
            // Poll shutdown without waiting on the receiver, which may be inside
            // a callback that needs this lock. Keep the entire protocol alive.
            retired.second->SetDisconnectWaitTimeoutMs(0);
            retired.second->Disconnect();
        }
    }
    ReapRetiredWebSockets();
    return retired_websockets_.empty();
}

void MemoriaProtocol::ReapRetiredWebSockets() {
    std::deque<std::unique_ptr<WebSocket>> ready_to_destroy;
    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        auto it = retired_websockets_.begin();
        while (it != retired_websockets_.end()) {
            MemoriaSslTransport* const ssl = it->second;
            if (ssl == nullptr || ssl->CanDestroyCallbackOwner()) {
                ready_to_destroy.push_back(std::move(it->first));
                it = retired_websockets_.erase(it);
            } else {
                ++it;
            }
        }
    }
    ready_to_destroy.clear();
}

void MemoriaProtocol::RetireOrDestroyWebSocket(std::unique_ptr<WebSocket> retiring_websocket,
                                               MemoriaSslTransport* retiring_ssl,
                                               bool send_ws_close) {
    ReapRetiredWebSockets();
    if (retiring_websocket == nullptr) {
        return;
    }
    if (send_ws_close) {
        retiring_websocket->Close();
    }
    // Upstream WebSocket::~WebSocket only calls tcp_->Disconnect() when
    // WebSocket::connected_ is true, and declares tcp_ as its first member so
    // WebSocket's own members are destroyed before tcp_. Explicitly disconnect
    // the dedicated SSL transport before destroying WebSocket, and if an
    // in-flight callback or ssl_receive task is still unwinding after a
    // bounded Disconnect() timeout, defer WebSocket destruction until
    // CanDestroyCallbackOwner() reports quiescence.
    if (retiring_ssl != nullptr) {
        retiring_ssl->Disconnect();
        if (!retiring_ssl->CanDestroyCallbackOwner()) {
            std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
            retired_websockets_.emplace_back(std::move(retiring_websocket), retiring_ssl);
            return;
        }
    }
    retiring_websocket.reset();
}

std::unique_ptr<WebSocket> MemoriaProtocol::CreateMediaWebSocket(uint32_t websocket_attempt,
                                                                 bool dedicated_wifi_wss) {
    media_ssl_transport_ = nullptr;
    NetworkInterface* const network = Board::GetInstance().GetNetwork();
    if (network == nullptr) {
        return nullptr;
    }
    if (!dedicated_wifi_wss) {
        return network->CreateWebSocket(1);
    }
    g_media_network.Configure(network, [this, websocket_attempt]() -> std::unique_ptr<Tcp> {
        auto ssl = std::make_unique<MemoriaSslTransport>(
            [this, websocket_attempt](const char* payload, size_t len) {
                if (shutting_down_.load(std::memory_order_acquire)) { return; }
                QueueTransportPong(websocket_attempt,
                                   std::string(payload != nullptr ? payload : "", len));
            });
        media_ssl_transport_ = ssl.get();
        return ssl;
    });
    return g_media_network.CreateWebSocket(1);
}

bool MemoriaProtocol::OpenAudioChannel() {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return false;
    }
    if (IsAudioChannelOpened()) {
        return true;
    }
    CloseAudioChannel(false);
    MediaSession session;
    if (!CreateMediaSession(&session)) {
        SetError("设备媒体会话创建失败");
        return false;
    }
    ResetSessionState();
    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        stream_epoch_ = session.stream_epoch;
        protocol_version_ = session.protocol_version;
        session_id_ = session.session_id;
        terminal_session_close_ = false;
        error_occurred_ = false;
    }
    xEventGroupClearBits(event_group_, kSessionReadyBit | kSessionClosedBit);
    uint32_t websocket_attempt = 0;
    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        websocket_attempt = ++websocket_attempt_id_;
        transport_close_notified_ = false;
    }

    // WifiBoard -> EspNetwork -> a new TLS client per WSS socket. It owns a
    // dedicated ssl_receive task; WebSocket::OnTcpData calls OnData inline on
    // that task. AT/network callbacks can be shared and must never be boosted.
    const bool dedicated_wifi_wss =
#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT
        Board::GetInstance().GetBoardType() == "wifi" &&
        session.websocket_url.rfind("wss://", 0) == 0;
#else
        false;
#endif
    websocket_ = CreateMediaWebSocket(websocket_attempt, dedicated_wifi_wss);
    if (websocket_ == nullptr) {
        SetError("设备媒体连接不可用");
        return false;
    }
    WebSocket* const websocket = websocket_.get();
    websocket->SetReceiveBufferSize(MemoriaAudioFrame::kHeaderSize + MemoriaAudioFrame::kMaxPayloadBytes);
    const std::string authorization = "Bearer " + session.media_token;
    websocket->SetHeader("Authorization", authorization.c_str());
    websocket->SetHeader("Protocol-Version", std::to_string(protocol_version_).c_str());
    websocket->SetHeader("Device-Id", identity_.device_id().c_str());
    // The legacy Python gateway contract predates the direct Edge and binds
    // Client-Id. The v2-only Go Edge intentionally uses X-Client-ID. Keep the
    // negotiated transports explicit: an alias accepted by one ingress must
    // not silently become part of the other protocol.
    if (protocol_version_ == kProtocolVersionV1) {
        websocket->SetHeader("Client-Id", identity_.client_id().c_str());
    } else {
        websocket->SetHeader("X-Client-ID", identity_.client_id().c_str());
    }
    websocket->OnData([this, websocket, websocket_attempt, dedicated_wifi_wss](
                          const char* data, size_t size, bool binary) {
        if (shutting_down_.load(std::memory_order_acquire)) { return; }
        ObserveMediaReceiveTask(websocket_attempt, dedicated_wifi_wss);
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        if (websocket_attempt != websocket_attempt_id_.load()) {
            return;
        }
        const bool accepted = binary
                                  ? HandleDownlink(reinterpret_cast<const uint8_t*>(data), size)
                                  : HandleServerText(data, size);
        if (!accepted) {
            ESP_LOGE(kTag, "Rejected invalid device media message");
            error_occurred_ = true;
            // Close exactly the transport that delivered the violation. A
            // late old callback must never retire a newer member socket. Do
            // not call WebSocket::Close from its receive callback: AT-backed
            // transports would wait for an acknowledgement that this same
            // callback task is responsible for parsing.
            RetireTransportAttempt(websocket_attempt);
        } else {
            // Any accepted application frame proves the receive path alive;
            // do not require a redundant Pong while useful downlink traffic
            // is already flowing.
            MarkTransportAlive(websocket_attempt);
        }
        last_incoming_time_ = std::chrono::steady_clock::now();
    });
    websocket->OnPong([this, websocket_attempt](const char*, size_t) {
        if (shutting_down_.load(std::memory_order_acquire)) { return; }
        MarkTransportAlive(websocket_attempt);
    });
    websocket->OnDisconnected([this, websocket, websocket_attempt]() {
        if (shutting_down_.load(std::memory_order_acquire)) { return; }
        ESP_LOGW(kTag, "Device WebSocket disconnected attempt=%u tcp_error=%d",
                 static_cast<unsigned int>(websocket_attempt), websocket->GetLastError());
        RetireTransportAttempt(websocket_attempt);
    });
    websocket->OnError([this, websocket_attempt](int error) {
        if (shutting_down_.load(std::memory_order_acquire)) { return; }
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        if (websocket_attempt != websocket_attempt_id_.load()) return;
        ESP_LOGE(kTag, "Device WebSocket error=%d", error);
        error_occurred_ = true;
        RetireTransportAttempt(websocket_attempt);
    });
    const bool ws_connected = websocket->Connect(session.websocket_url.c_str());
    g_media_network.Configure(Board::GetInstance().GetNetwork(), nullptr);
    if (!ws_connected) {
        ESP_LOGE(kTag, "Device WebSocket connect failed, code=%d", websocket->GetLastError());
        // WebSocket::~WebSocket only calls tcp_->Disconnect() when connected_ is
        // true. If the HTTP upgrade failed or timed out after TCP/TLS connected,
        // stop the dedicated SSL transport immediately while keeping websocket_
        // allocated until the next open/close cycle (or deferring it if an
        // in-flight callback is still unwinding).
        std::unique_ptr<WebSocket> failed_websocket;
        MemoriaSslTransport* failed_ssl = nullptr;
        {
            std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
            failed_ssl = media_ssl_transport_;
            media_ssl_transport_ = nullptr;
            failed_websocket = std::move(websocket_);
        }
        RetireOrDestroyWebSocket(std::move(failed_websocket), failed_ssl, false);
        SetError("设备媒体连接失败");
        return false;
    }
    session.media_token.assign(session.media_token.size(), '\0');
    const std::string hello = DeviceHello();
    if (hello.empty() || !SendText(hello)) {
        CloseAudioChannel(false);
        return false;
    }
    const EventBits_t bits = xEventGroupWaitBits(event_group_,
                                                  kSessionReadyBit | kSessionClosedBit,
                                                  pdTRUE,
                                                  pdFALSE,
                                                  pdMS_TO_TICKS(kSessionReadyTimeoutMs));
    if ((bits & kSessionClosedBit) != 0) {
        ESP_LOGE(kTag, "Device media channel closed before session.ready/accepted");
        // WebSocket delivers OnData/OnDisconnected from its TCP receive task.
        // Setting kSessionClosedBit inside RetireTransportAttempt wakes
        // Application's higher-priority main task before WebSocket::OnTcpData
        // has returned. Stop the dedicated SSL transport now (which shuts down
        // the socket, clears callbacks, and waits up to the bounded timeout for
        // ssl_receive to exit) and only destroy WebSocket once
        // CanDestroyCallbackOwner() confirms no callback is in flight.
        std::unique_ptr<WebSocket> closed_websocket;
        MemoriaSslTransport* closed_ssl = nullptr;
        {
            std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
            closed_ssl = media_ssl_transport_;
            media_ssl_transport_ = nullptr;
            if (closed_ssl != nullptr) {
                closed_websocket = std::move(websocket_);
            }
            ResetSessionState();
        }
        if (closed_ssl != nullptr) {
            RetireOrDestroyWebSocket(std::move(closed_websocket), closed_ssl, false);
        }
        SetError("设备媒体服务不可用");
        return false;
    }
    if ((bits & kSessionReadyBit) == 0) {
        ESP_LOGE(kTag, "Timed out waiting for session.ready/session.accepted");
        CloseAudioChannel(false);
        SetError("设备媒体握手超时");
        return false;
    }
    last_incoming_time_ = std::chrono::steady_clock::now();
    if (on_audio_channel_opened_ != nullptr) {
        on_audio_channel_opened_();
    }
    return true;
}

void MemoriaProtocol::CloseAudioChannel(bool send_goodbye) {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return;
    }
    bool drain_v2_goodbye = false;
    bool send_v1_goodbye = false;
    bool notify_closed = false;
    uint32_t closing_attempt = 0;
    uint32_t legacy_stream_epoch = 0;
    std::unique_ptr<WebSocket> retiring_websocket;
    MemoriaSslTransport* retiring_ssl = nullptr;
    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        closing_attempt = websocket_attempt_id_.load();
        // `send_goodbye` is also the local terminal-close authority when the
        // transport has already disappeared. Set it before checking the socket so
        // cancelling an in-flight recovery cannot leave a resumable Session id
        // behind for a later accidental reopen.
        if (send_goodbye) {
            terminal_session_close_ = true;
        }
        if (send_goodbye && websocket_ != nullptr && websocket_->IsConnected() &&
            stream_epoch_ != 0) {
            if (protocol_version_ == kProtocolVersionV2) {
                // Keep the complete graceful boundary on the one main-task
                // FIFO: any older receipts, vad.end, session.close, then the
                // attempt retirement. Drain happens after releasing the state
                // lock and before the WebSocket owner is detached/reset.
                SendVadState(false);
                if (closing_attempt == websocket_attempt_id_.load() &&
                    QueueSessionClose("device_close")) {
                    QueueTransportRetire();
                    drain_v2_goodbye = true;
                } else {
                    RetireTransportAttempt(closing_attempt);
                }
            } else {
                // v1 has no sequenced control FIFO. Snapshot its fence here,
                // then perform both potentially blocking writes without the
                // playback/session mutex.
                send_v1_goodbye = true;
                legacy_stream_epoch = stream_epoch_;
            }
        }
        if (terminal_session_close_) {
            resumable_session_id_.clear();
            resumable_stream_epoch_ = 0;
        }
    }

    if (send_v1_goodbye) {
        SendVadState(false);
        bool attempt_is_current = false;
        {
            std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
            attempt_is_current =
                closing_attempt == websocket_attempt_id_.load() && websocket_ != nullptr &&
                websocket_->IsConnected() && protocol_version_ == kProtocolVersionV1 &&
                stream_epoch_ == legacy_stream_epoch && !error_occurred_;
        }
        if (attempt_is_current) {
            ScopedJson goodbye{cJSON_CreateObject()};
            if (goodbye.value != nullptr) {
                cJSON_AddStringToObject(goodbye.value, "type", "session.close");
                cJSON_AddNumberToObject(goodbye.value, "stream_epoch", legacy_stream_epoch);
                cJSON_AddStringToObject(goodbye.value, "reason", "device_close");
                SendText(RenderJson(goodbye.value));
            }
        }
    }

    if (drain_v2_goodbye) {
        DrainTransportActions();
    }

    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        const bool had_channel_state = websocket_ != nullptr || stream_epoch_ != 0 ||
                                       !session_id_.empty() || !resumable_session_id_.empty();
        if (!transport_close_notified_) {
            transport_close_notified_ = true;
            notify_closed = had_channel_state;
            // An already-retired owner has advanced this fence and notified
            // Application. Collecting that owner must not advance it again or
            // make the scheduled close callback look stale.
            ++websocket_attempt_id_;
        }
        // Retire this transport and detach its owner while holding the state
        // lock. Old queued audio/UI work immediately fails the same attempt
        // fence. WebSocket close/destruction happens only after unlocking: its
        // TCP receive task can synchronously run the stale disconnect callback
        // and must be able to acquire playback_state_mutex_.
        retiring_ssl = media_ssl_transport_;
        media_ssl_transport_ = nullptr;
        retiring_websocket = std::move(websocket_);
        ResetSessionState();
    }
    ReapRetiredWebSockets();
    if (retiring_websocket != nullptr) {
        retiring_websocket->Close();
        RetireOrDestroyWebSocket(std::move(retiring_websocket), retiring_ssl, false);
    }
    if (notify_closed) {
        if (on_disconnected_ != nullptr) {
            on_disconnected_();
        }
        if (on_audio_channel_closed_ != nullptr) {
            on_audio_channel_closed_();
        }
    }
}

bool MemoriaProtocol::IsAudioChannelOpened() const {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return false;
    }
    // A device session may be intentionally uplink-only for longer than the
    // upstream protocol's 120-second application-message timer. Transport
    // callbacks and WebSocket connectivity own liveness here; treating server
    // silence as disconnect both rejects valid microphone frames and logs once
    // per 20 ms audio packet after the deadline.
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    return websocket_ != nullptr && websocket_->IsConnected() && stream_epoch_ != 0 &&
           !error_occurred_;
}

// Called only by the media OnData callback; serialize the fence and priority
// change with retirement so a late callback cannot affect even its old task.
void MemoriaProtocol::ObserveMediaReceiveTask(uint32_t attempt, bool dedicated_wifi_wss) {
#if CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT
    // Sample before this mutex can lend us a waiter's priority. OnData calls
    // here before taking the state lock; otherwise a temporary inheritance
    // could be mistaken for a permanent receive-task priority.
    const UBaseType_t before = uxTaskPriorityGet(nullptr);
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    if (!dedicated_wifi_wss || attempt != websocket_attempt_id_.load() ||
        receive_priority_attempt_ == attempt) {
        return;
    }
    if (before < 4) {
        vTaskPrioritySet(nullptr, 4);
    }
    receive_priority_before_ = before;
    receive_priority_actual_ = uxTaskPriorityGet(nullptr);
    receive_priority_attempt_ = attempt;
    receive_priority_pending_ = true;
#else
    (void)attempt;
    (void)dedicated_wifi_wss;
#endif
}

void MemoriaProtocol::PollTransportLiveness() {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return;
    }
    if (transport_owner_task_ == nullptr) {
        transport_owner_task_ = xTaskGetCurrentTaskHandle();
    }
    configASSERT(transport_owner_task_ == xTaskGetCurrentTaskHandle());
    ReapRetiredWebSockets();
    uint32_t receive_attempt = 0;
    unsigned receive_before = 0;
    unsigned receive_actual = 0;
    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        if (receive_priority_pending_) {
            receive_attempt = receive_priority_attempt_;
            receive_before = receive_priority_before_;
            receive_actual = receive_priority_actual_;
            receive_priority_pending_ = false;
        }
    }
    // At most once per observed attempt; no serial I/O in the receive callback.
    if (receive_attempt != 0) {
        ESP_LOGI(kTag, "media receive task attempt=%lu priority_before=%u priority_actual=%u",
                 static_cast<unsigned long>(receive_attempt), receive_before, receive_actual);
    }
    WebSocket* ping_websocket = nullptr;
    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        if (websocket_ == nullptr || !websocket_->IsConnected() || stream_epoch_ == 0 ||
            error_occurred_) {
            return;
        }
        // The legacy Python/ASGI gateway owns a peer-initiated WebSocket
        // heartbeat that this network abstraction answers internally but does
        // not surface to MemoriaProtocol. It did not echo a client-initiated
        // Pong on the real production path, so absence of that optional Pong
        // is not evidence of a dead v1 transport. Its TCP disconnect callback,
        // send failures, and gateway close remain authoritative. The direct
        // v2 Edge is under our protocol control and keeps the strict 30s/10s
        // active Ping/Pong failure detector below.
        if (protocol_version_ == kProtocolVersionV1) {
            return;
        }

        const int64_t now_us = esp_timer_get_time();
        if (transport_pong_pending_) {
            if (now_us < transport_pong_deadline_us_) {
                return;
            }
            // This is a one-shot attempt retirement, not a per-audio-frame
            // predicate. The existing attempt fence makes every late callback
            // harmless and Application owns bounded reconnect/backoff.
            ESP_LOGW(kTag, "Device WebSocket heartbeat timed out; retiring transport attempt=%u",
                     static_cast<unsigned int>(websocket_attempt_id_.load()));
            error_occurred_ = true;
            RetireTransportAttempt(websocket_attempt_id_.load());
            return;
        } else if (transport_ping_due_us_ != 0 && now_us >= transport_ping_due_us_) {
            transport_pong_pending_ = true;
            transport_pong_deadline_us_ = now_us + kTransportPongTimeoutUs;
            ping_websocket = websocket_.get();
        }
    }

    // WebSocket::SendControlFrame owns its send mutex. Do not hold the
    // playback/session lock while it waits behind an audio frame. The raw
    // pointer is safe because this method is asserted onto the sole task that
    // can replace/destroy websocket_. Receive callbacks may retire an attempt,
    // but intentionally leave its owner for that task to collect.
    if (ping_websocket != nullptr) {
        ping_websocket->Ping();
    }
}

bool MemoriaProtocol::CanResumeSession() {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return false;
    }
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    const bool active_v2_session =
        protocol_version_ == kProtocolVersionV2 && !session_id_.empty();
    const bool saved_v2_resume =
        !resumable_session_id_.empty() && resumable_stream_epoch_ != 0;
    return !terminal_session_close_ && (active_v2_session || saved_v2_resume);
}

bool MemoriaProtocol::HasActivePlaybackGeneration() {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return false;
    }
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    return protocol_version_ == kProtocolVersionV2 && stream_epoch_ != 0 &&
           fence_.valid() && playback_active_ && playback_audio_ready_ && !playback_paused_;
}

bool MemoriaProtocol::AllowsPlaybackBargeIn() const {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return false;
    }
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    return audio_mode_ == "interrupt_assist" || audio_mode_ == "full_duplex_verified";
}

bool MemoriaProtocol::VoiceBargeInAllowed() const {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return false;
    }
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    return voice_barge_in_allowed_;
}

bool MemoriaProtocol::KeywordBargeInAllowed() const {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return false;
    }
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    return protocol_version_ == kProtocolVersionV2 && stream_epoch_ != 0 &&
           keyword_barge_in_allowed_;
}

bool MemoriaProtocol::LocalStopKeywordArmed() {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return false;
    }
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    // Edge ignores keyword.detected without the signed keyword source, and a
    // stop outside real playback would flush nothing but still report one.
    return KeywordBargeInAllowed() && HasActivePlaybackGeneration();
}

bool MemoriaProtocol::SendAudio(std::unique_ptr<AudioStreamPacket> packet) {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return false;
    }
    // Drain already-encoded packets while standby capture is closed.
    if (g_listening_closed.load(std::memory_order_relaxed)) {
        return true;
    }
    if (!IsAudioChannelOpened() || packet == nullptr || packet->payload.empty() ||
        packet->sample_rate != static_cast<int>(kUplinkSampleRate) ||
        packet->frame_duration != static_cast<int>(kFrameMs)) {
        return false;
    }
    MemoriaAudioFrameMetadata metadata{};
    metadata.direction = MemoriaAudioDirection::kUplink;
    metadata.stream_epoch = stream_epoch_;
    metadata.sequence = uplink_sequence_;
    metadata.sample_start = uplink_sample_start_;
    metadata.frame_samples = MemoriaAudioFrame::kUplinkFrameSamples;
    metadata.generation_id = 0;
    std::vector<uint8_t> encoded(MemoriaAudioFrame::kHeaderSize + packet->payload.size());
    size_t encoded_size = 0;
    const MemoriaAudioFrameError result = MemoriaAudioFrame::Encode(
        metadata, packet->payload.data(), packet->payload.size(), encoded.data(), encoded.size(),
        &encoded_size);
    if (result != MemoriaAudioFrameError::kOk) {
        ESP_LOGE(kTag, "Uplink frame encode failed: %s", MemoriaAudioFrameErrorName(result));
        return false;
    }
    if (!websocket_->Send(encoded.data(), encoded_size, true)) {
        return false;
    }
    uplink_sequence_ = (uplink_sequence_ + 1) & 0xffffffffU;
    uplink_sample_start_ += MemoriaAudioFrame::kUplinkFrameSamples;
    TrackUplinkVadFrame(packet->near_end_rms, packet->afe_output_ms);
    if (vad_active_ && uplink_sample_start_ - vad_started_sample_ >= kMaxVadSpeechSamples) {
        ESP_LOGW(kTag, "Closing overlong device VAD epoch at sample=%llu",
                 static_cast<unsigned long long>(uplink_sample_start_));
        SendVadState(false);
    }
    return true;
}

bool MemoriaProtocol::HandleDownlink(const uint8_t* data, size_t size) {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    MemoriaAudioFrameMetadata metadata{};
    const uint8_t* payload = nullptr;
    size_t payload_size = 0;
    const MemoriaAudioFrameError result =
        MemoriaAudioFrame::Decode(data, size, &metadata, &payload, &payload_size);
    if (result != MemoriaAudioFrameError::kOk ||
        metadata.direction != MemoriaAudioDirection::kDownlink) {
        ++protocol_violations_;
        ESP_LOGE(kTag, "Downlink frame malformed: %s", MemoriaAudioFrameErrorName(result));
        return false;
    }
    if (metadata.stream_epoch != stream_epoch_) {
        // An old epoch reviving on the current connection is a hard protocol
        // violation: its sample clock is meaningless on this session.
        ++protocol_violations_;
        ESP_LOGE(kTag, "Downlink frame from a different stream epoch=%u", metadata.stream_epoch);
        return false;
    }
    if (metadata.frame_samples != downlink_frame_samples_) {
        ++protocol_violations_;
        ESP_LOGE(kTag, "Downlink frame size %u does not match negotiated rate %u Hz",
                 metadata.frame_samples, downlink_sample_rate_);
        return false;
    }
    if (metadata.generation_id == 0) {
        // Generation 0 means "no valid playback" on the wire. Drop and count;
        // it is not a session-breaking violation.
        ++dropped_frames_.generation_zero;
        ESP_LOGD(kTag, "Dropping downlink frame with generation 0 (no valid playback)");
        return true;
    }
    // Generation classification comes before the current-generation
    // continuity check: frames of a stale/stopped/paused generation carry
    // their own per-generation clock and must never be judged against the
    // current one (or break the session).
    if (protocol_version_ == kProtocolVersionV2) {
        if (!playback_active_ || metadata.generation_id != fence_.generation_id) {
            // Late frames of an old/stopped/not-yet-started generation must not
            // break the session or move the current generation clock; they
            // are dropped and counted. This runs before the paused handling:
            // a stale frame that arrives during a pause must never advance or
            // be judged against the current expected clock.
            ++dropped_frames_.stale;
            ESP_LOGD(kTag, "Dropping stale downlink generation=%u (active=%d fence=%u)",
                     metadata.generation_id, playback_active_, fence_.generation_id);
            return true;
        }
        if (playback_paused_) {
            // Current-generation frames keep arriving while paused: advance
            // the transport expected clock so resume stays continuous, but
            // never render them or move receipt state. The same gap rules
            // apply: only a flagged consistent forward gap may jump the
            // clock while paused.
            if (!AdvanceTransportClock(metadata)) {
                ++protocol_violations_;
                ESP_LOGE(kTag,
                         "Downlink gap while paused is not a flagged consistent "
                         "forward gap: seq=%llu/%llu sample=%llu/%llu",
                         static_cast<unsigned long long>(metadata.sequence),
                         static_cast<unsigned long long>(downlink_sequence_),
                         static_cast<unsigned long long>(metadata.sample_start),
                         static_cast<unsigned long long>(downlink_sample_start_));
                return false;
            }
            ++dropped_frames_.paused;
            ESP_LOGD(kTag, "Advancing transport clock for paused downlink frame generation=%u",
                     metadata.generation_id);
            return true;
        }
    } else if (metadata.generation_id == stopped_generation_id_) {
        // The locally stopped generation must never resume playback.
        ++dropped_frames_.locally_stopped;
        ESP_LOGD(kTag, "Dropping downlink frame of locally stopped generation=%u",
                 metadata.generation_id);
        return true;
    }

    // Current-generation continuity: consecutive frames advance one step,
    // and an explicitly flagged forward gap is accepted only when the
    // sequence and sample deltas are consistent multiples of frame_samples.
    // Backward, forged (inconsistent) and unflagged gaps are session-breaking
    // violations.
    if (!AdvanceTransportClock(metadata)) {
        ++protocol_violations_;
        ESP_LOGE(kTag, "Downlink sequence/sample gap: seq=%llu/%llu sample=%llu/%llu",
                 static_cast<unsigned long long>(metadata.sequence),
                 static_cast<unsigned long long>(downlink_sequence_),
                 static_cast<unsigned long long>(metadata.sample_start),
                 static_cast<unsigned long long>(downlink_sample_start_));
        return false;
    }

    const uint64_t frame_end = metadata.sample_start + metadata.frame_samples;
    fence_.generation_id = metadata.generation_id;
    const bool first_audio_frame = !playback_audio_ready_;
    playback_audio_ready_ = true;
    if (protocol_version_ == kProtocolVersionV1 && !downlink_started_) {
        downlink_started_ = true;
        EmitLegacyTts("start");
    } else if (protocol_version_ == kProtocolVersionV2 && first_audio_frame) {
        // A server generation is not audible merely because its fence is
        // active. Enter the legacy speaking/UI path only after a valid frame
        // has reached the device media boundary.
        ESP_LOGI(kTag, "First playable downlink frame generation=%u seq=%llu",
                 static_cast<unsigned int>(metadata.generation_id),
                 static_cast<unsigned long long>(metadata.sequence));
        EmitLegacyTts("start");
    }
    if (receipt_generation_id_ != metadata.generation_id) {
        receipt_generation_id_ = metadata.generation_id;
        playback_started_receipted_ = false;
        playback_terminal_receipted_ = false;
        playback_completion_pending_ = false;
        accepted_frames_ = 0;
        active_generation_received_end_ = 0;
        active_generation_received_sequence_ = 0;
        playback_output_end_ = 0;
        playback_output_sequence_ = 0;
        playback_output_frames_ = 0;
        playback_output_approximate_ = true;
        if (protocol_version_ == kProtocolVersionV1 &&
            on_local_flush_requested_ != nullptr) {
            on_local_flush_requested_(metadata.generation_id);
        }
    }
    ++accepted_frames_;
    active_generation_received_end_ = frame_end;
    active_generation_received_sequence_ = metadata.sequence;
    // v2 started/progress receipts are not sent from the receive path: their
    // watermark must be the output-commit point reported by AudioService
    // (NotifyPlaybackOutput), not a network-received position.
    if (on_incoming_audio_ != nullptr) {
        on_incoming_audio_(std::make_unique<AudioStreamPacket>(AudioStreamPacket{
            .sample_rate = static_cast<int>(downlink_sample_rate_),
            .frame_duration = static_cast<int>(kFrameMs),
            .timestamp = static_cast<uint32_t>(metadata.sample_start * 1000 / downlink_sample_rate_),
            .payload = std::vector<uint8_t>(payload, payload + payload_size),
            .generation_id = metadata.generation_id,
            .sample_start = metadata.sample_start,
            .sample_end = frame_end,
            .received_sequence = static_cast<uint32_t>(metadata.sequence)}));
    }
    return true;
}

bool MemoriaProtocol::AdvanceTransportClock(
    const MemoriaAudioFrameMetadata& metadata) {
    if (metadata.sequence == downlink_sequence_ &&
        metadata.sample_start == downlink_sample_start_) {
        // Consecutive frame: advance exactly one frame.
        downlink_sequence_ = (downlink_sequence_ + 1) & 0xffffffffU;
        downlink_sample_start_ += metadata.frame_samples;
        return true;
    }
    // Only the edge may open a same-generation forward gap, and only with
    // the header discontinuity flag (bit 0x0001; any other bit was already
    // rejected by frame validation). Backward, duplicate and unflagged gaps
    // are protocol violations.
    if ((metadata.flags & MemoriaAudioFrame::kDiscontinuityFlag) == 0) {
        return false;
    }
    if (metadata.sequence <= downlink_sequence_ ||
        metadata.sample_start < downlink_sample_start_) {
        return false;
    }
    const uint64_t sequence_delta = metadata.sequence - downlink_sequence_;
    const uint64_t sample_delta = metadata.sample_start - downlink_sample_start_;
    if (sequence_delta == 0 || metadata.frame_samples == 0 ||
        sequence_delta >
            std::numeric_limits<uint64_t>::max() / metadata.frame_samples) {
        return false;
    }
    // The gap is honest only when the sequence and sample deltas are
    // consistent multiples of the negotiated frame size: a forged jump that
    // does not line up is rejected.
    if (sample_delta != sequence_delta * metadata.frame_samples) {
        return false;
    }
    if (metadata.sample_start >
        std::numeric_limits<uint64_t>::max() - metadata.frame_samples) {
        return false;
    }
    // The stored clock always denotes the next expected frame, including
    // after a discontinuity. Advancing only to the received frame would make
    // the immediately following consecutive frame look like another gap.
    downlink_sequence_ =
        static_cast<uint32_t>((metadata.sequence + 1) & 0xffffffffULL);
    downlink_sample_start_ = metadata.sample_start + metadata.frame_samples;
    return true;
}

bool MemoriaProtocol::ValidateServerBase(const cJSON* root,
                                         const char* type,
                                         uint32_t* control_sequence,
                                         uint64_t* server_monotonic_ms) {
    std::string version_session_id;
    uint32_t version = 0;
    uint32_t epoch = 0;
    if (root == nullptr || !GetPositiveUint32(root, "version", &version) ||
        version != kProtocolVersionV2 ||
        !GetString(root, "session_id", &version_session_id, 128) ||
        version_session_id != session_id_ ||
        !GetPositiveUint32(root, "stream_epoch", &epoch) || epoch != stream_epoch_ ||
        !GetUint32(root, "control_sequence", control_sequence) ||
        !GetUint64(root, "server_monotonic_ms", server_monotonic_ms)) {
        ESP_LOGE(kTag, "Invalid v2 server control %s", type);
        return false;
    }
    return true;
}

bool MemoriaProtocol::ParseGenerationFence(const cJSON* object, GenerationFence* fence) {
    if (object == nullptr || !cJSON_IsObject(object)) {
        return false;
    }
    GenerationFence parsed{};
    if (!GetPositiveUint32(object, "turn_id", &parsed.turn_id) ||
        !GetPositiveUint32(object, "generation_id", &parsed.generation_id) ||
        !GetUint32(object, "tool_epoch", &parsed.tool_epoch) ||
        !GetPositiveUint32(object, "session_epoch", &parsed.session_epoch)) {
        return false;
    }
    if (fence != nullptr) {
        *fence = parsed;
    }
    return true;
}

bool MemoriaProtocol::HandleSessionAccepted(const cJSON* root) {
    // session.accepted is the bootstrap handshake, not an ordered server
    // control: the Go edge contract sends no control_sequence or
    // server_monotonic_ms on this message, so only the accept fields
    // (version, session_id, stream_epoch) are validated here.
    uint32_t version = 0;
    std::string response_session_id;
    uint32_t epoch = 0;
    if (root == nullptr || !GetPositiveUint32(root, "version", &version) ||
        version != kProtocolVersionV2 ||
        !GetString(root, "session_id", &response_session_id, 128) ||
        response_session_id != session_id_ ||
        !GetPositiveUint32(root, "stream_epoch", &epoch) || epoch != stream_epoch_) {
        ESP_LOGE(kTag, "Invalid v2 session.accepted bootstrap");
        return false;
    }
    std::string authority;
    std::string audio_mode;
    uint32_t downlink_rate = 0;
    uint32_t profile_version = 0;
    if (!GetString(root, "interaction_authority", &authority, 64) ||
        authority != "python_authoritative" ||
        !GetString(root, "audio_mode", &audio_mode, 32) ||
        !GetPositiveUint32(root, "downlink_sample_rate", &downlink_rate) ||
        !GetPositiveUint32(root, "runtime_profile_version", &profile_version)) {
        ESP_LOGE(kTag, "session.accepted is missing required fields");
        return false;
    }
    const cJSON* settings = cJSON_GetObjectItemCaseSensitive(root, "device_settings");
    uint32_t settings_version = 0;
    uint32_t volume_limit = 0;
    uint32_t screen_brightness = 0;
    std::string requested_audio_mode;
    std::string wake_word_id;
    std::string wake_word_pinyin;
    std::string wake_word_display;
    bool voice_barge_in_allowed = false;
    bool keyword_barge_in_allowed = false;
    bool barge_in_none = false;
    if (settings == nullptr || !cJSON_IsObject(settings) ||
        !GetUint32(settings, "settings_version", &settings_version) ||
        !GetUint32(settings, "volume_limit", &volume_limit) || volume_limit > 100 ||
        !GetUint32(settings, "screen_brightness", &screen_brightness) ||
        screen_brightness > 100 ||
        !GetString(settings, "audio_mode", &requested_audio_mode, 32) ||
        requested_audio_mode != audio_mode) {
        ESP_LOGE(kTag, "session.accepted device settings are invalid or not effective");
        return false;
    }
    const cJSON* allowed_barge_in =
        cJSON_GetObjectItemCaseSensitive(settings, "allowed_barge_in");
    if (allowed_barge_in == nullptr || !cJSON_IsArray(allowed_barge_in)) {
        ESP_LOGE(kTag, "session.accepted device settings are missing allowed_barge_in");
        return false;
    }
    const int allowed_count = cJSON_GetArraySize(allowed_barge_in);
    if (allowed_count < 1 || allowed_count > 4) {
        ESP_LOGE(kTag, "session.accepted allowed_barge_in has an invalid size");
        return false;
    }
    for (int index = 0; index < allowed_count; ++index) {
        const cJSON* kind = cJSON_GetArrayItem(allowed_barge_in, index);
        if (!cJSON_IsString(kind) || kind->valuestring == nullptr) {
            ESP_LOGE(kTag, "session.accepted allowed_barge_in entry is invalid");
            return false;
        }
        const std::string_view entry(kind->valuestring);
        if (entry != "none" && entry != "button" && entry != "keyword" &&
            entry != "voice") {
            ESP_LOGE(kTag, "session.accepted allowed_barge_in entry is unknown");
            return false;
        }
        if (entry == "voice") {
            voice_barge_in_allowed = true;
        }
        if (entry == "keyword") {
            keyword_barge_in_allowed = true;
        }
        if (entry == "none") {
            barge_in_none = true;
        }
    }
    // "none" disables every source, matching Edge's bargeInSourceAllowed.
    if (barge_in_none) {
        keyword_barge_in_allowed = false;
    }
    const cJSON* wake_word_item =
        cJSON_GetObjectItemCaseSensitive(settings, "wake_word_id");
    if (wake_word_item != nullptr && cJSON_IsString(wake_word_item) &&
        wake_word_item->valuestring != nullptr) {
        wake_word_id = wake_word_item->valuestring;
    }
    const cJSON* wake_word_pinyin_item =
        cJSON_GetObjectItemCaseSensitive(settings, "wake_word_pinyin");
    if (wake_word_pinyin_item != nullptr && cJSON_IsString(wake_word_pinyin_item) &&
        wake_word_pinyin_item->valuestring != nullptr) {
        wake_word_pinyin = wake_word_pinyin_item->valuestring;
    }
    const cJSON* wake_word_display_item =
        cJSON_GetObjectItemCaseSensitive(settings, "wake_word_display");
    if (wake_word_display_item != nullptr && cJSON_IsString(wake_word_display_item) &&
        wake_word_display_item->valuestring != nullptr) {
        wake_word_display = wake_word_display_item->valuestring;
    }
    const WakeWordSelection selection =
        ResolveWakeWordSelection(wake_word_id, wake_word_pinyin, wake_word_display);
    WakeWordRegistry::GetInstance().Configure(selection);
    WakeWordRegistry::GetInstance().PersistToNvs();
    ESP_LOGI(kTag, "applying device settings version=%u profile=%u wake_word=%s command=%s audio_mode=%s",
             settings_version, profile_version, selection.id.c_str(), selection.command.c_str(),
             audio_mode.c_str());
    // VoCat can keep capture open during playback. interrupt_assist is the
    // current ceiling until T1-T14 attest full_duplex_verified. Anything
    // outside the three contracted modes is a protocol violation.
    if (audio_mode != "half_duplex_safe" && audio_mode != "interrupt_assist" &&
        audio_mode != "full_duplex_verified") {
        ESP_LOGE(kTag, "session.accepted audio_mode=%s contradicts device capabilities",
                 audio_mode.c_str());
        ++protocol_violations_;
        return false;
    }
    uint32_t frame_samples = 0;
    if (downlink_rate == kDownlinkSampleRate16k) {
        frame_samples = MemoriaAudioFrame::kDownlinkFrameSamples16k;
    } else if (downlink_rate == kDownlinkSampleRate24k) {
        frame_samples = MemoriaAudioFrame::kDownlinkFrameSamples24k;
    } else {
        ESP_LOGE(kTag, "session.accepted downlink_sample_rate=%u is unsupported", downlink_rate);
        return false;
    }
    const cJSON* fence_item = cJSON_GetObjectItemCaseSensitive(root, "current_fence");
    GenerationFence fence{};
    const bool has_fence = fence_item != nullptr && !cJSON_IsNull(fence_item);
    bool current_generation_active = false;
    if (!GetBool(root, "current_generation_active", &current_generation_active) ||
        (has_fence && !ParseGenerationFence(fence_item, &fence)) ||
        (current_generation_active && !has_fence)) {
        ESP_LOGE(kTag, "session.accepted current_fence is invalid");
        return false;
    }
    fence_ = fence;
    playback_active_ = has_fence && current_generation_active;
    playback_audio_ready_ = false;
    playback_paused_ = false;
    downlink_sample_rate_ = downlink_rate;
    downlink_frame_samples_ = frame_samples;
    server_sample_rate_ = downlink_rate;
    runtime_profile_version_ = profile_version;
    settings_version_ = settings_version;
    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        audio_mode_ = audio_mode;
        voice_barge_in_allowed_ = voice_barge_in_allowed;
        keyword_barge_in_allowed_ = keyword_barge_in_allowed;
    }
    ESP_LOGI(kTag, "barge-in sources: voice=%d keyword=%d",
             voice_barge_in_allowed ? 1 : 0, keyword_barge_in_allowed ? 1 : 0);
    runtime_profile_pending_ = false;
    if (on_local_flush_requested_ != nullptr) {
        on_local_flush_requested_(playback_active_ ? fence_.generation_id : 0);
    }
    if (on_device_settings_received_ != nullptr) {
        on_device_settings_received_(volume_limit, screen_brightness);
    }
    SendRuntimeProfileApplied(runtime_profile_version_, settings_version_);
    xEventGroupSetBits(event_group_, kSessionReadyBit);
    return true;
}

bool MemoriaProtocol::HandleGenerationStarted(const cJSON* root, const GenerationFence& fence) {
    if (!fence.valid()) {
        return false;
    }
    // The device downlink clock restarts at 0 for every generation.started,
    // so a start must strictly advance the current authoritative fence: an
    // equal or backward fence (duplicate start, cancelled-generation
    // reactivation, reordered control) would reset the clock mid-generation
    // and is a wire violation (fail closed).
    if (fence_.valid() &&
        (fence.session_epoch < fence_.session_epoch ||
         (fence.session_epoch == fence_.session_epoch && fence.turn_id < fence_.turn_id) ||
         (fence.session_epoch == fence_.session_epoch && fence.turn_id == fence_.turn_id &&
          fence.generation_id < fence_.generation_id) ||
         (fence.session_epoch == fence_.session_epoch && fence.turn_id == fence_.turn_id &&
          fence.generation_id == fence_.generation_id &&
          fence.tool_epoch <= fence_.tool_epoch))) {
        ++protocol_violations_;
        ESP_LOGE(kTag, "generation.started fence does not strictly advance "
                       "(session=%u/%u turn=%u/%u gen=%u/%u tool=%u/%u)",
                 static_cast<unsigned int>(fence.session_epoch),
                 static_cast<unsigned int>(fence_.session_epoch),
                 static_cast<unsigned int>(fence.turn_id),
                 static_cast<unsigned int>(fence_.turn_id),
                 static_cast<unsigned int>(fence.generation_id),
                 static_cast<unsigned int>(fence_.generation_id),
                 static_cast<unsigned int>(fence.tool_epoch),
                 static_cast<unsigned int>(fence_.tool_epoch));
        return false;
    }
    // P0: a new generation must not hear the previous generation's queue
    // tail. Switch authority first, then atomically replace the AudioService
    // generation gate and clear its old work under one queue lock.
    fence_ = fence;
    playback_active_ = true;
    vad_handoff_.Completed(false);
    vad_gate_.Reset();
    vad_onset_valid_ = false;
    playback_audio_ready_ = false;
    playback_paused_ = false;
    stopped_generation_id_ = 0;
    receipt_generation_id_ = 0;
    playback_started_receipted_ = false;
    playback_terminal_receipted_ = false;
    playback_completion_pending_ = false;
    accepted_frames_ = 0;
    active_generation_received_end_ = 0;
    active_generation_received_sequence_ = 0;
    playback_output_end_ = 0;
    playback_output_sequence_ = 0;
    playback_output_frames_ = 0;
    playback_output_approximate_ = true;
    downlink_started_ = false;
    // A fixed safety script is the one reply that must not be cut short. The
    // platform owns the script text; the device only honours the flag so a
    // pat or a screen tap cannot silence it. The board reads the published
    // value, never the protocol object.
    const cJSON* interruptible = cJSON_GetObjectItemCaseSensitive(root, "interruptible");
    const bool interruptible_field = interruptible != nullptr && cJSON_IsBool(interruptible);
    const auto stop = memoria::PlaybackStopFromInterruptible(
        memoria::InterruptibleFromWire(interruptible != nullptr, interruptible_field,
                                       cJSON_IsTrue(interruptible)));
    memoria::PlaybackStopRegistry::GetInstance().Set(stop);
    ESP_LOGI(kTag, "generation %u playback_stop=%s",
             static_cast<unsigned int>(fence.generation_id), memoria::PlaybackStopName(stop));
    // Authoritative generation.started: the downlink sequence/sample clock
    // resets to 0 for the new generation (contract downlink_clock).
    downlink_sequence_ = 0;
    downlink_sample_start_ = 0;
    if (on_local_flush_requested_ != nullptr) {
        on_local_flush_requested_(fence_.generation_id);
    }
    return true;
}

bool MemoriaProtocol::HandleGenerationPauseResume(const cJSON* root,
                                                  const GenerationFence& fence,
                                                  bool pause) {
    (void)root;
    if (!fence.valid() || fence.session_epoch != fence_.session_epoch ||
        fence.generation_id != fence_.generation_id) {
        return false;
    }
    if (pause) {
        vad_handoff_.Completed(false);
        // No local duck yet: pausing stops rendering until resume. P0 closes
        // the atomic queue gate on the receive path.
        playback_paused_ = true;
        playback_audio_ready_ = false;
        if (on_local_flush_requested_ != nullptr) {
            on_local_flush_requested_(0);
        }
    } else {
        playback_paused_ = false;
        playback_audio_ready_ = false;
        if (on_local_flush_requested_ != nullptr) {
            on_local_flush_requested_(fence_.generation_id);
        }
    }
    return true;
}

bool MemoriaProtocol::HandleGenerationTerminal(const cJSON* root,
                                               const GenerationFence& fence,
                                               bool cancelled) {
    (void)root;
    if (!fence.valid()) {
        return false;
    }
    // A terminal for a generation that is no longer current (superseded by a
    // later generation.started) is a stale barrier: ignore it, receipts must
    // only ever refer to the current fence.
    if (fence.session_epoch != fence_.session_epoch ||
        fence.generation_id != fence_.generation_id) {
        ESP_LOGW(kTag, "Ignoring terminal %s for stale generation=%u (current=%u)",
                 cancelled ? "generation.cancelled" : "generation.completed",
                 static_cast<unsigned int>(fence.generation_id),
                 static_cast<unsigned int>(fence_.generation_id));
        return true;
    }
    if (cancelled) {
        vad_handoff_.Completed(false);
        // generation.cancelled is never a normal completion. Finalize the
        // receipt state first so the atomic queue flush below cannot emit a
        // second terminal receipt through its drain callback, then close the
        // local pipeline immediately (P0).
        const GenerationFence stopped_fence = fence_;
        // The ended watermark is the output-commit position only: frames
        // that were received but never handed to the playback pipeline were
        // discarded by this flush and were never played. With no committed
        // output the receipt honestly reports 0.
        const uint32_t stopped_sequence =
            playback_output_frames_ > 0 ? playback_output_sequence_ : 0;
        const uint64_t stopped_end =
            playback_output_frames_ > 0 ? playback_output_end_ : 0;
        const bool stopped_approximate =
            playback_output_frames_ > 0 && playback_output_approximate_;
        const bool had_audio = receipt_generation_id_ != 0 && !playback_terminal_receipted_ &&
                               playback_audio_ready_ && active_generation_received_end_ > 0;
        playback_completion_pending_ = false;
        fence_ = fence;
        playback_active_ = false;
        playback_audio_ready_ = false;
        playback_paused_ = false;
        receipt_generation_id_ = 0;
        playback_started_receipted_ = false;
        playback_terminal_receipted_ = false;
        accepted_frames_ = 0;
        active_generation_received_end_ = 0;
        active_generation_received_sequence_ = 0;
        playback_output_end_ = 0;
        playback_output_sequence_ = 0;
        playback_output_frames_ = 0;
        playback_output_approximate_ = true;
        downlink_started_ = false;
        if (on_local_flush_requested_ != nullptr) {
            on_local_flush_requested_(0);
        }
        if (had_audio) {
            SendPlaybackReceipt("playback.ended", stopped_fence, stopped_sequence, stopped_end,
                                stopped_approximate);
        }
        EmitLegacyTts("stop");
        return true;
    }
    // generation.completed is an authoritative end-of-stream barrier. It only
    // marks completion pending: playback.ended, the legacy tts stop and the
    // generation clear wait until AudioService's real decode/playback queue
    // drains. When the queue is already empty, finalize immediately through
    // the Application-injected idle probe.
    playback_completion_pending_ = true;
    if (playback_terminal_receipted_ ||
        (is_playback_idle_ != nullptr && is_playback_idle_())) {
        FinalizePlaybackEnded();
    }
    return true;
}

bool MemoriaProtocol::HandlePlaybackFlushV2(const cJSON* root) {
    uint32_t control_sequence = 0;
    uint64_t server_monotonic_ms = 0;
    if (!ValidateServerBase(root, "playback.flush", &control_sequence, &server_monotonic_ms)) {
        return false;
    }
    GenerationFence flush_fence;
    if (!ParseGenerationFence(cJSON_GetObjectItemCaseSensitive(root, "fence"), &flush_fence)) {
        return false;
    }
    uint32_t replacement_generation_id = 0;
    if (!GetPositiveUint32(root, "replacement_generation_id", &replacement_generation_id)) {
        return false;
    }
    const cJSON* duck = cJSON_GetObjectItemCaseSensitive(root, "duck_db");
    if (!cJSON_IsNumber(duck) || duck->valuedouble < 0 || duck->valuedouble > 60) {
        return false;
    }
    // A flush only replaces the generation this device is currently
    // rendering. The fence must be the immediate successor of the active
    // fence (same turn and tool epoch, generation + 1) and the replacement
    // id must equal it. A flush for a stale, cancelled or locally stopped
    // generation is a wire violation (fail closed): accepting it would reset
    // the downlink clock mid-generation and break continuity.
    if (!fence_.valid() || !playback_active_ ||
        flush_fence.turn_id != fence_.turn_id ||
        flush_fence.tool_epoch != fence_.tool_epoch ||
        flush_fence.generation_id != fence_.generation_id + 1 ||
        replacement_generation_id != flush_fence.generation_id) {
        ++protocol_violations_;
        ESP_LOGE(kTag, "playback.flush does not replace the active generation "
                       "(active gen=%u flush gen=%u replacement=%u)",
                 static_cast<unsigned int>(fence_.generation_id),
                 static_cast<unsigned int>(flush_fence.generation_id),
                 static_cast<unsigned int>(replacement_generation_id));
        return false;
    }
    // A pending completion is superseded by this flush; finalize it first so
    // the atomic queue flush below cannot double-report the old generation
    // through its drain callback.
    const GenerationFence flushed_fence = fence_;
    const bool had_audio = receipt_generation_id_ != 0 && !playback_terminal_receipted_ &&
                           playback_audio_ready_ && active_generation_received_end_ > 0;
    const uint32_t flush_sequence =
        playback_output_frames_ > 0 ? playback_output_sequence_ : 0;
    const uint64_t flush_end = playback_output_frames_ > 0 ? playback_output_end_ : 0;
    const bool flush_approximate =
        playback_output_frames_ > 0 && playback_output_approximate_;
    playback_completion_pending_ = false;
    // P0: install the replacement authority, then atomically replace the
    // AudioService generation gate and clear the old queue tail.
    fence_.turn_id = flush_fence.turn_id;
    fence_.tool_epoch = flush_fence.tool_epoch;
    fence_.session_epoch = flush_fence.session_epoch;
    fence_.generation_id = replacement_generation_id;
    vad_handoff_.Completed(false);
    vad_gate_.Reset();
    vad_onset_valid_ = false;
    playback_active_ = true;
    playback_audio_ready_ = false;
    playback_paused_ = false;
    receipt_generation_id_ = 0;
    playback_started_receipted_ = false;
    playback_terminal_receipted_ = false;
    accepted_frames_ = 0;
    active_generation_received_end_ = 0;
    active_generation_received_sequence_ = 0;
    playback_output_end_ = 0;
    playback_output_sequence_ = 0;
    playback_output_frames_ = 0;
    playback_output_approximate_ = true;
    downlink_started_ = false;
    // playback.flush replacement: the downlink sequence/sample clock resets
    // to 0 for the replacement generation (contract downlink_clock).
    downlink_sequence_ = 0;
    downlink_sample_start_ = 0;
    if (on_local_flush_requested_ != nullptr) {
        on_local_flush_requested_(fence_.generation_id);
    }
    if (had_audio) {
        // Receipt may arrive after the replacement starts; its saved fence
        // keeps the old generation terminal and cannot move the new ledger.
        SendPlaybackReceipt("playback.ended", flushed_fence, flush_sequence, flush_end,
                            flush_approximate);
    }
    return true;
}

bool MemoriaProtocol::HandleRuntimeProfileInvalidated(const cJSON* root) {
    uint32_t control_sequence = 0;
    uint64_t server_monotonic_ms = 0;
    if (!ValidateServerBase(root, "runtime_profile.invalidated", &control_sequence,
                            &server_monotonic_ms)) {
        return false;
    }
    uint32_t profile_version = 0;
    if (!GetPositiveUint32(root, "profile_version", &profile_version)) {
        return false;
    }
    std::string apply_at;
    if (!GetString(root, "apply_at", &apply_at, 32)) {
        return false;
    }
    if (apply_at == "next_session") {
        // The device holds no persona data; the next session simply receives
        // the new profile from the server.
        ESP_LOGI(kTag, "runtime profile %u applies at the next session", profile_version);
        return true;
    }
    if (apply_at != "next_safe_point" && apply_at != "immediate_fail_closed") {
        return false;
    }
    runtime_profile_pending_ = true;
    runtime_profile_pending_version_ = profile_version;
    runtime_profile_apply_mode_ = apply_at == "immediate_fail_closed"
                                      ? ProfileApplyMode::kImmediateFailClosed
                                      : ProfileApplyMode::kNextSafePoint;
    if (runtime_profile_apply_mode_ == ProfileApplyMode::kImmediateFailClosed) {
        // Fail closed now: stop accepting playback and end the session; the
        // next session renegotiates the profile.
        playback_active_ = false;
        playback_audio_ready_ = false;
        playback_paused_ = false;
        playback_completion_pending_ = false;
        receipt_generation_id_ = 0;
        playback_started_receipted_ = false;
        playback_terminal_receipted_ = false;
        if (on_local_flush_requested_ != nullptr) {
            on_local_flush_requested_(0);
        }
    }
    MaybeApplyPendingProfile();
    return true;
}

bool MemoriaProtocol::HandleSessionError(const cJSON* root) {
    uint32_t control_sequence = 0;
    uint64_t server_monotonic_ms = 0;
    if (!ValidateServerBase(root, "session.error", &control_sequence, &server_monotonic_ms)) {
        return false;
    }
    std::string code;
    bool retryable = false;
    if (!GetString(root, "code", &code, 128) || !GetBool(root, "retryable", &retryable)) {
        return false;
    }
    ESP_LOGW(kTag, "Server closed media session: code=%s retryable=%d", code.c_str(), retryable);
    if (!retryable) {
        terminal_session_close_ = true;
        resumable_session_id_.clear();
        resumable_stream_epoch_ = 0;
        SetError("设备媒体会话被服务端终止");
    }
    playback_active_ = false;
    playback_audio_ready_ = false;
    playback_paused_ = false;
    playback_completion_pending_ = false;
    receipt_generation_id_ = 0;
    downlink_started_ = false;
    if (on_local_flush_requested_ != nullptr) {
        on_local_flush_requested_(0);
    }
    RetireTransportAttempt(websocket_attempt_id_.load());
    return true;
}

void MemoriaProtocol::MaybeApplyPendingProfile() {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    if (!runtime_profile_pending_) {
        return;
    }
    if (playback_active_ || receipt_generation_id_ != 0) {
        return;  // not a safe point yet; retried when playback drains
    }
    if (runtime_profile_apply_mode_ == ProfileApplyMode::kNextSafePoint ||
        runtime_profile_apply_mode_ == ProfileApplyMode::kImmediateFailClosed) {
        ESP_LOGI(kTag, "Applying runtime profile %u at the session boundary",
                 runtime_profile_pending_version_);
        runtime_profile_pending_ = false;
        terminal_session_close_ = true;
        resumable_session_id_.clear();
        resumable_stream_epoch_ = 0;
        // This function may run in WebSocket's receive callback. Queue the
        // graceful close followed by attempt retirement; transport I/O is
        // owned by Application's main task.
        const uint32_t closing_attempt = websocket_attempt_id_.load();
        if (QueueSessionClose("runtime_profile_invalidated")) {
            QueueTransportRetire();
        } else {
            // QueueTransportAction already retires a full/unavailable queue.
            // A render/allocation failure happens before that helper, so use
            // the captured fence to retire exactly the same attempt once.
            RetireTransportAttempt(closing_attempt);
        }
    }
}

void MemoriaProtocol::SendButtonStop(const GenerationFence& fence, uint64_t local_flush_sample_end) {
    if (protocol_version_ != kProtocolVersionV2 || !fence.valid()) {
        return;
    }
    ++control_sequence_;
    ScopedJson root{cJSON_CreateObject()};
    if (root.value == nullptr) {
        return;
    }
    cJSON_AddStringToObject(root.value, "type", "button.stop");
    cJSON_AddNumberToObject(root.value, "version", 2);
    cJSON_AddNumberToObject(root.value, "stream_epoch", stream_epoch_);
    cJSON_AddNumberToObject(root.value, "control_sequence", control_sequence_);
    cJSON_AddNumberToObject(root.value, "device_monotonic_ms", DeviceMonotonicMs());
    cJSON* expected_fence = cJSON_CreateObject();
    cJSON_AddNumberToObject(expected_fence, "turn_id", fence.turn_id);
    cJSON_AddNumberToObject(expected_fence, "generation_id", fence.generation_id);
    cJSON_AddNumberToObject(expected_fence, "tool_epoch", fence.tool_epoch);
    cJSON_AddNumberToObject(expected_fence, "session_epoch", fence.session_epoch);
    cJSON_AddItemToObject(root.value, "expected_fence", expected_fence);
    cJSON_AddNumberToObject(root.value, "local_flush_sample_end",
                            static_cast<double>(local_flush_sample_end));
    QueueTransportText(RenderJson(root.value));
}

void MemoriaProtocol::SendKeywordStop(const GenerationFence& fence,
                                      const LocalStopKeywordHit& hit) {
    if (protocol_version_ != kProtocolVersionV2 || !fence.valid()) {
        return;
    }
    // Only table ids are sent: they are the wire identifiers Edge validates.
    const LocalStopPhrase* phrase = FindLocalStopPhrase(hit.keyword_id.c_str());
    if (phrase == nullptr) {
        ESP_LOGW(kTag, "Local stop keyword %s is not in the phrase table", hit.keyword_id.c_str());
        return;
    }
    // MultiNet reports the hit at the end of the phrase. The uplink sample
    // clock is the capture clock Edge and Voice Core anchor evidence on; the
    // phrase is estimated to span its nominal duration before that point.
    const uint64_t duration_samples =
        static_cast<uint64_t>(phrase->nominal_duration_ms) * kUplinkSampleRate / 1000;
    const uint64_t detected_sample =
        uplink_sample_start_ > duration_samples ? uplink_sample_start_ - duration_samples : 0;
    const float confidence = LocalStopReportedConfidence(hit.score);
    const float near_end_rms =
        std::isfinite(hit.near_end_rms) && hit.near_end_rms > 0.0f ? hit.near_end_rms : 0.0f;
    ++control_sequence_;
    ScopedJson root{cJSON_CreateObject()};
    if (root.value == nullptr) {
        return;
    }
    // The field set must stay exactly Edge's deviceKeywordEvent: Edge decodes
    // with DisallowUnknownFields and closes the socket on a mismatch.
    cJSON_AddStringToObject(root.value, "type", "keyword.detected");
    cJSON_AddNumberToObject(root.value, "version", 2);
    cJSON_AddNumberToObject(root.value, "stream_epoch", stream_epoch_);
    cJSON_AddNumberToObject(root.value, "control_sequence", control_sequence_);
    cJSON_AddNumberToObject(root.value, "device_monotonic_ms", DeviceMonotonicMs());
    cJSON_AddStringToObject(root.value, "keyword_id", phrase->id);
    cJSON_AddNumberToObject(root.value, "confidence", static_cast<double>(confidence));
    cJSON_AddBoolToObject(root.value, "hard_stop", true);
    cJSON* evidence = cJSON_CreateObject();
    cJSON_AddNumberToObject(evidence, "detected_sample", static_cast<double>(detected_sample));
    cJSON_AddStringToObject(evidence, "source", "local_kws");
    cJSON_AddNumberToObject(evidence, "duration_ms", phrase->nominal_duration_ms);
    cJSON_AddStringToObject(evidence, "aec_mode", "fd_low_cost");
    cJSON_AddBoolToObject(evidence, "aec_verified", false);
    // The AFE VAD is binary; its state is mapped onto the unit interval.
    cJSON_AddNumberToObject(evidence, "vad_probability", hit.voice_detected ? 1.0 : 0.0);
    cJSON_AddNumberToObject(evidence, "near_end_rms", static_cast<double>(near_end_rms));
    // No far-end level is metered on this board.
    cJSON_AddNumberToObject(evidence, "far_end_rms", 0);
    // One bound owner per device, but no voiceprint: the talker is unknown.
    cJSON_AddStringToObject(evidence, "speaker_class", "unknown");
    cJSON_AddItemToObject(root.value, "evidence", evidence);
    cJSON* expected_fence = cJSON_CreateObject();
    cJSON_AddNumberToObject(expected_fence, "turn_id", fence.turn_id);
    cJSON_AddNumberToObject(expected_fence, "generation_id", fence.generation_id);
    cJSON_AddNumberToObject(expected_fence, "tool_epoch", fence.tool_epoch);
    cJSON_AddNumberToObject(expected_fence, "session_epoch", fence.session_epoch);
    cJSON_AddItemToObject(root.value, "expected_fence", expected_fence);
    if (QueueTransportText(RenderJson(root.value))) {
        ESP_LOGI(kTag,
                 "Local stop keyword reported: id=%s score=%.3f confidence=%.2f generation=%u "
                 "detected_sample=%llu",
                 phrase->id, static_cast<double>(hit.score), static_cast<double>(confidence),
                 static_cast<unsigned int>(fence.generation_id),
                 static_cast<unsigned long long>(detected_sample));
    }
}

void MemoriaProtocol::SendPlaybackReceipt(const char* type,
                                          const GenerationFence& fence,
                                          uint32_t received_sequence,
                                          uint64_t rendered_sample_end,
                                          bool approximate) {
    if (protocol_version_ != kProtocolVersionV2 || !fence.valid()) {
        return;
    }
    ++control_sequence_;
    ScopedJson root{cJSON_CreateObject()};
    if (root.value == nullptr) {
        return;
    }
    cJSON_AddStringToObject(root.value, "type", type);
    cJSON_AddNumberToObject(root.value, "version", 2);
    cJSON_AddNumberToObject(root.value, "stream_epoch", stream_epoch_);
    cJSON_AddNumberToObject(root.value, "control_sequence", control_sequence_);
    cJSON_AddNumberToObject(root.value, "device_monotonic_ms", DeviceMonotonicMs());
    cJSON* fence_json = cJSON_CreateObject();
    cJSON_AddNumberToObject(fence_json, "turn_id", fence.turn_id);
    cJSON_AddNumberToObject(fence_json, "generation_id", fence.generation_id);
    cJSON_AddNumberToObject(fence_json, "tool_epoch", fence.tool_epoch);
    cJSON_AddNumberToObject(fence_json, "session_epoch", fence.session_epoch);
    cJSON_AddItemToObject(root.value, "fence", fence_json);
    cJSON_AddNumberToObject(root.value, "received_sequence", received_sequence);
    cJSON_AddNumberToObject(root.value, "rendered_sample_end",
                            static_cast<double>(rendered_sample_end));
    // exact means the whole reported frame was shifted out by I2S GDMA TX EOF.
    // It is a precise digital playout boundary, not acoustic proof that the
    // amplifier and speaker were audible.
    cJSON_AddBoolToObject(root.value, "approximate", approximate);
    QueueTransportText(RenderJson(root.value));
}

bool MemoriaProtocol::QueueSessionClose(const char* reason) {
    if (protocol_version_ != kProtocolVersionV2) {
        return false;
    }
    ++control_sequence_;
    ScopedJson root{cJSON_CreateObject()};
    if (root.value == nullptr) {
        return false;
    }
    cJSON_AddStringToObject(root.value, "type", "session.close");
    cJSON_AddNumberToObject(root.value, "version", 2);
    cJSON_AddNumberToObject(root.value, "stream_epoch", stream_epoch_);
    cJSON_AddNumberToObject(root.value, "control_sequence", control_sequence_);
    cJSON_AddNumberToObject(root.value, "device_monotonic_ms", DeviceMonotonicMs());
    cJSON* value = cJSON_CreateObject();
    cJSON_AddStringToObject(value, "reason", reason);
    cJSON_AddItemToObject(root.value, "value", value);
    return QueueTransportText(RenderJson(root.value));
}

void MemoriaProtocol::SendRuntimeProfileApplied(uint32_t profile_version,
                                                uint32_t settings_version) {
    if (protocol_version_ != kProtocolVersionV2 || profile_version == 0) {
        return;
    }
    ++control_sequence_;
    ScopedJson root{cJSON_CreateObject()};
    if (root.value == nullptr) {
        return;
    }
    cJSON_AddStringToObject(root.value, "type", "runtime_profile.applied");
    cJSON_AddNumberToObject(root.value, "version", 2);
    cJSON_AddNumberToObject(root.value, "stream_epoch", stream_epoch_);
    cJSON_AddNumberToObject(root.value, "control_sequence", control_sequence_);
    cJSON_AddNumberToObject(root.value, "device_monotonic_ms", DeviceMonotonicMs());
    cJSON_AddNumberToObject(root.value, "profile_version", profile_version);
    cJSON_AddNumberToObject(root.value, "settings_version", settings_version);
    QueueTransportText(RenderJson(root.value));
}

bool MemoriaProtocol::HandleServerText(const char* data, size_t size) {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    ScopedJson root{cJSON_ParseWithLength(data, size)};
    if (root.value == nullptr) {
        ++protocol_violations_;
        return false;
    }
    std::string type;
    if (!GetString(root.value, "type", &type, 64)) {
        ++protocol_violations_;
        return false;
    }
    const cJSON* epoch = cJSON_GetObjectItemCaseSensitive(root.value, "stream_epoch");
    if (cJSON_IsNumber(epoch) && epoch->valuedouble != static_cast<double>(stream_epoch_)) {
        // A control from another epoch on this connection is a hard violation.
        ++protocol_violations_;
        ESP_LOGE(kTag, "Server control %s carries a foreign stream_epoch", type.c_str());
        return false;
    }

    if (protocol_version_ == kProtocolVersionV2) {
        if (type == "session.accepted") {
            return HandleSessionAccepted(root.value);
        }
        if (type == "session.ready") {
            // WSS authentication headers were already fixed by the negotiated
            // v2 ingress before Connect(). A legacy handshake cannot mutate
            // that wire contract after the connection exists.
            ++protocol_violations_;
            ESP_LOGE(kTag, "Legacy session.ready received on negotiated v2 transport");
            return false;
        }
        if (type == "generation.started") {
            GenerationFence fence;
            if (!ParseGenerationFence(cJSON_GetObjectItemCaseSensitive(root.value, "fence"),
                                      &fence)) {
                return false;
            }
            return HandleGenerationStarted(root.value, fence);
        }
        if (type == "generation.pause" || type == "generation.resume") {
            GenerationFence fence;
            if (!ParseGenerationFence(cJSON_GetObjectItemCaseSensitive(root.value, "fence"),
                                      &fence)) {
                return false;
            }
            return HandleGenerationPauseResume(root.value, fence, type == "generation.pause");
        }
        if (type == "generation.cancelled" || type == "generation.completed") {
            GenerationFence fence;
            if (!ParseGenerationFence(cJSON_GetObjectItemCaseSensitive(root.value, "fence"),
                                      &fence)) {
                return false;
            }
            return HandleGenerationTerminal(root.value, fence, type == "generation.cancelled");
        }
        if (type == "playback.flush") {
            return HandlePlaybackFlushV2(root.value);
        }
        if (type == "playback.duck") {
            // The device declares no local duck; accept the control and keep
            // rendering. Real ducking is a later milestone.
            uint32_t control_sequence = 0;
            uint64_t server_monotonic_ms = 0;
            return ValidateServerBase(root.value, "playback.duck", &control_sequence,
                                      &server_monotonic_ms);
        }
        if (type == "runtime_profile.invalidated") {
            return HandleRuntimeProfileInvalidated(root.value);
        }
        if (type == "session.error") {
            return HandleSessionError(root.value);
        }
        // UI events are shared with v1 servers.
        if (type == "screen.subtitle") {
            std::string text;
            if (GetString(root.value, "text", &text, 4096)) {
                EmitLegacyTts("sentence_start", text.c_str());
            }
            return true;
        }
        if (type == "screen.expression") {
            std::string expression;
            if (GetString(root.value, "expression", &expression, 64)) {
                EmitLegacyExpression(expression.c_str());
            }
            return true;
        }
        if (type == "transcript.final" || type == "transcript.partial") {
            std::string text;
            if (GetString(root.value, "text", &text, 4096)) {
                EmitLegacyStt(text.c_str());
            }
            return true;
        }
        if (type == "assistant.state") {
            uint32_t control_sequence = 0;
            uint64_t server_monotonic_ms = 0;
            if (!ValidateServerBase(root.value, "assistant.state", &control_sequence,
                                    &server_monotonic_ms)) {
                return false;
            }
            std::string assistant_state;
            if (!GetString(root.value, "state", &assistant_state, 64)) {
                return false;
            }
            if (assistant_state == "listening_closed" || assistant_state == "listening") {
                const bool closed = assistant_state == "listening_closed";
                if (closed) {
                    vad_handoff_.Completed(false);
                    vad_gate_.Reset();
                    vad_onset_valid_ = false;
                }
                ESP_LOGI(kTag, "listening_closed closed=%s", closed ? "true" : "false");
                NotifyListeningClosed(closed);
            }
            return true;
        }
        if (type == "session.close") {
            uint32_t control_sequence = 0;
            uint64_t server_monotonic_ms = 0;
            if (!ValidateServerBase(root.value, "session.close", &control_sequence,
                                    &server_monotonic_ms)) {
                return false;
            }
            std::string close_reason;
            if (GetString(root.value, "reason", &close_reason, 128)) {
                // An older server omits play_line; absence means the line was
                // already heard on the wire.
                bool play_line = false;
                GetBool(root.value, "play_line", &play_line);
                const SessionCloseFallback fallback =
                    ClassifySessionCloseFallback(close_reason, play_line);
                if (fallback != SessionCloseFallback::kNone) {
                    OfflineMenuSignals::PostLlmUnavailable(
                        fallback == SessionCloseFallback::kMenuPlayLine);
                }
            }
            NotifyListeningClosed(false);
            terminal_session_close_ = true;
            resumable_session_id_.clear();
            resumable_stream_epoch_ = 0;
            playback_active_ = false;
            playback_audio_ready_ = false;
            playback_paused_ = false;
            playback_completion_pending_ = false;
            receipt_generation_id_ = 0;
            if (on_local_flush_requested_ != nullptr) {
                on_local_flush_requested_(0);
            }
            RetireTransportAttempt(websocket_attempt_id_.load());
            return true;
        }
        // Unknown but well-formed v2 messages (conversation.state,
        // assistant.audio.started, ...) are tolerated for forward
        // compatibility; they are not session-breaking violations.
        ESP_LOGD(kTag, "Ignoring unknown v2 server message type=%s", type.c_str());
        return true;
    }

    if (type == "session.ready") {
        if (protocol_version_ != kProtocolVersionV1) {
            ++protocol_violations_;
            return false;
        }
        std::string response_session_id;
        if (!GetString(root.value, "session_id", &response_session_id, 128) ||
            response_session_id != session_id_) {
            return false;
        }
        xEventGroupSetBits(event_group_, kSessionReadyBit);
        return true;
    }
    if (type == "playback.flush") {
        uint32_t generation = 0;
        if (!GetPositiveUint32(root.value, "generation_id", &generation) ||
            generation <= fence_.generation_id) {
            return false;
        }
        fence_.generation_id = generation;
        playback_active_ = true;
        stopped_generation_id_ = 0;
        active_generation_received_end_ = downlink_sample_start_;
        playback_receipted_end_ = downlink_sample_start_;
        downlink_started_ = true;
        if (on_local_flush_requested_ != nullptr) {
            on_local_flush_requested_(generation);
        }
        EmitLegacyTts("start");
        return true;
    }
    if (type == "screen.subtitle") {
        std::string text;
        if (GetString(root.value, "text", &text, 4096)) {
            EmitLegacyTts("sentence_start", text.c_str());
        }
        return true;
    }
    if (type == "transcript.final" || type == "transcript.partial") {
        std::string text;
        if (GetString(root.value, "text", &text, 4096)) {
            EmitLegacyStt(text.c_str());
        }
        return true;
    }
    if (type == "screen.expression") {
        std::string expression;
        if (GetString(root.value, "expression", &expression, 64)) {
            EmitLegacyExpression(expression.c_str());
        }
        return true;
    }
    if (type == "assistant.state") {
        std::string state;
        if (!GetString(root.value, "state", &state, 64)) {
            return false;
        }
        if (state == "speaking") {
            EmitLegacyTts("start");
        } else if (state == "ready" || state == "listening" || state == "interrupted" ||
                   state == "idle") {
            EmitLegacyTts("stop");
        }
        return true;
    }
    if (type == "session.close") {
        return true;
    }
    // Forward no arbitrary server JSON into the upstream command handler.
    return type == "transcript.partial";
}

std::string MemoriaProtocol::DeviceHello() const {
    return protocol_version_ == kProtocolVersionV2 ? DeviceHelloV2() : DeviceHelloV1();
}

std::string MemoriaProtocol::DeviceHelloV1() const {
    ScopedJson root{cJSON_CreateObject()};
    if (root.value == nullptr) {
        return {};
    }
    cJSON_AddStringToObject(root.value, "type", "device.hello");
    cJSON_AddNumberToObject(root.value, "version", 1);
    cJSON_AddStringToObject(root.value, "device_id", identity_.device_id().c_str());
    cJSON_AddStringToObject(root.value, "firmware_version", esp_app_get_description()->version);
    cJSON_AddStringToObject(root.value, "board_profile", BOARD_NAME);
    cJSON_AddNumberToObject(root.value, "stream_epoch", stream_epoch_);
    cJSON* capabilities = cJSON_CreateObject();
    cJSON_AddBoolToObject(capabilities, "display", true);
    cJSON_AddBoolToObject(capabilities, "microphone", true);
    cJSON_AddBoolToObject(capabilities, "speaker", true);
    cJSON_AddBoolToObject(capabilities, "device_aec", false);
    cJSON_AddBoolToObject(capabilities, "physical_button", true);
    cJSON_AddItemToObject(root.value, "capabilities", capabilities);
    cJSON* audio = cJSON_CreateObject();
    cJSON_AddStringToObject(audio, "uplink_codec", "opus");
    cJSON_AddNumberToObject(audio, "uplink_sample_rate", kUplinkSampleRate);
    cJSON_AddNumberToObject(audio, "downlink_sample_rate", kDownlinkSampleRate24k);
    cJSON_AddNumberToObject(audio, "channels", 1);
    cJSON_AddNumberToObject(audio, "frame_ms", kFrameMs);
    cJSON_AddItemToObject(root.value, "audio", audio);
    return RenderJson(root.value);
}

std::string MemoriaProtocol::DeviceHelloV2() const {
    ScopedJson root{cJSON_CreateObject()};
    if (root.value == nullptr) {
        return {};
    }
    cJSON_AddStringToObject(root.value, "type", "device.hello");
    cJSON_AddNumberToObject(root.value, "version", 2);
    cJSON_AddStringToObject(root.value, "device_id", identity_.device_id().c_str());
    cJSON_AddStringToObject(root.value, "firmware_version", esp_app_get_description()->version);
    cJSON_AddStringToObject(root.value, "board_profile", BOARD_NAME);
    cJSON_AddNumberToObject(root.value, "stream_epoch", stream_epoch_);
    cJSON* capabilities = cJSON_CreateObject();
    cJSON_AddBoolToObject(capabilities, "display", true);
    cJSON_AddBoolToObject(capabilities, "microphone", true);
    cJSON_AddBoolToObject(capabilities, "speaker", true);
    // VoCat ES7210+ES8311 exposes a playback-reference channel and can keep
    // capture open during playback. AEC is present but not T1-T14 verified,
    // so the server may open interrupt_assist and must not claim
    // full_duplex_verified from this hello alone.
    cJSON_AddBoolToObject(capabilities, "simultaneous_capture_playback", true);
    cJSON_AddStringToObject(capabilities, "aec_mode", "fd_low_cost");
    cJSON_AddStringToObject(capabilities, "aec_reference", "software_post_gain_pre_i2s");
    cJSON_AddBoolToObject(capabilities, "aec_reference_verified", false);
    cJSON_AddBoolToObject(capabilities, "local_vad", true);
    // MultiNet stop phrases (memoria_stop_keyword.h) run on the AEC/NS output
    // during playback and report keyword.detected with hard_stop=true.
    cJSON_AddBoolToObject(capabilities, "local_stop_keyword", kLocalStopKeywordEnabled);
    cJSON_AddBoolToObject(capabilities, "physical_stop_button", true);
    cJSON_AddStringToObject(capabilities, "playback_watermark", "exact");
    cJSON_AddBoolToObject(capabilities, "local_duck", false);
    cJSON_AddNumberToObject(capabilities, "barge_in_level", 1);
    cJSON_AddItemToObject(root.value, "capabilities", capabilities);
    cJSON* audio = cJSON_CreateObject();
    cJSON_AddStringToObject(audio, "uplink_codec", "opus");
    cJSON_AddNumberToObject(audio, "uplink_sample_rate", kUplinkSampleRate);
    cJSON* downlink_rates = cJSON_CreateArray();
    // Both rates are genuinely playable by this board: 24 kHz is the native
    // codec rate and 16 kHz is resampled by the playback pipeline.
    cJSON_AddItemToArray(downlink_rates, cJSON_CreateNumber(kDownlinkSampleRate16k));
    cJSON_AddItemToArray(downlink_rates, cJSON_CreateNumber(kDownlinkSampleRate24k));
    cJSON_AddItemToObject(audio, "downlink_sample_rates", downlink_rates);
    cJSON_AddNumberToObject(audio, "channels", 1);
    cJSON_AddNumberToObject(audio, "frame_ms", kFrameMs);
    cJSON_AddItemToObject(root.value, "audio", audio);
    return RenderJson(root.value);
}

bool MemoriaProtocol::SendText(const std::string& text) {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return false;
    }
    return websocket_ != nullptr && websocket_->IsConnected() && websocket_->Send(text);
}

bool MemoriaProtocol::QueueTransportAction(TransportActionKind kind, std::string text) {
    std::function<void()> wake_main_task;
    uint32_t websocket_attempt = 0;
    bool fail_closed = false;
    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        websocket_attempt = websocket_attempt_id_.load();
        if (websocket_ == nullptr || stream_epoch_ == 0 || error_occurred_) {
            return false;
        }
        if (on_transport_action_ready_ == nullptr ||
            transport_actions_.size() >= kMaxTransportActions) {
            ESP_LOGE(kTag,
                     "Device transport action queue unavailable/full; retiring attempt=%u size=%u",
                     static_cast<unsigned int>(websocket_attempt),
                     static_cast<unsigned int>(transport_actions_.size()));
            error_occurred_ = true;
            fail_closed = true;
        } else {
            transport_actions_.push_back(
                TransportAction{kind, websocket_attempt, std::move(text)});
            wake_main_task = on_transport_action_ready_;
        }
    }
    if (fail_closed) {
        RetireTransportAttempt(websocket_attempt);
        return false;
    }
    // The callback only sets an Application event bit and is safe from the
    // WebSocket receive task or audio callback tasks.
    wake_main_task();
    return true;
}

bool MemoriaProtocol::QueueTransportText(std::string text) {
    if (text.empty()) {
        return false;
    }
    return QueueTransportAction(TransportActionKind::kSendText, std::move(text));
}

bool MemoriaProtocol::QueueTransportPong(uint32_t websocket_attempt, std::string payload) {
    std::string frame;
    if (!WebSocketPingInterceptor::BuildMaskedPongFrame(
            payload.data(), payload.size(), esp_random(), &frame)) {
        return false;
    }
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    if (websocket_attempt != websocket_attempt_id_.load()) {
        return false;
    }
    return QueueTransportAction(TransportActionKind::kSendPong, std::move(frame));
}

void MemoriaProtocol::QueueTransportRetire() {
    QueueTransportAction(TransportActionKind::kRetire);
}

void MemoriaProtocol::SetTransportActionWakeCallback(std::function<void()> callback) {
    std::function<void()> wake_main_task;
    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        on_transport_action_ready_ = std::move(callback);
        if (!transport_actions_.empty()) {
            wake_main_task = on_transport_action_ready_;
        }
    }
    if (wake_main_task != nullptr) {
        wake_main_task();
    }
}

void MemoriaProtocol::DrainTransportActions() {
    if (shutting_down_.load(std::memory_order_acquire)) {
        return;
    }
    if (transport_owner_task_ == nullptr) {
        transport_owner_task_ = xTaskGetCurrentTaskHandle();
    }
    configASSERT(transport_owner_task_ == xTaskGetCurrentTaskHandle());
    while (true) {
        TransportAction action;
        WebSocket* action_websocket = nullptr;
        MemoriaSslTransport* action_ssl = nullptr;
        {
            std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
            if (transport_actions_.empty()) {
                return;
            }
            action = std::move(transport_actions_.front());
            transport_actions_.pop_front();
            if (action.websocket_attempt != websocket_attempt_id_.load()) {
                continue;
            }
            if (action.kind == TransportActionKind::kSendText ||
                action.kind == TransportActionKind::kSendPong) {
                if (websocket_ == nullptr || stream_epoch_ == 0 || error_occurred_) {
                    continue;
                }
                action_websocket = websocket_.get();
                action_ssl = media_ssl_transport_;
            }
        }

        if (action.kind == TransportActionKind::kRetire) {
            RetireTransportAttempt(action.websocket_attempt);
            continue;
        }

        if (action.kind == TransportActionKind::kSendPong) {
            if (action_ssl == nullptr || !action_websocket->IsConnected() ||
                !action_ssl->SendRawFrame(action.text)) {
                {
                    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
                    if (action.websocket_attempt == websocket_attempt_id_.load()) {
                        error_occurred_ = true;
                    }
                }
                RetireTransportAttempt(action.websocket_attempt);
                return;
            }
            continue;
        }

        // Only this owner task can destroy/replace websocket_. Do not hold the
        // state lock during Send: AT-backed transports need their receive/event
        // task to parse the send acknowledgement, and that task may also be
        // trying to enter a fenced protocol callback.
        if (!action_websocket->IsConnected() || !action_websocket->Send(action.text)) {
            {
                std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
                if (action.websocket_attempt == websocket_attempt_id_.load()) {
                    error_occurred_ = true;
                }
            }
            RetireTransportAttempt(action.websocket_attempt);
            return;
        }
    }
}

bool MemoriaProtocol::SendListeningResume() {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    if (protocol_version_ != kProtocolVersionV2 || stream_epoch_ == 0) {
        return false;
    }
    ++control_sequence_;
    ScopedJson root{cJSON_CreateObject()};
    if (root.value == nullptr) {
        return false;
    }
    cJSON_AddStringToObject(root.value, "type", "listening.resume");
    cJSON_AddNumberToObject(root.value, "version", 2);
    cJSON_AddNumberToObject(root.value, "stream_epoch", stream_epoch_);
    cJSON_AddNumberToObject(root.value, "control_sequence", control_sequence_);
    cJSON_AddNumberToObject(root.value, "device_monotonic_ms", DeviceMonotonicMs());
    cJSON_AddNumberToObject(root.value, "wake_sample", static_cast<double>(uplink_sample_start_));
    return QueueTransportText(RenderJson(root.value));
}

void MemoriaProtocol::SendWakeWordDetected(const std::string& wake_word) {
    (void)wake_word;
    SendStartListening(kListeningModeAutoStop);
}

void MemoriaProtocol::SendStartListening(ListeningMode mode) {
    (void)mode;
    if (stream_epoch_ == 0 || protocol_version_ == kProtocolVersionV2) {
        // v2 has no listen.start/listen.stop; turn boundaries are carried by
        // vad events and server generation controls.
        return;
    }
    ScopedJson event{cJSON_CreateObject()};
    if (event.value == nullptr) {
        return;
    }
    cJSON_AddStringToObject(event.value, "type", "listen.start");
    cJSON_AddNumberToObject(event.value, "stream_epoch", stream_epoch_);
    cJSON_AddNumberToObject(event.value, "sample_start",
                            static_cast<double>(uplink_sample_start_));
    SendText(RenderJson(event.value));
}

void MemoriaProtocol::SendStopListening() {
    if (stream_epoch_ == 0) {
        return;
    }
    SendVadState(false);
    if (protocol_version_ == kProtocolVersionV2) {
        return;
    }
    ScopedJson event{cJSON_CreateObject()};
    if (event.value == nullptr) {
        return;
    }
    cJSON_AddStringToObject(event.value, "type", "listen.stop");
    cJSON_AddNumberToObject(event.value, "stream_epoch", stream_epoch_);
    cJSON_AddNumberToObject(event.value, "sample_start",
                            static_cast<double>(uplink_sample_start_));
    SendText(RenderJson(event.value));
}

bool MemoriaProtocol::CanRecoverVad(uint32_t afe_epoch) const {
    return afe_epoch != 0 && afe_epoch == afe_vad_epoch.load(std::memory_order_relaxed) &&
           protocol_version_ == kProtocolVersionV2 && !playback_active_ &&
           !g_listening_closed.load(std::memory_order_relaxed) &&
           Application::GetInstance().GetDeviceState() == kDeviceStateListening &&
           is_playback_idle_ && is_playback_idle_();
}

void MemoriaProtocol::NotifyPlaybackListening() {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    const uint32_t epoch = afe_vad_epoch.load();
    if (protocol_version_ != kProtocolVersionV2 || playback_active_ || g_listening_closed.load() ||
        !is_playback_idle_ || !is_playback_idle_()) {
        vad_handoff_.Completed(false);
    } else if (vad_handoff_.Listening(epoch, DeviceMonotonicMs())) {
        ESP_LOGI(kTag, "vad.handoff armed epoch=%lu", static_cast<unsigned long>(epoch));
    }
}

void MemoriaProtocol::TrackUplinkVadFrame(float packet_rms, uint64_t produced_ms) {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    const uint64_t now_ms = DeviceMonotonicMs();
    const float floor = vad_noise_floor_.EffectiveFloor(ConfiguredVadStartRmsFloor(), now_ms);
    if (vad_handoff_.pending()) {
        const uint32_t epoch = afe_vad_epoch.load();
        if (vad_handoff_.TryArm(epoch, now_ms)) {
            ESP_LOGI(kTag, "vad.handoff armed epoch=%lu late=1", static_cast<unsigned long>(epoch));
        }
    }
    const uint32_t handoff_epoch = vad_handoff_.epoch();
    if (handoff_epoch && vad_handoff_.Frame(!vad_active_ && CanRecoverVad(handoff_epoch),
                                             produced_ms, now_ms, packet_rms, floor)) {
        constexpr uint64_t span = 2 * MemoriaAudioFrame::kUplinkFrameSamples;
        vad_onset_sample_ = uplink_sample_start_ >= span ? uplink_sample_start_ - span : 0;
        vad_onset_valid_ = true;
        ESP_LOGI(kTag, "vad.start handoff sample=%llu epoch=%lu", static_cast<unsigned long long>(vad_onset_sample_),
                 static_cast<unsigned long>(handoff_epoch));
        SendVadState(true, packet_rms);
    }
    const bool recovering = vad_gate_.recovering();
    if (!(recovering || vad_gate_.awaiting()) || !CanRecoverVad(vad_candidate_epoch_)) {
        if (vad_candidate_epoch_ && vad_gate_.awaiting()) {
            vad_gate_.Reset();
            vad_onset_valid_ = false;
        }
        vad_candidate_epoch_ = 0;
    }
    const float rms = recovering ? packet_rms : uplink_frame_rms.load(std::memory_order_relaxed);
    const VadEdgeAction edge = vad_gate_.Frame(rms, floor, now_ms, vad_candidate_epoch_ != 0);
    if (edge == VadEdgeAction::kReport) {
        if (recovering) {
            constexpr uint64_t span = 2 * MemoriaAudioFrame::kUplinkFrameSamples;
            vad_onset_sample_ = uplink_sample_start_ >= span ? uplink_sample_start_ - span : 0;
            vad_onset_valid_ = true;
            ESP_LOGI(kTag, "vad.start recovered sample=%llu", static_cast<unsigned long long>(vad_onset_sample_));
        }
        SendVadState(true, vad_gate_.peak());
    } else if (edge == VadEdgeAction::kSuppressStart) {
        LogVadStartSuppressed(rms, vad_gate_.peak(), floor);
        vad_onset_valid_ = false;
        vad_frames_since_log_ = 0;
        vad_quiet_peak_ = 0.0f;
    }
    if (vad_active_) {
        vad_frames_since_log_ = 0;
        vad_quiet_peak_ = 0.0f;
        return;
    }
    if (!vad_gate_.awaiting() && !vad_gate_.recovering() && !HasActivePlaybackGeneration() &&
        !g_listening_closed.load(std::memory_order_relaxed) && vad_handoff_.epoch() == 0) {
        vad_noise_floor_.Observe(packet_rms, now_ms);
    }
    if (rms > vad_quiet_peak_) {
        vad_quiet_peak_ = rms;
    }
    if (++vad_frames_since_log_ < kVadQuietLogFrames) {
        return;
    }
    const float effective_floor =
        vad_noise_floor_.EffectiveFloor(ConfiguredVadStartRmsFloor(), now_ms);
    ESP_LOGI(kTag, "vad.idle rms=%.4f peak=%.4f floor=%.4f ambient=%.4f frames=%u",
             static_cast<double>(rms), static_cast<double>(vad_quiet_peak_),
             static_cast<double>(effective_floor), static_cast<double>(vad_noise_floor_.ambient()),
             static_cast<unsigned int>(vad_frames_since_log_));
    vad_frames_since_log_ = 0;
    vad_quiet_peak_ = 0.0f;
}

void MemoriaProtocol::SendVadState(bool speaking, float near_end_rms, uint32_t afe_epoch) {
    // VAD is emitted from Application's main task while playback receipts and
    // profile acknowledgements can originate on audio/receive tasks. Allocate
    // the sequence and enqueue the control under the same state lock/FIFO so
    // wire order can never become N+1 followed by N.
    uint32_t legacy_attempt = 0;
    uint32_t legacy_stream_epoch = 0;
    uint64_t legacy_sample_position = 0;
    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        if (stream_epoch_ == 0 || (speaking && g_listening_closed.load(std::memory_order_relaxed))) {
            return;
        }
        if (speaking || !afe_epoch || (vad_handoff_.epoch() && afe_epoch == afe_vad_epoch.load())) {
            vad_handoff_.Completed(false);
        }
        const uint64_t now_ms = DeviceMonotonicMs();
        const float floor = vad_noise_floor_.EffectiveFloor(ConfiguredVadStartRmsFloor(), now_ms);
        const VadEdgeAction vad_edge =
            vad_gate_.Edge(vad_active_, speaking, near_end_rms, floor, now_ms);
        if (vad_edge == VadEdgeAction::kIgnore) {
            return;
        }
        // Edge rejects playback-window vad.start unless the signed settings
        // allow the voice source. Suppress self-echo here instead of letting
        // the control violation close the transport mid-greeting.
        if (speaking && HasActivePlaybackGeneration() && !voice_barge_in_allowed_) {
            ESP_LOGW(kTag,
                       "Suppressing playback-window VAD start: voice barge-in not allowed");
            vad_gate_.Reset();
            vad_onset_valid_ = false;
            return;
        }
        if (vad_edge == VadEdgeAction::kAwaitPeak) {
            vad_candidate_epoch_ = CanRecoverVad(afe_epoch) ? afe_epoch : 0;
            vad_onset_sample_ = uplink_sample_start_;
            vad_onset_valid_ = true;
            vad_frames_since_log_ = 0;
            vad_quiet_peak_ = near_end_rms;
            return;
        }
        if (vad_edge == VadEdgeAction::kSuppressStart) {
            LogVadStartSuppressed(near_end_rms, vad_gate_.peak(), floor);
            vad_gate_.Reset();
            vad_onset_valid_ = false;
            vad_frames_since_log_ = 0;
            vad_quiet_peak_ = 0.0f;
            return;
        }
        // A start confirmed by the window keeps the sample where the AFE first
        // reported it, not where the window closed.
        const uint64_t edge_sample =
            (speaking && vad_onset_valid_) ? vad_onset_sample_ : uplink_sample_start_;
        vad_onset_valid_ = false;
        vad_frames_since_log_ = 0;
        vad_quiet_peak_ = 0.0f;
        if (protocol_version_ == kProtocolVersionV2) {
            ++control_sequence_;
            ScopedJson root{cJSON_CreateObject()};
            if (root.value == nullptr) {
                return;
            }
            cJSON_AddStringToObject(root.value, "type", speaking ? "vad.start" : "vad.end");
            cJSON_AddNumberToObject(root.value, "version", 2);
            cJSON_AddNumberToObject(root.value, "stream_epoch", stream_epoch_);
            cJSON_AddNumberToObject(root.value, "control_sequence", control_sequence_);
            cJSON_AddNumberToObject(root.value, "device_monotonic_ms", DeviceMonotonicMs());
            cJSON_AddNumberToObject(root.value, "sample_position",
                                    static_cast<double>(edge_sample));
            if (!speaking) {
                const uint64_t hangover_start =
                    uplink_sample_start_ > kAfeVadHangoverSamples
                        ? uplink_sample_start_ - kAfeVadHangoverSamples
                        : 0;
                const uint64_t voiced_end_sample =
                    std::max(vad_started_sample_, hangover_start);
                cJSON_AddNumberToObject(root.value, "voiced_end_sample",
                                        static_cast<double>(voiced_end_sample));
            }
            // The AFE exposes a binary VAD state, not a model confidence; the
            // probability is that state mapped to the unit interval.
            cJSON_AddNumberToObject(root.value, "probability", speaking ? 1.0 : 0.0);
            cJSON_AddNumberToObject(root.value, "near_end_rms", near_end_rms);
            if (QueueTransportText(RenderJson(root.value))) {
                vad_active_ = speaking;
                vad_started_sample_ = speaking ? edge_sample : 0;
                ESP_LOGI(kTag, "Device VAD %s at sample=%llu rms=%.4f",
                         speaking ? "start" : "end",
                         static_cast<unsigned long long>(edge_sample),
                         static_cast<double>(near_end_rms));
                NotifyVoiceActivity(speaking);
            }
            return;
        }
        legacy_attempt = websocket_attempt_id_.load();
        legacy_stream_epoch = stream_epoch_;
        legacy_sample_position = speaking ? edge_sample : uplink_sample_start_;
    }
    // The legacy gateway also validates an exact key set. Build the event as
    // JSON instead of concatenating a wire string so quotes/escaping cannot
    // turn a valid VAD boundary into a session-breaking protocol error. The
    // state lock is deliberately released before legacy WebSocket I/O.
    ScopedJson legacy_event{cJSON_CreateObject()};
    if (legacy_event.value == nullptr) {
        return;
    }
    cJSON_AddStringToObject(legacy_event.value, "type", speaking ? "vad.start" : "vad.end");
    cJSON_AddNumberToObject(legacy_event.value, "stream_epoch", legacy_stream_epoch);
    cJSON_AddNumberToObject(legacy_event.value, "sample_position",
                            static_cast<double>(legacy_sample_position));
    if (SendText(RenderJson(legacy_event.value))) {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        if (legacy_attempt == websocket_attempt_id_.load() &&
            protocol_version_ == kProtocolVersionV1 && stream_epoch_ == legacy_stream_epoch &&
            !error_occurred_) {
            vad_active_ = speaking;
            vad_started_sample_ = speaking ? legacy_sample_position : 0;
            ESP_LOGI(kTag, "Device VAD %s at sample=%llu rms=%.4f",
                     speaking ? "start" : "end",
                     static_cast<unsigned long long>(legacy_sample_position),
                     static_cast<double>(near_end_rms));
            NotifyVoiceActivity(speaking);
        }
    }
}

void MemoriaProtocol::SendAbortSpeaking(AbortReason reason) {
    // The Memoria board routes button stops through NotifyLocalFlush before a
    // synchronous atomic audio-pipeline flush. This virtual is kept for the upstream call
    // sites that do not run on the Memoria board.
    (void)reason;
    NotifyLocalFlush();
}

void MemoriaProtocol::SendMcpMessage(const std::string& message) {
    (void)message;
    ESP_LOGW(kTag, "MCP is disabled on the hardware media boundary");
}

void MemoriaProtocol::NotifyLocalFlush() {
    LocalHardStop(nullptr);
}

bool MemoriaProtocol::NotifyLocalKeywordStop(const LocalStopKeywordHit& hit) {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    // The hit crossed from the audio task to the main task; the generation
    // may have ended or been cancelled meanwhile. Never flush or report then.
    if (!LocalStopKeywordArmed()) {
        return false;
    }
    LocalHardStop(&hit);
    return true;
}

void MemoriaProtocol::LocalHardStop(const LocalStopKeywordHit* keyword) {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    vad_handoff_.Completed(false);
    if (protocol_version_ == kProtocolVersionV2) {
        // Capture the fence and the delivered watermark before clearing, so
        // the server can close the generation gate and compute actual-heard.
        const GenerationFence fence = fence_;
        // Physical stop reports the output-commit bound, never the newest
        // network-received frame that may still be queued and unheard.
        const uint64_t local_flush_sample_end =
            playback_output_frames_ > 0 ? playback_output_end_ : 0;
        const bool had_active_generation = playback_active_ && fence.valid();
        // The button stop reports the terminal state itself; a pending
        // completion must not re-report ended through a later drain.
        playback_completion_pending_ = false;
        playback_active_ = false;
        playback_audio_ready_ = false;
        playback_paused_ = false;
        downlink_started_ = false;
        // Revoke the receipt identity before Application flushes. An output
        // callback already in flight after this terminal cannot emit a late
        // started/progress receipt for the stopped generation.
        receipt_generation_id_ = 0;
        playback_started_receipted_ = false;
        if (on_local_flush_requested_ != nullptr) {
            // The local hard stop owns immediate speaker silence; report to
            // Edge only after the queue gate is closed and tails are flushed.
            on_local_flush_requested_(0);
        }
        if (had_active_generation) {
            // The button stop is itself the terminal receipt for this
            // generation: mark it receipted so the authoritative
            // generation.cancelled that follows cannot re-report a duplicate
            // playback.ended with the same watermark. HandleGenerationStarted
            // and FinalizePlaybackEnded clear this flag for the next
            // generation, so the marker never leaks forward.
            playback_terminal_receipted_ = true;
            if (keyword != nullptr) {
                // Stop keyword: the same local flush, reported as a hard-stop
                // keyword so the server cancels with keyword_interrupt.
                SendKeywordStop(fence, *keyword);
            } else {
                SendButtonStop(fence, local_flush_sample_end);
            }
        }
    } else {
        if (stream_epoch_ != 0) {
            ScopedJson event{cJSON_CreateObject()};
            if (event.value != nullptr) {
                cJSON_AddStringToObject(event.value, "type", "button.event");
                cJSON_AddNumberToObject(event.value, "stream_epoch", stream_epoch_);
                cJSON_AddStringToObject(event.value, "button", "primary");
                cJSON_AddStringToObject(event.value, "action", "press");
                cJSON_AddNumberToObject(event.value, "generation_id", fence_.generation_id);
                SendText(RenderJson(event.value));
            }
        }
        // The locally stopped generation must not resume; drop and count its
        // late frames until the server starts a new generation.
        if (fence_.generation_id != 0) {
            stopped_generation_id_ = fence_.generation_id;
        }
        if (on_local_flush_requested_ != nullptr) {
            on_local_flush_requested_(0);
        }
    }
    EmitLegacyTts("stop");
}

void MemoriaProtocol::NotifyPlaybackDrained() {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    if (!IsAudioChannelOpened()) {
        return;
    }
    if (protocol_version_ == kProtocolVersionV2) {
        // A plain queue drain is not a terminal event: ended is only emitted
        // after the authoritative generation.completed barrier. A temporary
        // drain between frames must never end a still-streaming generation.
        if (playback_completion_pending_) {
            FinalizePlaybackEnded();
        }
        MaybeApplyPendingProfile();
        return;
    }
    if (fence_.generation_id == 0 ||
        active_generation_received_end_ <= playback_receipted_end_) {
        return;
    }
    ScopedJson receipt{cJSON_CreateObject()};
    if (receipt.value == nullptr) {
        return;
    }
    cJSON_AddStringToObject(receipt.value, "type", "playback.ended");
    cJSON_AddNumberToObject(receipt.value, "stream_epoch", stream_epoch_);
    cJSON_AddNumberToObject(receipt.value, "generation_id", fence_.generation_id);
    cJSON_AddNumberToObject(receipt.value, "played_sample_end",
                            static_cast<double>(active_generation_received_end_));
    cJSON_AddStringToObject(receipt.value, "reason", "drained");
    if (QueueTransportText(RenderJson(receipt.value))) {
        playback_receipted_end_ = active_generation_received_end_;
    }
}

void MemoriaProtocol::NotifyPlaybackOutput(uint32_t generation_id,
                                           uint64_t rendered_sample_end,
                                           uint32_t received_sequence,
                                           bool approximate) {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    if (protocol_version_ != kProtocolVersionV2 || !IsAudioChannelOpened()) {
        return;
    }
    if (generation_id == 0 || generation_id != receipt_generation_id_) {
        // Output that belongs to a flushed/stopped generation or to a local
        // sound (generation 0) must not move the current receipts.
        return;
    }
    playback_output_end_ = rendered_sample_end;
    playback_output_sequence_ = received_sequence;
    playback_output_approximate_ = approximate;
    ++playback_output_frames_;
    if (!playback_started_receipted_) {
        playback_started_receipted_ = true;
        SendPlaybackReceipt("playback.started", fence_, received_sequence,
                            rendered_sample_end, approximate);
    } else if (playback_output_frames_ % kProgressReceiptIntervalFrames == 0) {
        SendPlaybackReceipt("playback.progress", fence_, received_sequence,
                            rendered_sample_end, approximate);
    }
}

void MemoriaProtocol::SetIsPlaybackIdleCallback(std::function<bool()> callback) {
    is_playback_idle_ = std::move(callback);
}

void MemoriaProtocol::FinalizePlaybackEnded() {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    const GenerationFence ended_fence = fence_;
    const bool send_ended = receipt_generation_id_ != 0 && !playback_terminal_receipted_ &&
                            playback_audio_ready_ && active_generation_received_end_ > 0;
    const uint64_t ended_end =
        playback_output_frames_ > 0 ? playback_output_end_ : 0;
    const uint32_t ended_sequence =
        playback_output_frames_ > 0 ? playback_output_sequence_ : 0;
    const bool ended_approximate =
        playback_output_frames_ > 0 && playback_output_approximate_;
    vad_handoff_.Completed(send_ended && ended_end > 0 && ended_end == active_generation_received_end_ &&
                           !ended_approximate && !vad_active_);
    if (send_ended) {
        // The ended watermark is the last full frame confirmed by the codec's
        // output-completion source. With no confirmed output the receipt
        // honestly reports 0: queued audio must never masquerade as played.
        playback_terminal_receipted_ = true;
    }
    playback_completion_pending_ = false;
    receipt_generation_id_ = 0;
    playback_started_receipted_ = false;
    playback_terminal_receipted_ = false;
    accepted_frames_ = 0;
    active_generation_received_end_ = 0;
    active_generation_received_sequence_ = 0;
    playback_output_end_ = 0;
    playback_output_sequence_ = 0;
    playback_output_frames_ = 0;
    playback_output_approximate_ = true;
    playback_active_ = false;
    playback_audio_ready_ = false;
    playback_paused_ = false;
    downlink_started_ = false;
    if (on_local_flush_requested_ != nullptr) {
        on_local_flush_requested_(0);
    }
    if (send_ended) {
        SendPlaybackReceipt("playback.ended", ended_fence, ended_sequence, ended_end,
                            ended_approximate);
    }
    EmitLegacyTts("stop");
}

void MemoriaProtocol::NotifyPlaybackDecodeError() {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    if (protocol_version_ != kProtocolVersionV2 || receipt_generation_id_ == 0 ||
        playback_terminal_receipted_) {
        return;
    }
    ESP_LOGW(kTag, "Playback decode error for generation=%u",
             static_cast<unsigned int>(receipt_generation_id_));
    vad_handoff_.Completed(false);
    const uint32_t error_sequence =
        playback_output_frames_ > 0 ? playback_output_sequence_ : 0;
    const uint64_t error_end =
        playback_output_frames_ > 0 ? playback_output_end_ : 0;
    const GenerationFence error_fence = fence_;
    playback_terminal_receipted_ = true;
    playback_completion_pending_ = false;
    playback_active_ = false;
    playback_audio_ready_ = false;
    playback_paused_ = false;
    receipt_generation_id_ = 0;
    playback_started_receipted_ = false;
    if (on_local_flush_requested_ != nullptr) {
        on_local_flush_requested_(0);
    }
    SendPlaybackReceipt("playback.error", error_fence, error_sequence, error_end,
                        playback_output_frames_ > 0 && playback_output_approximate_);
}

void MemoriaProtocol::SetLocalFlushCallback(std::function<void(uint32_t)> callback) {
    on_local_flush_requested_ = std::move(callback);
}

void MemoriaProtocol::SetDeviceSettingsCallback(
    std::function<void(uint32_t volume_limit, uint32_t screen_brightness)> callback) {
    on_device_settings_received_ = std::move(callback);
}

void MemoriaProtocol::EmitLegacyTts(const char* state, const char* text) {
    if (on_incoming_json_ == nullptr) {
        return;
    }
    ScopedJson root{cJSON_CreateObject()};
    cJSON_AddStringToObject(root.value, "type", "tts");
    cJSON_AddStringToObject(root.value, "state", state);
    if (text != nullptr) {
        cJSON_AddStringToObject(root.value, "text", text);
    }
    on_incoming_json_(root.value);
}

void MemoriaProtocol::EmitLegacyStt(const char* text) {
    if (on_incoming_json_ == nullptr) {
        return;
    }
    ScopedJson root{cJSON_CreateObject()};
    cJSON_AddStringToObject(root.value, "type", "stt");
    cJSON_AddStringToObject(root.value, "text", text);
    on_incoming_json_(root.value);
}

void MemoriaProtocol::EmitLegacyExpression(const char* expression) {
    if (on_incoming_json_ == nullptr) {
        return;
    }
    ScopedJson root{cJSON_CreateObject()};
    cJSON_AddStringToObject(root.value, "type", "llm");
    cJSON_AddStringToObject(root.value, "emotion", expression);
    on_incoming_json_(root.value);
}

bool MemoriaProtocol::RetireTransportAttempt(uint32_t websocket_attempt) {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    if (websocket_attempt != websocket_attempt_id_.load()) {
        return false;
    }
    // Linearization point for every passive WSS failure. From here onward all
    // queued work captured from this transport is stale, even if Application
    // has not consumed its scheduled close task yet.
    // The owner WebSocket can deliberately remain allocated until the main
    // task collects it, and some implementations can still report connected
    // during that grace window. Make the retired attempt observably closed at
    // this same linearization point so audio, heartbeat, and a later user open
    // can never reuse it.
    error_occurred_ = true;
    ++websocket_attempt_id_;
    transport_actions_.clear();
    xEventGroupSetBits(event_group_, kSessionClosedBit);
    if (!transport_close_notified_) {
        transport_close_notified_ = true;
        if (on_disconnected_ != nullptr) {
            on_disconnected_();
        }
        if (on_audio_channel_closed_ != nullptr) {
            on_audio_channel_closed_();
        }
    }
    return true;
}

void MemoriaProtocol::MarkTransportAlive(uint32_t websocket_attempt) {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    if (websocket_attempt != websocket_attempt_id_.load()) {
        return;
    }
    transport_pong_pending_ = false;
    transport_pong_deadline_us_ = 0;
    transport_ping_due_us_ = esp_timer_get_time() + kTransportPingIntervalUs;
}

void MemoriaProtocol::ResetSessionState() {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    NotifyListeningClosed(false);
    if (!terminal_session_close_ && protocol_version_ == kProtocolVersionV2 &&
        !session_id_.empty()) {
        resumable_session_id_ = session_id_;
        if (stream_epoch_ > resumable_stream_epoch_) {
            resumable_stream_epoch_ = stream_epoch_;
        }
    }
    stream_epoch_ = 0;
    session_id_.clear();
    uplink_sequence_ = 0;
    uplink_sample_start_ = 0;
    vad_active_ = false;
    vad_started_sample_ = 0;
    vad_handoff_.Completed(false);
    vad_gate_.Reset();
    vad_onset_valid_ = false;
    vad_frames_since_log_ = 0;
    vad_quiet_peak_ = 0.0f;
    transport_ping_due_us_ = 0;
    transport_pong_deadline_us_ = 0;
    transport_pong_pending_ = false;
    transport_actions_.clear();
    downlink_started_ = false;
    downlink_sequence_ = 0;
    downlink_sample_start_ = 0;
    downlink_sample_rate_ = kDownlinkSampleRate24k;
    downlink_frame_samples_ = MemoriaAudioFrame::kDownlinkFrameSamples24k;
    fence_ = GenerationFence{};
    playback_active_ = false;
    playback_audio_ready_ = false;
    playback_paused_ = false;
    if (on_local_flush_requested_ != nullptr) {
        on_local_flush_requested_(0);
    }
    stopped_generation_id_ = 0;
    receipt_generation_id_ = 0;
    playback_started_receipted_ = false;
    playback_terminal_receipted_ = false;
    playback_completion_pending_ = false;
    accepted_frames_ = 0;
    active_generation_received_end_ = 0;
    active_generation_received_sequence_ = 0;
    playback_output_end_ = 0;
    playback_output_sequence_ = 0;
    playback_output_frames_ = 0;
    playback_output_approximate_ = true;
    playback_receipted_end_ = 0;
    dropped_frames_ = DroppedFrameCounters{};
    protocol_violations_ = 0;
    runtime_profile_version_ = 0;
    settings_version_ = 0;
    audio_mode_.clear();
    voice_barge_in_allowed_ = false;
    keyword_barge_in_allowed_ = false;
    runtime_profile_pending_ = false;
    runtime_profile_pending_version_ = 0;
    runtime_profile_apply_mode_ = ProfileApplyMode::kNextSession;
    // control_sequence_ survives intentionally as a device-lifetime monotonic counter.
    // protocol_version_ resets to the safe legacy default and is re-negotiated from the next ticket.
    // A saved v2 resume is identified by its non-empty Session id + positive epoch.
    protocol_version_ = kProtocolVersionV1;
}

}  // namespace memoria
