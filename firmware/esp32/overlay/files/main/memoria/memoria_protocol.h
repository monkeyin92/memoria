#pragma once

#include <cstdint>
#include <memory>
#include <string>

#include "device_identity.h"
#include "memoria_activation_client.h"
#include "protocol.h"

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>

class WebSocket;

namespace memoria {

class MemoriaProtocol final : public Protocol {
public:
    MemoriaProtocol();
    ~MemoriaProtocol() override;

    bool Start() override;
    bool OpenAudioChannel() override;
    void CloseAudioChannel(bool send_goodbye = true) override;
    bool IsAudioChannelOpened() const override;
    bool SendAudio(std::unique_ptr<AudioStreamPacket> packet) override;
    void SendWakeWordDetected(const std::string& wake_word) override;
    void SendStartListening(ListeningMode mode) override;
    void SendStopListening() override;
    void SendVadState(bool speaking);
    void SendAbortSpeaking(AbortReason reason) override;
    void SendMcpMessage(const std::string& message) override;

    // Called by Application only after AudioService reports its real playback
    // pipeline drained. This is the authority for playback.ended receipts.
    void NotifyPlaybackDrained();

protected:
    bool SendText(const std::string& text) override;

private:
    static constexpr EventBits_t kSessionReadyBit = BIT0;
    static constexpr EventBits_t kSessionClosedBit = BIT1;

    struct MediaSession {
        std::string session_id;
        uint32_t stream_epoch = 0;
        std::string websocket_url;
        std::string media_token;
    };

    DeviceIdentity identity_;
    ActivationProfile activation_;
    std::unique_ptr<WebSocket> websocket_;
    EventGroupHandle_t event_group_ = nullptr;
    uint32_t stream_epoch_ = 0;
    uint32_t uplink_sequence_ = 0;
    uint64_t uplink_sample_start_ = 0;
    bool vad_active_ = false;
    bool downlink_started_ = false;
    uint32_t downlink_sequence_ = 0;
    uint64_t downlink_sample_start_ = 0;
    uint32_t active_generation_id_ = 0;
    uint64_t active_generation_received_end_ = 0;
    uint64_t active_generation_receipted_end_ = 0;

    bool CreateMediaSession(MediaSession* session);
    bool HandleServerText(const char* data, size_t size);
    bool HandleDownlink(const uint8_t* data, size_t size);
    std::string DeviceHello() const;
    void EmitLegacyTts(const char* state, const char* text = nullptr);
    void EmitLegacyStt(const char* text);
    void EmitLegacyExpression(const char* expression);
    void ResetSessionState();
};

}  // namespace memoria
