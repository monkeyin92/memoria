import json
import subprocess
from pathlib import Path

SOURCE = (
    Path(__file__).parents[1]
    / "overlay"
    / "files"
    / "main"
    / "memoria"
    / "memoria_protocol.cc"
).read_text(encoding="utf-8")
APPLICATION_PATCH = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0004-use-memoria-activation-and-media.patch"
).read_text(encoding="utf-8")
AFE_PATCH = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0005-tune-memoria-afe-vad.patch"
).read_text(encoding="utf-8")
HALF_DUPLEX_PATCH = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0006-fence-simplex-listening-playback.patch"
).read_text(encoding="utf-8")
DEPENDENCY_LOCK = (
    Path(__file__).parents[1] / "overlay" / "files" / "dependencies.lock"
).read_text(encoding="utf-8")
PROTOCOL_HEADER = (
    Path(__file__).parents[1] / "overlay" / "files" / "main" / "memoria" / "memoria_protocol.h"
).read_text(encoding="utf-8")
AUDIO_FRAME_SOURCE = (
    Path(__file__).parents[1] / "overlay" / "files" / "main" / "memoria" / "memoria_audio_frame.cc"
).read_text(encoding="utf-8")
AUDIO_FRAME_HEADER = (
    Path(__file__).parents[1] / "overlay" / "files" / "main" / "memoria" / "memoria_audio_frame.h"
).read_text(encoding="utf-8")
PATCH_0007 = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0007-device-media-v2-and-local-hard-stop.patch"
).read_text(encoding="utf-8")
PATCH_0008 = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0008-device-media-v2-playback-completion-barrier.patch"
).read_text(encoding="utf-8")
PATCH_0009 = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0009-preserve-session-on-network-reconnect.patch"
).read_text(encoding="utf-8")
PATCH_0010 = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0010-atomic-playback-generation-gate.patch"
).read_text(encoding="utf-8")
PATCH_0011 = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0011-recover-media-session.patch"
).read_text(encoding="utf-8")
PATCH_0012 = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0012-main-task-transport-actions.patch"
).read_text(encoding="utf-8")
PATCH_0013 = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0013-prioritize-media-close-recovery.patch"
).read_text(encoding="utf-8")
PATCH_0014 = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0014-load-memoria-assets-before-audio.patch"
).read_text(encoding="utf-8")
PATCH_0016 = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0016-order-device-playback-barrier.patch"
).read_text(encoding="utf-8")
PATCH_0017 = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0017-i2s-tx-eof-exact-playback-watermark.patch"
).read_text(encoding="utf-8")
PATCH_0018 = (
    Path(__file__).parents[1]
    / "overlay"
    / "patches"
    / "0018-disable-simplex-playback-wake-word.patch"
).read_text(encoding="utf-8")
CONTRACT = json.loads(
    (Path(__file__).parents[3] / "packages" / "contracts" / "device-media-v2.json").read_text(
        encoding="utf-8"
    )
)
FIRMWARE_README = (Path(__file__).parents[3] / "README.md").read_text(encoding="utf-8")
BUILD_SCRIPT = (Path(__file__).parents[1] / "scripts" / "build.sh").read_text(
    encoding="utf-8"
)
BOARD_CONFIG = json.loads(
    (
        Path(__file__).parents[1]
        / "overlay"
        / "files"
        / "main"
        / "boards"
        / "memoria"
        / "atk-dnesp32s3-v1"
        / "config.json"
    ).read_text(encoding="utf-8")
)
BOARD_SOURCE = (
    Path(__file__).parents[1]
    / "overlay"
    / "files"
    / "main"
    / "boards"
    / "memoria"
    / "atk-dnesp32s3-v1"
    / "memoria_atk_dnesp32s3_v1.cc"
).read_text(encoding="utf-8")
CONTRACTS_README = FIRMWARE_README


def test_product_build_has_a_real_idle_session_entry() -> None:
    assert 'WAKE_WORD_MODEL="${MEMORIA_FIRMWARE_WAKE_WORD_MODEL:-}"' in BUILD_SCRIPT
    sdkconfig = BOARD_CONFIG["builds"][0]["sdkconfig_append"]
    assert "CONFIG_USE_CUSTOM_WAKE_WORD=y" in sdkconfig
    assert 'CONFIG_CUSTOM_WAKE_WORD="mo li"' in sdkconfig
    assert 'CONFIG_CUSTOM_WAKE_WORD_DISPLAY="茉莉"' in sdkconfig
    assert "CONFIG_SEND_WAKE_WORD_DATA=n" in sdkconfig
    assert "CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=n" in sdkconfig
    assert "CONFIG_SR_WN_WN9L_NIHAOXIAOZHI_TTS3=n" in sdkconfig
    assert "CONFIG_SR_MN_CN_MULTINET6_QUANT=y" in sdkconfig
    assert "默认启用本地唤醒词“梅莫里亚”" in FIRMWARE_README
    assert "普通“你好你好”不是唤醒词" in FIRMWARE_README


def test_board_mic_gain_keeps_normal_distance_speech_above_denoiser_floor() -> None:
    assert "constexpr float kMicInputGainDb = 18.0f;" in BOARD_SOURCE
    assert "audio_codec.SetInputGain(kMicInputGainDb);" in BOARD_SOURCE
    assert "SetInputGain(12.0f)" not in BOARD_SOURCE
    assert "SetInputGain(24.0f)" not in BOARD_SOURCE
    assert "Keep the strict local AFE VAD" in BOARD_SOURCE


def test_simplex_playback_cannot_grant_wake_word_local_stop_authority() -> None:
    assert PATCH_0018.count("CONFIG_BOARD_TYPE_MEMORIA_ATK_DNESP32S3_V1") == 2
    assert "audio_service_.EnableWakeWordDetection(false);" in PATCH_0018
    assert "Ignoring wake word detected during simplex playback" in PATCH_0018
    late_event_guard = PATCH_0018[PATCH_0018.index("state == kDeviceStateSpeaking") :]
    late_event_guard = late_event_guard[: late_event_guard.index("#endif")]
    assert "AbortSpeaking" not in late_event_guard
    assert "physical button as the playback stop authority" in PATCH_0018


def test_memoria_activation_applies_assets_before_audio_engine_can_load_models() -> None:
    assert "auto& assets = Assets::GetInstance();" in PATCH_0014
    assert "if (!assets.partition_valid() || !assets.Apply())" in PATCH_0014
    assert "esp_srmodel_init(\"model\")" in PATCH_0014
    assert "Memoria assets partition/model load failed" in PATCH_0014


def test_unbound_activation_retries_after_nearby_binding_and_clears_qr() -> None:
    assert "TaskHandle_t activation_retry_task_ = nullptr;" in PROTOCOL_HEADER
    assert "void StartActivationRetry();" in PROTOCOL_HEADER
    assert "StartActivationRetry();" in SOURCE
    assert "void MemoriaProtocol::RunActivationRetry()" in SOURCE
    retry = SOURCE[SOURCE.index("void MemoriaProtocol::RunActivationRetry") :]
    retry = retry[: retry.index("bool MemoriaProtocol::CreateMediaSession")]
    assert "MemoriaActivationClient activation_client(identity_);" in retry
    assert "activation_client.Activate(&activation_)" in retry
    assert "MemoriaBootstrap::GetInstance().Stop();" in retry
    assert "Activation completed after nearby bootstrap" in retry


def test_media_challenge_post_has_an_explicit_json_body() -> None:
    start = SOURCE.index('device_path + "/media-challenge"')
    end = SOURCE.index("&challenge_response", start)
    request = SOURCE[start:end]

    assert '"{}"' in request
    assert "SetContent(std::move(body))" in SOURCE


def test_afe_vad_is_fenced_and_sent_on_the_device_media_protocol() -> None:
    vad = SOURCE[SOURCE.index("void MemoriaProtocol::SendVadState") :]
    vad = vad[: vad.index("void MemoriaProtocol::SendAbortSpeaking")]
    assert 'speaking ? "vad.start" : "vad.end"' in vad
    assert "std::lock_guard<std::recursive_mutex> state_lock" in vad
    v2_vad = vad[: vad.index("ScopedJson legacy_event")]
    assert "QueueTransportText(RenderJson(root.value))" in v2_vad
    assert "SendText(" not in v2_vad
    assert v2_vad.index("++control_sequence_") < v2_vad.index(
        "QueueTransportText(RenderJson(root.value))"
    )
    assert 'cJSON_AddNumberToObject(legacy_event.value, "sample_position"' in vad
    assert "SendText(RenderJson(legacy_event.value))" in vad
    legacy_vad = vad[vad.index("ScopedJson legacy_event") :]
    assert "state lock is deliberately released before legacy WebSocket I/O" in vad
    assert "std::lock_guard" not in legacy_vad[: legacy_vad.index("if (SendText(")]
    legacy_commit = legacy_vad[legacy_vad.index("if (SendText(") :]
    assert "std::lock_guard<std::recursive_mutex> state_lock" in legacy_commit
    assert "legacy_attempt == websocket_attempt_id_.load()" in legacy_commit
    assert "stream_epoch_ == legacy_stream_epoch" in legacy_commit
    assert '"Device VAD %s at sample=%llu rms=%.4f"' in vad
    assert "speaking == vad_active_" in SOURCE
    assert "vad_active_ = speaking" in SOURCE
    assert "Schedule([this, speaking, listening]()" in APPLICATION_PATCH
    assert "const bool listening = GetDeviceState() == kDeviceStateListening" in APPLICATION_PATCH
    assert "memoria_protocol->SendVadState(speaking)" in APPLICATION_PATCH


def test_memoria_afe_uses_bounded_noise_tolerant_endpointing() -> None:
    assert "CONFIG_BOARD_TYPE_MEMORIA_ATK_DNESP32S3_V1" in AFE_PATCH
    assert "larger VAD modes as having a higher speech-trigger probability" in AFE_PATCH
    assert "afe_config->vad_mode = VAD_MODE_0" in AFE_PATCH
    assert "afe_config->vad_mode = VAD_MODE_2" not in AFE_PATCH
    assert "afe_config->vad_min_noise_ms = 900" in AFE_PATCH
    assert "900 ms quiet tail for natural pauses" in AFE_PATCH
    assert "+    afe_config->ns_init" not in AFE_PATCH


