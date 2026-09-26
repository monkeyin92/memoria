package mediaedge

// P2-04 fault injection: every way a device connection can end must release
// its goroutines, connection entry, lease and Voice Core runtime, and a fault
// in the middle of a turn must still tell the device why (session.error)
// without stale audio behind it.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"runtime"
	"runtime/pprof"
	"sync"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

func (env *deviceTestEnv) connectionCount() int {
	env.server.mu.Lock()
	defer env.server.mu.Unlock()
	return len(env.server.conns)
}

func (env *deviceTestEnv) core(sessionID string) *deviceTestCore {
	env.mu.Lock()
	defer env.mu.Unlock()
	return env.cores[sessionID]
}

// connectAt opens an accepted connection at the given stream epoch with its
// own single-use ticket.
func connectAt(t *testing.T, env *deviceTestEnv, epoch uint64) (*websocket.Conn, *deviceTestCore) {
	t.Helper()
	token := env.token(t, func(claims *DeviceMediaClaims) {
		claims.StreamEpoch = epoch
		claims.JTI = fmt.Sprintf("ticket_%d", epoch)
	})
	connection, response := env.dial(t, token, "client_1")
	if connection == nil {
		t.Fatalf("dial epoch %d failed: %v", epoch, response)
	}
	hello := deviceV2Hello()
	hello.StreamEpoch = epoch
	writeDeviceJSON(t, connection, hello)
	deviceReadAccepted(t, connection)
	return connection, env.core("session_1")
}

func waitCoreClosed(t *testing.T, core *deviceTestCore, path string) {
	t.Helper()
	select {
	case <-core.closed:
	case <-time.After(3 * time.Second):
		t.Fatalf("%s: Voice Core runtime was not closed", path)
	}
}

