package mediaedge

import (
	"encoding/binary"
	"encoding/hex"
	"strings"
	"testing"
)

func TestMemoriaAudioFrameV1GoldenBytes(t *testing.T) {
	cases := []struct {
		name  string
		frame MemoriaAudioFrameV1
		want  string
	}{
		{
			name: "uplink-v1",
			frame: MemoriaAudioFrameV1{
				Version: 1, Direction: 1, Flags: 0,
				StreamEpoch: 18, Sequence: 7, SampleStart: 2240,
				FrameSamples: 320, GenerationID: 0,
				Payload: []byte{0x01, 0x02, 0x03},
			},
			want: "01010000000000120000000700000000000008c000000140000000000003010203",
		},
		{
			name: "downlink-24k-v1",
			frame: MemoriaAudioFrameV1{
				Version: 1, Direction: 2, Flags: 0,
				StreamEpoch: 18, Sequence: 3, SampleStart: 1440,
				FrameSamples: 480, GenerationID: 1,
				Payload: []byte{0x04, 0x05},
			},
			want: "01020000000000120000000300000000000005a0000001e00000000100020405",
		},
		{
			name: "downlink-16k-negotiated",
			frame: MemoriaAudioFrameV1{
				Version: 1, Direction: 2, Flags: 0,
				StreamEpoch: 18, Sequence: 3, SampleStart: 960,
				FrameSamples: 320, GenerationID: 1,
				Payload: []byte{0x04, 0x05},
			},
			want: "01020000000000120000000300000000000003c0000001400000000100020405",
		},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			wire, err := testCase.frame.MarshalBinary()
			if err != nil {
				t.Fatal(err)
			}
			if hex.EncodeToString(wire) != testCase.want {
				t.Fatalf("golden frame mismatch:\n got %s\nwant %s", hex.EncodeToString(wire), testCase.want)
			}
			var decoded MemoriaAudioFrameV1
			if err := decoded.UnmarshalBinary(wire); err != nil {
				t.Fatal(err)
			}
			if decoded.Version != testCase.frame.Version || decoded.Direction != testCase.frame.Direction ||
				decoded.StreamEpoch != testCase.frame.StreamEpoch ||
				decoded.Sequence != testCase.frame.Sequence ||
				decoded.SampleStart != testCase.frame.SampleStart ||
				decoded.FrameSamples != testCase.frame.FrameSamples ||
				decoded.GenerationID != testCase.frame.GenerationID ||
				strings.Compare(string(decoded.Payload), string(testCase.frame.Payload)) != 0 {
				t.Fatal("decoded frame does not round trip")
			}
			if err := decoded.Validate(18, testCase.frame.Direction); err != nil {
				t.Fatalf("golden frame failed validation: %v", err)
			}
		})
	}
}

func TestMemoriaAudioFrameV1RejectsInvalidHeaders(t *testing.T) {
	valid := MemoriaAudioFrameV1{
		Version: 1, Direction: 1, StreamEpoch: 7, Sequence: 1,
		SampleStart: 0, FrameSamples: 320, GenerationID: 0,
		Payload: []byte{0x01},
	}
	wire, err := valid.MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	var decoded MemoriaAudioFrameV1
	if err := decoded.UnmarshalBinary(wire[:10]); err == nil {
		t.Fatal("short frame was accepted")
	}
	if err := decoded.UnmarshalBinary(append(wire, 0x00)); err == nil {
		t.Fatal("payload length mismatch was accepted")
	}
	emptyPayload := valid
	emptyPayload.Payload = nil
	emptyWire, err := emptyPayload.MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	if err := decoded.UnmarshalBinary(emptyWire); err != nil {
		t.Fatalf("empty payload frame did not reach validation: %v", err)
	}
	if err := decoded.Validate(7, DeviceDirectionUplink); err == nil {
		t.Fatal("empty payload was accepted")
	}
	badEpoch := valid
	badEpoch.StreamEpoch = 8
	if err := badEpoch.Validate(7, DeviceDirectionUplink); err == nil {
		t.Fatal("epoch mismatch was accepted")
	}
	badDirection := valid
	badDirection.Direction = DeviceDirectionDownlink
	if err := badDirection.Validate(7, DeviceDirectionUplink); err == nil {
		t.Fatal("direction mismatch was accepted")
	}
	badGeneration := valid
	badGeneration.GenerationID = 2
	if err := badGeneration.Validate(7, DeviceDirectionUplink); err == nil {
		t.Fatal("uplink generation must be zero")
	}
	badSamples := valid
	badSamples.FrameSamples = 480
	if err := badSamples.Validate(7, DeviceDirectionUplink); err == nil {
		t.Fatal("uplink frame samples must be 320")
	}
}

