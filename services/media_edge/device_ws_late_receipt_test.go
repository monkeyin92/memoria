package mediaedge

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

// A spoken stop races the device's own playback receipts. The device keeps sending playback.progress for the
// story it is playing until it processes Voice Core's playback.flush, so one can be in flight when the
// CANCEL_GENERATION lands. Field 2026-10-02 (20261002-stop-pin-v1, 2 of 17 stops, once on a plain stop): the
// ledger accepted that receipt (the generation has no terminal receipt), Voice Core refused it as a stale
// generation, and the edge answered session.error playback_receipt_rejected and closed the WSS: the owner
// waited about 7 s for the reconnect and the next sentence was lost. 20260930-late-receipt-v1 only covered a
// receipt the LEDGER refuses and a replaced generation's playback.ended / playback.error.

type cancelledStoryScene struct {
	env        *deviceTestEnv
	connection *websocket.Conn
	core       *deviceTestCore
	serverConn *DeviceConnection
	story      deviceFence
	sequence   uint64
}

func newCancelledStoryScene(t *testing.T) *cancelledStoryScene {
	t.Helper()
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.SessionCloseReportHook = func(DeviceSessionCloseReport) {}
	})
	connection, _ := env.dial(t, env.token(t, func(claims *DeviceMediaClaims) {
		claims.DeviceSettings.AllowedBargeIn = []string{"button", "keyword"}
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
	core.mu.Lock()
	core.strictPlayback = true
	core.current = Fence{SessionID: "session_1", TurnID: 1, GenerationID: 1, SessionEpoch: 1}
	core.mu.Unlock()
	core.inject(deviceGenerationEvent("session_1", 18, 1, 1, 1, mediav1.GenerationAction_GENERATION_ACTION_START))
	scene := &cancelledStoryScene{
		env: env, connection: connection, core: core, serverConn: serverConn,
		story: deviceFence{TurnID: 1, GenerationID: 1, ToolEpoch: 0, SessionEpoch: 1},
	}
	scene.readUntil(t, "generation.started")
	return scene
}

func (s *cancelledStoryScene) readUntil(t *testing.T, marker string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		messageType, payload, err := readDeviceMessage(s.connection, time.Until(deadline))
		if err != nil {
			t.Fatalf("waiting for %s: %v", marker, err)
		}
		if messageType == websocket.TextMessage && strings.Contains(string(payload), marker) {
			return
		}
	}
	t.Fatalf("device never received %s", marker)
}

// receipt sends one device playback receipt for the story generation; the frame it reports was sent.
func (s *cancelledStoryScene) receipt(t *testing.T, messageType string, sequence uint64) {
	t.Helper()
	s.receiptFor(t, s.story, messageType, sequence)
}

func (s *cancelledStoryScene) receiptFor(t *testing.T, fence deviceFence, messageType string, sequence uint64) {
	t.Helper()
	s.serverConn.ledger.recordSent(fence, sequence, 4800*sequence)
	s.sequence++
	writeDeviceJSON(t, s.connection, devicePlaybackReceipt{
		deviceEventBase: deviceEventBase{
			Type: messageType, Version: 2, StreamEpoch: 18,
			ControlSequence: 100 + s.sequence, DeviceMonotonicMS: sequence,
		},
		Fence:             fence,
		ReceivedSequence:  sequence,
		RenderedSampleEnd: 4800 * sequence,
		Approximate:       true,
	})
}

func (s *cancelledStoryScene) playbackWindow() (bool, deviceFence) {
	s.serverConn.stateMu.Lock()
	defer s.serverConn.stateMu.Unlock()
	return s.serverConn.playbackActive, s.serverConn.playbackFence
}

// cancel is Voice Core's spoken stop: CANCEL_GENERATION names the replacement generation 2.
func (s *cancelledStoryScene) cancel(t *testing.T) {
	t.Helper()
	s.core.inject(&mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_RealtimeEffect{
		RealtimeEffect: &mediav1.RealtimeEffect{
			Identity: deviceCoreIdentity("session_1", 18), Sequence: 2,
			SessionId: "session_1", StreamEpoch: 18,
			EffectId: "voice-stop-1", EffectKind: mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION,
			SourceEventId: "voice_stop_command", Payload: []byte(`{"reason":"voice_stop_command"}`),
			TurnId: 1, GenerationId: 2, ToolEpoch: 0, SessionEpoch: 1,
		},
	}})
	s.readUntil(t, "playback.flush")
	s.moveCoreToReplacement()
}

