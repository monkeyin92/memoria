package mediaedge

// Hardware device WSS protocol seam for the public device-media-v2 contract.
// Text frames carry versioned JSON control messages; binary frames carry the
// 30-byte MemoriaAudioFrameV1 header plus Opus payload. This direct endpoint
// is v2-only; legacy v1 sessions remain on the livekit_compat gateway.

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"time"
)

const (
	DeviceFrameVersion             = 1
	DeviceFrameHeaderSize          = 30
	DeviceDirectionUplink          = 1
	DeviceDirectionDownlink        = 2
	DeviceUplinkFrameSamples       = 320 // 16 kHz * 20 ms
	DeviceDownlinkFrameSamples16k  = 320
	DeviceDownlinkFrameSamples24k  = 480
	DeviceMaxControlBytes          = 16 * 1024
	DeviceMaxAudioPayloadBytes     = 2048
	DeviceMaxAudioFrameBytes       = DeviceFrameHeaderSize + DeviceMaxAudioPayloadBytes
	DeviceHelloTimeout             = 10 * time.Second
	DeviceControlSequenceMax       = 1<<32 - 1
	DeviceInteractionAuthority     = "python_authoritative"
	DeviceAudioModeHalfDuplexSafe  = "half_duplex_safe"
	DeviceAudioModeInterruptAssist = "interrupt_assist"
	DeviceAudioModeFullDuplex      = "full_duplex_verified"
	// DeviceFrameFlagDiscontinuity is header flags bit 0 (0x0001) of
	// device-media-v2: a downlink-only marking the edge sets at socket-write
	// time on the first frame after a same-generation forward gap caused by
	// queue drops. Uplink frames must carry flags 0.
	DeviceFrameFlagDiscontinuity uint16 = 0x0001
)

// MemoriaAudioFrameV1 is the golden 30-byte network-order header plus Opus
// payload defined in packages/contracts/device-media-v2.json. Version 1
// frames carry generation_id 0 (uplink) or the authoritative generation
// (downlink); no mapping onto a client-side "device generation" is allowed.
type MemoriaAudioFrameV1 struct {
	Version      uint8
	Direction    uint8
	Flags        uint16
	StreamEpoch  uint32
	Sequence     uint32
	SampleStart  uint64
	FrameSamples uint32
	GenerationID uint32
	Payload      []byte
}

func (f MemoriaAudioFrameV1) MarshalBinary() ([]byte, error) {
	if len(f.Payload) > DeviceMaxAudioPayloadBytes {
		return nil, fmt.Errorf("device audio payload exceeds bound")
	}
	header := make([]byte, DeviceFrameHeaderSize)
	header[0] = f.Version
	header[1] = f.Direction
	binary.BigEndian.PutUint16(header[2:4], f.Flags)
	binary.BigEndian.PutUint32(header[4:8], f.StreamEpoch)
	binary.BigEndian.PutUint32(header[8:12], f.Sequence)
	binary.BigEndian.PutUint64(header[12:20], f.SampleStart)
	binary.BigEndian.PutUint32(header[20:24], f.FrameSamples)
	binary.BigEndian.PutUint32(header[24:28], f.GenerationID)
	binary.BigEndian.PutUint16(header[28:30], uint16(len(f.Payload)))
	return append(header, f.Payload...), nil
}

func (f *MemoriaAudioFrameV1) UnmarshalBinary(data []byte) error {
	if len(data) < DeviceFrameHeaderSize {
		return fmt.Errorf("device audio frame header is incomplete")
	}
	payloadSize := int(binary.BigEndian.Uint16(data[28:30]))
	if len(data) != DeviceFrameHeaderSize+payloadSize {
		return fmt.Errorf("device audio payload length does not match header")
	}
	f.Version = data[0]
	f.Direction = data[1]
	f.Flags = binary.BigEndian.Uint16(data[2:4])
	f.StreamEpoch = binary.BigEndian.Uint32(data[4:8])
	f.Sequence = binary.BigEndian.Uint32(data[8:12])
	f.SampleStart = binary.BigEndian.Uint64(data[12:20])
	f.FrameSamples = binary.BigEndian.Uint32(data[20:24])
	f.GenerationID = binary.BigEndian.Uint32(data[24:28])
	f.Payload = append([]byte(nil), data[DeviceFrameHeaderSize:]...)
	return nil
}

