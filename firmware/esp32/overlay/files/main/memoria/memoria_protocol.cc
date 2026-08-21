#include "memoria_protocol.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <limits>
#include <string_view>
#include <utility>
#include <vector>

#include "board.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "http.h"
#include "memoria_audio_frame.h"
#include "sodium.h"
#include "web_socket.h"

namespace memoria {
namespace {

constexpr const char* kTag = "MemoriaProtocol";
constexpr int kHttpTimeoutMs = 10000;
constexpr int kSessionReadyTimeoutMs = 10000;
constexpr int64_t kTransportPingIntervalUs = 30LL * 1000LL * 1000LL;
constexpr int64_t kTransportPongTimeoutUs = 10LL * 1000LL * 1000LL;
constexpr uint32_t kUplinkSampleRate = 16000;
constexpr uint32_t kDownlinkSampleRate24k = 24000;
constexpr uint32_t kDownlinkSampleRate16k = 16000;
constexpr uint32_t kFrameMs = 20;
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

}  // namespace

MemoriaProtocol::MemoriaProtocol() {
    event_group_ = xEventGroupCreate();
    server_sample_rate_ = kDownlinkSampleRate24k;
    server_frame_duration_ = kFrameMs;
}

MemoriaProtocol::~MemoriaProtocol() {
    // Application owns the AudioService captured by this callback and its
    // lifetime can end before Protocol member destruction.
    on_local_flush_requested_ = nullptr;
    on_audio_channel_closed_ = nullptr;
    on_disconnected_ = nullptr;
    on_transport_action_ready_ = nullptr;
    CloseAudioChannel(false);
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
    if (activation_result != ESP_OK) {
        ESP_LOGE(kTag, "Memoria activation failed, code=%s", esp_err_to_name(activation_result));
        SetError("Memoria 激活失败");
        return false;
    }
    if (on_connected_ != nullptr) {
        on_connected_();
    }
    return true;
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

bool MemoriaProtocol::OpenAudioChannel() {
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

    websocket_ = Board::GetInstance().GetNetwork()->CreateWebSocket(1);
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
    websocket->OnData([this, websocket, websocket_attempt](const char* data, size_t size,
                                                           bool binary) {
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
        MarkTransportAlive(websocket_attempt);
    });
    websocket->OnDisconnected([this, websocket, websocket_attempt]() {
        ESP_LOGW(kTag, "Device WebSocket disconnected attempt=%u tcp_error=%d",
                 static_cast<unsigned int>(websocket_attempt), websocket->GetLastError());
        RetireTransportAttempt(websocket_attempt);
    });
    websocket->OnError([this, websocket_attempt](int error) {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        if (websocket_attempt != websocket_attempt_id_.load()) return;
        ESP_LOGE(kTag, "Device WebSocket error=%d", error);
        error_occurred_ = true;
        RetireTransportAttempt(websocket_attempt);
    });
    if (!websocket->Connect(session.websocket_url.c_str())) {
        ESP_LOGE(kTag, "Device WebSocket connect failed, code=%d", websocket->GetLastError());
        // A handshake error can be reported from the TCP receive task. Keep
        // the object alive until the next user open or delayed recovery tick;
        // destroying its callback storage on that task's return edge is racy.
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
        // WebSocket delivers OnDisconnected from its TCP receive task.  The
        // event above wakes Application's higher-priority main task before
        // WebSocket::OnTcpData has returned; destroying websocket_ here would
        // race that callback and corrupt the heap.  Keep the already-closed
        // object alive.  The next explicit open starts by collecting it after
        // the receive callback/task has unwound.
        ResetSessionState();
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
    bool drain_v2_goodbye = false;
    bool send_v1_goodbye = false;
    bool notify_closed = false;
    uint32_t closing_attempt = 0;
    uint32_t legacy_stream_epoch = 0;
    std::unique_ptr<WebSocket> retiring_websocket;
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
        retiring_websocket = std::move(websocket_);
        ResetSessionState();
    }
    if (retiring_websocket != nullptr) {
        retiring_websocket->Close();
        retiring_websocket.reset();
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
    // A device session may be intentionally uplink-only for longer than the
    // upstream protocol's 120-second application-message timer. Transport
    // callbacks and WebSocket connectivity own liveness here; treating server
    // silence as disconnect both rejects valid microphone frames and logs once
    // per 20 ms audio packet after the deadline.
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    return websocket_ != nullptr && websocket_->IsConnected() && stream_epoch_ != 0 &&
           !error_occurred_;
}

void MemoriaProtocol::PollTransportLiveness() {
    if (transport_owner_task_ == nullptr) {
        transport_owner_task_ = xTaskGetCurrentTaskHandle();
    }
    configASSERT(transport_owner_task_ == xTaskGetCurrentTaskHandle());
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
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    const bool active_v2_session =
        protocol_version_ == kProtocolVersionV2 && !session_id_.empty();
    const bool saved_v2_resume =
        !resumable_session_id_.empty() && resumable_stream_epoch_ != 0;
    return !terminal_session_close_ && (active_v2_session || saved_v2_resume);
}

bool MemoriaProtocol::HasActivePlaybackGeneration() {
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
    return protocol_version_ == kProtocolVersionV2 && stream_epoch_ != 0 &&
           fence_.valid() && playback_active_ && playback_audio_ready_ && !playback_paused_;
}

bool MemoriaProtocol::SendAudio(std::unique_ptr<AudioStreamPacket> packet) {
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
        !GetUint32(object, "tool_epoch", &parsed.tool_epoch)) {
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
    ESP_LOGI(kTag, "applying device settings version=%u profile=%u", settings_version,
             profile_version);
    // This board declares no AEC reference, no simultaneous capture and
    // playback, no local stop keyword and no duck. The physical stop button
    // and local VAD honestly permit interrupt_assist, but never verified full
    // duplex. Anything outside those two modes is a protocol violation.
    if (audio_mode != "half_duplex_safe" && audio_mode != "interrupt_assist") {
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
    (void)root;
    if (!fence.valid()) {
        return false;
    }
    // The device downlink clock restarts at 0 for every generation.started,
    // so a start must strictly advance the current authoritative fence: an
    // equal or backward fence (duplicate start, cancelled-generation
    // reactivation, reordered control) would reset the clock mid-generation
    // and is a wire violation (fail closed).
    if (fence_.valid() &&
        (fence.turn_id < fence_.turn_id ||
         (fence.turn_id == fence_.turn_id && fence.generation_id < fence_.generation_id) ||
         (fence.turn_id == fence_.turn_id && fence.generation_id == fence_.generation_id &&
          fence.tool_epoch <= fence_.tool_epoch))) {
        ++protocol_violations_;
        ESP_LOGE(kTag, "generation.started fence does not strictly advance "
                       "(turn=%u/%u gen=%u/%u tool=%u/%u)",
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
    if (!fence.valid() || fence.generation_id != fence_.generation_id) {
        return false;
    }
    if (pause) {
        // No local duck on this simplex board: pausing stops rendering until
        // resume. P0 closes the atomic queue gate on the receive path.
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
    if (fence.generation_id != fence_.generation_id) {
        ESP_LOGW(kTag, "Ignoring terminal %s for stale generation=%u (current=%u)",
                 cancelled ? "generation.cancelled" : "generation.completed",
                 static_cast<unsigned int>(fence.generation_id),
                 static_cast<unsigned int>(fence_.generation_id));
        return true;
    }
    if (cancelled) {
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
    fence_.generation_id = replacement_generation_id;
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
    cJSON_AddItemToObject(root.value, "expected_fence", expected_fence);
    cJSON_AddNumberToObject(root.value, "local_flush_sample_end",
                            static_cast<double>(local_flush_sample_end));
    QueueTransportText(RenderJson(root.value));
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
        if (type == "session.close") {
            uint32_t control_sequence = 0;
            uint64_t server_monotonic_ms = 0;
            if (!ValidateServerBase(root.value, "session.close", &control_sequence,
                                    &server_monotonic_ms)) {
                return false;
            }
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
    // Honest capability declaration for this simplex board: no AEC reference,
    // no simultaneous capture and playback, no local stop keyword, no local
    // duck. Playback watermarks are confirmed by I2S GDMA TX EOF; this is an
    // exact digital boundary but still not acoustic DAC/speaker proof. The server must not
    // open full duplex on top of these capabilities.
    cJSON_AddBoolToObject(capabilities, "simultaneous_capture_playback", false);
    cJSON_AddStringToObject(capabilities, "aec_mode", "none");
    cJSON_AddStringToObject(capabilities, "aec_reference", "none");
    cJSON_AddBoolToObject(capabilities, "aec_reference_verified", false);
    cJSON_AddBoolToObject(capabilities, "local_vad", true);
    cJSON_AddBoolToObject(capabilities, "local_stop_keyword", false);
    cJSON_AddBoolToObject(capabilities, "physical_stop_button", true);
    cJSON_AddStringToObject(capabilities, "playback_watermark", "exact");
    cJSON_AddBoolToObject(capabilities, "local_duck", false);
    cJSON_AddNumberToObject(capabilities, "barge_in_level", 0);
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
    if (transport_owner_task_ == nullptr) {
        transport_owner_task_ = xTaskGetCurrentTaskHandle();
    }
    configASSERT(transport_owner_task_ == xTaskGetCurrentTaskHandle());
    while (true) {
        TransportAction action;
        WebSocket* action_websocket = nullptr;
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
            if (action.kind == TransportActionKind::kSendText) {
                if (websocket_ == nullptr || stream_epoch_ == 0 || error_occurred_) {
                    continue;
                }
                action_websocket = websocket_.get();
            }
        }

        if (action.kind == TransportActionKind::kRetire) {
            RetireTransportAttempt(action.websocket_attempt);
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

void MemoriaProtocol::SendVadState(bool speaking, float near_end_rms) {
    // VAD is emitted from Application's main task while playback receipts and
    // profile acknowledgements can originate on audio/receive tasks. Allocate
    // the sequence and enqueue the control under the same state lock/FIFO so
    // wire order can never become N+1 followed by N.
    uint32_t legacy_attempt = 0;
    uint32_t legacy_stream_epoch = 0;
    uint64_t legacy_sample_position = 0;
    {
        std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
        if (stream_epoch_ == 0 || speaking == vad_active_) {
            return;
        }
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
                                    static_cast<double>(uplink_sample_start_));
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
                vad_started_sample_ = speaking ? uplink_sample_start_ : 0;
            }
            return;
        }
        legacy_attempt = websocket_attempt_id_.load();
        legacy_stream_epoch = stream_epoch_;
        legacy_sample_position = uplink_sample_start_;
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
    std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_);
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
            SendButtonStop(fence, local_flush_sample_end);
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
    runtime_profile_pending_ = false;
    runtime_profile_pending_version_ = 0;
    runtime_profile_apply_mode_ = ProfileApplyMode::kNextSession;
    // control_sequence_ survives intentionally as a device-lifetime monotonic counter.
    // protocol_version_ resets to the safe legacy default and is re-negotiated from the next ticket.
    // A saved v2 resume is identified by its non-empty Session id + positive epoch.
    protocol_version_ = kProtocolVersionV1;
}

}  // namespace memoria