def test_simplex_playback_and_state_changes_cannot_leave_a_vad_epoch_open() -> None:
    assert "(!speaking || GetDeviceState() == kDeviceStateListening)" in HALF_DUPLEX_PATCH

    listener = HALF_DUPLEX_PATCH.index("old_state == kDeviceStateListening")
    close = HALF_DUPLEX_PATCH.index("memoria_protocol->SendVadState(false)", listener)
    assert listener < close

    start = HALF_DUPLEX_PATCH.index("void Application::StartListeningAudio")
    send_start = HALF_DUPLEX_PATCH.index("protocol_->SendStartListening", start)
    play_cue = HALF_DUPLEX_PATCH.index(
        "audio_service_.PlaySound(Lang::Sounds::OGG_POPUP)", start
    )
    assert play_cue < send_start
    assert "pending_listening_start_ = true" in HALF_DUPLEX_PATCH[start:send_start]
    assert "playback-drained event resumes this same seam" in HALF_DUPLEX_PATCH


def test_esp_component_versions_are_pinned_for_clean_rebuilds() -> None:
    assert "target: esp32s3" in DEPENDENCY_LOCK
    assert "version: 3.0.0" in DEPENDENCY_LOCK
    assert "version: 1.1.1~2" in DEPENDENCY_LOCK
    assert "version: 1.1.0~2" in DEPENDENCY_LOCK
    assert "version: 1.2.0~3" in DEPENDENCY_LOCK


