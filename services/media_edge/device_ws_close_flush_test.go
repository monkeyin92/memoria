package mediaedge

import (
	"encoding/json"
	"errors"
	"fmt"
	"testing"
	"time"

	"github.com/gorilla/websocket"
)

// The firmware decides terminal vs. retryable only from session.error; it
// never reads the WebSocket close code. A queued session.error must therefore
// reach the socket before the close frame, or a terminal refusal looks like a
// network drop and the device keeps resuming (P2-04).
func readSessionErrorThenClose(t *testing.T, connection *websocket.Conn) (deviceSessionError, *websocket.CloseError) {
	t.Helper()
	var sessionError deviceSessionError
	deadline := time.Now().Add(3 * time.Second)
	for {
		messageType, payload, err := readDeviceMessage(connection, time.Until(deadline))
		if err != nil {
			var closeErr *websocket.CloseError
			if !errors.As(err, &closeErr) {
				t.Fatalf("connection failed without a close frame: %v", err)
			}
			if sessionError.Type == "" {
				t.Fatalf("close %d %q arrived without a session.error", closeErr.Code, closeErr.Text)
			}
			return sessionError, closeErr
		}
		if messageType != websocket.TextMessage {
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
}

func TestDeviceWSSHelloRefusalDeliversSessionErrorBeforeClose(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	hello := deviceV2Hello()
	hello.DeviceID = "dev_other"
	writeDeviceJSON(t, connection, hello)

	sessionError, closeErr := readSessionErrorThenClose(t, connection)
	if sessionError.Code != "invalid_hello" || sessionError.Retryable {
		t.Fatalf("session.error = %+v, want terminal invalid_hello", sessionError)
	}
	if closeErr.Code != 4002 {
		t.Fatalf("close code = %d, want 4002", closeErr.Code)
	}
}

func TestDeviceWSSRefusedControlDeliversSessionErrorBeforeClose(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	writeDeviceJSON(t, connection, map[string]any{
		"type": "device.unknown", "version": 2, "stream_epoch": 18,
		"control_sequence": 1, "device_monotonic_ms": 1,
	})

	sessionError, _ := readSessionErrorThenClose(t, connection)
	if sessionError.Code != "unsupported_control" || sessionError.Retryable {
		t.Fatalf("session.error = %+v, want terminal unsupported_control", sessionError)
	}
}

func TestDeviceWSSBridgeFailureDeliversRetryableSessionErrorBeforeClose(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	env.mu.Lock()
	request := env.requests["session_1"]
	env.mu.Unlock()

	env.server.HandleBridgeError(request, fmt.Errorf("voice core stream reset"))

	sessionError, closeErr := readSessionErrorThenClose(t, connection)
	if sessionError.Code != "voice_core_unavailable" || !sessionError.Retryable {
		t.Fatalf("session.error = %+v, want retryable voice_core_unavailable", sessionError)
	}
	if closeErr.Code != websocket.CloseInternalServerErr {
		t.Fatalf("close code = %d, want 1011", closeErr.Code)
	}
}
