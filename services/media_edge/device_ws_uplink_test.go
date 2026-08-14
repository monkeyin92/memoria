package mediaedge

import (
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

func TestBargeInSourceAllowed(t *testing.T) {
	cases := []struct {
		name    string
		allowed []string
		source  string
		want    bool
	}{
		{"button allowed", []string{"button"}, "button", true},
		{"button missing", []string{"keyword"}, "button", false},
		{"none disables button", []string{"none", "button"}, "button", false},
		{"none disables keyword", []string{"none", "keyword"}, "keyword", false},
		{"none disables voice", []string{"none", "voice"}, "voice", false},
		{"voice allowed", []string{"voice"}, "voice", true},
		{"empty allowlist", []string{}, "button", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := bargeInSourceAllowed(tc.allowed, tc.source); got != tc.want {
				t.Fatalf("bargeInSourceAllowed(%v, %q) = %v, want %v", tc.allowed, tc.source, got, tc.want)
			}
		})
	}
}

func TestDeviceWSSBargeInIngressRequiresSignedSources(t *testing.T) {
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.SessionCloseReportHook = func(DeviceSessionCloseReport) {}
	})
	connection, _ := env.dial(t, env.token(t, func(claims *DeviceMediaClaims) {
		claims.DeviceSettings.AllowedBargeIn = []string{"button"}
	}), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	env.mu.Lock()
	core := env.cores["session_1"]
	env.mu.Unlock()
	core.mu.Lock()
	core.current = Fence{SessionID: "session_1", TurnID: 1, GenerationID: 1}
	core.mu.Unlock()
	core.inject(deviceGenerationEvent(
		"session_1", 18, 1, 1, 1,
		mediav1.GenerationAction_GENERATION_ACTION_START,
	))
	if _, payload, err := readDeviceMessage(connection, 3*time.Second); err != nil ||
		!strings.Contains(string(payload), "generation.started") {
		t.Fatalf("generation 1 did not become active: payload=%s err=%v", payload, err)
	}

	// keyword.detected without the keyword source is rejected and the
	// control violation closes the connection without forwarding.
	writeDeviceJSON(t, connection, deviceKeywordEvent{
		deviceEventBase: deviceEventBase{
			Type: "keyword.detected", Version: 2, StreamEpoch: 18,
			ControlSequence: 1, DeviceMonotonicMS: 1,
		},
		KeywordID: "kw_1", Confidence: 0.9,
		Evidence: deviceInterruptionEvidence{
			DetectedSample: 160, Source: "keyword", DurationMS: 20,
			AECMode: "fd_low_cost", AECVerified: true,
			VADProbability: 0.9, NearEndRMS: 0.1, FarEndRMS: 0.01,
			SpeakerClass: "owner",
		},
		ExpectedFence: deviceFence{GenerationID: 1, TurnID: 1, ToolEpoch: 0},
	})
	waitUntil(t, 3*time.Second, func() bool {
		core.mu.Lock()
		keywords := len(core.keywords)
		core.mu.Unlock()
		return keywords == 0 && env.server.Leases.ActiveCount() == 0
	})

	// Reconnect with a fresh epoch; button.stop IS allowed with the button
	// source and reaches the runtime.
	reconnect := env.dialReconnect(t, 19, "ticket_b")
	writeDeviceJSON(t, reconnect, deviceV2HelloWithEpoch(19))
	deviceReadAccepted(t, reconnect)
	env.mu.Lock()
	coreB := env.cores["session_1"]
	env.mu.Unlock()
	coreB.mu.Lock()
	coreB.current = Fence{SessionID: "session_1", TurnID: 1, GenerationID: 1}
	coreB.mu.Unlock()
	coreB.inject(deviceGenerationEvent(
		"session_1", 19, 1, 1, 1,
		mediav1.GenerationAction_GENERATION_ACTION_START,
	))
	if _, payload, err := readDeviceMessage(reconnect, 3*time.Second); err != nil ||
		!strings.Contains(string(payload), "generation.started") {
		t.Fatalf("reconnect generation did not start: payload=%s err=%v", payload, err)
	}
	writeDeviceJSON(t, reconnect, deviceButtonStop{
		deviceEventBase: deviceEventBase{
			Type: "button.stop", Version: 2, StreamEpoch: 19,
			ControlSequence: 2, DeviceMonotonicMS: 2,
		},
		ExpectedFence: deviceFence{GenerationID: 1, TurnID: 1, ToolEpoch: 0},
	})
	waitUntil(t, 3*time.Second, func() bool {
		coreB.mu.Lock()
		defer coreB.mu.Unlock()
		return len(coreB.stops) == 1
	})

	// The signed-ticket boundary rejects an invalid mixed none+active-source
	// allowlist before the WebSocket upgrade; ingress can never observe it.
	env2 := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.SessionCloseReportHook = func(DeviceSessionCloseReport) {}
	})
	connection2, response2 := env2.dial(t, env2.token(t, func(claims *DeviceMediaClaims) {
		claims.DeviceSettings.AllowedBargeIn = []string{"none", "button"}
		claims.SessionID = "session_none"
		claims.StreamEpoch = 1
		claims.JTI = "ticket_none"
	}), "client_1")
	if connection2 != nil || response2 == nil || response2.StatusCode != 401 {
		t.Fatalf("mixed none allowlist was not rejected: conn=%v response=%v", connection2, response2)
	}
}