def test_every_overlay_patch_has_a_well_formed_unified_diff() -> None:
    patch_dir = Path(__file__).parents[1] / "overlay" / "patches"
    for patch in sorted(patch_dir.glob("*.patch")):
        completed = subprocess.run(
            ["git", "apply", "--numstat", str(patch)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, f"{patch.name}: {completed.stderr}"


def test_active_vad_is_closed_at_all_media_lifecycle_boundaries() -> None:
    close_start = SOURCE.index("void MemoriaProtocol::CloseAudioChannel")
    close_end = SOURCE.index("bool MemoriaProtocol::IsAudioChannelOpened", close_start)
    close_body = SOURCE[close_start:close_end]
    # A transport-only loss must never synchronously send into the dead path;
    # terminal v2 close drains vad.end then session.close on the owner FIFO
    # before reset can clear either action.
    assert close_body.index("if (send_goodbye &&") < close_body.index("SendVadState(false)")
    assert close_body.index("SendVadState(false)") < close_body.index(
        'QueueSessionClose("device_close")'
    )
    assert close_body.index('QueueSessionClose("device_close")') < close_body.index(
        "QueueTransportRetire()"
    )
    assert close_body.index("DrainTransportActions()") < close_body.index("ResetSessionState()")
    assert "SendSessionClose(" not in close_body

    stop_start = SOURCE.index("void MemoriaProtocol::SendStopListening")
    stop_end = SOURCE.index("void MemoriaProtocol::SendVadState", stop_start)
    stop_body = SOURCE[stop_start:stop_end]
    assert stop_body.index("SendVadState(false)") < stop_body.index("SendText(")

    assert "kMaxVadSpeechSamples" in SOURCE
    assert "uplink_sample_start_ - vad_started_sample_ >= kMaxVadSpeechSamples" in SOURCE
    assert "vad_started_sample_ = speaking ? uplink_sample_start_ : 0" in SOURCE


def test_send_audio_rejects_null_or_empty_packets_before_encoding() -> None:
    start = SOURCE.index("bool MemoriaProtocol::SendAudio")
    end = SOURCE.index("void MemoriaProtocol::SendVadState", start)
    send_audio = SOURCE[start:end]

    assert "packet == nullptr" in send_audio
    assert "packet->payload.empty()" in send_audio
    assert send_audio.index("packet == nullptr") < send_audio.index(
        "MemoriaAudioFrame::Encode"
    )
    assert send_audio.index("packet->payload.empty()") < send_audio.index(
        "MemoriaAudioFrame::Encode"
    )


def test_json_integer_parsing_is_finite_and_fail_closed_at_uint64_bound() -> None:
    assert "#include <cmath>" in SOURCE
    assert "bool IsFiniteInteger(double value)" in SOURCE
    assert "std::isfinite(value)" in SOURCE
    assert "std::floor(value) == value" in SOURCE

    positive = SOURCE[SOURCE.index("bool GetPositiveUint32") : SOURCE.index("bool GetUint32")]
    uint32 = SOURCE[SOURCE.index("bool GetUint32") : SOURCE.index("bool GetUint64")]
    uint64 = SOURCE[SOURCE.index("bool GetUint64") : SOURCE.index("bool GetBool")]

    for parser in (positive, uint32, uint64):
        assert "!cJSON_IsNumber(item)" in parser
        assert "!IsFiniteInteger(value)" in parser

    assert "value >= std::ldexp(1.0, 64)" in uint64
    assert "static_cast<uint64_t>(item->valuedouble)" not in uint64
    assert uint64.index("!IsFiniteInteger(value)") < uint64.index(
        "static_cast<uint64_t>(value)"
    )


def test_hello_v2_declares_only_honest_simplex_capabilities() -> None:
    start = SOURCE.index("std::string MemoriaProtocol::DeviceHelloV2()")
    end = SOURCE.index("bool MemoriaProtocol::SendText", start)
    hello_v2 = SOURCE[start:end]

    assert '"version", 2' in hello_v2
    assert '"simultaneous_capture_playback"' in hello_v2
    assert 'cJSON_AddBoolToObject(capabilities, "simultaneous_capture_playback", false)' in hello_v2
    assert 'cJSON_AddStringToObject(capabilities, "aec_mode", "none")' in hello_v2
    assert 'cJSON_AddStringToObject(capabilities, "aec_reference", "none")' in hello_v2
    assert 'cJSON_AddBoolToObject(capabilities, "aec_reference_verified", false)' in hello_v2
    assert 'cJSON_AddBoolToObject(capabilities, "local_stop_keyword", false)' in hello_v2
    assert 'cJSON_AddBoolToObject(capabilities, "local_duck", false)' in hello_v2
    assert 'cJSON_AddNumberToObject(capabilities, "barge_in_level", 0)' in hello_v2
    assert 'cJSON_AddStringToObject(capabilities, "playback_watermark", "exact")' in hello_v2
    assert '"local_vad"' in hello_v2
    assert '"physical_stop_button"' in hello_v2

    # No AEC/offline KWS/natural barge-in claims anywhere in the v2 hello.
    for forbidden in (
        '"fd_low_cost"',
        '"fd_high_quality"',
        '"software_post_gain_pre_i2s"',
        '"hardware_loopback"',
        '"keyword.detected"',
        '"full_duplex_verified"',
    ):
        assert forbidden not in hello_v2, f"v2 hello must not claim {forbidden}"

    required_caps = set(CONTRACT["$defs"]["device_capabilities_v2"]["required"])
    for key in required_caps:
        assert f'"{key}"' in hello_v2, f"v2 hello is missing capability key {key}"
    required_audio = set(CONTRACT["$defs"]["device_audio_v2"]["required"])
    for key in required_audio:
        assert f'"{key}"' in hello_v2, f"v2 hello is missing audio key {key}"


def test_hello_v2_declares_only_playable_downlink_rates() -> None:
    start = SOURCE.index("std::string MemoriaProtocol::DeviceHelloV2()")
    end = SOURCE.index("bool MemoriaProtocol::SendText", start)
    hello_v2 = SOURCE[start:end]

    assert '"downlink_sample_rates"' in hello_v2
    assert "kDownlinkSampleRate16k" in hello_v2
    assert "kDownlinkSampleRate24k" in hello_v2
    # Both rates are actually playable: 24 kHz native, 16 kHz via the playback
    # pipeline resampler. The v1 hello keeps the legacy single 24 kHz field.
    start = SOURCE.index("std::string MemoriaProtocol::DeviceHelloV1()")
    hello_v1 = SOURCE[start : SOURCE.index("std::string MemoriaProtocol::DeviceHelloV2()", start)]
    assert '"downlink_sample_rate"' in hello_v1
    assert "kDownlinkSampleRate24k" in hello_v1
    assert '"downlink_sample_rates"' not in hello_v1


def test_session_accepted_v2_parses_the_complete_generation_fence() -> None:
    assert '"session.accepted"' in SOURCE
    accepted = SOURCE[SOURCE.index("HandleSessionAccepted") :]
    for field in (
        "interaction_authority",
        "python_authoritative",
        "audio_mode",
        "downlink_sample_rate",
        "runtime_profile_version",
        "device_settings",
        "volume_limit",
        "screen_brightness",
        "current_fence",
        "current_generation_active",
        "turn_id",
        "generation_id",
        "tool_epoch",
    ):
        assert f'"{field}"' in accepted
    # Physical stop + local VAD permit interrupt_assist, while unverified full
    # duplex still fails closed.
    assert '"half_duplex_safe"' in accepted
    assert '"interrupt_assist"' in accepted
    assert 'audio_mode != "half_duplex_safe" &&' in accepted
    assert 'audio_mode != "interrupt_assist"' in accepted
    assert "contradicts device capabilities" in accepted
    assert "kDownlinkFrameSamples16k" in accepted
    assert "kDownlinkFrameSamples24k" in accepted
    assert "xEventGroupSetBits(event_group_, kSessionReadyBit)" in accepted
    assert "on_device_settings_received_" in accepted
    assert "current_generation_active && !has_fence" in accepted
    assert "playback_active_ = has_fence && current_generation_active" in accepted


def test_v2_playback_speaking_requires_a_real_downlink_frame() -> None:
    accepted = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleSessionAccepted") :]
    accepted = accepted[: accepted.index("bool MemoriaProtocol::HandleGenerationStarted")]
    assert "playback_active_ = has_fence && current_generation_active" in accepted
    assert "playback_audio_ready_ = false" in accepted
    assert 'EmitLegacyTts("start")' not in accepted

    started = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleGenerationStarted") :]
    started = started[: started.index("bool MemoriaProtocol::HandleGenerationPauseResume")]
    assert "playback_active_ = true" in started
    assert "playback_audio_ready_ = false" in started
    assert 'EmitLegacyTts("start")' not in started

    downlink = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleDownlink") :]
    downlink = downlink[: downlink.index("bool MemoriaProtocol::AdvanceTransportClock")]
    assert "const bool first_audio_frame = !playback_audio_ready_" in downlink
    assert "playback_audio_ready_ = true" in downlink
    assert 'EmitLegacyTts("start")' in downlink
    assert downlink.index("playback_audio_ready_ = true") < downlink.index(
        'EmitLegacyTts("start")'
    )

    active = SOURCE[SOURCE.index("bool MemoriaProtocol::HasActivePlaybackGeneration") :]
    active = active[: active.index("bool MemoriaProtocol::SendAudio")]
    assert "playback_active_ && playback_audio_ready_ && !playback_paused_" in active


def test_local_flush_reports_button_stop_for_any_active_generation() -> None:
    flush = SOURCE[SOURCE.index("void MemoriaProtocol::NotifyLocalFlush") :]
    flush = flush[: flush.index("void MemoriaProtocol::NotifyPlaybackDrained")]
    assert "const bool had_active_generation = playback_active_ && fence.valid()" in flush
    assert "had_active_audio" not in flush
    assert flush.index("had_active_generation") < flush.index(
        "SendButtonStop(fence, local_flush_sample_end)"
    )
    assert "if (had_active_generation)" in flush
    assert "playback_audio_ready_ = false" in flush


def test_session_error_revokes_audio_ready_and_legacy_tts_start_state() -> None:
    error = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleSessionError") :]
    error = error[: error.index("void MemoriaProtocol::MaybeApplyPendingProfile")]
    assert "playback_active_ = false" in error
    assert "playback_audio_ready_ = false" in error
    assert "downlink_started_ = false" in error


def test_recovered_audio_callback_uses_protocol_generation_not_ui_speaking_state() -> None:
    callback = PATCH_0011[PATCH_0011.index("protocol_->OnIncomingAudio") :]
    callback = callback[: callback.index("protocol_->OnAudioChannelClosed")]
    added_callback = "\n".join(
        line[1:]
        for line in callback.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    assert "audio_transport_attempt" in added_callback
    assert "TransportAttemptId() == audio_transport_attempt" in added_callback
    assert "HasActivePlaybackGeneration()" in added_callback
    assert "GetDeviceState() == kDeviceStateSpeaking" not in added_callback


def test_websocket_client_id_header_is_bound_to_negotiated_ingress() -> None:
    open_channel = SOURCE[SOURCE.index("bool MemoriaProtocol::OpenAudioChannel") :]
    open_channel = open_channel[: open_channel.index("void MemoriaProtocol::CloseAudioChannel")]
    assert 'protocol_version_ == kProtocolVersionV1' in open_channel
    assert 'SetHeader("Client-Id", identity_.client_id().c_str())' in open_channel
    assert 'SetHeader("X-Client-ID", identity_.client_id().c_str())' in open_channel
    assert open_channel.count('SetHeader("Client-Id"') == 1
    assert open_channel.count('SetHeader("X-Client-ID"') == 1
    create = SOURCE[SOURCE.index("bool MemoriaProtocol::CreateMediaSession") :]
    create = create[: create.index("bool MemoriaProtocol::OpenAudioChannel")]
    assert "protocol_version != kProtocolVersionV1" in create
    assert "protocol_version != kProtocolVersionV2" in create
    legacy = open_channel.index('SetHeader("Client-Id"')
    direct = open_channel.index('SetHeader("X-Client-ID"')
    branch = open_channel.index('protocol_version_ == kProtocolVersionV1')
    assert branch < legacy < direct
    assert "legacy Python gateway" in open_channel
    assert "v2-only Go Edge" in open_channel


def test_memoria_transport_liveness_does_not_expire_on_server_silence() -> None:
    opened = SOURCE[SOURCE.index("bool MemoriaProtocol::IsAudioChannelOpened") :]
    opened = opened[: opened.index("void MemoriaProtocol::PollTransportLiveness")]
    assert "websocket_->IsConnected()" in opened
    assert "!error_occurred_" in opened
    assert "IsTimeout()" not in opened
    assert "intentionally uplink-only" in opened
    assert "per 20 ms audio packet" in opened

    poll = SOURCE[SOURCE.index("void MemoriaProtocol::PollTransportLiveness") :]
    poll = poll[: poll.index("bool MemoriaProtocol::CanResumeSession")]
    assert "transport_pong_pending_" in poll
    assert "transport_pong_deadline_us_" in poll
    assert "kTransportPongTimeoutUs" in poll
    assert "RetireTransportAttempt(websocket_attempt_id_.load())" in poll
    assert "timed_out_websocket" not in poll
    assert "->Close()" not in poll
    assert poll.count("heartbeat timed out") == 1
    assert "IsTimeout()" not in poll
    assert "configASSERT(transport_owner_task_ == xTaskGetCurrentTaskHandle())" in poll
    legacy_compat = "if (protocol_version_ == kProtocolVersionV1)"
    assert legacy_compat in poll
    assert poll.index(legacy_compat) < poll.index("transport_pong_pending_")
    assert "peer-initiated WebSocket" in poll
    assert "active Ping/Pong failure detector" in poll

    open_channel = SOURCE[SOURCE.index("bool MemoriaProtocol::OpenAudioChannel") :]
    open_channel = open_channel[: open_channel.index("void MemoriaProtocol::CloseAudioChannel")]
    assert "OnPong([this, websocket_attempt]" in open_channel
    assert "MarkTransportAlive(websocket_attempt)" in open_channel

    mark_alive = SOURCE[SOURCE.index("void MemoriaProtocol::MarkTransportAlive") :]
    mark_alive = mark_alive[: mark_alive.index("void MemoriaProtocol::ResetSessionState")]
    assert mark_alive.index("websocket_attempt != websocket_attempt_id_.load()") < mark_alive.index(
        "transport_pong_pending_ = false"
    )
    assert "transport_ping_due_us_ = esp_timer_get_time() + kTransportPingIntervalUs" in mark_alive

    reset = SOURCE[SOURCE.index("void MemoriaProtocol::ResetSessionState") :]
    assert "transport_ping_due_us_ = 0" in reset
    assert "transport_pong_deadline_us_ = 0" in reset
    assert "transport_pong_pending_ = false" in reset

    clock_tick = PATCH_0011[PATCH_0011.index("if (bits & MAIN_EVENT_CLOCK_TICK)") :]
    clock_tick = clock_tick[: clock_tick.index("display->UpdateStatusBar()")]
    assert "PollTransportLiveness()" in clock_tick
    assert clock_tick.index("PollTransportLiveness()") < clock_tick.index(
        "ContinueMediaRecovery()"
    )


def test_receive_and_audio_callbacks_defer_transport_io_to_the_main_task() -> None:
    assert "kMaxTransportActions = 64" in PROTOCOL_HEADER
    assert "std::deque<TransportAction> transport_actions_" in PROTOCOL_HEADER
    assert "mutable std::recursive_mutex playback_state_mutex_" in PROTOCOL_HEADER

    opened = SOURCE[SOURCE.index("bool MemoriaProtocol::IsAudioChannelOpened") :]
    opened = opened[: opened.index("void MemoriaProtocol::PollTransportLiveness")]
    assert "std::lock_guard<std::recursive_mutex> state_lock" in opened

    queue = SOURCE[SOURCE.index("bool MemoriaProtocol::QueueTransportAction") :]
    queue = queue[: queue.index("void MemoriaProtocol::SendWakeWordDetected")]
    assert "transport_actions_.size() >= kMaxTransportActions" in queue
    assert "websocket_ == nullptr || stream_epoch_ == 0 || error_occurred_" in queue
    assert "kind == TransportActionKind::kSendText && error_occurred_" not in queue
    assert "RetireTransportAttempt(websocket_attempt)" in queue
    assert "wake_main_task()" in queue
    assert "void MemoriaProtocol::DrainTransportActions" in queue
    assert "configASSERT(transport_owner_task_ == xTaskGetCurrentTaskHandle())" in queue
    assert "action_websocket->Send(action.text)" in queue
    assert "state lock during Send" in queue
    assert "task to parse the send acknowledgement" in queue

    receipt = SOURCE[SOURCE.index("void MemoriaProtocol::SendPlaybackReceipt") :]
    receipt = receipt[: receipt.index("bool MemoriaProtocol::QueueSessionClose")]
    assert "QueueTransportText(RenderJson(root.value))" in receipt
    assert "SendText(" not in receipt

    profile_ack = SOURCE[SOURCE.index("void MemoriaProtocol::SendRuntimeProfileApplied") :]
    profile_ack = profile_ack[: profile_ack.index("bool MemoriaProtocol::HandleServerText")]
    assert "QueueTransportText(RenderJson(root.value))" in profile_ack
    assert "SendText(" not in profile_ack

    vad = SOURCE[SOURCE.index("void MemoriaProtocol::SendVadState") :]
    vad = vad[: vad.index("void MemoriaProtocol::SendAbortSpeaking")]
    v2_vad = vad[: vad.index("ScopedJson legacy_event")]
    assert "QueueTransportText(RenderJson(root.value))" in v2_vad
    assert "SendText(" not in v2_vad
    legacy_vad = vad[vad.index("ScopedJson legacy_event") :]
    assert "std::lock_guard" not in legacy_vad[: legacy_vad.index("if (SendText(")]
    assert "legacy_attempt == websocket_attempt_id_.load()" in legacy_vad

    profile_boundary = SOURCE[SOURCE.index("void MemoriaProtocol::MaybeApplyPendingProfile") :]
    profile_boundary = profile_boundary[: profile_boundary.index("void MemoriaProtocol::SendButtonStop")]
    assert "std::lock_guard<std::recursive_mutex> state_lock" in profile_boundary
    assert "const uint32_t closing_attempt = websocket_attempt_id_.load()" in profile_boundary
    assert "if (QueueSessionClose" in profile_boundary
    assert "RetireTransportAttempt(closing_attempt)" in profile_boundary
    assert profile_boundary.index("if (QueueSessionClose") < profile_boundary.index(
        "QueueTransportRetire()"
    )

    session_error = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleSessionError") :]
    session_error = session_error[: session_error.index("void MemoriaProtocol::MaybeApplyPendingProfile")]
    assert "RetireTransportAttempt(websocket_attempt_id_.load())" in session_error
    assert "->Close()" not in session_error

    profile_apply = SOURCE[SOURCE.index("void MemoriaProtocol::MaybeApplyPendingProfile") :]
    profile_apply = profile_apply[: profile_apply.index("void MemoriaProtocol::SendButtonStop")]
    assert profile_apply.index('QueueSessionClose("runtime_profile_invalidated")') < profile_apply.index(
        "QueueTransportRetire()"
    )
    assert "->Close()" not in profile_apply

    assert "MAIN_EVENT_MEMORIA_TRANSPORT" in PATCH_0012
    assert "SetTransportActionWakeCallback" in PATCH_0012
    assert "DrainTransportActions()" in PATCH_0012
    assert PATCH_0012.index("DrainTransportActions()") < PATCH_0012.index(
        "SetTransportActionWakeCallback"
    )


def test_session_ready_cannot_mutate_the_negotiated_wire_protocol() -> None:
    handler = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleServerText") :]
    handler = handler[: handler.index("std::string MemoriaProtocol::DeviceHelloV1")]
    v2 = handler[handler.index("if (protocol_version_ == kProtocolVersionV2)") :]
    v2 = v2[: v2.index('if (type == "generation.started")')]
    assert 'if (type == "session.ready")' in v2
    assert "Legacy session.ready received on negotiated v2 transport" in v2
    assert "++protocol_violations_" in v2
    assert "return false" in v2

    legacy = handler[handler.rindex('if (type == "session.ready")') :]
    legacy = legacy[: legacy.index('if (type == "playback.flush")')]
    assert "protocol_version_ != kProtocolVersionV1" in legacy
    assert '"session_id"' in legacy
    assert "protocol_version_ = kProtocolVersionV1" not in legacy
    assert "Downgrading v2 session" not in handler


def test_device_playback_barrier_cannot_overtake_audio_admission() -> None:
    callback = PATCH_0016[PATCH_0016.index("protocol_->OnIncomingAudio") :]
    callback = callback[: callback.index("#else")]
    additions = "\n".join(line[1:] for line in callback.splitlines() if line.startswith("+"))
    assert "PushServerPacketToDecodeQueue(std::move(packet))" in additions
    assert "Schedule(" not in additions
    assert "GetDeviceState()" not in additions
    assert "NotifyPlaybackDecodeError()" in additions

    vad = SOURCE[SOURCE.index("void MemoriaProtocol::SendVadState") :]
    vad = vad[: vad.index("void MemoriaProtocol::SendAbortSpeaking")]
    assert "kAfeVadHangoverSamples" in SOURCE
    assert "static_cast<uint64_t>(kUplinkSampleRate) * 900 / 1000" in SOURCE
    assert "std::max(vad_started_sample_, hangover_start)" in vad
    assert "static_cast<double>(voiced_end_sample)" in vad
    assert "afe_config->vad_min_noise_ms = 900" in AFE_PATCH


def test_generation_zero_has_no_valid_playback() -> None:
    downlink = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleDownlink") :]
    zero = downlink[downlink.index("metadata.generation_id == 0") :]
    assert "dropped_frames_.generation_zero" in zero
    assert "return true" in zero  # drop and count, do not break the session
    assert "no valid playback" in zero


def test_stale_generation_frames_are_dropped_and_counted_not_fatal() -> None:
    downlink = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleDownlink") :]
    stale = downlink[downlink.index("dropped_frames_.stale") :]
    assert "return true" in stale
    assert "Dropping stale downlink generation" in stale
    paused = downlink[downlink.index("dropped_frames_.paused") :]
    assert "return true" in paused
    # Real protocol violations still close the session.
    assert "protocol_violations_" in downlink
    assert "return false" in downlink
    assert "metadata.stream_epoch != stream_epoch_" in downlink
    assert "metadata.frame_samples != downlink_frame_samples_" in downlink
    # Continuity is a single gate shared by the render path and the paused
    # path; stale frames never reach it and never move the current clock.
    assert "AdvanceTransportClock(metadata)" in downlink


def test_stale_and_paused_handling_precedes_current_generation_continuity() -> None:
    downlink = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleDownlink") :]
    downlink = downlink[: downlink.index("bool MemoriaProtocol::AdvanceTransportClock")]
    # The render-path continuity call is the last one in HandleDownlink; the
    # earlier one belongs to the paused branch.
    continuity = downlink.rindex("AdvanceTransportClock(metadata)")
    generation_zero = downlink.index("dropped_frames_.generation_zero")
    stale = downlink.index("dropped_frames_.stale")
    paused = downlink.index("dropped_frames_.paused")
    locally_stopped = downlink.index("dropped_frames_.locally_stopped")
    assert generation_zero < stale < paused < locally_stopped < continuity
    # v1 locally stopped frames are also classified before continuity.
    assert locally_stopped < continuity
    # The stale branch is a pure drop: it must not advance the transport
    # clock or judge the frame against the current generation's position.
    stale_branch = downlink[stale : downlink.index("if (playback_paused_) {")]
    assert "AdvanceTransportClock" not in stale_branch
    assert "dropped_frames_.stale" in stale_branch


def test_paused_current_generation_frames_advance_clock_but_not_playback() -> None:
    downlink = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleDownlink") :]
    paused = downlink[downlink.index("if (playback_paused_) {") :]
    paused = paused[: paused.index("dropped_frames_.locally_stopped")]
    assert "AdvanceTransportClock(metadata)" in paused
    assert "return true" in paused
    assert "resume stays continuous" in paused
    # Paused frames must never move receipt state or reach the playback
    # pipeline.
    assert "accepted_frames_" not in paused
    assert "SendPlaybackReceipt" not in paused
    assert "on_incoming_audio_" not in paused


def test_downlink_gap_acceptance_requires_flag_and_consistent_deltas() -> None:
    helper = SOURCE[SOURCE.index("bool MemoriaProtocol::AdvanceTransportClock") :]
    helper = helper[: helper.index("bool MemoriaProtocol::ValidateServerBase")]
    assert "kDiscontinuityFlag" in helper
    assert "metadata.sequence <= downlink_sequence_" in helper
    assert "metadata.sample_start < downlink_sample_start_" in helper
    assert "sample_delta != sequence_delta * metadata.frame_samples" in helper
    assert "sequence_delta == 0" in helper
    assert "metadata.sequence + 1" in helper
    assert "metadata.sample_start + metadata.frame_samples" in helper
    assert "next expected frame" in helper
    # Backward, forged (inconsistent) and unflagged gaps fail closed.
    assert "return false" in helper
    assert "Consecutive frame" in helper
    assert "AdvanceTransportClock" in PROTOCOL_HEADER


def test_downlink_clock_resets_to_zero_on_generation_start_and_flush() -> None:
    started = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleGenerationStarted") :]
    started = started[: started.index("bool MemoriaProtocol::HandleGenerationPauseResume")]
    assert "downlink_sequence_ = 0" in started
    assert "downlink_sample_start_ = 0" in started
    assert "resets to 0 for the new generation" in started

    flush = SOURCE[SOURCE.index("bool MemoriaProtocol::HandlePlaybackFlushV2") :]
    flush = flush[: flush.index("bool MemoriaProtocol::HandleRuntimeProfileInvalidated")]
    assert "downlink_sequence_ = 0" in flush
    assert "downlink_sample_start_ = 0" in flush
    assert "replacement generation" in flush

    reset = SOURCE[SOURCE.index("void MemoriaProtocol::ResetSessionState") :]
    assert "downlink_sequence_ = 0" in reset
    assert "downlink_sample_start_ = 0" in reset

    # Cross-layer: the contract names the same reset authorities.
    clock = CONTRACT["binary_audio"]["generation_semantics"]["downlink_clock"]
    assert clock["reset_on"] == ["generation.started", "playback.flush"]
    assert clock["reset_to"] == 0


def test_audio_frame_flags_allow_only_downlink_discontinuity() -> None:
    assert "kDiscontinuityFlag = 0x0001" in AUDIO_FRAME_HEADER
    validate = AUDIO_FRAME_SOURCE[
        AUDIO_FRAME_SOURCE.index("MemoriaAudioFrameError MemoriaAudioFrame::Validate") :
    ]
    assert "kDiscontinuityFlag" in validate
    assert "kInvalidFlags" in validate
    assert "metadata.direction == MemoriaAudioDirection::kUplink" in validate
    flags = CONTRACT["binary_audio"]["flags"]
    assert flags["discontinuity"] == 1
    assert flags["uplink_allowed"] is False


def test_local_hard_stop_flushes_before_button_stop_and_never_resumes() -> None:
    abort = PATCH_0007[PATCH_0007.index("void Application::AbortSpeaking") :]
    assert "dropped and counted" in abort

    atomic_abort = PATCH_0010[PATCH_0010.index("void Application::AbortSpeaking") :]
    assert "NotifyLocalFlush()" in atomic_abort
    assert "ResetDecoderForServerGeneration(0)" in atomic_abort
    assert "cannot enter the queue afterwards" in atomic_abort

    flush = SOURCE[SOURCE.index("void MemoriaProtocol::NotifyLocalFlush") :]
    assert '"button.stop"' in SOURCE
    assert "SendButtonStop(fence, local_flush_sample_end)" in flush
    assert "playback_active_ = false" in flush
    assert "had_active_generation" in flush
    assert "stopped_generation_id_" in flush  # v1: the stopped generation must not recover

    stop = SOURCE[SOURCE.index("void MemoriaProtocol::SendButtonStop") :]
    required = CONTRACT["$defs"]["button_stop_v2"]["required"]
    for key in required:
        assert f'"{key}"' in stop, f"button.stop v2 is missing {key}"
    assert '"expected_fence"' in stop
    assert '"local_flush_sample_end"' in stop
    assert "control_sequence_" in stop
    assert "device_monotonic_ms" in stop
    # v1 sessions keep the legacy button.event path.
    assert '"button.event"' in flush
    assert '"button", "primary"' in flush


def test_playback_receipts_v2_carry_full_fence_and_source_precision() -> None:
    generation_fence = CONTRACT["$defs"]["generation_fence"]
    assert generation_fence["required"] == [
        "turn_id",
        "generation_id",
        "tool_epoch",
        "session_epoch",
    ]
    assert 'GetPositiveUint32(object, "session_epoch"' in SOURCE
    assert 'cJSON_AddNumberToObject(fence_json, "session_epoch"' in SOURCE

    receipt = SOURCE[SOURCE.index("void MemoriaProtocol::SendPlaybackReceipt") :]
    required = CONTRACT["$defs"]["playback_receipt_v2"]["required"]
    for key in required:
        assert f'"{key}"' in receipt, f"playback receipt v2 is missing {key}"
    assert '"approximate"' in receipt
    assert '"rendered_sample_end"' in receipt
    assert '"received_sequence"' in receipt
    assert '"fence"' in receipt
    assert 'cJSON_AddBoolToObject(root.value, "approximate", approximate)' in receipt
    # Exact receipts are GDMA TX-EOF-confirmed digital boundaries. The source
    # explicitly keeps this separate from acoustic speaker proof.
    assert "precise digital playout boundary" in receipt
    assert "not acoustic proof" in receipt
    for event in ("playback.started", "playback.progress", "playback.ended", "playback.error"):
        assert event in SOURCE
    assert "kProgressReceiptIntervalFrames" in SOURCE
    # ended is the authority of the real drained pipeline; decode errors are a
    # terminal playback.error.
    drained = SOURCE[SOURCE.index("void MemoriaProtocol::NotifyPlaybackDrained") :]
    assert '"playback.ended"' in drained
    error = SOURCE[SOURCE.index("void MemoriaProtocol::NotifyPlaybackDecodeError") :]
    assert '"playback.error"' in error
    assert "playback_terminal_receipted_ = true" in error


def test_decode_error_and_flush_are_wired_through_audio_service_and_application() -> None:
    assert "on_playback_decode_error" in PATCH_0007
    assert "NotifyPlaybackDecodeError()" in PATCH_0007
    audio_service = PATCH_0007[PATCH_0007.index("main/audio/audio_service.cc") :]
    assert "notify_decode_error" in audio_service
    assert "callbacks_.on_playback_decode_error" in audio_service


def test_es8388_tx_eof_gates_exact_terminal_playback_boundary() -> None:
    assert "i2s_channel_register_event_callback" in PATCH_0017
    assert ".on_sent = OnOutputSent" in PATCH_0017
    assert "output_completion_counter_.fetch_add" in PATCH_0017
    assert "AUDIO_CODEC_DMA_DESC_NUM" in PATCH_0017
    assert "exact_output_pending_" in PATCH_0017
    assert "OutputCompletionCounter() >= exact_output_target_" in PATCH_0017
    assert "received_sequence, false" in PATCH_0017
    assert "!exact_output_pending_" in PATCH_0017
    # OutputData still emits an explicitly approximate live watermark; only
    # the full DMA-ring completion barrier is promoted to exact.
    assert "task->received_sequence, true" in PATCH_0017
    # Server-initiated P0 controls atomically replace the queue gate on the
    # receive path.
    assert "SetLocalFlushCallback" in PATCH_0007
    assert "ResetDecoderForServerGeneration" in PATCH_0010
    assert "on_local_flush_requested_" in SOURCE
    flush_v2 = SOURCE[SOURCE.index("HandlePlaybackFlushV2") :]
    assert "on_local_flush_requested_" in flush_v2
    terminal = SOURCE[SOURCE.index("HandleGenerationTerminal") :]
    assert "on_local_flush_requested_" in terminal
    started = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleGenerationStarted") :]
    assert "on_local_flush_requested_" in started


def test_runtime_profile_invalidated_fails_closed_or_defers_to_session() -> None:
    handler = SOURCE[SOURCE.index("HandleRuntimeProfileInvalidated") :]
    assert '"runtime_profile.invalidated"' in handler
    for mode in ("next_safe_point", "next_session", "immediate_fail_closed"):
        assert f'"{mode}"' in handler
    assert "MaybeApplyPendingProfile" in handler
    assert 'QueueSessionClose("runtime_profile_invalidated")' in handler
    assert "QueueTransportRetire()" in handler
    assert "playback_active_ = false" in handler
    assert "ProfileApplyMode" in PROTOCOL_HEADER


def test_device_acknowledges_the_profile_and_settings_it_actually_applied() -> None:
    accepted = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleSessionAccepted") :]
    assert '"runtime_profile.applied"' in accepted
    assert "SendRuntimeProfileApplied(runtime_profile_version_, settings_version_)" in accepted
    assert "profile_version" in accepted
    assert "settings_version" in accepted


def test_reconnect_epoch_resets_session_state_but_keeps_device_lifetime_sequence() -> None:
    reset = SOURCE[SOURCE.index("void MemoriaProtocol::ResetSessionState") :]
    for field in (
        "downlink_sample_rate_",
        "downlink_frame_samples_",
        "fence_",
        "playback_active_",
        "playback_paused_",
        "stopped_generation_id_",
        "receipt_generation_id_",
        "playback_completion_pending_",
        "playback_output_end_",
        "playback_output_sequence_",
        "playback_output_frames_",
        "dropped_frames_",
        "protocol_violations_",
    ):
        assert field in reset
    assert "device-lifetime monotonic counter" in reset
    assert "control_sequence_" in reset
    assert "re-negotiated from the next ticket" in reset
    assert "stream_epoch" in SOURCE


def test_playback_pipeline_rejects_flushed_generation_tails() -> None:
    # AudioService is the single cross-task render gate; Application does not
    # read protocol-owned playback state from a different FreeRTOS task.
    assert "AcceptsGeneration" not in PROTOCOL_HEADER
    assert "generation_id = 0" in PATCH_0007  # AudioStreamPacket field
    # The decisive recheck lives inside the AudioService queue lock. P0 flush
    # and enqueue therefore cannot interleave into a stale-tail TOCTOU window.
    assert "PushServerPacketToDecodeQueue" in PATCH_0010
    assert "packet->generation_id != accepted_server_generation_" in PATCH_0010
    assert "ResetDecoderForServerGeneration" in PATCH_0010
    assert "accepted_server_generation_ = accepted_server_generation" in PATCH_0010
    assert "std::lock_guard<std::mutex> lock(audio_queue_mutex_)" in PATCH_0010
    assert "ResetDecoderLocked(accepted_server_generation_)" in PATCH_0010
    assert "accepted_server_generation_ = 0" in PATCH_0010
    assert "OnAudioChannelClosed" in PATCH_0010
    assert "+                 memoria_protocol->AcceptsGeneration" not in PATCH_0010


def test_wakeword_listening_popup_branch_is_symmetric_with_speaking() -> None:
    branch = PATCH_0007[PATCH_0007.index("if (state == kDeviceStateListening)") :]
    branch = branch[: branch.index("#endif")]
    assert "SendVadState(false, audio_service_.LastUplinkRms())" in branch
    assert "audio_service_.EnableVoiceProcessing(false)" in branch
    assert "play_popup_on_listening_ = true" in branch
    assert "StartListeningAudio()" in branch
    # The cue must be played before the mic epoch reopens (cue-first seam),
    # matching the speaking branch in 0006.
    assert "cue-first seam" in branch


def test_audio_frame_validates_both_negotiated_downlink_rates() -> None:
    assert "kDownlinkFrameSamples16k = 320" in AUDIO_FRAME_HEADER
    assert "kDownlinkFrameSamples24k = 480" in AUDIO_FRAME_HEADER
    assert "IsValidDownlinkFrameSamples" in AUDIO_FRAME_SOURCE
    assert "kDownlinkFrameSamples16k" in AUDIO_FRAME_SOURCE
    assert "kDownlinkFrameSamples24k" in AUDIO_FRAME_SOURCE
    assert "kUplinkFrameSamples" in AUDIO_FRAME_SOURCE
    assert "static_assert(sizeof(MemoriaAudioFrameHeaderV1) == 30" in AUDIO_FRAME_HEADER

    # Cross-check against the contract's golden downlink frames.
    for golden in CONTRACT["binary_audio"]["golden_frames"]:
        if golden["name"].startswith("downlink"):
            assert golden["frame_samples"] in (320, 480)


def test_v2_device_events_include_control_sequence_and_monotonic_clock() -> None:
    assert "control_sequence_" in SOURCE
    assert "DeviceMonotonicMs()" in SOURCE
    assert "esp_timer_get_time() / 1000" in SOURCE
    assert "device_monotonic_ms" in SOURCE


def test_v1_event_shapes_are_kept_exact_for_legacy_gateway() -> None:
    # The v1 gateway validates exact key sets; v2 fields must not leak into
    # v1-shaped events. Structured JSON generation also prevents malformed
    # quote concatenation from closing a real device session.
    vad_start = SOURCE.index("ScopedJson legacy_event")
    vad_v1 = SOURCE[vad_start : SOURCE.index("void MemoriaProtocol::SendAbortSpeaking", vad_start)]
    assert vad_v1.count("cJSON_AddStringToObject") == 1
    assert vad_v1.count("cJSON_AddNumberToObject") == 2
    assert '"type", speaking ? "vad.start" : "vad.end"' in vad_v1
    assert '"stream_epoch", legacy_stream_epoch' in vad_v1
    assert '"sample_position"' in vad_v1
    assert "SendText(RenderJson(legacy_event.value))" in vad_v1
    assert '"control_sequence"' not in vad_v1
    assert '"device_monotonic_ms"' not in vad_v1
    close_start = SOURCE.index("void MemoriaProtocol::CloseAudioChannel")
    v1_close = SOURCE[close_start : SOURCE.index("bool MemoriaProtocol::IsAudioChannelOpened", close_start)]
    assert '"type", "session.close"' in v1_close


def test_outbound_websocket_controls_never_hand_concatenate_json() -> None:
    # Canonical signature payloads are intentionally hand-rendered elsewhere;
    # every WebSocket control must use cJSON + RenderJson so malformed quoting
    # cannot tear down a real device session.
    canonical_start = SOURCE.index("std::string MediaProofPayload")
    canonical_end = SOURCE.index("bool HttpJson", canonical_start)
    outside_canonical_payload = SOURCE[:canonical_start] + SOURCE[canonical_end:]
    assert '"{\\"' not in outside_canonical_payload
    assert 'R"({' not in outside_canonical_payload

    send_sites = [
        line.strip()
        for line in SOURCE.splitlines()
        if "SendText(" in line and "bool MemoriaProtocol::SendText" not in line
    ]
    assert send_sites
    for site in send_sites:
        assert "SendText(RenderJson(" in site or "SendText(hello)" in site, site
    queued_sites = [
        line.strip()
        for line in SOURCE.splitlines()
        if "QueueTransportText(" in line
        and "bool MemoriaProtocol::QueueTransportText" not in line
    ]
    assert queued_sites
    for site in queued_sites:
        assert "QueueTransportText(RenderJson(" in site, site
    for event_type in (
        "session.close",
        "listen.start",
        "listen.stop",
        "vad.start",
        "button.event",
        "playback.ended",
    ):
        assert f'"{event_type}"' in SOURCE


def test_generation_completed_only_marks_completion_pending() -> None:
    # The completed branch must never end/clear/stop on the receive path: the
    # ended receipt, tts stop and generation clear wait for the real drain.
    terminal = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleGenerationTerminal") :]
    terminal = terminal[: terminal.index("bool MemoriaProtocol::HandlePlaybackFlushV2")]
    completed = terminal[terminal.index("// generation.completed is an authoritative") :]
    assert "playback_completion_pending_ = true" in completed
    assert 'SendPlaybackReceipt("playback.ended"' not in completed
    assert 'EmitLegacyTts("stop")' not in completed
    assert "receipt_generation_id_ = 0" not in completed
    assert "playback_active_ = false" not in completed
    # The Application-injected idle probe finalizes an already-empty queue
    # immediately; otherwise the drain edge does it.
    assert "is_playback_idle_ != nullptr && is_playback_idle_()" in completed
    assert "FinalizePlaybackEnded()" in completed


def test_generation_completed_stale_barrier_is_ignored() -> None:
    terminal = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleGenerationTerminal") :]
    assert "fence.generation_id != fence_.generation_id" in terminal
    assert "Ignoring terminal" in terminal
    assert "stale generation" in terminal


def test_generation_cancelled_flushes_p0_and_reports_received_watermark() -> None:
    terminal = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleGenerationTerminal") :]
    terminal = terminal[: terminal.index("bool MemoriaProtocol::HandlePlaybackFlushV2")]
    cancel = terminal[
        terminal.index("// generation.cancelled is never a normal completion") :
        terminal.index("// generation.completed is an authoritative")
    ]
    # The receipt state is finalized before the atomic queue flush so the
    # ResetDecoder drain callback cannot double-report the generation.
    assert "playback_completion_pending_ = false" in cancel
    assert "on_local_flush_requested_" in cancel
    assert 'SendPlaybackReceipt("playback.ended", stopped_fence' in cancel
    assert "stopped_end" in cancel
    assert cancel.index("receipt_generation_id_ = 0") < cancel.index(
        'SendPlaybackReceipt("playback.ended", stopped_fence'
    )
    assert 'EmitLegacyTts("stop")' in cancel
    # A cancel is never treated as a normal completed barrier.
    assert "playback_completion_pending_ = true" not in cancel


def test_plain_queue_drain_without_completion_never_ends_playback() -> None:
    drained = SOURCE[SOURCE.index("void MemoriaProtocol::NotifyPlaybackDrained") :]
    drained = drained[: drained.index("void MemoriaProtocol::NotifyPlaybackOutput")]
    v2 = drained[drained.index("protocol_version_ == kProtocolVersionV2") :]
    assert "playback_completion_pending_" in v2
    assert "FinalizePlaybackEnded()" in v2
    # A temporary drain between frames of a still-streaming generation must
    # not emit ended; only the authoritative barrier may open the gate.
    assert "A plain queue drain is not a terminal event" in v2
    assert 'SendPlaybackReceipt("playback.ended"' not in v2


def test_finalize_playback_ended_uses_output_commit_watermark() -> None:
    finalize = SOURCE[SOURCE.index("void MemoriaProtocol::FinalizePlaybackEnded") :]
    assert 'SendPlaybackReceipt("playback.ended"' in finalize
    assert "playback_output_frames_ > 0 ? playback_output_end_ : 0" in finalize
    assert "playback_output_frames_ > 0 ? playback_output_sequence_ : 0" in finalize
    assert "active_generation_received_end_" in finalize
    assert "playback_completion_pending_ = false" in finalize
    assert "receipt_generation_id_ = 0" in finalize
    assert "playback_output_frames_ = 0" in finalize
    assert "playback_active_ = false" in finalize
    assert 'EmitLegacyTts("stop")' in finalize


def test_v2_receipts_move_to_output_commit_not_receive_path() -> None:
    # started/progress are emitted only after AudioService commits the frame
    # to the codec output (NotifyPlaybackOutput), never from HandleDownlink.
    downlink = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleDownlink") :]
    downlink = downlink[: downlink.index("bool MemoriaProtocol::ValidateServerBase", 0)]
    assert 'SendPlaybackReceipt("playback.started"' not in downlink
    assert 'SendPlaybackReceipt("playback.progress"' not in downlink
    assert "not a network-received position" in downlink

    output = SOURCE[SOURCE.index("void MemoriaProtocol::NotifyPlaybackOutput") :]
    assert "generation_id != receipt_generation_id_" in output
    assert "playback_output_end_ = rendered_sample_end" in output
    assert 'SendPlaybackReceipt("playback.started"' in output
    assert "kProgressReceiptIntervalFrames" in output
    assert 'SendPlaybackReceipt("playback.progress"' in output

    # The frame metadata travels through the packet and decode task so the
    # output task can report the downlink sample-clock end.
    assert "sample_start = 0" in PATCH_0008
    assert "sample_end = 0" in PATCH_0008
    assert "received_sequence = 0" in PATCH_0008
    assert "on_playback_output" in PATCH_0008
    assert "task->generation_id = packet->generation_id" in PATCH_0008
    assert "task->sample_end = packet->sample_end" in PATCH_0008
    assert "task->received_sequence = packet->received_sequence" in PATCH_0008
    assert "codec output actually consumed" in PATCH_0008
    # Application wires both the output callback and the idle probe.
    assert "NotifyPlaybackOutput(generation_id, rendered_sample_end" in PATCH_0008
    assert "SetIsPlaybackIdleCallback" in PATCH_0008
    assert "audio_service_.IsPlaybackIdle()" in PATCH_0008
    # The packet carries the downlink position from the device protocol.
    assert ".sample_start = metadata.sample_start" in SOURCE
    assert ".sample_end = frame_end" in SOURCE
    assert ".received_sequence = static_cast<uint32_t>(metadata.sequence)" in SOURCE


def test_flush_and_local_stop_cancel_pending_completion() -> None:
    flush = SOURCE[SOURCE.index("HandlePlaybackFlushV2") :]
    assert "playback_completion_pending_ = false" in flush
    local_flush = SOURCE[SOURCE.index("void MemoriaProtocol::NotifyLocalFlush") :]
    assert "playback_completion_pending_ = false" in local_flush
    error = SOURCE[SOURCE.index("void MemoriaProtocol::NotifyPlaybackDecodeError") :]
    assert "playback_completion_pending_ = false" in error


def test_completion_pending_is_wired_into_receipt_state_transitions() -> None:
    started = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleGenerationStarted") :]
    assert "playback_completion_pending_ = false" in started
    assert "playback_output_frames_ = 0" in started
    downlink_switch = SOURCE[
        SOURCE.index("if (receipt_generation_id_ != metadata.generation_id)") :
    ]
    assert "playback_completion_pending_ = false" in downlink_switch
    assert "playback_output_frames_ = 0" in downlink_switch
    assert "playback_completion_pending_" in PROTOCOL_HEADER
    assert "SetIsPlaybackIdleCallback" in PROTOCOL_HEADER
    assert "NotifyPlaybackOutput" in PROTOCOL_HEADER
    assert "FinalizePlaybackEnded" in PROTOCOL_HEADER


def test_playback_flush_must_replace_the_active_generation_successor() -> None:
    flush = SOURCE[SOURCE.index("bool MemoriaProtocol::HandlePlaybackFlushV2") :]
    flush = flush[: flush.index("bool MemoriaProtocol::HandleRuntimeProfileInvalidated")]
    # A flush is only valid for the generation this device is currently
    # rendering: the fence must be the immediate successor (same turn and
    # tool epoch, generation + 1) and the replacement id must equal it.
    assert "!fence_.valid() || !playback_active_" in flush
    assert "flush_fence.turn_id != fence_.turn_id" in flush
    assert "flush_fence.tool_epoch != fence_.tool_epoch" in flush
    assert "flush_fence.generation_id != fence_.generation_id + 1" in flush
    assert "replacement_generation_id != flush_fence.generation_id" in flush
    assert "protocol_violations_" in flush
    assert "does not replace the active generation" in flush
    # The stale-flush gate runs before any receipt, flush or clock mutation.
    gate = flush.index("!fence_.valid() || !playback_active_")
    pending = flush.index("playback_completion_pending_ = false")
    assert gate < pending


def test_generation_started_must_strictly_advance_the_current_fence() -> None:
    started = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleGenerationStarted") :]
    started = started[: started.index("bool MemoriaProtocol::HandleGenerationPauseResume")]
    assert "fence.turn_id < fence_.turn_id" in started
    assert "fence.generation_id < fence_.generation_id" in started
    assert "fence.tool_epoch <= fence_.tool_epoch" in started
    assert "strictly advance" in started
    assert "protocol_violations_" in started
    # Monotonicity fails closed before the atomic queue flush and state reset.
    monotonic = started.index("fence.turn_id < fence_.turn_id")
    flush = started.index("on_local_flush_requested_")
    assert monotonic < flush


def test_local_hard_stop_marks_the_generation_terminal_receipted() -> None:
    local_flush = SOURCE[SOURCE.index("void MemoriaProtocol::NotifyLocalFlush") :]
    # The button stop is the terminal receipt for the stopped generation: the
    # authoritative generation.cancelled that follows must not re-report a
    # duplicate playback.ended with the same watermark.
    terminal = local_flush.index("playback_terminal_receipted_ = true")
    button_stop = local_flush.index("SendButtonStop(fence, local_flush_sample_end)")
    assert terminal < button_stop
    assert "mark it receipted" in local_flush
    assert "receipt_generation_id_ = 0" in local_flush
    assert local_flush.index("receipt_generation_id_ = 0") < local_flush.index(
        "SendButtonStop(fence, local_flush_sample_end)"
    )
    # The marker is scoped to the stopped generation: both a new generation
    # start and a normal finalize clear it, so it never leaks forward.
    started = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleGenerationStarted") :]
    assert "playback_terminal_receipted_ = false" in started
    finalize = SOURCE[SOURCE.index("void MemoriaProtocol::FinalizePlaybackEnded") :]
    assert "playback_terminal_receipted_ = false" in finalize


def test_session_accepted_is_a_bootstrap_not_an_ordered_control() -> None:
    accepted = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleSessionAccepted") :]
    accepted = accepted[: accepted.index("bool MemoriaProtocol::HandleGenerationStarted")]
    # The handshake must only validate accept fields (version/session_id/
    # stream_epoch plus the negotiated facts); the Go edge contract sends no
    # control_sequence/server_monotonic_ms on session.accepted.
    assert 'ValidateServerBase(root, "session.accepted"' not in accepted
    assert "&control_sequence" not in accepted
    assert "uint64_t server_monotonic_ms" not in accepted
    assert "response_session_id != session_id_" in accepted
    assert "epoch != stream_epoch_" in accepted
    # Ordered-control messages still require the full server base.
    assert 'ValidateServerBase(root, "playback.flush"' in SOURCE
    assert 'ValidateServerBase(root.value, "playback.duck"' in SOURCE
    assert 'ValidateServerBase(root, "runtime_profile.invalidated"' in SOURCE
    assert 'ValidateServerBase(root, "session.error"' in SOURCE


def test_terminal_ended_watermarks_use_only_confirmed_output_boundaries() -> None:
    # cancel/flush/finalize ended receipts must report the latest confirmed
    # output boundary and honestly report 0 when none exists; the received or
    # queued position is never a fallback for played watermarks.
    cancel = SOURCE[SOURCE.index("bool MemoriaProtocol::HandleGenerationTerminal") :]
    cancel = cancel[: cancel.index("bool MemoriaProtocol::HandlePlaybackFlushV2")]
    cancel_branch = cancel[
        cancel.index("// generation.cancelled is never a normal completion") :
    ]
    assert "playback_output_frames_ > 0 ? playback_output_end_ : 0" in cancel_branch
    assert "playback_output_frames_ > 0 ? playback_output_sequence_ : 0" in cancel_branch
    assert (
        'SendPlaybackReceipt("playback.ended", stopped_fence, stopped_sequence, stopped_end,'
        in cancel_branch
    )
    # The had-audio gate still uses the receive bound, but the reported
    # watermarks are never read from the received counters anymore.
    assert "stopped_sequence = active_generation_received_sequence_" not in cancel_branch
    assert "stopped_end = active_generation_received_end_" not in cancel_branch

    flush = SOURCE[SOURCE.index("bool MemoriaProtocol::HandlePlaybackFlushV2") :]
    flush = flush[: flush.index("bool MemoriaProtocol::HandleRuntimeProfileInvalidated")]
    assert "playback_output_frames_ > 0 ? playback_output_end_ : 0" in flush
    assert "playback_output_frames_ > 0 ? playback_output_sequence_ : 0" in flush
    assert (
        'SendPlaybackReceipt("playback.ended", flushed_fence, flush_sequence, flush_end,'
        in flush
    )

    finalize = SOURCE[SOURCE.index("void MemoriaProtocol::FinalizePlaybackEnded") :]
    assert "playback_output_frames_ > 0 ? playback_output_end_ : 0" in finalize
    assert "playback_output_frames_ > 0 ? playback_output_sequence_ : 0" in finalize
    assert "honestly reports 0" in finalize

    local_flush = SOURCE[SOURCE.index("void MemoriaProtocol::NotifyLocalFlush") :]
    local_flush = local_flush[: local_flush.index("void MemoriaProtocol::NotifyPlaybackDrained")]
    assert "playback_output_frames_ > 0 ? playback_output_end_ : 0" in local_flush
    assert "local_flush_sample_end = active_generation_received_end_" not in local_flush

    decode_error = SOURCE[SOURCE.index("void MemoriaProtocol::NotifyPlaybackDecodeError") :]
    decode_error = decode_error[: decode_error.index("void MemoriaProtocol::SetLocalFlushCallback")]
    assert "playback_output_frames_ > 0 ? playback_output_end_ : 0" in decode_error
    assert "playback_output_frames_ > 0 ? playback_output_sequence_ : 0" in decode_error
    assert "active_generation_received_sequence_" not in decode_error
    assert "active_generation_received_end_" not in decode_error


def test_media_session_request_advertises_supported_protocol_versions() -> None:
    create = SOURCE[SOURCE.index("bool MemoriaProtocol::CreateMediaSession") :]
    create = create[: create.index("bool MemoriaProtocol::OpenAudioChannel")]
    assert '"supported_protocol_versions"' in create
    start = create.index("supported_versions = cJSON_CreateArray()")
    end = create.index("std::string session_response", start)
    advertisement = create[start:end]
    # v2 (v2-only direct edge) is advertised first; v1 remains only as the
    # legacy livekit_compat gateway fallback.
    assert "kProtocolVersionV2" in advertisement
    assert "kProtocolVersionV1" in advertisement
    assert advertisement.index("kProtocolVersionV2") < advertisement.index("kProtocolVersionV1")
    assert "cJSON_AddItemToArray" in advertisement
    assert "legacy livekit_compat" in create


def test_legacy_control_schema_fallback_is_exact_and_fail_closed() -> None:
    http = SOURCE[SOURCE.index("bool HttpJson") :]
    http = http[: http.index("bool GetString")]
    assert "int* status_code = nullptr" in http
    assert "*status_code = status" in http
    assert http.index("*response = http->ReadAll()") < http.index(
        'ESP_LOGE(kTag, "Media HTTP rejected, status=%d"'
    )

    detector = SOURCE[SOURCE.index("enum class LegacyProtocolSchemaRejection") :]
    detector = detector[: detector.index("bool GetString")]
    assert 'std::string_view(type->valuestring) != "extra_forbidden"' in detector
    assert 'std::string_view(scope->valuestring) != "body"' in detector
    assert (
        'std::string_view(field->valuestring) == "supported_protocol_versions"'
        not in detector
    )
    assert 'field_name == "supported_protocol_versions"' in detector
    assert 'field_name == "resume_session_id"' in detector
    assert "error_count < 1 || error_count > 2" in detector
    assert "kAdvertisementOnly" in detector
    assert "kAdvertisementAndResume" in detector

    create = SOURCE[SOURCE.index("bool MemoriaProtocol::CreateMediaSession") :]
    create = create[: create.index("bool MemoriaProtocol::OpenAudioChannel")]
    assert "DetectLegacyProtocolSchemaRejection(session_response)" in create
    assert "!resume_requested &&" in create
    assert "legacy_rejection == LegacyProtocolSchemaRejection::kAdvertisementOnly" in create
    assert "legacy_rejection == LegacyProtocolSchemaRejection::kAdvertisementAndResume" in create
    assert "resumable_session_id_ == requested_session_id" in create
    assert "resumable_stream_epoch_ == requested_after_epoch" in create
    assert "!terminal_session_close_" in create
    assert 'cJSON_DeleteItemFromObjectCaseSensitive(request.value, "resume_session_id")' in create
    assert "resumable_session_id_.clear()" in create
    assert "resumable_stream_epoch_ = 0" in create
    assert (
        'cJSON_DeleteItemFromObjectCaseSensitive(request.value, '
        '"supported_protocol_versions")' in create
    )
    assert create.count("HttpJson(session_url") == 2
    assert "other 422/auth/runtime error remains fail-closed" in create
    assert 'Media session negotiated protocol v%u' in create


def test_network_disconnect_actively_recovers_the_same_session() -> None:
    assert "HandleNetworkDisconnectedEvent" in PATCH_0009
    assert "CloseAudioChannel(false)" in PATCH_0009
    assert "strictly newer stream_epoch" in PATCH_0009
    create = SOURCE[SOURCE.index("bool MemoriaProtocol::CreateMediaSession") :]
    create = create[: create.index("bool MemoriaProtocol::OpenAudioChannel")]
    assert '"resume_session_id"' in create
    assert "resumable_session_id_" in create
    assert "resumable_stream_epoch_" in create
    assert "std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_)" in create
    assert '"resume_session_id",\n                                requested_session_id.c_str()' in create
    assert '"resume_session_id",\n                                resumable_session_id_.c_str()' not in create
    assert "session->session_id != requested_session_id" in create
    assert "protocol_version != kProtocolVersionV2" in create
    assert "session->stream_epoch <= requested_after_epoch" in create
    assert "same-Session/newer-epoch fence" in create

    # Preserving an id is not recovery by itself. Application owns a bounded
    # RECOVERING state and actively opens a freshly ticketed WSS after either
    # Wi-Fi or transport-only failure.
    assert "kDeviceStateRecovering" in PATCH_0011
    assert "media_recovery_pending_" in PATCH_0011
    assert "media_network_connected_" in PATCH_0011
    assert "std::atomic<bool> media_network_connected_" in PATCH_0011
    assert "media_network_connected_.store(true)" in PATCH_0011
    assert "media_network_connected_.store(false)" in PATCH_0011
    assert "Event bits can contain an older Connected" in PATCH_0011
    assert "Disconnected was followed by Connected" in PATCH_0011
    assert "kMediaReconnectMaxAttempts = 5" in PATCH_0011
    assert "ContinueMediaRecovery" in PATCH_0011
    assert "protocol_->OpenAudioChannel()" in PATCH_0011
    assert "1U << (media_reconnect_attempt_ - 1U)" in PATCH_0011
    assert "kMediaReconnectCallbackGraceUs = 100000" in PATCH_0011
    assert "the next clock tick crosses the explicit grace" in PATCH_0011
    assert "closed_transport_attempt" in PATCH_0011
    assert "TransportAttemptId() != closed_transport_attempt" in PATCH_0011
    assert "no authority over the new UI/session state" in PATCH_0011
    assert "audio_transport_attempt" in PATCH_0011
    assert "TransportAttemptId() == audio_transport_attempt" in PATCH_0011
    assert "json_transport_attempt" in PATCH_0011
    assert "TransportAttemptId() != json_transport_attempt" in PATCH_0011
    assert "HasActivePlaybackGeneration" in PATCH_0011
    assert "SetDeviceState(kDeviceStateSpeaking)" in PATCH_0011
    assert "media_recovery_previous_state_ == kDeviceStateSpeaking" in PATCH_0011
    assert "listening_mode_ != kListeningModeManualStop" in PATCH_0011
    assert "normal tts-stop path" in PATCH_0011
    assert "CancelMediaRecovery(true)" in PATCH_0011
    # A failure before session.accepted must retain the exact open/listening
    # intent, not merely recover the Session and strand the UI in idle.
    assert "listening_mode_ = mode" in PATCH_0011
    assert "media_recovery_previous_state_ = kDeviceStateListening" in PATCH_0011
    assert "silently collapsing the open intent to idle" in PATCH_0011
    assert "media_recovery_pending_ = false" in PATCH_0011
    assert "void Application::ResetProtocol" in PATCH_0011
    assert "if (protocol_)" in PATCH_0011

    board_source = (
        Path(__file__).parents[1]
        / "overlay"
        / "files"
        / "main"
        / "boards"
        / "memoria"
        / "atk-dnesp32s3-v1"
        / "memoria_atk_dnesp32s3_v1.cc"
    ).read_text(encoding="utf-8")
    assert "state == kDeviceStateRecovering" in board_source
    assert "app.ToggleChatState()" in board_source
    assert "app.Schedule([this]() { EnterWifiConfigMode(); })" in board_source


def test_transport_attempt_fence_blocks_late_old_websocket_callbacks() -> None:
    open_channel = SOURCE[SOURCE.index("bool MemoriaProtocol::OpenAudioChannel") :]
    open_channel = open_channel[: open_channel.index("void MemoriaProtocol::CloseAudioChannel")]
    assert "websocket_attempt_id_" in PROTOCOL_HEADER
    assert "TransportAttemptId() const" in PROTOCOL_HEADER
    assert "bool transport_close_notified_ = true" in PROTOCOL_HEADER
    assert "++websocket_attempt_id_" in open_channel
    assert "uint32_t websocket_attempt = 0" in open_channel
    assert "transport_close_notified_ = false" in open_channel
    assert open_channel.count("websocket_attempt != websocket_attempt_id_.load()") == 2
    assert "WebSocket* const websocket = websocket_.get()" in open_channel
    assert "OnData([this, websocket, websocket_attempt]" in open_channel
    assert "OnDisconnected([this, websocket, websocket_attempt]" in open_channel
    assert "OnError([this, websocket_attempt]" in open_channel
    assert "websocket->Close()" not in open_channel
    assert "not call WebSocket::Close from its receive callback" in open_channel
    assert "websocket_->Close()" not in open_channel[open_channel.index("OnData(") :]
    retire = SOURCE[SOURCE.index("bool MemoriaProtocol::RetireTransportAttempt") :]
    retire = retire[: retire.index("void MemoriaProtocol::ResetSessionState")]
    assert "websocket_attempt != websocket_attempt_id_.load()" in retire
    assert "error_occurred_ = true" in retire
    assert retire.index("error_occurred_ = true") < retire.index("++websocket_attempt_id_")
    assert "++websocket_attempt_id_" in retire
    assert "Linearization point for every passive WSS failure" in retire
    assert "if (!transport_close_notified_)" in retire
    assert "transport_close_notified_ = true" in retire


def test_terminal_session_authority_cannot_be_auto_resumed() -> None:
    close = SOURCE[SOURCE.index("void MemoriaProtocol::CloseAudioChannel") :]
    close = close[: close.index("bool MemoriaProtocol::IsAudioChannelOpened")]
    assert "std::lock_guard<std::recursive_mutex> state_lock(playback_state_mutex_)" in close
    assert "++websocket_attempt_id_" in close
    assert "Old queued audio/UI work immediately fails" in close
    assert "on_audio_channel_closed_()" in close
    assert "retiring_websocket = std::move(websocket_)" in close
    unlocked_close = close.index("    if (retiring_websocket != nullptr)")
    assert close.index("retiring_websocket = std::move(websocket_)") < unlocked_close
    assert unlocked_close < close.index("retiring_websocket->Close()")
    assert "if (!transport_close_notified_)" in close
    assert close.index("if (!transport_close_notified_)") < close.index(
        "++websocket_attempt_id_"
    )
    assert close.index("DrainTransportActions()") < close.index("ResetSessionState()")
    assert 'QueueSessionClose("device_close")' in close
    assert "QueueTransportRetire()" in close
    assert "SendSessionClose(" not in close
    # Even if the socket is already gone, an explicit local close is terminal
    # and clears the durable resume identity.
    assert close.index("terminal_session_close_ = true") < close.index(
        "websocket_ != nullptr && websocket_->IsConnected()"
    )
    assert "resumable_session_id_.clear()" in close

    server_close = SOURCE[SOURCE.index('if (type == "session.close")') :]
    server_close = server_close[: server_close.index("Ignoring unknown v2 server message")]
    assert 'ValidateServerBase(root.value, "session.close"' in server_close
    assert "terminal_session_close_ = true" in server_close
    assert "resumable_session_id_.clear()" in server_close
    assert "on_local_flush_requested_(0)" in server_close
    assert "RetireTransportAttempt(websocket_attempt_id_.load())" in server_close
    assert "QueueTransportRetire()" not in server_close

    can_resume = SOURCE[SOURCE.index("bool MemoriaProtocol::CanResumeSession") :]
    can_resume = can_resume[: can_resume.index("bool MemoriaProtocol::HasActivePlaybackGeneration")]
    assert "!terminal_session_close_" in can_resume
    assert "active_v2_session" in can_resume
    assert "saved_v2_resume" in can_resume
    assert "resumable_stream_epoch_ != 0" in can_resume


def test_stale_transport_close_is_fenced_before_clearing_new_epoch_ui() -> None:
    callback = PATCH_0011[PATCH_0011.index("protocol_->OnAudioChannelClosed") :]
    callback = callback[: callback.index("protocol_->OnIncomingJson")]
    stale_fence = "TransportAttemptId() != closed_transport_attempt"
    assert callback.index(stale_fence) < callback.index('SetChatMessage("system", "")')


def test_transport_close_recovery_is_frozen_before_a_coalesced_error() -> None:
    run = PATCH_0013[PATCH_0013.index("void Application::Run") :]
    run = run[: run.index("void Application::HandleNetworkDisconnectedEvent")]
    close_event = "if (bits & MAIN_EVENT_MEMORIA_MEDIA_CLOSED)"
    error_event = "if (bits & MAIN_EVENT_ERROR)"
    assert "MAIN_EVENT_MEMORIA_MEDIA_CLOSED (1 << 15)" in PATCH_0013
    assert close_event in run
    assert error_event in run
    assert run.index(close_event) < run.index(error_event)
    assert "generic scheduled UI work is intentionally not reordered" in run

    handler = PATCH_0013[PATCH_0013.index("HandleMediaTransportClosedEvent") :]
    handler = handler[: handler.index("void Application::CancelMediaRecovery")]
    stale_fence = "TransportAttemptId() != closed_transport_attempt"
    freeze = "media_recovery_previous_state_ = state"
    assert stale_fence in handler
    assert freeze in handler
    assert handler.index(stale_fence) < handler.index('SetChatMessage("system", "")')
    assert handler.index(freeze) < handler.index("SetDeviceState(kDeviceStateRecovering)")
    assert "state == kDeviceStateListening" in handler
    assert "state == kDeviceStateSpeaking" in handler
    assert "A close notification and SetError can wake the same EventGroup" in handler


def test_websocket_close_callback_only_publishes_an_attempt_fence() -> None:
    callback = PATCH_0013[PATCH_0013.index("protocol_->OnAudioChannelClosed") :]
    callback = callback[: callback.index("protocol_->OnIncomingJson")]
    added_callback = "\n".join(
        line[1:]
        for line in callback.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    board_branch = added_callback[: added_callback.index("#else")]
    assert "media_closed_transport_attempt_.store(closed_transport_attempt)" in board_branch
    assert "xEventGroupSetBits(event_group_, MAIN_EVENT_MEMORIA_MEDIA_CLOSED)" in board_branch
    assert "media_recovery_previous_state_" not in board_branch
    assert "SetDeviceState" not in board_branch


def test_readme_direct_edge_is_v2_only_and_v1_is_legacy_gateway() -> None:
    # The firmware README must not claim the direct WSS accepts v1: v1 is
    # served by the legacy livekit_compat gateway only.
    assert "supported_protocol_versions: [2, 1]" in FIRMWARE_README
    assert "direct Edge 是 v2-only" in FIRMWARE_README
    assert "direct WSS 不接受 v1" in FIRMWARE_README
    assert "legacy livekit_compat Gateway" in FIRMWARE_README
    assert "v1 回滚路径" not in FIRMWARE_README
    assert "A v1 hello is accepted only for safe" not in CONTRACTS_README
    assert "the direct edge is v2-only and never accepts a" in CONTRACTS_README