// moveCoreToReplacement is what VoiceCoreSession does as soon as it validates the core's cancel.
func (s *cancelledStoryScene) moveCoreToReplacement() {
	s.core.mu.Lock()
	s.core.current = Fence{SessionID: "session_1", TurnID: 1, GenerationID: 2, SessionEpoch: 1}
	s.core.mu.Unlock()
}

// requireSessionKept proves the same transport still serves the owner: the next utterance's VAD start reaches
// Voice Core instead of a reconnect on a new stream epoch.
// sendVAD sends a voice VAD start; every device message of the scene shares one increasing control sequence.
func (s *cancelledStoryScene) sendVAD(t *testing.T) {
	t.Helper()
	s.sequence++
	controlSequence := 100 + s.sequence
	writeDeviceJSON(t, s.connection, deviceVADEvent{
		deviceEventBase: deviceEventBase{
			Type: "vad.start", Version: 2, StreamEpoch: 18,
			ControlSequence: controlSequence, DeviceMonotonicMS: controlSequence,
		},
		SamplePosition: 16_000, VoicedEndSample: 16_000, Probability: 1, NearEndRMS: 0.1,
	})
}

func (s *cancelledStoryScene) requireSessionKept(t *testing.T) {
	t.Helper()
	s.sendVAD(t)
	waitUntil(t, 3*time.Second, func() bool {
		s.core.mu.Lock()
		defer s.core.mu.Unlock()
		return len(s.core.vad) == 1
	})
	if s.env.server.Leases.ActiveCount() == 0 {
		t.Fatal("a late playback receipt closed the device session")
	}
}

func (s *cancelledStoryScene) forwarded() int {
	s.core.mu.Lock()
	defer s.core.mu.Unlock()
	return len(s.core.playback)
}

func TestDeviceWSSReceiptInFlightDuringTheCancelKeepsSession(t *testing.T) {
	for _, tc := range []struct {
		name          string
		messageType   string
		liveReceipts  int // receipts that reached Voice Core before the cancel
		sequenceAfter uint64
	}{
		{name: "progress", messageType: "playback.progress", liveReceipts: 1, sequenceAfter: 2},
		{name: "started", messageType: "playback.started", liveReceipts: 0, sequenceAfter: 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			scene := newCancelledStoryScene(t)
			if tc.liveReceipts == 1 {
				scene.receipt(t, "playback.started", 1)
				waitUntil(t, 3*time.Second, scene.serverConn.isPlaybackActive)
			}
			scene.cancel(t)

			// Sent by the device before it processed the flush; the edge session is already on generation 2.
			scene.receipt(t, tc.messageType, tc.sequenceAfter)
			// The firmware then reports the flushed generation's end, as it does after every flush.
			scene.receipt(t, "playback.ended", tc.sequenceAfter+1)
			waitUntil(t, 3*time.Second, func() bool { return !scene.serverConn.isPlaybackActive() })

			scene.requireSessionKept(t)
			if got := scene.forwarded(); got != tc.liveReceipts {
				t.Fatalf("playback receipts forwarded to Voice Core = %d, want %d (the late one is dropped)", got, tc.liveReceipts)
			}
		})
	}
}

