package mediaedge

// Connection teardown and routing faults found by reading the writer and
// registry paths.
//
// A device socket write failure must tear the connection down immediately.
// gorilla/websocket keeps the write error sticky but does not close the
// underlying conn, so without an explicit close the reader keeps forwarding
// uplink to Voice Core while nothing reaches the device, until the 90 s idle
// read deadline fires (pongs stop because pings stop).

import (
	"encoding/json"
	"errors"
	"net"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

var errInjectedDeviceWrite = errors.New("injected device socket write failure")

// failOnceConn fails exactly one Write after it is armed; every other write
// passes through, so only the writer's own handling can end the session.
type failOnceConn struct {
	net.Conn
	armed *atomic.Bool
}

func (c *failOnceConn) Write(p []byte) (int, error) {
	if c.armed.CompareAndSwap(true, false) {
		return 0, errInjectedDeviceWrite
	}
	return c.Conn.Write(p)
}

type failOnceListener struct {
	net.Listener
	armed *atomic.Bool
}

func (l *failOnceListener) Accept() (net.Conn, error) {
	conn, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	return &failOnceConn{Conn: conn, armed: l.armed}, nil
}

func TestDeviceWSSWriteFailureClosesConnectionPromptly(t *testing.T) {
	var (
		reportMu sync.Mutex
		reports  []DeviceSessionCloseReport
	)
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.SessionCloseReportHook = func(report DeviceSessionCloseReport) {
			reportMu.Lock()
			reports = append(reports, report)
			reportMu.Unlock()
		}
	})
	armed := &atomic.Bool{}
	failing := httptest.NewUnstartedServer(env.server)
	failing.Listener = &failOnceListener{Listener: failing.Listener, armed: armed}
	failing.Start()
	t.Cleanup(failing.Close)
	env.wsURL = "ws" + strings.TrimPrefix(failing.URL, "http") + DeviceMediaEndpoint

	device, core := connectAt(t, env, 18)
	connection := env.server.connectionBySession("session_1")
	if connection == nil {
		t.Fatal("accepted connection is not registered")
	}

	armed.Store(true)
	started := time.Now()
	connection.sendControl(0, []byte(`{"type":"probe"}`))

	select {
	case <-connection.closed:
	case <-time.After(2 * time.Second):
		t.Fatal("connection lingered after a socket write failure")
	}
	if elapsed := time.Since(started); elapsed > time.Second {
		t.Fatalf("write-failure close took %s", elapsed)
	}
	if armed.Load() {
		t.Fatal("injected write failure was never exercised")
	}
	readUntilClosed(t, device, 2*time.Second)
	waitCoreClosed(t, core, "write failure")
	waitReleased(t, env, "write failure")

	// close(c.closed) precedes the synchronous report hook in teardown.
	waitUntil(t, 2*time.Second, func() bool {
		reportMu.Lock()
		defer reportMu.Unlock()
		return len(reports) > 0
	})
	reportMu.Lock()
	defer reportMu.Unlock()
	if len(reports) != 1 {
		t.Fatalf("close reports = %d, want 1: %+v", len(reports), reports)
	}
	// Control's close-report allowlist classifies a writer loss as transport
	// "network" (Session authority kept for same-Session reconnect).
	if reports[0].Reason != SessionCloseReasonNetwork {
		t.Fatalf("close report reason = %q, want %q", reports[0].Reason, SessionCloseReasonNetwork)
	}
	connection.stateMu.Lock()
	cause := connection.closeCause
	connection.stateMu.Unlock()
	if cause != deviceCloseCauseDownlinkWrite {
		t.Fatalf("close cause = %q, want %q", cause, deviceCloseCauseDownlinkWrite)
	}
	if got := env.server.metrics.writeFailures.Load(); got != 1 {
		t.Fatalf("downlink write failures = %d, want 1", got)
	}
}

// A newer socket that fails before hello/lease install must not steal, and
// then delete, the session route of the still-accepted older connection.
func TestDeviceWSSFailedHandshakeKeepsAcceptedSessionRoute(t *testing.T) {
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.SessionCloseReportHook = func(DeviceSessionCloseReport) {}
	})
	device, core := connectAt(t, env, 18)
	accepted := env.server.connectionBySession("session_1")
	if accepted == nil {
		t.Fatal("accepted connection is not routable by session")
	}

	token := env.token(t, func(claims *DeviceMediaClaims) {
		claims.StreamEpoch = 19
		claims.JTI = "ticket_19"
	})
	intruder, response := env.dial(t, token, "client_1")
	if intruder == nil {
		t.Fatalf("dial epoch 19 failed: %v", response)
	}
	if err := intruder.WriteMessage(websocket.TextMessage, []byte(`{"type":"device.hello","version":1}`)); err != nil {
		t.Fatal(err)
	}
	readUntilClosed(t, intruder, 3*time.Second)
	waitUntil(t, 3*time.Second, func() bool { return env.connectionCount() == 1 })

	if got := env.server.connectionBySession("session_1"); got != accepted {
		t.Fatalf("session route = %p, want accepted connection %p", got, accepted)
	}
	core.inject(deviceGenerationEvent("session_1", 18, 1, 1, 1, mediav1.GenerationAction_GENERATION_ACTION_START))
	messageType, payload, err := readDeviceMessage(device, 3*time.Second)
	if err != nil {
		t.Fatalf("Core event did not reach the accepted device: %v", err)
	}
	var generation deviceGenerationControl
	if messageType != websocket.TextMessage ||
		json.Unmarshal(payload, &generation) != nil || generation.Type != "generation.started" {
		t.Fatalf("unexpected device message type=%d payload=%s", messageType, payload)
	}
}
