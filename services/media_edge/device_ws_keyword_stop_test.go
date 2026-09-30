package mediaedge

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

// loadKeywordStopFixture returns the keyword.detected message exactly as the
// ESP-VoCat firmware renders it for a local stop keyword. The firmware host
// tests pin memoria_protocol.cc's SendKeywordStop field set to the same file.
func loadKeywordStopFixture(t *testing.T) map[string]any {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("..", "..", "packages", "contracts", "device-keyword-stop-v2.example.json"))
	if err != nil {
		t.Fatal(err)
	}
	// The raw bytes must pass the strict device decoder as-is.
	var strict deviceKeywordEvent
	if err := jsonUnmarshalStrict(raw, &strict); err != nil {
		t.Fatalf("firmware keyword stop does not match deviceKeywordEvent: %v", err)
	}
	if err := strict.validate(); err != nil || !strict.HardStop ||
		!validInterruptionEvidence(strict.Evidence) || !strict.ExpectedFence.valid() ||
		float32(strict.Confidence) < KWSHardStopMinConfidence {
		t.Fatalf("firmware keyword stop is not an acceptable hard stop: %+v err=%v", strict, err)
	}
	var message map[string]any
	if err := json.Unmarshal(raw, &message); err != nil {
		t.Fatal(err)
	}
	return message
}

func writeRawDeviceJSON(t *testing.T, connection *websocket.Conn, value map[string]any) {
	t.Helper()
	payload, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if err := connection.WriteMessage(websocket.TextMessage, payload); err != nil {
		t.Fatal(err)
	}
}

func TestDeviceWSSLocalStopKeywordHardStopsAndClosesPlaybackWindow(t *testing.T) {
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
	serverConn := env.server.connectionBySession("session_1")
	if serverConn == nil {
		t.Fatal("server connection not registered")
	}
	playing := deviceFence{TurnID: 1, GenerationID: 1, ToolEpoch: 0, SessionEpoch: 1}
	serverConn.stateMu.Lock()
	serverConn.playbackActive = true
	serverConn.playbackFence = playing
	serverConn.stateMu.Unlock()

	// The firmware's own bytes: accepted, forwarded as a hard stop, and the
	// playback window closes like button.stop (no playback.ended follows a
	// local flush).
	stop := loadKeywordStopFixture(t)
	writeRawDeviceJSON(t, connection, stop)
	waitUntil(t, 3*time.Second, func() bool {
		core.mu.Lock()
		keywords := len(core.keywords)
		core.mu.Unlock()
		if keywords != 1 {
			return false
		}
		serverConn.stateMu.Lock()
		defer serverConn.stateMu.Unlock()
		return !serverConn.playbackActive
	})
	core.mu.Lock()
	keyword := core.keywords[0]
	core.mu.Unlock()
	if keyword != "ting_yi_xia" {
		t.Fatalf("forwarded keyword = %q", keyword)
	}
	runtime, _ := serverConn.runtimeRef()
	if runtime == nil {
		t.Fatal("runtime not attached")
	}
	if current, active := runtime.session.GenerationSnapshot(); active || current.GenerationID != 2 {
		t.Fatalf("hard-stop keyword did not cancel generation 1: fence=%+v active=%v", current, active)
	}

	// A second stop for the already-cancelled generation (a repeat, or one
	// racing a server-side cancel) is stale, not a protocol violation: it is
	// not forwarded and the transport stays open.
	stale := loadKeywordStopFixture(t)
	stale["control_sequence"] = 2
	stale["device_monotonic_ms"] = 3000
	stale["evidence"].(map[string]any)["detected_sample"] = 48000
	writeRawDeviceJSON(t, connection, stale)
	waitUntil(t, 3*time.Second, func() bool {
		return env.server.metrics.staleGeneration.Load() == 1
	})
	core.mu.Lock()
	forwarded := len(core.keywords)
	core.mu.Unlock()
	if forwarded != 1 {
		t.Fatalf("stale keyword stop was forwarded: %v", core.keywords)
	}
	if env.server.metrics.controlRejected.Load() != 0 {
		t.Fatalf("keyword stop counted as rejected control: %d", env.server.metrics.controlRejected.Load())
	}
	if env.server.Leases.ActiveCount() == 0 {
		t.Fatal("stale keyword stop closed the session")
	}
}

func TestDeviceWSSLocalStopKeywordRejectsUnknownFields(t *testing.T) {
	// Edge decodes device controls strictly; a firmware that adds a field
	// (for example the raw MultiNet score) would lose its transport.
	stop := loadKeywordStopFixture(t)
	stop["raw_score"] = 0.31
	payload, err := json.Marshal(stop)
	if err != nil {
		t.Fatal(err)
	}
	var event deviceKeywordEvent
	if err := jsonUnmarshalStrict(payload, &event); err == nil {
		t.Fatal("strict decoder accepted an unknown keyword.detected field")
	}
}
