#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "$SCRIPT_DIR/common.sh"

need_command bash
need_command cmp
need_command git
need_command rg
assert_tls_verification
python_bin="$(select_python)"
ca_file="$(python_certifi_ca "$python_bin")"
configure_python_tls "$python_bin" "$ca_file"

for script in "$SCRIPT_DIR"/*.sh; do
    bash -n "$script"
    rg -q '^set -euo pipefail$' "$script" || die "missing strict shell mode: $script"
done

rg -Fq 'WAKE_WORD_MODEL="${MEMORIA_FIRMWARE_WAKE_WORD_MODEL:-}"' \
    "$SCRIPT_DIR/build.sh" || die "product build must use the board's Memoria wake word by default"

"$python_bin" -m json.tool \
    "$MEMORIA_FIRMWARE_ROOT/overlay/files/main/boards/memoria/esp-vocat/config.json" \
    >/dev/null

"$SCRIPT_DIR/bootstrap.sh" --no-idf-install

[[ -s "$MEMORIA_FIRMWARE_ROOT/overlay/files/dependencies.lock" ]] || \
    die "pinned ESP component dependency lock is missing"
cmp -s \
    "$MEMORIA_FIRMWARE_ROOT/overlay/files/dependencies.lock" \
    "$MEMORIA_UPSTREAM_DIR/dependencies.lock" || \
    die "upstream ESP component dependency lock differs from the overlay pin"

board_dir="$MEMORIA_UPSTREAM_DIR/main/boards/memoria/esp-vocat"
[[ -f "$board_dir/memoria_esp_vocat.cc" ]] || die "board source missing"
[[ -f "$board_dir/config.h" ]] || die "board config missing"
[[ -f "$board_dir/config.json" ]] || die "board manifest missing"
[[ -f "$board_dir/memoria_face.h" ]] || die "conversation face header missing"
[[ -f "$board_dir/memoria_face.cc" ]] || die "conversation face renderer missing"
[[ -f "$board_dir/memoria_face_display.h" ]] || die "conversation face display header missing"
[[ -f "$board_dir/memoria_face_display.cc" ]] || die "conversation face display missing"
[[ -f "$board_dir/memoria_pat.h" ]] || die "body-pat detector header missing"
rg -q 'new MemoriaFaceDisplay\(' "$board_dir/memoria_esp_vocat.cc" || \
    die "board does not use the conversation face display"
rg -Fq '#include "memoria_pat.h"' "$board_dir/memoria_esp_vocat.cc" || \
    die "board does not use the host-testable pat detector"
rg -Fq 'idle screen tap ignored; wake word or BOOT starts chat' \
    "$board_dir/memoria_esp_vocat.cc" || \
    die "idle screen tap must not start a conversation"
rg -Fq 'Device pat detected' "$board_dir/memoria_esp_vocat.cc" || \
    die "BMI270 pat must be wired to a local face, not ToggleChatState"
rg -Fq 'SetEmotion("surprised")' "$board_dir/memoria_esp_vocat.cc" || \
    die "idle body pat must show a local surprised face"
rg -Fq 'Device shake ignored' "$board_dir/memoria_esp_vocat.cc" || \
    die "sustained IMU shake must not be treated as a pat"
rg -Fq 'MuteImuForTouch' "$board_dir/memoria_esp_vocat.cc" || \
    die "screen touch must mute IMU so a tap does not look like a pat"
rg -Fq 'Device pat ignored (touch rumble' "$board_dir/memoria_esp_vocat.cc" || \
    die "screen-tap rumble must cancel a pending body pat"
rg -q 'GetTheme\("dark"\)' "$board_dir/memoria_face_display.cc" || \
    die "conversation face must pin the dark theme"
rg -q 'bg_image_src' "$board_dir/memoria_face_display.cc" || \
    die "conversation face is not attached to the display background"
rg -q 'kMouthY' "$board_dir/memoria_face.cc" || \
    die "conversation face renderer must draw a mouth"
if rg -Fq '{"embarrassed", "happy"}' "$board_dir/memoria_face.cc"; then
    die "embarrassed must not alias to happy"
fi
rg -q '"speaking"' "$board_dir/memoria_face.cc" || \
    die "conversation face must include a speaking viseme"
[[ -f "$MEMORIA_UPSTREAM_DIR/main/memoria/device_identity.h" ]] || die "device identity header missing"
[[ -f "$MEMORIA_UPSTREAM_DIR/main/memoria/device_identity.cc" ]] || die "device identity source missing"
[[ -f "$MEMORIA_UPSTREAM_DIR/main/memoria/memoria_audio_frame.h" ]] || die "audio frame header missing"
[[ -f "$MEMORIA_UPSTREAM_DIR/main/memoria/memoria_audio_frame.cc" ]] || die "audio frame source missing"
[[ -f "$MEMORIA_UPSTREAM_DIR/partitions/v2/32m.csv" ]] || die "Memoria partition table missing"

rg -q '^memoria_identity,[[:space:]]*data,[[:space:]]*nvs,[[:space:]]*0x10000,[[:space:]]*64K' \
    "$MEMORIA_UPSTREAM_DIR/partitions/v2/32m.csv" || die "identity partition is not at 0x10000/64K"
rg -q 'espressif/libsodium:[[:space:]]*\^1\.0\.22' "$MEMORIA_UPSTREAM_DIR/main/idf_component.yml" || \
    die "libsodium dependency missing"
rg -q 'memoria/device_identity\.cc' "$MEMORIA_UPSTREAM_DIR/main/CMakeLists.txt" || die "device identity is not in CMake"
rg -q 'memoria/memoria_audio_frame\.cc' "$MEMORIA_UPSTREAM_DIR/main/CMakeLists.txt" || die "audio frame is not in CMake"
"$python_bin" -m py_compile "$MEMORIA_FIRMWARE_ROOT/scripts/provision_identity.py"
rg -q '"write-flash"' "$MEMORIA_FIRMWARE_ROOT/scripts/provision_identity.py" && \
rg -q 'PARTITION_OFFSET = 0x10000' "$MEMORIA_FIRMWARE_ROOT/scripts/provision_identity.py" || \
    die "identity provisioning script does not pin the flash range"

if rg -n 'esp_video|EspVideo|GetCamera|InitializeCamera|CAM_PIN|OV_' \
    "$board_dir/memoria_esp_vocat.cc" "$board_dir/config.h"; then
    die "camera code/config leaked into Memoria board"
fi

rg -q 'memoria-esp-vocat' "$board_dir/config.json" || die "wrong board identity"
rg -q '^project\(memoria\)$' "$MEMORIA_UPSTREAM_DIR/CMakeLists.txt" || \
    die "Memoria build must use project(memoria)"
if rg -q '^project\(xiaozhi\)$' "$MEMORIA_UPSTREAM_DIR/CMakeLists.txt"; then
    die "upstream product project name leaked into Memoria build"
fi
rg -q 'config.ssid_prefix = "Memoria";' "$MEMORIA_UPSTREAM_DIR/main/boards/common/wifi_board.cc" || \
    die "Memoria Wi-Fi identity prefix is missing"
rg -q 'config.show_ota_config = false;' "$MEMORIA_UPSTREAM_DIR/main/boards/common/wifi_board.cc" || \
    die "Memoria Wi-Fi OTA configuration must be hidden"
rg -q '#if !CONFIG_BOARD_TYPE_MEMORIA_ESP_VOCAT' "$MEMORIA_UPSTREAM_DIR/main/mcp_server.cc" || \
    die "upstream firmware upgrade MCP tool is not removed for Memoria"
rg -q 'Memoria OTA is disabled' "$MEMORIA_UPSTREAM_DIR/main/application.cc" || \
    die "Application OTA must fail closed for Memoria"
"$python_bin" - "$board_dir/config.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    manifest = json.load(handle)
sdkconfig = set(manifest["builds"][0]["sdkconfig_append"])
required = {
    "CONFIG_USE_CUSTOM_WAKE_WORD=y",
    "CONFIG_OTA_URL=\"\"",
    'CONFIG_CUSTOM_WAKE_WORD="mo li"',
    'CONFIG_CUSTOM_WAKE_WORD_DISPLAY="茉莉"',
    "CONFIG_SEND_WAKE_WORD_DATA=n",
    "CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=n",
    "CONFIG_SR_WN_WN9L_NIHAOXIAOZHI_TTS3=n",
    "CONFIG_SR_MN_CN_MULTINET6_QUANT=y",
    "CONFIG_SR_NSN_WEBRTC=y",
}
missing = sorted(required - sdkconfig)
if missing:
    raise SystemExit("missing Memoria wake-word settings: " + ", ".join(missing))
if "CONFIG_SR_MN_CN_NONE=y" in sdkconfig:
    raise SystemExit("Memoria custom wake word cannot use CONFIG_SR_MN_CN_NONE=y")
if any(
    item.startswith("CONFIG_SR_WN_") and item.endswith("=y")
    for item in sdkconfig
):
    raise SystemExit("Memoria board must not enable an upstream Xiaozhi WakeNet model")
if any(
    item.startswith("CONFIG_SR_MN_CN_MULTINET")
    and item.endswith("=y")
    and item != "CONFIG_SR_MN_CN_MULTINET6_QUANT=y"
    for item in sdkconfig
):
    raise SystemExit("Memoria board must select only CONFIG_SR_MN_CN_MULTINET6_QUANT=y")
PY
(cd "$MEMORIA_UPSTREAM_DIR" && git diff --check)

board_json="$(cd "$MEMORIA_UPSTREAM_DIR" && "$python_bin" scripts/build.py --list-boards --json)"
printf '%s\n' "$board_json" | "$python_bin" -c '
import json
import sys

variants = json.load(sys.stdin)
match = [item for item in variants if item.get("name") == "memoria-esp-vocat"]
if len(match) != 1 or match[0].get("type") != "memoria-esp-vocat":
    raise SystemExit("Memoria board variant is not in upstream build manifest")
if match[0].get("target") != "esp32s3":
    raise SystemExit("Memoria board target is not esp32s3")
'

protocol_source="$MEMORIA_UPSTREAM_DIR/main/memoria/memoria_protocol.cc"
protocol_header="$MEMORIA_UPSTREAM_DIR/main/memoria/memoria_protocol.h"
frame_header="$MEMORIA_UPSTREAM_DIR/main/memoria/memoria_audio_frame.h"
[[ -f "$protocol_source" ]] || die "protocol v2 source missing"
[[ -f "$protocol_header" ]] || die "protocol v2 header missing"

# Device protocol v2 must declare the honest interrupt_assist capabilities the
# ESP-VoCat board actually has: capture stays open during playback, AEC is
# present but unverified, and there is no local stop keyword or duck. The
# server must not derive full duplex from this hello alone; playback precision
# is independently backed by the GDMA completion barrier.
rg -q 'cJSON_AddBoolToObject\(capabilities, "simultaneous_capture_playback", true\)' \
    "$protocol_source" || die "hello v2 must declare simultaneous capture/playback"
rg -q 'cJSON_AddStringToObject\(capabilities, "aec_mode", "fd_low_cost"\)' \
    "$protocol_source" || die "hello v2 must declare aec_mode fd_low_cost"
rg -q 'cJSON_AddStringToObject\(capabilities, "aec_reference", "software_post_gain_pre_i2s"\)' \
    "$protocol_source" || die "hello v2 must declare the software AEC reference"
rg -q 'cJSON_AddBoolToObject\(capabilities, "aec_reference_verified", false\)' \
    "$protocol_source" || die "hello v2 must declare AEC reference unverified"
rg -q 'cJSON_AddBoolToObject\(capabilities, "local_stop_keyword", false\)' \
    "$protocol_source" || die "hello v2 must declare no local stop keyword"
rg -q 'cJSON_AddBoolToObject\(capabilities, "local_duck", false\)' \
    "$protocol_source" || die "hello v2 must declare no local duck"
rg -q 'cJSON_AddStringToObject\(capabilities, "playback_watermark", "exact"\)' \
    "$protocol_source" || die "hello v2 must declare the exact digital playback watermark"
rg -q 'cJSON_AddNumberToObject\(capabilities, "barge_in_level", 1\)' \
    "$protocol_source" || die "hello v2 must declare barge_in_level 1"
rg -q '"downlink_sample_rates"' "$protocol_source" || die "hello v2 must negotiate downlink rates"
rg -q 'kDownlinkSampleRate16k' "$protocol_source" || die "hello v2 must declare the playable 16 kHz rate"
rg -q 'kDownlinkSampleRate24k' "$protocol_source" || die "hello v2 must declare the playable 24 kHz rate"

# The v2 control plane and the complete generation fence must be implemented.
rg -q '"session.accepted"' "$protocol_source" || die "session.accepted v2 parsing is missing"
rg -q 'audio_mode != "half_duplex_safe" && audio_mode != "interrupt_assist"' \
    "$protocol_source" || \
    die "session.accepted must reject modes outside the declared capability ladder"
rg -Fq 'SetHeader("X-Client-ID"' "$protocol_source" || \
    die "device WSS must use the strict X-Client-ID header"
rg -q '"current_fence"' "$protocol_source" || die "generation fence parsing is missing"
rg -q '"device_settings"' "$protocol_source" || die "signed device settings parsing is missing"
rg -q 'on_device_settings_received_' "$protocol_source" || \
    die "signed device settings are not wired to Application"
rg -q '"turn_id"' "$protocol_source" || die "generation fence turn_id is missing"
rg -q '"tool_epoch"' "$protocol_source" || die "generation fence tool_epoch is missing"
rg -q '"runtime_profile.invalidated"' "$protocol_source" || \
    die "runtime_profile.invalidated handling is missing"
rg -q '"immediate_fail_closed"' "$protocol_source" || \
    die "runtime_profile.invalidated fail-closed handling is missing"
rg -q '"next_safe_point"' "$protocol_source" || \
    die "runtime_profile.invalidated next_safe_point handling is missing"
rg -q 'dropped_frames_.stale' "$protocol_source" || \
    die "stale generation frames must be dropped and counted"
rg -q 'metadata.generation_id == 0' "$protocol_source" || \
    die "generation 0 must be handled as no valid playback"
rg -q '"button.stop"' "$protocol_source" || die "button.stop v2 is missing"
rg -q '"expected_fence"' "$protocol_source" || die "button.stop v2 fence is missing"
rg -q '"local_flush_sample_end"' "$protocol_source" || \
    die "button.stop v2 local flush watermark is missing"
rg -q '"rendered_sample_end"' "$protocol_source" || \
    die "playback receipts v2 watermark is missing"
rg -q 'cJSON_AddBoolToObject\(root.value, "approximate", approximate\)' "$protocol_source" || \
    die "playback receipts v2 must preserve source precision"
rg -q 'kDownlinkFrameSamples16k = 320' "$frame_header" || \
    die "audio frame layer must accept the negotiated 16 kHz downlink"
rg -q 'kDownlinkFrameSamples24k = 480' "$frame_header" || \
    die "audio frame layer must accept the negotiated 24 kHz downlink"

overlay_patch_0006="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0006-fence-simplex-listening-playback.patch"
[[ -f "$overlay_patch_0006" ]] || die "overlay patch 0006 is missing"
rg -Fq 'pending_listening_start_' "$overlay_patch_0006" || \
    die "patch 0006 must fence deferred listening start"
rg -Fq 'SendVadState(false)' "$overlay_patch_0006" || \
    die "patch 0006 must stop VAD before simplex playback"

overlay_patch_0007="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0007-device-media-v2-and-local-hard-stop.patch"
[[ -f "$overlay_patch_0007" ]] || die "overlay patch 0007 is missing"
rg -q 'void Application::AbortSpeaking' "$overlay_patch_0007" || \
    die "patch 0007 must implement the L0 local hard stop"
rg -q 'ResetDecoder' "$overlay_patch_0007" || \
    die "patch 0007 must flush playback synchronously on the button stop"

overlay_patch_0010="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0010-atomic-playback-generation-gate.patch"
[[ -f "$overlay_patch_0010" ]] || die "overlay patch 0010 is missing"
rg -q 'PushServerPacketToDecodeQueue' "$overlay_patch_0010" || \
    die "server audio must enter through the atomic generation gate"
rg -q 'packet->generation_id != accepted_server_generation_' "$overlay_patch_0010" || \
    die "the audio queue must recheck the server generation under its lock"
rg -q 'ResetDecoderForServerGeneration' "$overlay_patch_0010" || \
    die "P0 flush must atomically replace the audio generation gate"

# generation.completed is an ordered barrier in the audio FIFO. The device
# must treat it as completion-pending only: playback.ended, the legacy tts
# stop and the generation clear wait for the real AudioService drain (or a
# synchronous cancel flush), and the queue-idle probe finalizes immediately
# when the completion arrives on an already-empty queue.
overlay_patch_0008="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0008-device-media-v2-playback-completion-barrier.patch"
[[ -f "$overlay_patch_0008" ]] || die "overlay patch 0008 is missing"
rg -q 'generation.completed' "$protocol_source" || die "generation.completed v2 handling is missing"
rg -q 'playback_completion_pending_' "$protocol_source" || die "generation.completed must only mark completion pending"
rg -q 'is_playback_idle_' "$protocol_source" || die "completed generation must finalize through the Application idle probe"
rg -q 'void MemoriaProtocol::FinalizePlaybackEnded' "$protocol_source" || die "the drained-queue completion finalize is missing"
rg -q 'NotifyPlaybackOutput' "$protocol_source" || die "output-commit playback receipts are missing"
rg -q 'on_playback_output' "$overlay_patch_0008" || die "patch 0008 must wire output-commit watermark callbacks"
rg -q 'SetIsPlaybackIdleCallback' "$overlay_patch_0008" || die "patch 0008 must inject the AudioService idle probe"
rg -q 'not a network-received position' "$protocol_source" || die "v2 started/progress must not be emitted from the receive path"

# The ES8388 completion boundary is hardware-backed by I2S GDMA TX EOF. Live
# write-acceptance receipts stay approximate; only a full DMA-ring cycle may
# release the drain barrier and advertise an exact terminal watermark.
overlay_patch_0017="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0017-i2s-tx-eof-exact-playback-watermark.patch"
[[ -f "$overlay_patch_0017" ]] || die "overlay patch 0017 is missing"
rg -q 'i2s_channel_register_event_callback' "$overlay_patch_0017" || \
    die "ES8388 TX EOF callback is not registered"
rg -Fq 'OutputCompletionCounter() >= exact_output_target_' "$overlay_patch_0017" || \
    die "exact playback boundary is not gated by TX EOF"
rg -q '!exact_output_pending_' "$overlay_patch_0017" || \
    die "playback drain does not wait for exact output completion"
rg -q 'playback_watermark", "exact"' "$protocol_source" || \
    die "device hello does not advertise the exact digital playback watermark"
rg -q '"approximate", approximate' "$protocol_source" || \
    die "playback receipts do not preserve source precision"

overlay_patch_0009="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0009-preserve-session-on-network-reconnect.patch"
[[ -f "$overlay_patch_0009" ]] || die "overlay patch 0009 is missing"
rg -q 'CloseAudioChannel\(false\)' "$overlay_patch_0009" || \
    die "network loss must preserve the same resumable Session id"
rg -q '"resume_session_id"' "$protocol_source" || \
    die "device media ticket request must carry the resumable Session id"
rg -q 'session->stream_epoch <= requested_after_epoch' "$protocol_source" || \
    die "the device must reject a resume response without a strictly newer epoch"
overlay_patch_0011="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0011-recover-media-session.patch"
[[ -f "$overlay_patch_0011" ]] || die "overlay patch 0011 is missing"
rg -q 'kDeviceStateRecovering' "$overlay_patch_0011" || \
    die "network recovery must be an explicit device state"
rg -q 'kMediaReconnectMaxAttempts = 5' "$overlay_patch_0011" || \
    die "same-Session WSS recovery must be bounded"
rg -q 'kMediaReconnectCallbackGraceUs = 100000' "$overlay_patch_0011" || \
    die "passive WSS failure must not reopen on its callback return edge"
rg -q 'std::atomic<bool> media_network_connected_' "$overlay_patch_0011" || \
    die "coalesced network event bits must defer to a latest-observation authority"
rg -q 'protocol_->OpenAudioChannel\(\)' "$overlay_patch_0011" || \
    die "network recovery must actively request a newer stream epoch"
rg -q 'websocket_attempt_id_' "$protocol_source" || \
    die "late callbacks from an old WSS must be fenced from a newer epoch"
rg -q 'bool MemoriaProtocol::CanResumeSession' "$protocol_source" || \
    die "terminal close and resumable transport loss must be distinguishable"
rg -q 'saved_v2_resume' "$protocol_source" || \
    die "a saved v2 resume identity must survive the safe protocol-version reset"
rg -q 'audio_transport_attempt' "$overlay_patch_0011" || \
    die "queued old-epoch audio must be fenced from a recovered WSS"
rg -q 'json_transport_attempt' "$overlay_patch_0011" || \
    die "queued old-epoch JSON controls and UI must be fenced from RECOVERING"
rg -q 'bool MemoriaProtocol::RetireTransportAttempt' "$protocol_source" || \
    die "passive WSS failure must retire its transport authority immediately"
overlay_patch_0012="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0012-main-task-transport-actions.patch"
[[ -f "$overlay_patch_0012" ]] || die "overlay patch 0012 is missing"
rg -Fq 'MAIN_EVENT_MEMORIA_TRANSPORT' "$overlay_patch_0012" || \
    die "patch 0012 must schedule transport actions on the main task"
rg -Fq 'DrainTransportActions' "$overlay_patch_0012" || \
    die "patch 0012 must drain transport actions on the main task"
overlay_patch_0013="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0013-prioritize-media-close-recovery.patch"
[[ -f "$overlay_patch_0013" ]] || die "overlay patch 0013 is missing"
rg -q 'MAIN_EVENT_MEMORIA_MEDIA_CLOSED' "$overlay_patch_0013" || \
    die "transport close must have a dedicated main-task recovery event"
rg -q 'HandleMediaTransportClosedEvent' "$overlay_patch_0013" || \
    die "transport-close recovery state must be frozen before generic errors"
rg -q 'media_closed_transport_attempt_' "$overlay_patch_0013" || \
    die "transport-close recovery must retain an old-attempt fence"
overlay_patch_0014="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0014-load-memoria-assets-before-audio.patch"
[[ -f "$overlay_patch_0014" ]] || die "overlay patch 0014 is missing"
rg -q 'auto& assets = Assets::GetInstance()' "$overlay_patch_0014" || \
    die "Memoria activation must resolve the assets singleton before audio starts"
rg -q 'assets.partition_valid() || !assets.Apply()' "$overlay_patch_0014" || \
    die "Memoria activation must fail closed when local assets/models cannot load"
rg -q 'esp_srmodel_init\("model"\)' "$overlay_patch_0014" || \
    die "the assets patch must document the legacy model-partition fallback it prevents"
overlay_patch_0015="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0015-memoria-product-identity-and-ota-fail-closed.patch"
[[ -f "$overlay_patch_0015" ]] || die "overlay patch 0015 is missing"
rg -Fq 'project(memoria)' "$overlay_patch_0015" || \
    die "patch 0015 must set the Memoria product identity"
rg -Fq 'UpgradeFirmware' "$overlay_patch_0015" || \
    die "patch 0015 must override the upstream firmware upgrade path"
rg -Fq 'Memoria OTA is disabled' "$overlay_patch_0015" || \
    die "patch 0015 must fail closed instead of using upstream OTA"

# AFE noise suppression: the Memoria simplex board runs ESP-SR WebRTC NS
# inside the AFE pipeline so steady room noise is suppressed before the
# on-device VAD, the gateway energy VAD and the server denoiser.
overlay_patch_0022="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0022-enable-memoria-afe-noise-suppression.patch"
[[ -f "$overlay_patch_0022" ]] || die "overlay patch 0022 is missing"
rg -q 'ns_init = kUseAfeNoiseSuppression' "$overlay_patch_0022" || \
    die "patch 0022 must gate AFE noise suppression behind the Memoria board switch"
rg -q 'AFE_NS_MODE_WEBRTC' "$overlay_patch_0022" || \
    die "patch 0022 must pin the WebRTC noise suppression mode"
afe_engine="$MEMORIA_UPSTREAM_DIR/main/audio/engines/afe_audio_engine.cc"
[[ -f "$afe_engine" ]] || die "AFE engine source missing"
rg -q 'ns_init = kUseAfeNoiseSuppression' "$afe_engine" || \
    die "AFE noise suppression must be wired for the Memoria build"

# Queue overflow is backpressure, not a terminal playback.error. Playback and
# Opus live on core 1 so AFE capture on core 0 cannot stall a long reply.
overlay_patch_0023="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0023-do-not-terminal-decode-queue-overflow.patch"
[[ -f "$overlay_patch_0023" ]] || die "overlay patch 0023 is missing"
rg -q 'kServerPacketQueueFull' "$overlay_patch_0023" || \
    die "patch 0023 must distinguish decode-queue overflow from decode failure"
rg -Fq 'Dropping server packet; decode queue full' "$overlay_patch_0023" || \
    die "patch 0023 must drop overflow frames instead of ending the reply"
rg -q 'xTaskCreatePinnedToCore' "$overlay_patch_0023" || \
    die "patch 0023 must pin VoCat playback off the AFE core"
application_source="$MEMORIA_UPSTREAM_DIR/main/application.cc"
audio_service_source="$MEMORIA_UPSTREAM_DIR/main/audio/audio_service.cc"
rg -q 'kServerPacketQueueFull' "$application_source" || \
    die "applied application.cc must not terminal-fail on decode queue overflow"
rg -Fq 'Dropping server packet; decode queue full' "$audio_service_source" || \
    die "applied audio_service.cc must log and drop decode queue overflow"

# interrupt_assist keeps capture open during playback. 0006's simplex VAD fence
# would swallow vad.start while Speaking, so an owner barge-in never reaches
# the Agent. 0024 reopens vad.start only for negotiated interrupt_assist /
# full_duplex_verified; vad.end stays unconditional. This is not a full-duplex
# product claim.
overlay_patch_0024="$MEMORIA_FIRMWARE_ROOT/overlay/patches/0024-send-vad-start-during-interrupt-assist-playback.patch"
[[ -f "$overlay_patch_0024" ]] || die "overlay patch 0024 is missing"
rg -q 'AllowsPlaybackBargeIn' "$overlay_patch_0024" || \
    die "patch 0024 must consult AllowsPlaybackBargeIn before playback vad.start"
rg -q 'speaking_barge_in' "$overlay_patch_0024" || \
    die "patch 0024 must name the playback barge-in VAD exception"
rg -Fq '(!speaking || listening || speaking_barge_in)' "$overlay_patch_0024" || \
    die "patch 0024 must keep vad.end unconditional while allowing playback vad.start"
rg -q 'bool MemoriaProtocol::AllowsPlaybackBargeIn' "$protocol_source" || \
    die "protocol must expose AllowsPlaybackBargeIn for the playback VAD exception"
rg -q 'AllowsPlaybackBargeIn' "$application_source" || \
    die "applied application.cc must send vad.start during interrupt_assist playback"

# The 20 s device VAD hard fence is the tuned field value; a silent drift
# back to the older 10 s bound would change conversation closure behavior.
rg -Fq 'kMaxVadSpeechSamples = static_cast<uint64_t>(kUplinkSampleRate) * 20' \
    "$protocol_source" || die "device VAD hard fence must stay at 20 s"

sdkconfig="$MEMORIA_UPSTREAM_DIR/sdkconfig"
metadata="$MEMORIA_UPSTREAM_DIR/build/project_description.json"
flasher_args="$MEMORIA_UPSTREAM_DIR/build/flasher_args.json"
[[ -s "$sdkconfig" ]] || die "final sdkconfig is missing"
rg -q '^CONFIG_OTA_URL=""$' "$sdkconfig" || die "final sdkconfig still exposes an OTA endpoint"
if rg -n 'api\.tenclass\.net/xiaozhi/ota|CONFIG_OTA_URL="https?://' "$sdkconfig"; then
    die "upstream OTA endpoint leaked into final sdkconfig"
fi
[[ -s "$metadata" ]] || die "build metadata is missing; run build.sh before this check"
[[ -s "$flasher_args" ]] || die "flasher arguments are missing; run build.sh before this check"
"$python_bin" - "$metadata" "$flasher_args" <<'PY'
import json
import pathlib
import sys

metadata_path = pathlib.Path(sys.argv[1])
flasher_path = pathlib.Path(sys.argv[2])
with metadata_path.open(encoding="utf-8") as handle:
    metadata = json.load(handle)
with flasher_path.open(encoding="utf-8") as handle:
    flasher = json.load(handle)
if metadata.get("project_name") != "memoria":
    raise SystemExit("build metadata project_name is not memoria")
app_bin = metadata.get("app_bin")
if not isinstance(app_bin, str) or app_bin == "xiaozhi.bin" or pathlib.Path(app_bin).name != app_bin:
    raise SystemExit(f"invalid product app_bin: {app_bin!r}")
flash_files = flasher.get("flash_files", {})
if "0x10000" in flash_files:
    raise SystemExit("flash plan would overwrite the protected identity partition")
if any("xiaozhi" in str(value).casefold() for value in flash_files.values()):
    raise SystemExit("flash plan exposes an upstream Xiaozhi image")
PY
if find "$MEMORIA_ARTIFACT_DIR" -maxdepth 1 -type f -name '*-xiaozhi.bin' -print -quit | rg -q .; then
    die "upstream-named product artifact remains in artifacts/"
fi
printf 'overlay check passed: %s\n' "$MEMORIA_BOARD_NAME"
