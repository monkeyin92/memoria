#include "memoria_protocol.h"

#include <algorithm>
#include <array>
#include <cstring>
#include <string_view>
#include <utility>
#include <vector>

#include "board.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "http.h"
#include "memoria_audio_frame.h"
#include "sodium.h"
#include "web_socket.h"

#include <cJSON.h>

namespace memoria {
namespace {

constexpr const char* kTag = "MemoriaProtocol";
constexpr int kHttpTimeoutMs = 10000;
constexpr int kSessionReadyTimeoutMs = 10000;
constexpr uint32_t kUplinkSampleRate = 16000;
constexpr uint32_t kDownlinkSampleRate = 24000;
constexpr uint32_t kFrameMs = 20;
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
              std::string* response) {
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
    if (status != 200) {
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

bool GetPositiveUint32(const cJSON* object, const char* key, uint32_t* output) {
    const cJSON* item = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsNumber(item) || item->valuedouble < 1 || item->valuedouble > UINT32_MAX ||
        item->valuedouble != static_cast<double>(static_cast<uint32_t>(item->valuedouble))) {
        return false;
    }
    if (output != nullptr) {
        *output = static_cast<uint32_t>(item->valuedouble);
    }
    return true;
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
    server_sample_rate_ = kDownlinkSampleRate;
    server_frame_duration_ = kFrameMs;
}

MemoriaProtocol::~MemoriaProtocol() {
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
    std::string session_response;
    if (!HttpJson(JoinUrl(base, device_path + "/media-sessions"), {}, RenderJson(request.value),
                  &session_response)) {
        return false;
    }
    ScopedJson response{cJSON_ParseWithLength(session_response.data(), session_response.size())};
    uint32_t protocol_version = 0;
    if (response.value == nullptr || !GetString(response.value, "session_id", &session->session_id, 128) ||
        !GetPositiveUint32(response.value, "stream_epoch", &session->stream_epoch) ||
        !GetString(response.value, "websocket_url", &session->websocket_url, 512) ||
        !GetString(response.value, "media_token", &session->media_token, 4096) ||
        !GetPositiveUint32(response.value, "protocol_version", &protocol_version) ||
        protocol_version != 1) {
        ESP_LOGE(kTag, "Media session response is invalid");
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
    stream_epoch_ = session.stream_epoch;
    session_id_ = session.session_id;
    error_occurred_ = false;
    xEventGroupClearBits(event_group_, kSessionReadyBit | kSessionClosedBit);

    websocket_ = Board::GetInstance().GetNetwork()->CreateWebSocket(1);
    if (websocket_ == nullptr) {
        SetError("设备媒体连接不可用");
        return false;
    }
    websocket_->SetReceiveBufferSize(MemoriaAudioFrame::kHeaderSize + MemoriaAudioFrame::kMaxPayloadBytes);
    const std::string authorization = "Bearer " + session.media_token;
    websocket_->SetHeader("Authorization", authorization.c_str());
    websocket_->SetHeader("Protocol-Version", "1");
    websocket_->SetHeader("Device-Id", identity_.device_id().c_str());
    websocket_->SetHeader("Client-Id", identity_.client_id().c_str());
    websocket_->OnData([this](const char* data, size_t size, bool binary) {
        const bool accepted = binary
                                  ? HandleDownlink(reinterpret_cast<const uint8_t*>(data), size)
                                  : HandleServerText(data, size);
        if (!accepted) {
            ESP_LOGE(kTag, "Rejected invalid device media message");
            error_occurred_ = true;
            if (websocket_ != nullptr) {
                websocket_->Close();
            }
        }
        last_incoming_time_ = std::chrono::steady_clock::now();
    });
    websocket_->OnDisconnected([this]() {
        xEventGroupSetBits(event_group_, kSessionClosedBit);
        if (on_disconnected_ != nullptr) {
            on_disconnected_();
        }
        if (on_audio_channel_closed_ != nullptr) {
            on_audio_channel_closed_();
        }
    });
    websocket_->OnError([this](int error) {
        ESP_LOGE(kTag, "Device WebSocket error=%d", error);
        error_occurred_ = true;
        xEventGroupSetBits(event_group_, kSessionClosedBit);
    });
    if (!websocket_->Connect(session.websocket_url.c_str())) {
        ESP_LOGE(kTag, "Device WebSocket connect failed, code=%d", websocket_->GetLastError());
        websocket_.reset();
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
        ESP_LOGE(kTag, "Device media channel closed before session.ready");
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
        ESP_LOGE(kTag, "Timed out waiting for session.ready");
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
    if (websocket_ != nullptr && websocket_->IsConnected() && stream_epoch_ != 0) {
        SendVadState(false);
        if (send_goodbye) {
            SendText("{\"type\":\"session.close\",\"stream_epoch\":" +
                     std::to_string(stream_epoch_) + ",\"reason\":\"device_close\"}");
        }
    }
    if (websocket_ != nullptr) {
        websocket_->Close();
        websocket_.reset();
    }
    ResetSessionState();
}

bool MemoriaProtocol::IsAudioChannelOpened() const {
    return websocket_ != nullptr && websocket_->IsConnected() && stream_epoch_ != 0 &&
           !error_occurred_ && !IsTimeout();
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
    MemoriaAudioFrameMetadata metadata{};
    const uint8_t* payload = nullptr;
    size_t payload_size = 0;
    const MemoriaAudioFrameError result =
        MemoriaAudioFrame::Decode(data, size, &metadata, &payload, &payload_size);
    if (result != MemoriaAudioFrameError::kOk ||
        metadata.direction != MemoriaAudioDirection::kDownlink ||
        metadata.stream_epoch != stream_epoch_ || metadata.generation_id == 0 ||
        metadata.sequence != downlink_sequence_ ||
        metadata.sample_start != downlink_sample_start_) {
        return false;
    }
    if (!downlink_started_) {
        downlink_started_ = true;
        EmitLegacyTts("start");
    }
    active_generation_id_ = metadata.generation_id;
    active_generation_received_end_ = metadata.sample_start + metadata.frame_samples;
    downlink_sequence_ = (downlink_sequence_ + 1) & 0xffffffffU;
    downlink_sample_start_ += metadata.frame_samples;
    if (on_incoming_audio_ != nullptr) {
        on_incoming_audio_(std::make_unique<AudioStreamPacket>(AudioStreamPacket{
            .sample_rate = static_cast<int>(kDownlinkSampleRate),
            .frame_duration = static_cast<int>(kFrameMs),
            .timestamp = static_cast<uint32_t>(metadata.sample_start * 1000 / kDownlinkSampleRate),
            .payload = std::vector<uint8_t>(payload, payload + payload_size)}));
    }
    return true;
}

bool MemoriaProtocol::HandleServerText(const char* data, size_t size) {
    ScopedJson root{cJSON_ParseWithLength(data, size)};
    if (root.value == nullptr) {
        return false;
    }
    std::string type;
    if (!GetString(root.value, "type", &type, 64)) {
        return false;
    }
    const cJSON* epoch = cJSON_GetObjectItemCaseSensitive(root.value, "stream_epoch");
    if (!cJSON_IsNumber(epoch) || epoch->valuedouble != static_cast<double>(stream_epoch_)) {
        return false;
    }
    if (type == "session.ready") {
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
            generation <= active_generation_id_) {
            return false;
        }
        active_generation_id_ = generation;
        active_generation_received_end_ = downlink_sample_start_;
        active_generation_receipted_end_ = downlink_sample_start_;
        downlink_started_ = true;
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
    cJSON_AddNumberToObject(audio, "downlink_sample_rate", kDownlinkSampleRate);
    cJSON_AddNumberToObject(audio, "channels", 1);
    cJSON_AddNumberToObject(audio, "frame_ms", kFrameMs);
    cJSON_AddItemToObject(root.value, "audio", audio);
    return RenderJson(root.value);
}

bool MemoriaProtocol::SendText(const std::string& text) {
    return websocket_ != nullptr && websocket_->IsConnected() && websocket_->Send(text);
}

void MemoriaProtocol::SendWakeWordDetected(const std::string& wake_word) {
    (void)wake_word;
    SendStartListening(kListeningModeAutoStop);
}

void MemoriaProtocol::SendStartListening(ListeningMode mode) {
    (void)mode;
    if (stream_epoch_ == 0) {
        return;
    }
    SendText("{\"type\":\"listen.start\",\"stream_epoch\":" +
             std::to_string(stream_epoch_) + ",\"sample_start\":" +
             std::to_string(uplink_sample_start_) + "}");
}

void MemoriaProtocol::SendStopListening() {
    if (stream_epoch_ == 0) {
        return;
    }
    SendVadState(false);
    SendText("{\"type\":\"listen.stop\",\"stream_epoch\":" +
             std::to_string(stream_epoch_) + ",\"sample_start\":" +
             std::to_string(uplink_sample_start_) + "}");
}

void MemoriaProtocol::SendVadState(bool speaking) {
    if (stream_epoch_ == 0 || speaking == vad_active_) {
        return;
    }
    const std::string event =
        "{\"type\":\"vad." + std::string(speaking ? "start" : "end") +
        "\",\"stream_epoch\":" + std::to_string(stream_epoch_) +
        ",\"sample_position\":" + std::to_string(uplink_sample_start_) + "}";
    if (SendText(event)) {
        vad_active_ = speaking;
        vad_started_sample_ = speaking ? uplink_sample_start_ : 0;
        ESP_LOGI(kTag, "Device VAD %s at sample=%llu", speaking ? "start" : "end",
                 static_cast<unsigned long long>(uplink_sample_start_));
    }
}

void MemoriaProtocol::SendAbortSpeaking(AbortReason reason) {
    (void)reason;
    if (stream_epoch_ == 0) {
        return;
    }
    SendText("{\"type\":\"button.event\",\"stream_epoch\":" +
             std::to_string(stream_epoch_) +
             ",\"button\":\"primary\",\"action\":\"press\",\"generation_id\":" +
             std::to_string(active_generation_id_) + "}");
    EmitLegacyTts("stop");
}

void MemoriaProtocol::SendMcpMessage(const std::string& message) {
    (void)message;
    ESP_LOGW(kTag, "MCP is disabled on the hardware media boundary");
}

void MemoriaProtocol::NotifyPlaybackDrained() {
    if (!IsAudioChannelOpened() || active_generation_id_ == 0 ||
        active_generation_received_end_ <= active_generation_receipted_end_) {
        return;
    }
    const std::string receipt =
        "{\"type\":\"playback.ended\",\"stream_epoch\":" +
        std::to_string(stream_epoch_) + ",\"generation_id\":" +
        std::to_string(active_generation_id_) + ",\"played_sample_end\":" +
        std::to_string(active_generation_received_end_) + ",\"reason\":\"drained\"}";
    if (SendText(receipt)) {
        active_generation_receipted_end_ = active_generation_received_end_;
    }
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

void MemoriaProtocol::ResetSessionState() {
    stream_epoch_ = 0;
    session_id_.clear();
    uplink_sequence_ = 0;
    uplink_sample_start_ = 0;
    vad_active_ = false;
    vad_started_sample_ = 0;
    downlink_started_ = false;
    downlink_sequence_ = 0;
    downlink_sample_start_ = 0;
    active_generation_id_ = 0;
    active_generation_received_end_ = 0;
    active_generation_receipted_end_ = 0;
}

}  // namespace memoria
