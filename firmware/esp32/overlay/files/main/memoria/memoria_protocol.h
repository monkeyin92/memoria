#pragma once

#include <atomic>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>

#include <cJSON.h>

#include "device_identity.h"
#include "memoria_audio_frame.h"
#include "memoria_activation_client.h"
#include "protocol.h"

#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/task.h>

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
    bool CanResumeSession();
    bool HasActivePlaybackGeneration();
    // Called from Application's one-second clock tick. A quiet server is
    // healthy when it answers WebSocket Ping with Pong; only a missed active
    // probe retires the fenced transport and enters normal media recovery.
    void PollTransportLiveness();
    // Receive/audio callbacks may parse and fence state on their native task,
    // but transport writes are serialized by Application's main task. The
    // wake callback only sets an event bit; DrainTransportActions performs the
    // potentially blocking socket operation on the owner task.
    void SetTransportActionWakeCallback(std::function<void()> callback);
    void DrainTransportActions();
    uint32_t TransportAttemptId() const { return websocket_attempt_id_.load(); }
    bool SendAudio(std::unique_ptr<AudioStreamPacket> packet) override;
    void SendWakeWordDetected(const std::string& wake_word) override;
    void SendStartListening(ListeningMode mode) override;
    void SendStopListening() override;
    void SendVadState(bool speaking, float near_end_rms = 0.0f);
    void SendAbortSpeaking(AbortReason reason) override;
    void SendMcpMessage(const std::string& message) override;

    // L0 physical hard stop. This first closes the protocol generation gate;
    // Application then atomically flushes the audio pipeline with generation
    // 0. In a v2
    // session the device reports button.stop with the expected generation
    // fence and the local flush sample end; v1 sessions keep the legacy
    // button.event. The locally stopped generation must never resume playback.
    void NotifyLocalFlush();

    // Called by Application after AudioService reports its real playback
    // pipeline drained. This is the authority for playback.ended receipts.
    void NotifyPlaybackDrained();

    // Called by Application when AudioService advances a playback boundary.
    // The Memoria ES8388 path reports a GDMA TX-EOF-confirmed boundary;
    // other codecs must keep approximate=true.
    void NotifyPlaybackOutput(uint32_t generation_id, uint64_t rendered_sample_end,
                              uint32_t received_sequence, bool approximate);

    // Application-injected queue-idle probe: true when AudioService has no
    // decode/playback work in flight. Lets a generation.completed that
    // arrives when the queue is already empty finalize immediately instead
    // of waiting for a drain edge that never comes.
    void SetIsPlaybackIdleCallback(std::function<bool()> callback);

    // Called by Application when the Opus decoder failed to render a packet
    // that belonged to the current playback generation.
    void NotifyPlaybackDecodeError();

    // P0 control flush hook: Application wires this to AudioService so
    // server-initiated playback.flush / generation.cancelled / pause stop the
    // speaker synchronously on the receive path, without waiting for the audio
    // queue. The callback atomically replaces the audio pipeline's accepted
    // generation (0 closes it) while clearing queued/in-flight tails.
    void SetLocalFlushCallback(std::function<void(uint32_t)> callback);

    // Apply the server-signed hardware settings snapshot carried by
    // session.accepted. Application owns the concrete codec/display objects.
    void SetDeviceSettingsCallback(
        std::function<void(uint32_t volume_limit, uint32_t screen_brightness)> callback);

protected:
    bool SendText(const std::string& text) override;