// TestMemoriaAudioFrameV1DiscontinuityFlag locks the downlink-only
// discontinuity marking of device-media-v2: bit 0 of the 16-bit flags field
// (header bytes 2-3, network order). It is set by the edge at socket-write
// time on the first frame after a same-generation forward gap caused by
// queue drops; uplink frames and unknown bits must be rejected.
func TestMemoriaAudioFrameV1DiscontinuityFlag(t *testing.T) {
	downlink := MemoriaAudioFrameV1{
		Version: 1, Direction: DeviceDirectionDownlink,
		Flags:       DeviceFrameFlagDiscontinuity,
		StreamEpoch: 18, Sequence: 3, SampleStart: 960,
		FrameSamples: 320, GenerationID: 1,
		Payload: []byte{0x04, 0x05},
	}
	wire, err := downlink.MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	// The downlink-16k golden frame with flags 0x0000 -> 0x0001.
	want := "01020001000000120000000300000000000003c0000001400000000100020405"
	if hex.EncodeToString(wire) != want {
		t.Fatalf("flagged golden frame mismatch: got %s want %s", hex.EncodeToString(wire), want)
	}
	var decoded MemoriaAudioFrameV1
	if err := decoded.UnmarshalBinary(wire); err != nil {
		t.Fatal(err)
	}
	if decoded.Flags != DeviceFrameFlagDiscontinuity {
		t.Fatalf("flags = %#x, want 0x0001", decoded.Flags)
	}
	// Downlink accepts the discontinuity bit; uplink and unknown bits do not.
	if err := decoded.Validate(18, DeviceDirectionDownlink); err != nil {
		t.Fatalf("flagged downlink frame failed validation: %v", err)
	}
	if err := decoded.Validate(18, DeviceDirectionUplink); err == nil {
		t.Fatal("uplink accepted the downlink discontinuity flag")
	}
	forged := downlink
	forged.Flags = 0x0002
	forgedWire, err := forged.MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	var forgedDecoded MemoriaAudioFrameV1
	if err := forgedDecoded.UnmarshalBinary(forgedWire); err != nil {
		t.Fatal(err)
	}
	if err := forgedDecoded.Validate(18, DeviceDirectionDownlink); err == nil {
		t.Fatal("downlink accepted an unknown/forged flag bit")
	}

	// setDeviceFrameDiscontinuity marks a marshaled downlink frame exactly
	// once (idempotent) and refuses uplink or short buffers.
	plain, err := (MemoriaAudioFrameV1{
		Version: 1, Direction: DeviceDirectionDownlink,
		StreamEpoch: 18, Sequence: 9, SampleStart: 2880,
		FrameSamples: 320, GenerationID: 1,
		Payload: []byte{0x01},
	}).MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	if !setDeviceFrameDiscontinuity(plain) {
		t.Fatal("downlink frame was not marked")
	}
	if binary.BigEndian.Uint16(plain[2:4]) != DeviceFrameFlagDiscontinuity {
		t.Fatalf("flags = %#x, want 0x0001", binary.BigEndian.Uint16(plain[2:4]))
	}
	setDeviceFrameDiscontinuity(plain)
	if binary.BigEndian.Uint16(plain[2:4]) != DeviceFrameFlagDiscontinuity {
		t.Fatalf("flag marking is not idempotent: %#x", binary.BigEndian.Uint16(plain[2:4]))
	}
	uplinkWire, err := (MemoriaAudioFrameV1{
		Version: 1, Direction: DeviceDirectionUplink,
		StreamEpoch: 18, Sequence: 0, SampleStart: 0,
		FrameSamples: 320, GenerationID: 0,
		Payload: []byte{0x01},
	}).MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	if setDeviceFrameDiscontinuity(uplinkWire) {
		t.Fatal("uplink frame was marked with the downlink discontinuity flag")
	}
	if setDeviceFrameDiscontinuity([]byte{0x01, 0x02, 0x03}) {
		t.Fatal("short buffer was marked")
	}
}

