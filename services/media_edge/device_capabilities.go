package mediaedge

// Device acoustic registry and audio-mode resolution. The server registry,
// not the device self-report, decides whether a board may open
// full_duplex_verified. Legacy v1 helpers remain contract fixtures for the
// separate livekit_compat gateway and never enter the direct WSS endpoint.

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"strings"
)

type DeviceAcousticProfile struct {
	BoardProfile    string `json:"board_profile"`
	ProfileVersion  uint64 `json:"profile_version"`
	Verified        bool   `json:"verified"`
	MaxBargeInLevel uint64 `json:"max_barge_in_level"`
}

// DeviceAcousticRegistry is the server-authoritative acoustic capability
// table (contract section 10.4). Absence of an entry never opens full
// duplex; it only downgrades.
type DeviceAcousticRegistry struct {
	profiles map[string]DeviceAcousticProfile
}

func NewDeviceAcousticRegistry() *DeviceAcousticRegistry {
	return &DeviceAcousticRegistry{profiles: make(map[string]DeviceAcousticProfile)}
}

func LoadDeviceAcousticRegistry(path string) (*DeviceAcousticRegistry, error) {
	registry := NewDeviceAcousticRegistry()
	if strings.TrimSpace(path) == "" {
		return registry, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read device acoustic registry: %w", err)
	}
	var entries []DeviceAcousticProfile
	if err := json.Unmarshal(data, &entries); err != nil {
		return nil, fmt.Errorf("parse device acoustic registry: %w", err)
	}
	for _, entry := range entries {
		if err := validateDeviceIdentifier(entry.BoardProfile, "board_profile"); err != nil {
			return nil, fmt.Errorf("device acoustic registry board_profile: %w", err)
		}
		if entry.ProfileVersion == 0 || entry.ProfileVersion > math.MaxUint32 {
			return nil, fmt.Errorf("device acoustic registry profile_version must be a positive uint32")
		}
		registry.profiles[entry.BoardProfile] = entry
	}
	return registry, nil
}

func (r *DeviceAcousticRegistry) Lookup(boardProfile string) (DeviceAcousticProfile, bool) {
	if r == nil {
		return DeviceAcousticProfile{}, false
	}
	profile, ok := r.profiles[boardProfile]
	return profile, ok
}

func (r *DeviceAcousticRegistry) Add(profile DeviceAcousticProfile) {
	if r == nil || profile.BoardProfile == "" || profile.ProfileVersion == 0 ||
		profile.ProfileVersion > math.MaxUint32 {
		return
	}
	r.profiles[profile.BoardProfile] = profile
}

// ResolveAudioModeV2 decides the hardware ceiling from the v2 hello and the
// server registry:
//   - full_duplex_verified only when the registry attests the board profile
//     AND the device itself reports a verified AEC reference and simultaneous
//     capture/playback;
//   - interrupt_assist when the device has a physical stop button, a local
//     stop keyword or local VAD;
//   - half_duplex_safe otherwise.
func (r *DeviceAcousticRegistry) ResolveAudioModeV2(hello deviceHelloV2) (string, error) {
	capabilities := hello.Capabilities
	profile, registered := r.Lookup(hello.BoardProfile)
	if registered && profile.Verified && capabilities.SimultaneousCapturePlayback &&
		capabilities.AECMode != "none" && capabilities.AECReference != "none" &&
		capabilities.AECReferenceVerified && capabilities.BargeInLevel > 0 &&
		capabilities.BargeInLevel <= profile.MaxBargeInLevel {
		return DeviceAudioModeFullDuplex, nil
	}
	if capabilities.PhysicalStopButton || capabilities.LocalStopKeyword || capabilities.LocalVAD {
		return DeviceAudioModeInterruptAssist, nil
	}
	return DeviceAudioModeHalfDuplexSafe, nil
}

// applyBargeInPolicy intersects the hardware ceiling with the signed source
// allowlist. The allowlist never raises capability: it can only preserve or
// lower the result negotiated from server acoustic evidence and device facts.
func applyBargeInPolicy(mode string, allowed []string) string {
	allowButton, allowKeyword, allowVoice := false, false, false
	for _, source := range allowed {
		switch source {
		case "button":
			allowButton = true
		case "keyword":
			allowKeyword = true
		case "voice":
			allowVoice = true
		case "none":
			return DeviceAudioModeHalfDuplexSafe
		}
	}
	if mode == DeviceAudioModeFullDuplex && !allowVoice {
		mode = DeviceAudioModeInterruptAssist
	}
	if mode == DeviceAudioModeInterruptAssist && !(allowButton || allowKeyword || allowVoice) {
		return DeviceAudioModeHalfDuplexSafe
	}
	return mode
}