func TestDeviceWSSPlaybackVADRequiresVoiceSourceAndTracksReceipts(t *testing.T) {
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.SessionCloseReportHook = func(DeviceSessionCloseReport) {}
	})
	connection, _ := env.dial(t, env.token(t, func(claims *DeviceMediaClaims) {
		claims.DeviceSettings.AllowedBargeIn = []string{"button"}
	}), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	env.mu.Lock()
	core := env.cores["session_1"]
	env.mu.Unlock()
	serverConn := env.server.connectionBySession("session_1")
	if serverConn == nil {
		t.Fatal("server connection not registered")
	}

	sendVAD := func(sequence uint64, start bool) {
		eventType := "vad.end"
		if start {
			eventType = "vad.start"
		}
		writeDeviceJSON(t, connection, deviceVADEvent{
			deviceEventBase: deviceEventBase{
				Type: eventType, Version: 2, StreamEpoch: 18,
				ControlSequence: sequence, DeviceMonotonicMS: sequence,
			},
			SamplePosition:  160 * sequence,
			VoicedEndSample: 160 * sequence,
			Probability:     0.8,
			NearEndRMS:      0.1,
		})
	}
	sendPlayback := func(messageType string, sequence, generationID uint64) {
		serverConn.ledger.recordSent(
			deviceFence{GenerationID: generationID, TurnID: generationID, ToolEpoch: 0},
			sequence,
			4800*sequence,
		)
		writeDeviceJSON(t, connection, devicePlaybackReceipt{
			deviceEventBase: deviceEventBase{
				Type: messageType, Version: 2, StreamEpoch: 18,
				ControlSequence: sequence, DeviceMonotonicMS: sequence,
			},
			Fence:             deviceFence{GenerationID: generationID, TurnID: generationID, ToolEpoch: 0},
			ReceivedSequence:  sequence,
			RenderedSampleEnd: 4800 * sequence,
		})
	}
	playbackState := func() bool {
		serverConn.stateMu.Lock()
		defer serverConn.stateMu.Unlock()
		return serverConn.playbackActive
	}

	// Ordinary listening VAD remains allowed without the voice source.
	sendVAD(1, true)
	waitUntil(t, 3*time.Second, func() bool {
		core.mu.Lock()
		defer core.mu.Unlock()
		return len(core.vad) == 1
	})

	// playback.progress keeps the playback window open.
	sendPlayback("playback.progress", 2, 1)
	waitUntil(t, 3*time.Second, playbackState)

	// playback.ended closes the window again.
	sendPlayback("playback.ended", 3, 1)
	waitUntil(t, 3*time.Second, func() bool {
		return !playbackState()
	})

	// Only a new authoritative fence may reopen the window after ended.
	core.inject(deviceGenerationEvent(
		"session_1", 18, 2, 2, 2,
		mediav1.GenerationAction_GENERATION_ACTION_START,
	))
	if _, payload, err := readDeviceMessage(connection, 3*time.Second); err != nil ||
		!strings.Contains(string(payload), `"generation_id":2`) {
		t.Fatalf("replacement generation did not start: payload=%s err=%v", payload, err)
	}
	sendPlayback("playback.started", 4, 2)
	waitUntil(t, 3*time.Second, playbackState)

	// A VAD start during playback is a barge candidate; without the voice
	// source it is rejected and the control violation closes the connection
	// without forwarding.
	sendVAD(5, true)
	waitUntil(t, 3*time.Second, func() bool {
		core.mu.Lock()
		vad := len(core.vad)
		core.mu.Unlock()
		return vad == 1 && env.server.Leases.ActiveCount() == 0
	})
}

func TestDeviceWSSApproximateWatermarkCannotClaimExactReceipt(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	hello := deviceV2Hello()
	hello.Capabilities.PlaybackWatermark = "approximate"
	writeDeviceJSON(t, connection, hello)
	deviceReadAccepted(t, connection)

	writeDeviceJSON(t, connection, devicePlaybackReceipt{
		deviceEventBase: deviceEventBase{
			Type: "playback.progress", Version: 2, StreamEpoch: 18,
			ControlSequence: 1, DeviceMonotonicMS: 1,
		},
		Fence:             deviceFence{GenerationID: 1, TurnID: 1},
		ReceivedSequence:  0,
		RenderedSampleEnd: 320,
		Approximate:       false,
	})
	if _, _, err := readDeviceMessage(connection, 3*time.Second); err == nil {
		t.Fatal("approximate device upgraded its receipt to exact")
	}
}

func (env *deviceTestEnv) dialReconnect(t *testing.T, epoch uint64, jti string) *websocket.Conn {
	t.Helper()
	token := env.token(t, func(claims *DeviceMediaClaims) {
		claims.StreamEpoch = epoch
		claims.JTI = jti
	})
	connection, response := env.dial(t, token, "client_1")
	if response != nil && response.StatusCode != http.StatusSwitchingProtocols {
		t.Fatalf("reconnect dial status: %s", response.Status)
	}
	return connection
}

func deviceV2HelloWithEpoch(epoch uint64) deviceHelloV2 {
	hello := deviceV2Hello()
	hello.StreamEpoch = epoch
	return hello
}
