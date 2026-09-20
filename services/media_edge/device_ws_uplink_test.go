package mediaedge

import (
	"errors"
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
	core.current = Fence{SessionID: "session_1", TurnID: 1, GenerationID: 1, SessionEpoch: 1}
	core.mu.Unlock()
	core.inject(deviceGenerationEvent(
		"session_1", 18, 1, 1, 1,
		mediav1.GenerationAction_GENERATION_ACTION_START,
	))
	if _, payload, err := readDeviceMessage(connection, 3*time.Second); err != nil ||
		!strings.Contains(string(payload), "generation.started") {
		t.Fatalf("generation 1 did not become active: payload=%s err=%v", payload, err)
	}

	// keyword.detected without the keyword source is ignored: the barge is not
	// forwarded, but the conversation stays open.  A terminal session error
	// here (the behaviour before 2026-09-20) killed the whole session and the
	// device showed "错误: 设备媒体会话被服务端终止" after a re-wake.
	writeDeviceJSON(t, connection, deviceKeywordEvent{
		deviceEventBase: deviceEventBase{
			Type: "keyword.detected", Version: 2, StreamEpoch: 18,
			ControlSequence: 1, DeviceMonotonicMS: 1,
		},
		KeywordID: "kw_1", Confidence: 0.9,
		Evidence: deviceInterruptionEvidence{
			DetectedSample: 160, Source: "local_kws", DurationMS: 20,
			AECMode: "fd_low_cost", AECVerified: true,
			VADProbability: 0.9, NearEndRMS: 0.1, FarEndRMS: 0.01,
			SpeakerClass: "owner",
		},
		ExpectedFence: deviceFence{GenerationID: 1, TurnID: 1, ToolEpoch: 0, SessionEpoch: 1},
	})
	waitUntil(t, 3*time.Second, func() bool {
		return env.server.metrics.bargeIgnored.Load() == 1
	})
	core.mu.Lock()
	forwardedKeywords := len(core.keywords)
	core.mu.Unlock()
	if forwardedKeywords != 0 {
		t.Fatalf("forbidden keyword barge was forwarded: %v", core.keywords)
	}
	if env.server.Leases.ActiveCount() == 0 {
		t.Fatal("forbidden keyword barge closed the session")
	}

	// The same connection still serves an allowed source: button.stop with the
	// button source reaches the runtime.
	writeDeviceJSON(t, connection, deviceButtonStop{
		deviceEventBase: deviceEventBase{
			Type: "button.stop", Version: 2, StreamEpoch: 18,
			ControlSequence: 2, DeviceMonotonicMS: 2,
		},
		ExpectedFence: deviceFence{GenerationID: 1, TurnID: 1, ToolEpoch: 0, SessionEpoch: 1},
	})
	waitUntil(t, 3*time.Second, func() bool {
		core.mu.Lock()
		defer core.mu.Unlock()
		return len(core.stops) == 1
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
			deviceFence{GenerationID: generationID, TurnID: generationID, ToolEpoch: 0, SessionEpoch: 1},
			sequence,
			4800*sequence,
		)
		writeDeviceJSON(t, connection, devicePlaybackReceipt{
			deviceEventBase: deviceEventBase{
				Type: messageType, Version: 2, StreamEpoch: 18,
				ControlSequence: sequence, DeviceMonotonicMS: sequence,
			},
			Fence:             deviceFence{GenerationID: generationID, TurnID: generationID, ToolEpoch: 0, SessionEpoch: 1},
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
		core.mu.Lock()
		defer core.mu.Unlock()
		return !playbackState() && len(core.playback) == 2
	})
	core.mu.Lock()
	terminalEvent := core.playback[1].EventType
	core.mu.Unlock()
	if terminalEvent != mediav1.PlaybackEventType_PLAYBACK_EVENT_TYPE_ENDED {
		t.Fatalf("terminal event type = %s, want ENDED", terminalEvent)
	}

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
	// source it is ignored -- not forwarded, and the conversation stays open.
	// Closing the session here was the 2026-09-20 re-wake defect: the device
	// showed "错误: 设备媒体会话被服务端终止" and reconnected.
	sendVAD(5, true)
	waitUntil(t, 3*time.Second, func() bool {
		return env.server.metrics.bargeIgnored.Load() == 1
	})
	core.mu.Lock()
	vadAfterBarge := len(core.vad)
	core.mu.Unlock()
	if vadAfterBarge != 1 {
		t.Fatalf("forbidden voice barge was forwarded: %d VAD events", vadAfterBarge)
	}
	if env.server.Leases.ActiveCount() == 0 {
		t.Fatal("forbidden voice barge closed the session")
	}

	// Playback ends, the floor is the owner's again, and the next VAD start is
	// forwarded: the ignored frame did not poison the session.
	sendPlayback("playback.ended", 6, 2)
	waitUntil(t, 3*time.Second, func() bool { return !playbackState() })
	sendVAD(7, true)
	waitUntil(t, 3*time.Second, func() bool {
		core.mu.Lock()
		defer core.mu.Unlock()
		return len(core.vad) == 2
	})

	// The revocation rule is fence-bound: a fence that is not the live window's
	// must not unblock it (stale events exist), while the live fence must close.
	// The production call site is the device's own button.stop after a local
	// flush; driving that end-to-end here would need the harness to own the
	// session's generation bookkeeping, so this test pins the rule itself and
	// the handler wiring is covered by TestDeviceWSSBargeInIngressRequiresSignedSources.
	live := deviceFence{GenerationID: 9, TurnID: 9, ToolEpoch: 0, SessionEpoch: 1}
	serverConn.stateMu.Lock()
	serverConn.playbackActive = true
	serverConn.playbackFence = live
	serverConn.stateMu.Unlock()
	serverConn.clearPlaybackActive("stale_event", deviceFence{
		GenerationID: 3, TurnID: 3, ToolEpoch: 0, SessionEpoch: 1,
	})
	if !playbackState() {
		t.Fatal("a stale fence cleared the live playback window")
	}
	serverConn.clearPlaybackActive("device_button_stop", live)
	if playbackState() {
		t.Fatal("the live fence did not clear the playback window")
	}

	// The next utterance is served rather than ignored as a stale barge.
	sendVAD(21, true)
	waitUntil(t, 3*time.Second, func() bool {
		core.mu.Lock()
		defer core.mu.Unlock()
		return len(core.vad) == 3
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
		Fence:             deviceFence{GenerationID: 1, TurnID: 1, SessionEpoch: 1},
		ReceivedSequence:  0,
		RenderedSampleEnd: 320,
		Approximate:       false,
	})
	// The frame is refused fail-closed. What the device observes on the wire is
	// the explicit close frame, not only a bare 1006: 4002 is the same
	// non-retryable session-failure code every other refused control frame
	// uses, so the device does not have to guess whether an approximate
	// receipt may be replayed as exact.
	//
	// The queued session.error stays best-effort: the lane is torn down when
	// the read loop returns, so a message still waiting to be written can be
	// dropped. The close frame is the deterministic contract; do not assert on
	// the diagnostic here.
	_, _, err := readDeviceMessage(connection, 3*time.Second)
	if err == nil {
		t.Fatal("approximate device upgraded its receipt to exact")
	}
	var closeErr *websocket.CloseError
	if !errors.As(err, &closeErr) {
		t.Fatalf("watermark refusal did not close the connection: %v", err)
	}
	if closeErr.Code != 4002 || closeErr.Text != "playback_watermark_precision_mismatch" {
		t.Fatalf("watermark refusal close code: %d %q", closeErr.Code, closeErr.Text)
	}
}