private:
    static constexpr EventBits_t kSessionReadyBit = BIT0;
    static constexpr EventBits_t kSessionClosedBit = BIT1;
    static constexpr uint32_t kProtocolVersionV1 = 1;
    static constexpr uint32_t kProtocolVersionV2 = 2;
    // One playback.progress receipt per 25 accepted frames (500 ms at 20 ms).
    static constexpr uint32_t kProgressReceiptIntervalFrames = 25;
    static constexpr size_t kMaxTransportActions = 64;

    enum class TransportActionKind {
        kSendText,
        kRetire,
    };

    struct TransportAction {
        TransportActionKind kind = TransportActionKind::kSendText;
        uint32_t websocket_attempt = 0;
        std::string text;
    };

    struct GenerationFence {
        uint32_t turn_id = 0;
        uint32_t generation_id = 0;
        uint32_t tool_epoch = 0;
        uint32_t session_epoch = 0;

        bool valid() const { return generation_id != 0 && session_epoch != 0; }
    };

    enum class ProfileApplyMode {
        kNextSession,
        kNextSafePoint,
        kImmediateFailClosed,
    };

    struct MediaSession {
        std::string session_id;
        uint32_t stream_epoch = 0;
        std::string websocket_url;
        std::string media_token;
        uint32_t protocol_version = kProtocolVersionV1;
    };

    // Last accepted direct Session id. It intentionally survives a transport
    // reset so the next signed media-session request can ask Control to resume
    // the same conversation with a higher stream_epoch. Explicit terminal
    // closes clear it before the next open.
    std::string resumable_session_id_;
    uint32_t resumable_stream_epoch_ = 0;
    bool terminal_session_close_ = false;

    struct DroppedFrameCounters {
        uint32_t generation_zero = 0;  // generation 0 carries no valid playback
        uint32_t stale = 0;            // old/future/not-started generation
        uint32_t locally_stopped = 0;  // generation cancelled by local hard stop
        uint32_t paused = 0;           // frames arriving while generation.pause held
    };

    DeviceIdentity identity_;
    ActivationProfile activation_;
    std::unique_ptr<WebSocket> websocket_;
    // Device-lifetime transport attempt. A delayed callback from an old WSS
    // must not close or mutate a newer same-Session epoch.
    std::atomic<uint32_t> websocket_attempt_id_{0};
    // One close notification per installed transport attempt. Passive failure
    // retires the attempt immediately but intentionally leaves the WebSocket
    // owner alive until a safe task can collect it.
    bool transport_close_notified_ = true;
    // Session-attempt heartbeat state. This deliberately does not use
    // Protocol::last_incoming_time_: a healthy microphone-only conversation
    // may have no server application messages for minutes.
    int64_t transport_ping_due_us_ = 0;
    int64_t transport_pong_deadline_us_ = 0;
    bool transport_pong_pending_ = false;
    std::deque<TransportAction> transport_actions_;
    std::function<void()> on_transport_action_ready_;
    TaskHandle_t transport_owner_task_ = nullptr;
    EventGroupHandle_t event_group_ = nullptr;
    std::function<void(uint32_t)> on_local_flush_requested_;
    std::function<void(uint32_t volume_limit, uint32_t screen_brightness)>
        on_device_settings_received_;
    TaskHandle_t activation_retry_task_ = nullptr;

    // Session/epoch scoped state; reset by ResetSessionState().
    uint32_t stream_epoch_ = 0;
    uint32_t protocol_version_ = kProtocolVersionV1;  // negotiated via the HTTP ticket
    uint32_t uplink_sequence_ = 0;
    uint64_t uplink_sample_start_ = 0;
    bool vad_active_ = false;
    uint64_t vad_started_sample_ = 0;
    bool downlink_started_ = false;
    uint32_t downlink_sequence_ = 0;
    uint64_t downlink_sample_start_ = 0;
    uint32_t downlink_sample_rate_ = 24000;  // negotiated; 24000 is the v1 default
    uint32_t downlink_frame_samples_ = 480;
    GenerationFence fence_;                   // latest generation fence from the server
    bool playback_active_ = false;            // server generation is authoritative
    bool playback_audio_ready_ = false;       // a valid current-generation frame arrived
    bool playback_paused_ = false;            // generation.pause in effect
    uint32_t stopped_generation_id_ = 0;      // v1 local-stop drop rule
    uint32_t receipt_generation_id_ = 0;      // generation the receipts refer to
    bool playback_started_receipted_ = false;
    bool playback_terminal_receipted_ = false;  // ended or error already reported
    bool playback_completion_pending_ = false;  // generation.completed seen, ended not yet sent
    std::function<bool()> is_playback_idle_;    // device-lifetime; set once by Application
    uint32_t accepted_frames_ = 0;
    uint64_t playback_output_end_ = 0;          // last output-commit sample end
    uint32_t playback_output_sequence_ = 0;     // last output-commit received sequence
    uint32_t playback_output_frames_ = 0;       // frames committed to the codec output
    bool playback_output_approximate_ = true;   // precision of the latest output boundary
    uint64_t active_generation_received_end_ = 0;
    uint32_t active_generation_received_sequence_ = 0;
    uint64_t playback_receipted_end_ = 0;       // v1 legacy ended watermark
    DroppedFrameCounters dropped_frames_;
    uint32_t protocol_violations_ = 0;
    uint32_t runtime_profile_version_ = 0;      // acked from session.accepted v2
    uint32_t settings_version_ = 0;
    bool runtime_profile_pending_ = false;
    uint32_t runtime_profile_pending_version_ = 0;
    ProfileApplyMode runtime_profile_apply_mode_ = ProfileApplyMode::kNextSession;

    // WebSocket controls and audio output/drain callbacks run on different
    // FreeRTOS tasks. Keep the complete playback fence/receipt state under one
    // recursive lock; P0 queue callbacks can synchronously re-enter a drain
    // notification on the same task.
    mutable std::recursive_mutex playback_state_mutex_;

    // Device lifetime; never reset across sessions/epochs so the server can
    // order device control events.
    uint32_t control_sequence_ = 0;

    void StartActivationRetry();
    void RunActivationRetry();
    static void ActivationRetryTask(void* context);
    bool CreateMediaSession(MediaSession* session);
    bool HandleServerText(const char* data, size_t size);
    bool HandleDownlink(const uint8_t* data, size_t size);
    // Advances the per-generation downlink transport expected clock for one
    // current-generation frame: consecutive frames advance one step, and an
    // explicitly flagged forward gap is accepted only when the sequence and
    // sample deltas are consistent multiples of frame_samples. Backward,
    // forged (inconsistent) and unflagged gaps return false.
    bool AdvanceTransportClock(const MemoriaAudioFrameMetadata& metadata);
    bool ValidateServerBase(const cJSON* root, const char* type, uint32_t* control_sequence,
                            uint64_t* server_monotonic_ms);
    bool ParseGenerationFence(const cJSON* object, GenerationFence* fence);
    bool HandleSessionAccepted(const cJSON* root);
    bool HandleGenerationStarted(const cJSON* root, const GenerationFence& fence);
    bool HandleGenerationPauseResume(const cJSON* root, const GenerationFence& fence, bool pause);
    bool HandleGenerationTerminal(const cJSON* root, const GenerationFence& fence, bool cancelled);
    bool HandlePlaybackFlushV2(const cJSON* root);
    bool HandleRuntimeProfileInvalidated(const cJSON* root);
    bool HandleSessionError(const cJSON* root);
    void MaybeApplyPendingProfile();
    void SendRuntimeProfileApplied(uint32_t profile_version, uint32_t settings_version);
    bool QueueSessionClose(const char* reason);
    bool QueueTransportText(std::string text);
    void QueueTransportRetire();
    bool QueueTransportAction(TransportActionKind kind, std::string text = {});
    void SendButtonStop(const GenerationFence& fence, uint64_t local_flush_sample_end);
    void SendPlaybackReceipt(const char* type, const GenerationFence& fence,
                             uint32_t received_sequence, uint64_t rendered_sample_end,
                             bool approximate);
    std::string DeviceHello() const;
    std::string DeviceHelloV1() const;
    std::string DeviceHelloV2() const;
    void EmitLegacyTts(const char* state, const char* text = nullptr);
    void EmitLegacyStt(const char* text);
    void EmitLegacyExpression(const char* expression);
    // generation.completed authority: send playback.ended with the last
    // output-commit watermark, clear the generation and emit the legacy tts
    // stop. Only the real playback drain (or a synchronous cancel flush) may
    // call this.
    void FinalizePlaybackEnded();
    void MarkTransportAlive(uint32_t websocket_attempt);
    bool RetireTransportAttempt(uint32_t websocket_attempt);
    void ResetSessionState();
};

}  // namespace memoria