// Voice Core's own session moves to the replacement fence when it validates the cancel, a moment before the
// edge Session applies it; a receipt that lands in that gap is exactly as late.
func TestDeviceWSSReceiptInTheGapBeforeTheEdgeAppliesTheCancelKeepsSession(t *testing.T) {
	scene := newCancelledStoryScene(t)
	scene.receipt(t, "playback.started", 1)
	waitUntil(t, 3*time.Second, scene.serverConn.isPlaybackActive)
	scene.moveCoreToReplacement() // the edge Session has not seen the cancel yet

	scene.receipt(t, "playback.progress", 2)
	scene.receipt(t, "playback.ended", 3)
	waitUntil(t, 3*time.Second, func() bool { return !scene.serverConn.isPlaybackActive() })

	scene.requireSessionKept(t)
	if got := scene.forwarded(); got != 1 {
		t.Fatalf("playback receipts forwarded to Voice Core = %d, want only the live one", got)
	}
}

// The cancel can also land between the edge's check and the send to Voice Core: the receipt is accepted as
// live, Voice Core refuses it as stale a moment later, and it is the same late receipt. The window it just
// opened for the now dead generation is closed again.
func TestDeviceWSSCancelLandingBetweenTheCheckAndTheSendIsTheSameLateReceipt(t *testing.T) {
	scene := newCancelledStoryScene(t)
	scene.receipt(t, "playback.started", 1)
	waitUntil(t, 3*time.Second, scene.serverConn.isPlaybackActive)
	scene.core.mu.Lock()
	scene.core.beforePlayback = func() {
		scene.moveCoreToReplacement()
		scene.core.mu.Lock()
		scene.core.beforePlayback = nil
		scene.core.mu.Unlock()
	}
	scene.core.mu.Unlock()

	scene.receipt(t, "playback.progress", 2)

	waitUntil(t, 3*time.Second, func() bool { return !scene.serverConn.isPlaybackActive() })
	scene.requireSessionKept(t)
	if got := scene.forwarded(); got != 1 {
		t.Fatalf("playback receipts forwarded to Voice Core = %d, want only the live one", got)
	}
}

// A late receipt of the replaced generation must not overwrite or close the playback window of the
// generation that plays now (a delayed device can be seconds behind, as on 2026-09-30).
func TestDeviceWSSLateReceiptOfAReplacedGenerationLeavesTheNewWindowAlone(t *testing.T) {
	scene := newCancelledStoryScene(t)
	scene.receipt(t, "playback.started", 1)
	waitUntil(t, 3*time.Second, scene.serverConn.isPlaybackActive)
	scene.cancel(t) // generation 2 replaces the story, no ended receipt for it yet

	// The owner asks again: a new reply, turn 2 generation 3, starts playing.
	reply := deviceFence{TurnID: 2, GenerationID: 3, ToolEpoch: 0, SessionEpoch: 1}
	scene.core.mu.Lock()
	scene.core.current = Fence{SessionID: "session_1", TurnID: 2, GenerationID: 3, SessionEpoch: 1}
	scene.core.mu.Unlock()
	scene.core.inject(deviceGenerationEvent("session_1", 18, 3, 2, 3, mediav1.GenerationAction_GENERATION_ACTION_START))
	scene.readUntil(t, "generation.started")
	scene.receiptFor(t, reply, "playback.started", 1)
	waitUntil(t, 3*time.Second, func() bool {
		active, fence := scene.playbackWindow()
		return active && fence == reply
	})

	scene.receipt(t, "playback.progress", 2) // the story's, very late
	// A voice VAD while the reply plays is playback-window VAD and is ignored. Had the late receipt closed or
	// replaced the window, it would reach Voice Core.
	scene.sendVAD(t)
	scene.receiptFor(t, reply, "playback.progress", 2) // the reply's own receipt, read after both on this connection

	waitUntil(t, 3*time.Second, func() bool { return scene.forwarded() == 3 })
	scene.core.mu.Lock()
	vadSeen := len(scene.core.vad)
	scene.core.mu.Unlock()
	if vadSeen != 0 {
		t.Fatal("a late receipt of the replaced story closed the new reply's playback window: a VAD during playback reached Voice Core")
	}
	if active, fence := scene.playbackWindow(); !active || fence != reply {
		t.Fatalf("playback window = (active %v, %+v) after a late receipt of the replaced story, want the new reply's window %+v", active, fence, reply)
	}
	if scene.env.server.Leases.ActiveCount() == 0 {
		t.Fatal("a late playback receipt closed the device session")
	}
}