// Validate checks the header against the negotiated connection identity.
// Uplink frames must be 20 ms / 320 samples at 16 kHz with generation 0;
// downlink frame shapes are validated by the encoder, not by this method.
func (f MemoriaAudioFrameV1) Validate(expectedEpoch uint32, direction uint8) error {
	if f.Version != DeviceFrameVersion {
		return fmt.Errorf("device frame version is unsupported")
	}
	if f.Direction != direction {
		return fmt.Errorf("device frame direction does not match")
	}
	if f.StreamEpoch != expectedEpoch {
		return fmt.Errorf("device frame stream epoch does not match")
	}
	if len(f.Payload) == 0 || len(f.Payload) > DeviceMaxAudioPayloadBytes {
		return fmt.Errorf("device frame payload is out of bounds")
	}
	if direction == DeviceDirectionUplink {
		if f.Flags != 0 {
			return fmt.Errorf("device uplink frame flags must be zero")
		}
		if f.FrameSamples != DeviceUplinkFrameSamples {
			return fmt.Errorf("device uplink frame must be %d samples", DeviceUplinkFrameSamples)
		}
		if f.GenerationID != 0 {
			return fmt.Errorf("device uplink generation must be zero")
		}
	} else if f.Flags&^DeviceFrameFlagDiscontinuity != 0 {
		// Only the discontinuity bit is defined for downlink; any other bit
		// would be a forged marker and is rejected at the wire boundary.
		return fmt.Errorf("device downlink frame flags contain unsupported bits")
	}
	return nil
}

// setDeviceFrameDiscontinuity marks header flags bit 0 on a marshaled
// downlink frame. It is applied at socket-write time, after the frame has
// left the queue, so the flag is set exactly when the device observes the
// gap (queue drops removed the frames in between). Returns false when the
// buffer is not a downlink audio frame.
func setDeviceFrameDiscontinuity(wire []byte) bool {
	if len(wire) < DeviceFrameHeaderSize || wire[1] != DeviceDirectionDownlink {
		return false
	}
	flags := binary.BigEndian.Uint16(wire[2:4])
	binary.BigEndian.PutUint16(wire[2:4], flags|DeviceFrameFlagDiscontinuity)
	return true
}

// deviceFence is the generation_fence object of device-media-v2.
type deviceFence struct {
	TurnID       uint64 `json:"turn_id"`
	GenerationID uint64 `json:"generation_id"`
	ToolEpoch    uint64 `json:"tool_epoch"`
	SessionEpoch uint64 `json:"session_epoch"`
}

func (f deviceFence) isZero() bool {
	return f.TurnID == 0 && f.GenerationID == 0 && f.ToolEpoch == 0 && f.SessionEpoch == 0
}

func (f deviceFence) valid() bool {
	return f.TurnID > 0 && f.GenerationID > 0 &&
		f.SessionEpoch > 0 &&
		f.TurnID <= 1<<32-1 && f.GenerationID <= 1<<32-1 &&
		f.ToolEpoch <= 1<<32-1 && f.SessionEpoch <= 1<<32-1
}

func (f deviceFence) toFence(sessionID string) Fence {
	return Fence{SessionID: sessionID, TurnID: f.TurnID, GenerationID: f.GenerationID, ToolEpoch: f.ToolEpoch, SessionEpoch: f.SessionEpoch}
}

func fenceToDevice(fence Fence) deviceFence {
	return deviceFence{TurnID: fence.TurnID, GenerationID: fence.GenerationID, ToolEpoch: fence.ToolEpoch, SessionEpoch: fence.SessionEpoch}
}

func validateDeviceIdentifier(value, field string) error {
	if value == "" || len(value) > 128 {
		return fmt.Errorf("%s is required", field)
	}
	for index, char := range value {
		allowed := char >= 'a' && char <= 'z' || char >= 'A' && char <= 'Z' ||
			char >= '0' && char <= '9' || char == '.' || char == '_' || char == ':' || char == '-'
		if !allowed || (index == 0 && !(char >= 'a' && char <= 'z' || char >= 'A' && char <= 'Z' || char >= '0' && char <= '9')) {
			return fmt.Errorf("%s contains an invalid character", field)
		}
	}
	return nil
}