func waitReleased(t *testing.T, env *deviceTestEnv, path string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if env.connectionCount() == 0 && env.server.Leases.ActiveCount() == 0 &&
			env.server.metrics.activeConnections.Load() == 0 {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatalf("%s: connections=%d leases=%d active=%d after close", path,
		env.connectionCount(), env.server.Leases.ActiveCount(),
		env.server.metrics.activeConnections.Load())
}

func TestDeviceWSSEveryClosePathReleasesItsResources(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	baseline := runtime.NumGoroutine()
	epoch := uint64(18)
	next := func() uint64 { epoch++; return epoch }

	paths := []struct {
		name  string
		close func(t *testing.T, connection *websocket.Conn, epoch uint64)
	}{
		{"device session.close", func(t *testing.T, connection *websocket.Conn, epoch uint64) {
			writeDeviceJSON(t, connection, deviceSessionClose{
				deviceEventBase: deviceEventBase{
					Type: "session.close", Version: 2, StreamEpoch: epoch,
					ControlSequence: 1, DeviceMonotonicMS: 1,
				},
				Value: struct {
					Reason string `json:"reason"`
				}{Reason: "device_close"},
			})
		}},
		{"abrupt network loss", func(_ *testing.T, connection *websocket.Conn, _ uint64) {
			_ = connection.UnderlyingConn().Close()
		}},
		{"refused control", func(t *testing.T, connection *websocket.Conn, epoch uint64) {
			writeDeviceJSON(t, connection, map[string]any{
				"type": "device.unknown", "version": 2, "stream_epoch": epoch,
				"control_sequence": 1, "device_monotonic_ms": 1,
			})
		}},
		{"uplink sequence replay", func(t *testing.T, connection *websocket.Conn, epoch uint64) {
			encoder, err := newOpusEncoder(16_000, 1)
			if err != nil {
				t.Fatal(err)
			}
			defer encoder.close()
			first := encodeUplinkOpusFrame(t, encoder, uint32(epoch), 0, 0)
			for range 2 {
				if err := connection.WriteMessage(websocket.BinaryMessage, first); err != nil {
					t.Fatal(err)
				}
			}
		}},
		{"voice core stream failure", func(_ *testing.T, _ *websocket.Conn, _ uint64) {
			env.mu.Lock()
			request := env.requests["session_1"]
			env.mu.Unlock()
			env.server.HandleBridgeError(request, fmt.Errorf("injected stream reset"))
		}},
		{"control plane close", func(t *testing.T, _ *websocket.Conn, _ uint64) {
			if !env.server.CloseSession("session_1", SessionCloseReasonProfileInvalidated) {
				t.Fatal("control plane close found no session")
			}
		}},
	}

	// Three rounds so a per-connection leak shows up as a clear multiple.
	for round := 0; round < 3; round++ {
		for _, path := range paths {
			current := next()
			connection, core := connectAt(t, env, current)
			path.close(t, connection, current)
			waitCoreClosed(t, core, path.name)
			waitReleased(t, env, path.name)
			_ = connection.Close()
		}
		// Supersede: a newer epoch takes over a live connection.
		older, olderCore := connectAt(t, env, next())
		newer, newerCore := connectAt(t, env, next())
		waitCoreClosed(t, olderCore, "superseded")
		if env.connectionCount() != 1 || env.server.Leases.ActiveCount() != 1 {
			t.Fatalf("superseded: connections=%d leases=%d, want the newer one only",
				env.connectionCount(), env.server.Leases.ActiveCount())
		}
		_ = newer.UnderlyingConn().Close()
		waitCoreClosed(t, newerCore, "superseding connection")
		waitReleased(t, env, "superseding connection")
		_ = older.Close()
		_ = newer.Close()

		// A refused hello never becomes a session but must not leak either.
		refused, _ := env.dial(t, env.token(t, func(claims *DeviceMediaClaims) {
			claims.JTI = fmt.Sprintf("ticket_refused_%d", round)
		}), "client_1")
		hello := deviceV2Hello()
		hello.DeviceID = "dev_other"
		writeDeviceJSON(t, refused, hello)
		readUntilClosed(t, refused, 3*time.Second)
		_ = refused.Close()
		waitReleased(t, env, "refused hello")
	}

	deadline := time.Now().Add(5 * time.Second)
	for runtime.NumGoroutine() > baseline+2 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if leaked := runtime.NumGoroutine() - baseline; leaked > 2 {
		var dump bytes.Buffer
		_ = pprof.Lookup("goroutine").WriteTo(&dump, 1)
		t.Fatalf("%d goroutines outlived 24 closed connections:\n%s", leaked, dump.String())
	}
}

func TestDeviceWSSVoiceCoreFailureMidAnswerSendsNoAudioAfterSessionError(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, core := connectAt(t, env, 18)
	core.inject(deviceGenerationEvent("session_1", 18, 1, 1, 1,
		mediav1.GenerationAction_GENERATION_ACTION_START))
	samples := make([]int16, 480)
	for index := range samples {
		samples[index] = int16(math.Sin(2*math.Pi*440*float64(index)/24_000) * 8000)
	}
	stop := make(chan struct{})
	var feeder sync.WaitGroup
	feeder.Add(1)
	go func() {
		defer feeder.Done()
		for sequence := uint64(0); ; sequence++ {
			select {
			case <-stop:
				return
			case core.events <- deviceAudioEvent("session_1", 18, sequence, sequence*480, samples, 1, 1):
			case <-core.closed:
				return
			}
			time.Sleep(5 * time.Millisecond)
		}
	}()
	defer func() { close(stop); feeder.Wait() }()

	// Wait until the answer is audibly flowing, then fail the Voice Core stream.
	sawAudio := false
	for !sawAudio {
		messageType, _, err := readDeviceMessage(connection, 3*time.Second)
		if err != nil {
			t.Fatalf("answer audio never reached the device: %v", err)
		}
		sawAudio = messageType == websocket.BinaryMessage
	}
	env.mu.Lock()
	request := env.requests["session_1"]
	env.mu.Unlock()
	env.server.HandleBridgeError(request, fmt.Errorf("injected stream reset mid-answer"))

	var sessionError deviceSessionError
	for {
		messageType, payload, err := readDeviceMessage(connection, 3*time.Second)
		if err != nil {
			closeErr, ok := err.(*websocket.CloseError)
			if !ok || closeErr.Code != websocket.CloseInternalServerErr {
				t.Fatalf("mid-answer failure did not close with 1011: %v", err)
			}
			break
		}
		if messageType == websocket.BinaryMessage {
			if sessionError.Type != "" {
				t.Fatal("assistant audio arrived after session.error")
			}
			continue
		}
		var envelope struct {
			Type string `json:"type"`
		}
		if json.Unmarshal(payload, &envelope) == nil && envelope.Type == "session.error" {
			if err := json.Unmarshal(payload, &sessionError); err != nil {
				t.Fatal(err)
			}
		}
	}
	if sessionError.Code != "voice_core_unavailable" || !sessionError.Retryable {
		t.Fatalf("mid-answer failure session.error = %+v, want retryable voice_core_unavailable", sessionError)
	}
	waitCoreClosed(t, core, "mid-answer failure")
	waitReleased(t, env, "mid-answer failure")
}

func TestDeviceWSSLateCoreEventsRacingCloseAreDropped(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	for round := uint64(0); round < 5; round++ {
		epoch := 18 + round
		connection, core := connectAt(t, env, epoch)
		env.mu.Lock()
		request := env.requests["session_1"]
		env.mu.Unlock()
		var racers sync.WaitGroup
		racers.Add(2)
		go func() {
			defer racers.Done()
			for sequence := uint64(1); sequence <= 50; sequence++ {
				// Straight to the forwarder, as a late Core event would arrive.
				env.server.ForwardCoreEventForSession(request, deviceGenerationEvent(
					"session_1", epoch, sequence, sequence, sequence,
					mediav1.GenerationAction_GENERATION_ACTION_START,
				))
			}
		}()
		go func() {
			defer racers.Done()
			env.server.CloseSession("session_1", SessionCloseReasonNetwork)
		}()
		racers.Wait()
		waitCoreClosed(t, core, "close racing core events")
		waitReleased(t, env, "close racing core events")
		// After the close nothing may be forwarded to the closed transport.
		env.server.ForwardCoreEventForSession(request, deviceGenerationEvent(
			"session_1", epoch, 99, 99, 99, mediav1.GenerationAction_GENERATION_ACTION_START,
		))
		readUntilClosed(t, connection, 3*time.Second)
		_ = connection.Close()
	}
}

func TestDeviceWSSUplinkReorderIsRefusedAsRetryableSequenceGap(t *testing.T) {
	for _, next := range []uint32{0, 2} { // replayed frame, skipped frame
		t.Run(fmt.Sprintf("second_sequence_%d", next), func(t *testing.T) {
			env := newDeviceTestEnv(t, nil)
			connection, _ := connectAt(t, env, 18)
			encoder, err := newOpusEncoder(16_000, 1)
			if err != nil {
				t.Fatal(err)
			}
			defer encoder.close()
			frames := [][]byte{
				encodeUplinkOpusFrame(t, encoder, 18, 0, 0),
				encodeUplinkOpusFrame(t, encoder, 18, next, uint64(next)*DeviceUplinkFrameSamples),
			}
			for _, frame := range frames {
				if err := connection.WriteMessage(websocket.BinaryMessage, frame); err != nil {
					t.Fatal(err)
				}
			}
			sessionError, _ := readSessionErrorThenClose(t, connection)
			if sessionError.Code != "uplink_sequence_gap" || !sessionError.Retryable {
				t.Fatalf("reordered uplink = %+v, want retryable uplink_sequence_gap", sessionError)
			}
			waitReleased(t, env, "reordered uplink")
		})
	}
}