func TestDeviceControlPriorityLanes(t *testing.T) {
	if priority := deviceControlPriority("playback.flush"); priority != 0 {
		t.Fatalf("playback.flush must be P0, got %d", priority)
	}
	if priority := deviceControlPriority("generation.cancelled"); priority != 0 {
		t.Fatalf("generation.cancelled must be P0, got %d", priority)
	}
	if priority := deviceControlPriority("session.error"); priority != 0 {
		t.Fatalf("session.error must be P0, got %d", priority)
	}
	for _, messageType := range []string{
		"session.accepted", "generation.started", "vad.start", "keyword.detected",
		"button.stop", "playback.ended", "playback.duck", "runtime_profile.applied",
	} {
		if priority := deviceControlPriority(messageType); priority != 1 {
			t.Fatalf("%s must be P1, got %d", messageType, priority)
		}
	}
	if priority := deviceControlPriority("assistant.audio.frame"); priority != 2 {
		t.Fatalf("audio must be P2, got %d", priority)
	}
	if priority := deviceControlPriority("generation.completed"); priority != 2 {
		t.Fatalf("generation.completed must be an ordered P2 audio barrier, got %d", priority)
	}
	if priority := deviceControlPriority("device.telemetry"); priority != 3 {
		t.Fatalf("telemetry must be P3, got %d", priority)
	}
}

func TestParseDeviceControlLeavesTypedFieldsForTheHandler(t *testing.T) {
	if _, err := parseDeviceControl([]byte(`{"type":"vad.start","version":2,"extra":1}`)); err != nil {
		t.Fatalf("envelope parser consumed typed fields: %v", err)
	}
	if _, err := parseDeviceControl([]byte(`{"type":""}`)); err == nil {
		t.Fatal("missing type was accepted")
	}
	if _, err := parseDeviceControl([]byte(`{"type":"vad.start","version":2} {}`)); err == nil {
		t.Fatal("multiple JSON values were accepted")
	}
	var event deviceVADEvent
	if err := jsonUnmarshalStrict(
		[]byte(`{"type":"vad.start","version":2,"stream_epoch":1,"control_sequence":1,"device_monotonic_ms":1,"sample_position":0,"probability":0.5,"near_end_rms":1,"unexpected":true}`),
		&event,
	); err == nil {
		t.Fatal("unknown typed control field was accepted")
	}
}

func TestDeviceHelloV2Validation(t *testing.T) {
	hello := deviceHelloV2{
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
			AECReference: "software_post_gain_pre_i2s", AECReferenceVerified: false,
			LocalVAD: true, LocalStopKeyword: false, PhysicalStopButton: true,
			PlaybackWatermark: "exact", LocalDuck: false, BargeInLevel: 0,
		},
	}
	if err := hello.validate("dev_1", 18); err != nil {
		t.Fatal(err)
	}
	if err := hello.validate("dev_other", 18); err == nil {
		t.Fatal("cross-device hello was accepted")
	}
	if err := hello.validate("dev_1", 19); err == nil {
		t.Fatal("epoch mismatch was accepted")
	}
	no16k := hello
	no16k.Audio.DownlinkSampleRates = []uint64{24_000}
	if err := no16k.validate("dev_1", 18); err == nil {
		t.Fatal("v2 hello without 16 kHz downlink was accepted")
	}
	badAEC := hello
	badAEC.Capabilities.AECMode = "magic"
	if err := badAEC.validate("dev_1", 18); err == nil {
		t.Fatal("invalid aec_mode was accepted")
	}
	badLevel := hello
	badLevel.Capabilities.BargeInLevel = 4
	if err := badLevel.validate("dev_1", 18); err == nil {
		t.Fatal("barge_in_level above 3 was accepted")
	}
	inconsistentAEC := hello
	inconsistentAEC.Capabilities.AECMode = "none"
	if err := inconsistentAEC.validate("dev_1", 18); err == nil {
		t.Fatal("verified AEC reference without AEC mode was accepted")
	}
}

func TestDeviceHelloV1Validation(t *testing.T) {
	hello := deviceHelloV1{
		Type: "device.hello", Version: 1,
		DeviceID: "dev_1", FirmwareVersion: "0.1.0",
		BoardProfile: "memoria-atk-dnesp32s3-v1", StreamEpoch: 17,
		Audio: deviceLegacyAudio{
			UplinkCodec: "opus", UplinkSampleRate: 16_000,
			DownlinkSampleRate: 24_000, Channels: 1, FrameMS: 20,
		},
		Capabilities: deviceLegacyCapabilities{
			Display: true, Microphone: true, Speaker: true,
			DeviceAEC: false, PhysicalButton: true,
		},
	}
	if err := hello.validate("dev_1", 17); err != nil {
		t.Fatal(err)
	}
	badAudio := hello
	badAudio.Audio.DownlinkSampleRate = 16_000
	if err := badAudio.validate("dev_1", 17); err == nil {
		t.Fatal("legacy downlink must stay 24 kHz")
	}
}