// deviceHelloV1 is the legacy hello; it only ever downgrades to half-duplex.
type deviceHelloV1 struct {
	Type            string                   `json:"type"`
	Version         uint64                   `json:"version"`
	DeviceID        string                   `json:"device_id"`
	FirmwareVersion string                   `json:"firmware_version"`
	BoardProfile    string                   `json:"board_profile"`
	StreamEpoch     uint64                   `json:"stream_epoch"`
	Audio           deviceLegacyAudio        `json:"audio"`
	Capabilities    deviceLegacyCapabilities `json:"capabilities"`
}

type deviceLegacyAudio struct {
	UplinkCodec        string `json:"uplink_codec"`
	UplinkSampleRate   uint64 `json:"uplink_sample_rate"`
	DownlinkSampleRate uint64 `json:"downlink_sample_rate"`
	Channels           uint64 `json:"channels"`
	FrameMS            uint64 `json:"frame_ms"`
}

type deviceLegacyCapabilities struct {
	Display        bool `json:"display"`
	Microphone     bool `json:"microphone"`
	Speaker        bool `json:"speaker"`
	DeviceAEC      bool `json:"device_aec"`
	PhysicalButton bool `json:"physical_button"`
}

// deviceHelloV2 is the first-class hardware contract.
type deviceHelloV2 struct {
	Type            string               `json:"type"`
	Version         uint64               `json:"version"`
	DeviceID        string               `json:"device_id"`
	FirmwareVersion string               `json:"firmware_version"`
	BoardProfile    string               `json:"board_profile"`
	StreamEpoch     uint64               `json:"stream_epoch"`
	Audio           deviceAudioV2        `json:"audio"`
	Capabilities    deviceCapabilitiesV2 `json:"capabilities"`
}

type deviceAudioV2 struct {
	UplinkCodec         string   `json:"uplink_codec"`
	UplinkSampleRate    uint64   `json:"uplink_sample_rate"`
	DownlinkSampleRates []uint64 `json:"downlink_sample_rates"`
	Channels            uint64   `json:"channels"`
	FrameMS             uint64   `json:"frame_ms"`
}

type deviceCapabilitiesV2 struct {
	Display                     bool   `json:"display"`
	Microphone                  bool   `json:"microphone"`
	Speaker                     bool   `json:"speaker"`
	SimultaneousCapturePlayback bool   `json:"simultaneous_capture_playback"`
	AECMode                     string `json:"aec_mode"`
	AECReference                string `json:"aec_reference"`
	AECReferenceVerified        bool   `json:"aec_reference_verified"`
	LocalVAD                    bool   `json:"local_vad"`
	LocalStopKeyword            bool   `json:"local_stop_keyword"`
	PhysicalStopButton          bool   `json:"physical_stop_button"`
	PlaybackWatermark           string `json:"playback_watermark"`
	LocalDuck                   bool   `json:"local_duck"`
	BargeInLevel                uint64 `json:"barge_in_level"`
}

// deviceControlEnvelope is the shared text-frame envelope; typed validation
// happens per message kind.
type deviceControlEnvelope struct {
	Type    string          `json:"type"`
	Version json.RawMessage `json:"version"`
	Raw     json.RawMessage
}

func parseDeviceControl(data []byte) (deviceControlEnvelope, error) {
	if len(data) == 0 || len(data) > DeviceMaxControlBytes {
		return deviceControlEnvelope{}, fmt.Errorf("control message is out of bounds")
	}
	var envelope struct {
		Type    string          `json:"type"`
		Version json.RawMessage `json:"version"`
	}
	decoder := json.NewDecoder(bytes.NewReader(data))
	if err := decoder.Decode(&envelope); err != nil {
		return deviceControlEnvelope{}, fmt.Errorf("control message is not valid JSON")
	}
	if err := requireJSONEOF(decoder); err != nil {
		return deviceControlEnvelope{}, fmt.Errorf("control message is not one JSON value")
	}
	if envelope.Type == "" {
		return deviceControlEnvelope{}, fmt.Errorf("control message type is required")
	}
	return deviceControlEnvelope{Type: envelope.Type, Version: envelope.Version, Raw: append([]byte(nil), data...)}, nil
}

func jsonUnmarshalStrict(data []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	return requireJSONEOF(decoder)
}

func requireJSONEOF(decoder *json.Decoder) error {
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return fmt.Errorf("unexpected trailing JSON value")
		}
		return err
	}
	return nil
}

func requireVersion(raw json.RawMessage, expected uint64) error {
	var version uint64
	if err := json.Unmarshal(raw, &version); err != nil || version != expected {
		return fmt.Errorf("control message version must be %d", expected)
	}
	return nil
}