// A receipt Voice Core refuses while its generation is still the current one is a real fault, not a late tick:
// the edge still fails closed and tells the device (retryable) why.
func TestDeviceWSSPlaybackReceiptRefusedForTheCurrentGenerationStillClosesRetryably(t *testing.T) {
	scene := newCancelledStoryScene(t)
	scene.core.mu.Lock()
	scene.core.playbackErr = errors.New("voice-core stream closed")
	scene.core.mu.Unlock()

	scene.receipt(t, "playback.progress", 1)

	sessionError, closeErr := readSessionErrorThenClose(t, scene.connection)
	if sessionError.Code != "playback_receipt_rejected" || !sessionError.Retryable {
		t.Fatalf("session.error = %+v, want retryable playback_receipt_rejected", sessionError)
	}
	if closeErr == nil {
		t.Fatal("the connection stayed open after a refused receipt for the current generation")
	}
}

// A receipt for a generation Voice Core has not started yet is not "late"; it is refused as before.
func TestDeviceWSSReceiptForAGenerationAheadOfVoiceCoreStillCloses(t *testing.T) {
	scene := newCancelledStoryScene(t)
	scene.story = deviceFence{TurnID: 1, GenerationID: 5, ToolEpoch: 0, SessionEpoch: 1}

	scene.receipt(t, "playback.progress", 1)

	sessionError, closeErr := readSessionErrorThenClose(t, scene.connection)
	if closeErr == nil {
		t.Fatalf("a receipt for a future generation kept the session open (session.error=%+v)", sessionError)
	}
}

// GenerationReplaced says a receipt is late only when Voice Core's session or the edge Session has already
// moved PAST the receipt's generation, in the same session.
func TestGenerationReplacedConsultsBothSidesInOrder(t *testing.T) {
	story := Fence{SessionID: "s", TurnID: 1, GenerationID: 1, SessionEpoch: 1}
	next := Fence{SessionID: "s", TurnID: 1, GenerationID: 2, SessionEpoch: 1}
	future := Fence{SessionID: "s", TurnID: 1, GenerationID: 5, SessionEpoch: 1}
	otherSession := Fence{SessionID: "other", TurnID: 1, GenerationID: 1, SessionEpoch: 1}
	for _, tc := range []struct {
		name         string
		edge, core   Fence
		receiptFence Fence
		want         bool
	}{
		{"both still on the story", story, story, story, false},
		{"edge moved on, core too", next, next, story, true},
		{"edge moved on, core behind", next, story, story, true},
		{"core moved on first (the gap before the edge applies the cancel)", story, next, story, true},
		{"the receipt names the current generation", next, next, next, false},
		{"the receipt is ahead of both", next, next, future, false},
		{"another session's fence is never replaced", next, next, otherSession, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			session := runtimeSession(t, 1)
			defer session.Stop()
			if err := session.RestoreGeneration(tc.edge, true); err != nil {
				t.Fatal(err)
			}
			core := newFakeCoreStream()
			core.setCurrentFence(tc.core)
			runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			if got := runtime.GenerationReplaced(tc.receiptFence); got != tc.want {
				t.Fatalf("GenerationReplaced(%+v) = %v, want %v (edge %+v, core %+v)", tc.receiptFence, got, tc.want, tc.edge, tc.core)
			}
		})
	}
}
