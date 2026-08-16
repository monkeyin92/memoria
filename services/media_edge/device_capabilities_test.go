package mediaedge

import (
	"math"
	"testing"
)

func v2HelloForMode(verified bool) deviceHelloV2 {
	return deviceHelloV2{
		Type: "device.hello", Version: 2,
		DeviceID: "dev_1", FirmwareVersion: "0.2.0",
		BoardProfile: "memoria-atk-dnesp32s3-v1", StreamEpoch: 18,
		Audio: deviceAudioV2{
			UplinkCodec: "opus", UplinkSampleRate: 16_000,
			DownlinkSampleRates: []uint64{16_000, 24_000},
			Channels:            1, FrameMS: 20,
		},
		Capabilities: deviceCapabilitiesV2{
			Display: true, Microphone: true, Speaker: true,
			SimultaneousCapturePlayback: true, AECMode: "fd_low_cost",
			AECReference: "software_post_gain_pre_i2s", AECReferenceVerified: verified,
			LocalVAD: true, LocalStopKeyword: true, PhysicalStopButton: true,
			PlaybackWatermark: "exact", LocalDuck: true, BargeInLevel: 1,
		},
	}
}

func TestAcousticRegistryGatesFullDuplex(t *testing.T) {
	registry := NewDeviceAcousticRegistry()
	registry.Add(DeviceAcousticProfile{
		BoardProfile: "memoria-atk-dnesp32s3-v1", ProfileVersion: 4,
		Verified: true, MaxBargeInLevel: 2,
	})
	mode, err := registry.ResolveAudioModeV2(v2HelloForMode(true))
	if err != nil || mode != DeviceAudioModeFullDuplex {
		t.Fatalf("verified board + verified device AEC did not open full duplex: mode=%s err=%v", mode, err)
	}
	if mode := ResolveDownlinkRate(2, v2HelloForMode(true).Audio); mode != 24_000 {
		t.Fatalf("v2 downlink rate = %d, want 24000", mode)
	}
}

func TestAcousticRegistryNeverOpensFullDuplexWithoutServerAttestation(t *testing.T) {
	registry := NewDeviceAcousticRegistry()
	// Device self-reports a verified AEC reference, but the server has no
	// attested profile: full duplex must not be granted.
	mode, err := registry.ResolveAudioModeV2(v2HelloForMode(true))
	if err != nil || mode == DeviceAudioModeFullDuplex {
		t.Fatalf("full duplex opened without server attestation: mode=%s err=%v", mode, err)
	}
	if mode != DeviceAudioModeInterruptAssist {
		t.Fatalf("mode = %s, want interrupt_assist", mode)
	}
}

func TestAcousticRegistryRejectsUnverifiedRegistryEntry(t *testing.T) {
	registry := NewDeviceAcousticRegistry()
	registry.Add(DeviceAcousticProfile{
		BoardProfile: "memoria-atk-dnesp32s3-v1", ProfileVersion: 4,
		Verified: false,
	})
	mode, err := registry.ResolveAudioModeV2(v2HelloForMode(true))
	if err != nil || mode == DeviceAudioModeFullDuplex {
		t.Fatalf("unverified registry entry opened full duplex: mode=%s err=%v", mode, err)
	}
}

func TestAcousticRegistryRejectsProfileVersionOutsideDeviceUint32Domain(t *testing.T) {
	registry := NewDeviceAcousticRegistry()
	registry.Add(DeviceAcousticProfile{
		BoardProfile:   "memoria-atk-dnesp32s3-v1",
		ProfileVersion: uint64(math.MaxUint32) + 1,
		Verified:       true, MaxBargeInLevel: 2,
	})
	if _, ok := registry.Lookup("memoria-atk-dnesp32s3-v1"); ok {
		t.Fatal("out-of-range acoustic profile version entered the device registry")
	}
}

func TestAcousticRegistryRequiresDeviceAECFacts(t *testing.T) {
	registry := NewDeviceAcousticRegistry()
	registry.Add(DeviceAcousticProfile{
		BoardProfile: "memoria-atk-dnesp32s3-v1", ProfileVersion: 4, Verified: true,
	})
	hello := v2HelloForMode(true)
	hello.Capabilities.AECReference = "none"
	if mode, _ := registry.ResolveAudioModeV2(hello); mode == DeviceAudioModeFullDuplex {
		t.Fatal("full duplex opened without a device AEC reference")
	}
	hello = v2HelloForMode(false)
	if mode, _ := registry.ResolveAudioModeV2(hello); mode == DeviceAudioModeFullDuplex {
		t.Fatal("full duplex opened without device aec_reference_verified")
	}
}

func TestResolveDownlinkRatePrefersNative24kForV2(t *testing.T) {
	audio := deviceAudioV2{DownlinkSampleRates: []uint64{24_000, 16_000}}
	if rate := ResolveDownlinkRate(2, audio); rate != 24_000 {
		t.Fatalf("rate = %d, want 24000", rate)
	}
	if rate := ResolveDownlinkRate(2, deviceAudioV2{DownlinkSampleRates: []uint64{16_000}}); rate != 16_000 {
		t.Fatalf("16 kHz-only v2 rate = %d, want 16000 fallback", rate)
	}
	if rate := ResolveDownlinkRate(1, audio); rate != 24_000 {
		t.Fatalf("legacy rate = %d, want 24000", rate)
	}
}

func TestSignedBargeInPolicyCanOnlyDowngradeNegotiatedMode(t *testing.T) {
	if mode := applyBargeInPolicy(DeviceAudioModeFullDuplex, []string{"voice"}); mode != DeviceAudioModeFullDuplex {
		t.Fatalf("voice-enabled full duplex was downgraded to %s", mode)
	}
	if mode := applyBargeInPolicy(DeviceAudioModeFullDuplex, []string{"button"}); mode != DeviceAudioModeInterruptAssist {
		t.Fatalf("button-only policy = %s, want interrupt_assist", mode)
	}
	if mode := applyBargeInPolicy(DeviceAudioModeInterruptAssist, []string{"none"}); mode != DeviceAudioModeHalfDuplexSafe {
		t.Fatalf("none policy = %s, want half_duplex_safe", mode)
	}
	if mode := applyBargeInPolicy(DeviceAudioModeHalfDuplexSafe, []string{"voice"}); mode != DeviceAudioModeHalfDuplexSafe {
		t.Fatalf("allowlist raised hardware ceiling to %s", mode)
	}
}