// deviceEventBase carries the fields shared by every device->server event.
type deviceEventBase struct {
	Type              string `json:"type"`
	Version           uint64 `json:"version"`
	StreamEpoch       uint64 `json:"stream_epoch"`
	ControlSequence   uint64 `json:"control_sequence"`
	DeviceMonotonicMS uint64 `json:"device_monotonic_ms"`
}

func (b deviceEventBase) validate() error {
	if b.Version != 2 {
		return fmt.Errorf("device event version must be 2")
	}
	if b.StreamEpoch == 0 {
		return fmt.Errorf("device event stream_epoch is required")
	}
	if b.ControlSequence > DeviceControlSequenceMax {
		return fmt.Errorf("device event control_sequence is out of bounds")
	}
	return nil
}

type deviceVADEvent struct {
	deviceEventBase
	SamplePosition  uint64  `json:"sample_position"`
	VoicedEndSample uint64  `json:"voiced_end_sample,omitempty"`
	Probability     float64 `json:"probability"`
	NearEndRMS      float64 `json:"near_end_rms"`
}

type deviceKeywordEvent struct {
	deviceEventBase
	KeywordID     string                     `json:"keyword_id"`
	Confidence    float64                    `json:"confidence"`
	HardStop      bool                       `json:"hard_stop"`
	Evidence      deviceInterruptionEvidence `json:"evidence"`
	ExpectedFence deviceFence                `json:"expected_fence"`
}

type deviceInterruptionEvidence struct {
	DetectedSample uint64   `json:"detected_sample"`
	Source         string   `json:"source"`
	DurationMS     uint64   `json:"duration_ms"`
	AECMode        string   `json:"aec_mode"`
	AECVerified    bool     `json:"aec_verified"`
	VADProbability float64  `json:"vad_probability"`
	NearEndRMS     float64  `json:"near_end_rms"`
	FarEndRMS      float64  `json:"far_end_rms"`
	ResidualEcho   *float64 `json:"residual_echo_score,omitempty"`
	SpeakerClass   string   `json:"speaker_class"`
	ASRPrefix      *string  `json:"asr_prefix,omitempty"`
}

type deviceButtonStop struct {
	deviceEventBase
	ExpectedFence       deviceFence `json:"expected_fence"`
	LocalFlushSampleEnd uint64      `json:"local_flush_sample_end"`
}

type devicePlaybackReceipt struct {
	deviceEventBase
	Fence             deviceFence `json:"fence"`
	ReceivedSequence  uint64      `json:"received_sequence"`
	RenderedSampleEnd uint64      `json:"rendered_sample_end"`
	Approximate       bool        `json:"approximate"`
}

type deviceStateEvent struct {
	deviceEventBase
	Value map[string]any `json:"value"`
}

type deviceSessionClose struct {
	deviceEventBase
	Value struct {
		Reason string `json:"reason"`
	} `json:"value"`
}

type deviceRuntimeProfileApplied struct {
	deviceEventBase
	ProfileVersion  uint64 `json:"profile_version"`
	SettingsVersion uint64 `json:"settings_version"`
}

// deviceControlPriority maps message kinds onto the contract priority lanes:
// P0 flush/cancel/close, P1 state/VAD/KWS/playback facts, P2 audio, P3
// subtitle/expression/telemetry.
func deviceControlPriority(messageType string) int {
	switch messageType {
	case "playback.flush", "generation.cancelled", "session.close", "session.error":
		return 0
	case "session.accepted", "generation.started", "generation.pause",
		"generation.resume", "playback.duck",
		"runtime_profile.invalidated", "vad.start", "vad.end",
		"runtime_profile.applied",
		"keyword.detected", "button.stop", "playback.started",
		"playback.progress", "playback.ended", "playback.error",
		"device.mute_changed":
		return 1
	case "assistant.audio.frame", "generation.completed":
		return 2
	case "screen.subtitle", "screen.expression", "device.telemetry":
		return 3
	default:
		return 3
	}
}

func isDeviceControlType(messageType string) bool {
	switch messageType {
	case "device.hello", "vad.start", "vad.end", "keyword.detected",
		"button.stop", "playback.started", "playback.progress",
		"playback.ended", "playback.error", "device.mute_changed",
		"device.network_changed", "device.telemetry", "runtime_profile.applied",
		"session.close":
		return true
	}
	return false
}

