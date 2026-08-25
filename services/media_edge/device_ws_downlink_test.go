package mediaedge

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

// readDownlinkFrames drains the device socket until exactly want binary
// audio frames are collected, failing on any unexpected text control.
func readDownlinkFrames(t *testing.T, connection *websocket.Conn, want int) []MemoriaAudioFrameV1 {
	t.Helper()
	var frames []MemoriaAudioFrameV1
	for len(frames) < want {
		messageType, payload, err := readDeviceMessage(connection, 3*time.Second)
		if err != nil {
			t.Fatal(err)
		}
		if messageType != websocket.BinaryMessage {
			t.Fatalf("expected binary audio, got %d: %s", messageType, payload)
		}
		var frame MemoriaAudioFrameV1
		if err := frame.UnmarshalBinary(payload); err != nil {
			t.Fatal(err)
		}
		frames = append(frames, frame)
	}
	return frames
}

func readGenerationStarted(t *testing.T, connection *websocket.Conn, wantGeneration uint64) {
	t.Helper()
	messageType, payload, err := readDeviceMessage(connection, 3*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if messageType != websocket.TextMessage {
		t.Fatalf("expected generation.started text, got %d", messageType)
	}
	var generation deviceGenerationControl
	if err := json.Unmarshal(payload, &generation); err != nil ||
		generation.Type != "generation.started" {
		t.Fatalf("unexpected control: %s err=%v", payload, err)
	}
	if generation.Fence.GenerationID != wantGeneration {
		t.Fatalf("generation fence = %+v, want generation %d", generation.Fence, wantGeneration)
	}
}

func downlinkTestSamples() []int16 {
	samples := make([]int16, 480)
	for index := range samples {
		samples[index] = int16(math.Sin(2*math.Pi*440*float64(index)/24_000) * 8000)
	}
	return samples
}

// TestDeviceWSSDownlinkClockResetsForGenerationTwoWithoutReconnect is the
// cross-layer generation 1 -> generation 2 scenario: one connection, one
// stream epoch. The first generation's downlink clock runs 0..N; the
// authoritative generation.started for generation 2 resets the device
// sequence/sample clock to 0 and the new frames restart at zero without any
// discontinuity flag.
func TestDeviceWSSDownlinkClockResetsForGenerationTwoWithoutReconnect(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	env.mu.Lock()
	core := env.cores["session_1"]
	env.mu.Unlock()
	samples := downlinkTestSamples()

	// Generation 1: three 24 kHz source frames stay native on the target board
	// as 480-sample device frames with sequence/sample starting at 0.
	core.inject(deviceGenerationEvent("session_1", 18, 1, 1, 1, mediav1.GenerationAction_GENERATION_ACTION_START))
	for sequence := 0; sequence < 3; sequence++ {
		core.inject(deviceAudioEvent("session_1", 18, uint64(sequence), uint64(sequence)*480, samples, 1, 1))
	}
	readGenerationStarted(t, connection, 1)
	generationOne := readDownlinkFrames(t, connection, 3)
	for index, frame := range generationOne {
		if frame.GenerationID != 1 || frame.Flags != 0 ||
			frame.Sequence != uint32(index) ||
			frame.SampleStart != uint64(index)*480 {
			t.Fatalf("generation 1 frame %d mismatch: %+v", index, frame)
		}
	}

	// Generation 2 on the same connection: the clock must reset to 0 and no
	// flag may appear even though sequence 0 follows generation 1's 2.
	core.inject(deviceGenerationEvent("session_1", 18, 2, 2, 2, mediav1.GenerationAction_GENERATION_ACTION_START))
	for sequence := 0; sequence < 2; sequence++ {
		core.inject(deviceAudioEvent("session_1", 18, uint64(sequence), uint64(sequence)*480, samples, 2, 2))
	}
	readGenerationStarted(t, connection, 2)
	generationTwo := readDownlinkFrames(t, connection, 2)
	for index, frame := range generationTwo {
		if frame.GenerationID != 2 || frame.Flags != 0 ||
			frame.Sequence != uint32(index) ||
			frame.SampleStart != uint64(index)*480 {
			t.Fatalf("generation 2 frame %d mismatch: %+v", index, frame)
		}
	}
	if env.server.metrics.downlinkFrames.Load() != 5 {
		t.Fatalf("downlink frames = %d, want 5", env.server.metrics.downlinkFrames.Load())
	}
}

func TestDeviceWSSClosedConversationEntersStandbyAndRejectsLateAudio(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	env.mu.Lock()
	core := env.cores["session_1"]
	env.mu.Unlock()

	core.inject(deviceGenerationEvent(
		"session_1", 18, 1, 1, 1,
		mediav1.GenerationAction_GENERATION_ACTION_START,
	))
	readGenerationStarted(t, connection, 1)
	core.inject(deviceClosedStateEvent("session_1", 18, 2, "owner_silence_timeout"))

	messageType, payload, err := readDeviceMessage(connection, 3*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if messageType != websocket.TextMessage ||
		!strings.Contains(string(payload), `"type":"session.close"`) ||
		!strings.Contains(string(payload), `"reason":"owner_silence_timeout"`) {
		t.Fatalf("unexpected standby control: type=%d payload=%s", messageType, payload)
	}
	core.inject(deviceClosedStateEvent("session_1", 18, 3, "owner_silence_timeout"))
	if _, _, err := readDeviceMessage(connection, 300*time.Millisecond); err == nil {
		t.Fatal("duplicate session.close reached the device")
	}

	core.inject(deviceAudioEvent(
		"session_1", 18, 0, 0, downlinkTestSamples(), 1, 1,
	))
	if _, _, err := readDeviceMessage(connection, 300*time.Millisecond); err == nil {
		t.Fatal("late audio reached the device after conversation close")
	}
}