// ResolveDownlinkRate picks 16 kHz whenever the v2 audio contract offers it
// (the contract requires it), otherwise 24 kHz for legacy fixture callers.
func ResolveDownlinkRate(helloVersion uint64, audio deviceAudioV2) uint64 {
	if helloVersion >= 2 {
		for _, rate := range audio.DownlinkSampleRates {
			if rate == 16_000 {
				return 16_000
			}
		}
	}
	return 24_000
}

func (h deviceHelloV1) validate(expectedDeviceID string, expectedEpoch uint64) error {
	if h.Type != "device.hello" {
		return fmt.Errorf("hello type must be device.hello")
	}
	if h.Version != 1 {
		return fmt.Errorf("hello version must be 1")
	}
	if h.DeviceID != expectedDeviceID {
		return fmt.Errorf("hello device_id does not match the media token")
	}
	if h.StreamEpoch != expectedEpoch {
		return fmt.Errorf("hello stream_epoch does not match the media token")
	}
	if err := validateDeviceIdentifier(h.DeviceID, "device_id"); err != nil {
		return err
	}
	if err := validateDeviceIdentifier(h.BoardProfile, "board_profile"); err != nil {
		return err
	}
	if h.FirmwareVersion == "" || len(h.FirmwareVersion) > 128 {
		return fmt.Errorf("firmware_version is required")
	}
	audio := h.Audio
	if audio.UplinkCodec != "opus" || audio.UplinkSampleRate != 16_000 ||
		audio.DownlinkSampleRate != 24_000 || audio.Channels != 1 || audio.FrameMS != 20 {
		return fmt.Errorf("legacy hello audio must be opus 16 kHz uplink, 24 kHz downlink, mono, 20 ms")
	}
	return nil
}

func (h deviceHelloV2) validate(expectedDeviceID string, expectedEpoch uint64) error {
	if h.Type != "device.hello" {
		return fmt.Errorf("hello type must be device.hello")
	}
	if h.Version != 2 {
		return fmt.Errorf("hello version must be 2")
	}
	if h.DeviceID != expectedDeviceID {
		return fmt.Errorf("hello device_id does not match the media token")
	}
	if h.StreamEpoch != expectedEpoch {
		return fmt.Errorf("hello stream_epoch does not match the media token")
	}
	if err := validateDeviceIdentifier(h.DeviceID, "device_id"); err != nil {
		return err
	}
	if err := validateDeviceIdentifier(h.BoardProfile, "board_profile"); err != nil {
		return err
	}
	if h.FirmwareVersion == "" || len(h.FirmwareVersion) > 128 {
		return fmt.Errorf("firmware_version is required")
	}
	audio := h.Audio
	if audio.UplinkCodec != "opus" || audio.UplinkSampleRate != 16_000 ||
		audio.Channels != 1 || audio.FrameMS != 20 {
		return fmt.Errorf("v2 hello audio must be opus 16 kHz mono 20 ms")
	}
	if len(audio.DownlinkSampleRates) == 0 || len(audio.DownlinkSampleRates) > 2 {
		return fmt.Errorf("v2 hello downlink_sample_rates must list 1-2 rates")
	}
	has16k := false
	for _, rate := range audio.DownlinkSampleRates {
		if rate != 16_000 && rate != 24_000 {
			return fmt.Errorf("v2 hello downlink_sample_rates must be 16000 or 24000")
		}
		if rate == 16_000 {
			has16k = true
		}
	}
	if !has16k {
		return fmt.Errorf("v2 hello downlink_sample_rates must contain 16000")
	}
	capabilities := h.Capabilities
	switch capabilities.AECMode {
	case "none", "fd_low_cost", "fd_high_quality":
	default:
		return fmt.Errorf("v2 hello aec_mode is invalid")
	}
	switch capabilities.AECReference {
	case "none", "software_post_gain_pre_i2s", "hardware_loopback":
	default:
		return fmt.Errorf("v2 hello aec_reference is invalid")
	}
	if capabilities.AECReferenceVerified &&
		(capabilities.AECMode == "none" || capabilities.AECReference == "none") {
		return fmt.Errorf("v2 hello cannot verify a missing AEC reference")
	}
	if capabilities.AECMode == "none" && capabilities.AECReference != "none" {
		return fmt.Errorf("v2 hello AEC reference requires an AEC mode")
	}
	switch capabilities.PlaybackWatermark {
	case "none", "approximate", "exact":
	default:
		return fmt.Errorf("v2 hello playback_watermark is invalid")
	}
	if capabilities.BargeInLevel > 3 {
		return fmt.Errorf("v2 hello barge_in_level must be between 0 and 3")
	}
	return nil
}