func validUnitInterval(value float64) bool {
	return value >= 0 && value <= 1
}

// Server-to-device control messages.

func marshalDeviceControl(value any) ([]byte, error) {
	return json.Marshal(value)
}

type deviceSessionAccepted struct {
	Type                    string                     `json:"type"`
	Version                 uint64                     `json:"version"`
	SessionID               string                     `json:"session_id"`
	StreamEpoch             uint64                     `json:"stream_epoch"`
	InteractionAuthority    string                     `json:"interaction_authority"`
	AudioMode               string                     `json:"audio_mode"`
	DownlinkSampleRate      uint64                     `json:"downlink_sample_rate"`
	RuntimeProfileVersion   uint64                     `json:"runtime_profile_version"`
	DeviceSettings          DeviceSettingsClaim        `json:"device_settings"`
	CurrentFence            *deviceFence               `json:"current_fence"`
	CurrentGenerationActive bool                       `json:"current_generation_active"`
	AcousticAttestation     *deviceAcousticAttestation `json:"acoustic_attestation"`
}

type deviceAcousticAttestation struct {
	Verified       bool   `json:"verified"`
	BoardProfile   string `json:"board_profile"`
	ProfileVersion uint64 `json:"profile_version"`
}

type deviceGenerationControl struct {
	Type              string      `json:"type"`
	Version           uint64      `json:"version"`
	SessionID         string      `json:"session_id"`
	StreamEpoch       uint64      `json:"stream_epoch"`
	ControlSequence   uint64      `json:"control_sequence"`
	ServerMonotonicMS uint64      `json:"server_monotonic_ms"`
	Fence             deviceFence `json:"fence"`
	Reason            string      `json:"reason"`
}

type devicePlaybackControl struct {
	Type                  string      `json:"type"`
	Version               uint64      `json:"version"`
	SessionID             string      `json:"session_id"`
	StreamEpoch           uint64      `json:"stream_epoch"`
	ControlSequence       uint64      `json:"control_sequence"`
	ServerMonotonicMS     uint64      `json:"server_monotonic_ms"`
	Fence                 deviceFence `json:"fence"`
	ReplacementGeneration uint64      `json:"replacement_generation_id"`
	DuckDB                float64     `json:"duck_db"`
}

type deviceSessionError struct {
	Type              string `json:"type"`
	Version           uint64 `json:"version"`
	SessionID         string `json:"session_id"`
	StreamEpoch       uint64 `json:"stream_epoch"`
	ControlSequence   uint64 `json:"control_sequence"`
	ServerMonotonicMS uint64 `json:"server_monotonic_ms"`
	Code              string `json:"code"`
	Retryable         bool   `json:"retryable"`
}

type deviceServerSessionClose struct {
	Type              string `json:"type"`
	Version           uint64 `json:"version"`
	SessionID         string `json:"session_id"`
	StreamEpoch       uint64 `json:"stream_epoch"`
	ControlSequence   uint64 `json:"control_sequence"`
	ServerMonotonicMS uint64 `json:"server_monotonic_ms"`
	Reason            string `json:"reason,omitempty"`
}

type deviceScreenExpression struct {
	Type              string      `json:"type"`
	Version           uint64      `json:"version"`
	SessionID         string      `json:"session_id"`
	StreamEpoch       uint64      `json:"stream_epoch"`
	ControlSequence   uint64      `json:"control_sequence"`
	ServerMonotonicMS uint64      `json:"server_monotonic_ms"`
	Fence             deviceFence `json:"fence"`
	Expression        string      `json:"expression"`
}

func deviceScreenEmotion(expression string) string {
	switch expression {
	case "happy", "sad", "surprised", "neutral", "angry", "thinking", "loving":
		return expression
	case "curious":
		return "thinking"
	case "caring":
		return "loving"
	default:
		return "neutral"
	}
}

type deviceRuntimeProfileInvalidated struct {
	Type              string `json:"type"`
	Version           uint64 `json:"version"`
	SessionID         string `json:"session_id"`
	StreamEpoch       uint64 `json:"stream_epoch"`
	ControlSequence   uint64 `json:"control_sequence"`
	ServerMonotonicMS uint64 `json:"server_monotonic_ms"`
	ProfileVersion    uint64 `json:"profile_version"`
	ApplyAt           string `json:"apply_at"`
}
