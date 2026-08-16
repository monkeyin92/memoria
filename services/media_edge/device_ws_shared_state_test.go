package mediaedge

import (
	"crypto/tls"
	"errors"
	"fmt"
	"math"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/alicebob/miniredis/v2"
)

func TestRedisDeviceStateRejectsTLSIdentityOnPlaintextURL(t *testing.T) {
	_, err := NewRedisDeviceState(RedisDeviceStateConfig{
		URL:       "redis://127.0.0.1:6379/4",
		TLSConfig: &tls.Config{MinVersion: tls.VersionTLS12},
		OwnerID:   "edge-test",
	})
	if err == nil || !strings.Contains(err.Error(), "requires rediss://") {
		t.Fatalf("plaintext Redis accepted a TLS identity: %v", err)
	}
}

type redisTestProcess struct {
	url  string
	stop func()
}

func startRedisTestProcess(t *testing.T) redisTestProcess {
	t.Helper()
	path, err := exec.LookPath("redis-server")
	if os.Getenv("MEDIA_EDGE_TEST_FORCE_MINIREDIS") == "1" {
		err = exec.ErrNotFound
	}
	if err != nil {
		server, runErr := miniredis.Run()
		if runErr != nil {
			t.Fatal(runErr)
		}
		stopped := false
		stop := func() {
			if stopped {
				return
			}
			stopped = true
			server.Close()
		}
		t.Cleanup(stop)
		return redisTestProcess{url: "redis://" + server.Addr() + "/15", stop: stop}
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	port := listener.Addr().(*net.TCPAddr).Port
	_ = listener.Close()
	command := exec.Command(
		path,
		"--bind", "127.0.0.1",
		"--protected-mode", "yes",
		"--port", strconv.Itoa(port),
		"--save", "",
		"--appendonly", "no",
		"--dir", filepath.Clean(t.TempDir()),
	)
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	stopped := false
	stop := func() {
		if stopped {
			return
		}
		stopped = true
		if command.Process != nil {
			_ = command.Process.Kill()
		}
		_ = command.Wait()
	}
	t.Cleanup(stop)
	address := net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		connection, dialErr := net.DialTimeout("tcp", address, 50*time.Millisecond)
		if dialErr == nil {
			_ = connection.Close()
			return redisTestProcess{url: "redis://" + address + "/15", stop: stop}
		}
		time.Sleep(10 * time.Millisecond)
	}
	stop()
	t.Fatal("redis-server did not become ready")
	return redisTestProcess{}
}

func newRedisDeviceStateForTest(
	t *testing.T,
	url string,
	ownerID string,
	prefix string,
) *RedisDeviceState {
	t.Helper()
	state, err := NewRedisDeviceState(RedisDeviceStateConfig{
		URL: url, OwnerID: ownerID, KeyPrefix: prefix,
		CommandTimeout:     500 * time.Millisecond,
		LeaseTTL:           3 * time.Second,
		LeaseCheckInterval: 500 * time.Millisecond,
	})
	if err != nil {
		t.Fatal(err)
	}
	return state
}

func TestRedisDeviceTicketStoreRejectsReplayAcrossInstances(t *testing.T) {
	redisProcess := startRedisTestProcess(t)
	prefix := fmt.Sprintf("memoria:test:%d", time.Now().UnixNano())
	firstState := newRedisDeviceStateForTest(t, redisProcess.url, "edge-a", prefix)
	secondState := newRedisDeviceStateForTest(t, redisProcess.url, "edge-b", prefix)
	firstServer := NewDeviceWSServer(DeviceJWTVerifier{})
	secondServer := NewDeviceWSServer(DeviceJWTVerifier{})
	firstState.Install(firstServer)
	secondState.Install(secondServer)
	t.Cleanup(firstServer.Close)
	t.Cleanup(secondServer.Close)

	expiry := time.Now().Add(time.Minute).Unix()
	if err := firstServer.Tickets.Consume("single-use-ticket", expiry); err != nil {
		t.Fatal(err)
	}
	if err := secondServer.Tickets.Consume("single-use-ticket", expiry); err == nil {
		t.Fatal("ticket replay was accepted by a second Edge instance")
	}
	if secondServer.Tickets.ReplayCount() != 1 {
		t.Fatalf("replay count = %d, want 1", secondServer.Tickets.ReplayCount())
	}
}

func TestRedisDeviceLeaseAtomicallyTakesOverAndNotifiesOldOwner(t *testing.T) {
	redisProcess := startRedisTestProcess(t)
	prefix := fmt.Sprintf("memoria:test:%d", time.Now().UnixNano())
	firstState := newRedisDeviceStateForTest(t, redisProcess.url, "edge-a", prefix)
	secondState := newRedisDeviceStateForTest(t, redisProcess.url, "edge-b", prefix)
	firstServer := NewDeviceWSServer(DeviceJWTVerifier{})
	secondServer := NewDeviceWSServer(DeviceJWTVerifier{})
	firstState.Install(firstServer)
	secondState.Install(secondServer)
	t.Cleanup(firstServer.Close)
	t.Cleanup(secondServer.Close)

	superseded := make(chan DeviceLease, 1)
	firstServer.Leases.SetSupersedeHandler(func(lease DeviceLease) {
		superseded <- lease
	})
	first := DeviceLease{
		DeviceID: "dev_1", SessionID: "session_1", StreamEpoch: 18,
		ConnID: 1, OwnerID: firstServer.Leases.OwnerID(),
	}
	if old, replaced, err := firstServer.Leases.Install(first); err != nil || replaced || old != (DeviceLease{}) {
		t.Fatalf("first lease install: old=%+v replaced=%v err=%v", old, replaced, err)
	}
	second := DeviceLease{
		DeviceID: "dev_1", SessionID: "session_1", StreamEpoch: 19,
		ConnID: 1, OwnerID: secondServer.Leases.OwnerID(),
	}
	old, replaced, err := secondServer.Leases.Install(second)
	if err != nil || !replaced || old.StreamEpoch != 18 || old.OwnerID != "edge-a" {
		t.Fatalf("takeover: old=%+v replaced=%v err=%v", old, replaced, err)
	}
	select {
	case notification := <-superseded:
		if notification != first {
			t.Fatalf("supersede notification = %+v, want %+v", notification, first)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("old Edge owner did not receive the supersession notification")
	}

	if owned, refreshErr := firstServer.Leases.Refresh(first); refreshErr != nil || owned {
		t.Fatalf("old lease refresh: owned=%v err=%v", owned, refreshErr)
	}
	if owned, refreshErr := secondServer.Leases.Refresh(second); refreshErr != nil || !owned {
		t.Fatalf("new lease refresh: owned=%v err=%v", owned, refreshErr)
	}
	firstServer.Leases.Release(first.DeviceID, first.ConnID)
	if owned, refreshErr := secondServer.Leases.Refresh(second); refreshErr != nil || !owned {
		t.Fatalf("stale release removed new lease: owned=%v err=%v", owned, refreshErr)
	}
	stale := first
	stale.ConnID = 2
	if _, _, err := firstServer.Leases.Install(stale); err == nil {
		t.Fatal("older stream epoch replaced the shared lease")
	}
	secondServer.Leases.Release(second.DeviceID, second.ConnID)
	if owned, refreshErr := secondServer.Leases.Refresh(second); refreshErr != nil || owned {
		t.Fatalf("released lease refresh: owned=%v err=%v", owned, refreshErr)
	}
}

func TestRedisDeviceLeaseComparesFullUint64EpochWithoutLuaRounding(t *testing.T) {
	redisProcess := startRedisTestProcess(t)
	state := newRedisDeviceStateForTest(
		t,
		redisProcess.url,
		"edge-a",
		fmt.Sprintf("memoria:test:%d", time.Now().UnixNano()),
	)
	server := NewDeviceWSServer(DeviceJWTVerifier{})
	state.Install(server)
	t.Cleanup(server.Close)

	first := DeviceLease{
		DeviceID: "dev_1", SessionID: "session_1",
		StreamEpoch: math.MaxUint64 - 1, ConnID: 1,
		OwnerID: server.Leases.OwnerID(),
	}
	if _, _, err := server.Leases.Install(first); err != nil {
		t.Fatal(err)
	}
	stale := first
	stale.StreamEpoch = math.MaxUint64 - 2
	stale.ConnID = 2
	if _, _, err := server.Leases.Install(stale); err == nil {
		t.Fatal("Lua rounding allowed a stale uint64 stream epoch")
	}
	newer := first
	newer.StreamEpoch = math.MaxUint64
	newer.ConnID = 3
	old, replaced, err := server.Leases.Install(newer)
	if err != nil || !replaced || old.StreamEpoch != first.StreamEpoch {
		t.Fatalf("max uint64 takeover: old=%+v replaced=%v err=%v", old, replaced, err)
	}
}

func TestRedisDeviceServerShutdownLeavesTTLLeaseAsCrashFence(t *testing.T) {
	redisProcess := startRedisTestProcess(t)
	prefix := fmt.Sprintf("memoria:test:%d", time.Now().UnixNano())
	firstState := newRedisDeviceStateForTest(t, redisProcess.url, "edge-a", prefix)
	secondState := newRedisDeviceStateForTest(t, redisProcess.url, "edge-b", prefix)
	firstServer := NewDeviceWSServer(DeviceJWTVerifier{})
	secondServer := NewDeviceWSServer(DeviceJWTVerifier{})
	firstState.Install(firstServer)
	secondState.Install(secondServer)
	t.Cleanup(secondServer.Close)

	first := DeviceLease{
		DeviceID: "dev_1", SessionID: "session_1", StreamEpoch: 18,
		ConnID: 1, OwnerID: firstServer.Leases.OwnerID(),
	}
	if _, _, err := firstServer.Leases.Install(first); err != nil {
		t.Fatal(err)
	}
	firstServer.Close()

	equalEpoch := first
	equalEpoch.OwnerID = secondServer.Leases.OwnerID()
	if _, _, err := secondServer.Leases.Install(equalEpoch); err == nil {
		t.Fatal("shutdown removed the TTL fence and allowed an equal epoch")
	}
	higherEpoch := equalEpoch
	higherEpoch.StreamEpoch++
	if _, replaced, err := secondServer.Leases.Install(higherEpoch); err != nil || !replaced {
		t.Fatalf("higher epoch could not replace shutdown fence: replaced=%v err=%v", replaced, err)
	}
}

func TestRedisDeviceLeaseTakeoverClosesSocketOwnedByAnotherServer(t *testing.T) {
	redisProcess := startRedisTestProcess(t)
	prefix := fmt.Sprintf("memoria:test:%d", time.Now().UnixNano())
	firstState := newRedisDeviceStateForTest(t, redisProcess.url, "edge-a", prefix)
	secondState := newRedisDeviceStateForTest(t, redisProcess.url, "edge-b", prefix)
	firstEnv := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		firstState.Install(server)
	})
	secondEnv := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		secondState.Install(server)
	})

	firstSocket, response := firstEnv.dial(t, firstEnv.token(t, nil), "client_1")
	if firstSocket == nil {
		t.Fatalf("first device connection failed: response=%v", response)
	}
	writeDeviceJSON(t, firstSocket, deviceV2Hello())
	_ = deviceReadAccepted(t, firstSocket)

	secondToken := secondEnv.token(t, func(claims *DeviceMediaClaims) {
		claims.StreamEpoch = 19
		claims.JTI = "ticket_2"
	})
	secondSocket, response := secondEnv.dial(t, secondToken, "client_1")
	if secondSocket == nil {
		t.Fatalf("takeover device connection failed: response=%v", response)
	}
	secondHello := deviceV2Hello()
	secondHello.StreamEpoch = 19
	writeDeviceJSON(t, secondSocket, secondHello)
	_ = deviceReadAccepted(t, secondSocket)

	if _, _, err := readDeviceMessage(firstSocket, 2*time.Second); err == nil {
		t.Fatal("old socket remained open after a cross-server lease takeover")
	}
	deadline := time.Now().Add(2 * time.Second)
	for firstEnv.server.Leases.ActiveCount() != 0 && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if firstEnv.server.Leases.ActiveCount() != 0 {
		t.Fatal("old server retained a local lease after cross-server takeover")
	}
	if secondEnv.server.Leases.ActiveCount() != 1 {
		t.Fatalf("new server active leases = %d, want 1", secondEnv.server.Leases.ActiveCount())
	}
}

func TestRedisTakeoverDuringRuntimeHandshakeCannotReviveClosedSocket(t *testing.T) {
	redisProcess := startRedisTestProcess(t)
	prefix := fmt.Sprintf("memoria:test:%d", time.Now().UnixNano())
	firstState := newRedisDeviceStateForTest(t, redisProcess.url, "edge-a", prefix)
	secondState := newRedisDeviceStateForTest(t, redisProcess.url, "edge-b", prefix)
	factoryStarted := make(chan struct{})
	releaseFactory := make(chan struct{})
	firstEnv := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		firstState.Install(server)
		originalFactory := server.RuntimeFactory
		server.RuntimeFactory = func(
			request OpenSessionRequest,
			session *Session,
			sender DownlinkSender,
		) (*VoiceCoreMediaRuntime, error) {
			close(factoryStarted)
			<-releaseFactory
			return originalFactory(request, session, sender)
		}
	})
	secondEnv := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		secondState.Install(server)
	})

	firstSocket, response := firstEnv.dial(t, firstEnv.token(t, nil), "client_1")
	if firstSocket == nil {
		t.Fatalf("first device connection failed: response=%v", response)
	}
	writeDeviceJSON(t, firstSocket, deviceV2Hello())
	select {
	case <-factoryStarted:
	case <-time.After(2 * time.Second):
		t.Fatal("first RuntimeFactory did not block")
	}

	secondToken := secondEnv.token(t, func(claims *DeviceMediaClaims) {
		claims.StreamEpoch = 19
		claims.JTI = "ticket_2"
	})
	secondSocket, response := secondEnv.dial(t, secondToken, "client_1")
	if secondSocket == nil {
		t.Fatalf("takeover device connection failed: response=%v", response)
	}
	secondHello := deviceV2Hello()
	secondHello.StreamEpoch = 19
	writeDeviceJSON(t, secondSocket, secondHello)
	_ = deviceReadAccepted(t, secondSocket)
	close(releaseFactory)

	if _, _, err := readDeviceMessage(firstSocket, 2*time.Second); err == nil {
		t.Fatal("superseded handshake revived the old socket")
	}
	deadline := time.Now().Add(2 * time.Second)
	var firstCore *deviceTestCore
	for time.Now().Before(deadline) {
		firstEnv.mu.Lock()
		firstCore = firstEnv.cores["session_1"]
		firstEnv.mu.Unlock()
		if firstCore != nil {
			select {
			case <-firstCore.closed:
				return
			default:
			}
		}
		time.Sleep(10 * time.Millisecond)
	}
	if firstCore == nil {
		t.Fatal("blocked RuntimeFactory did not return a runtime for cleanup")
	}
	t.Fatal("runtime created after supersession was leaked instead of closed")
}

func TestRedisDeviceSharedStateFailsClosedAfterRedisLoss(t *testing.T) {
	redisProcess := startRedisTestProcess(t)
	state := newRedisDeviceStateForTest(
		t,
		redisProcess.url,
		"edge-a",
		fmt.Sprintf("memoria:test:%d", time.Now().UnixNano()),
	)
	server := NewDeviceWSServer(DeviceJWTVerifier{})
	state.Install(server)
	t.Cleanup(server.Close)
	redisProcess.stop()

	err := server.Tickets.Consume("ticket-after-loss", time.Now().Add(time.Minute).Unix())
	if !errors.Is(err, ErrDeviceSharedStateUnavailable) {
		t.Fatalf("ticket store error = %v, want shared-state unavailable", err)
	}
	lease := DeviceLease{
		DeviceID: "dev_1", SessionID: "session_1", StreamEpoch: 1,
		ConnID: 1, OwnerID: server.Leases.OwnerID(),
	}
	if _, _, err := server.Leases.Install(lease); !errors.Is(err, ErrDeviceSharedStateUnavailable) {
		t.Fatalf("lease store error = %v, want shared-state unavailable", err)
	}
	server.RequireSharedState = true
	if server.Ready() {
		t.Fatal("server remained ready after shared Redis loss")
	}
}

func TestRedisDeviceLeaseWatcherClosesActiveSocketAfterRedisLoss(t *testing.T) {
	redisProcess := startRedisTestProcess(t)
	state := newRedisDeviceStateForTest(
		t,
		redisProcess.url,
		"edge-a",
		fmt.Sprintf("memoria:test:%d", time.Now().UnixNano()),
	)
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		state.Install(server)
		server.RequireSharedState = true
	})
	socket, response := env.dial(t, env.token(t, nil), "client_1")
	if socket == nil {
		t.Fatalf("device connection failed: response=%v", response)
	}
	writeDeviceJSON(t, socket, deviceV2Hello())
	_ = deviceReadAccepted(t, socket)
	redisProcess.stop()

	if _, _, err := readDeviceMessage(socket, 3*time.Second); err == nil {
		t.Fatal("active socket survived loss of its shared lease authority")
	}
	if env.server.Ready() {
		t.Fatal("device server remained ready after shared Redis loss")
	}
}

func TestRedisDeviceStateRejectsUnsafeConfiguration(t *testing.T) {
	redisProcess := startRedisTestProcess(t)
	for name, config := range map[string]RedisDeviceStateConfig{
		"missing owner": {URL: redisProcess.url},
		"unsafe owner":  {URL: redisProcess.url, OwnerID: "edge/a"},
		"unsafe prefix": {URL: redisProcess.url, OwnerID: "edge-a", KeyPrefix: "../edge"},
		"short ttl": {
			URL: redisProcess.url, OwnerID: "edge-a",
			LeaseTTL: time.Second, LeaseCheckInterval: time.Second,
		},
	} {
		t.Run(name, func(t *testing.T) {
			if state, err := NewRedisDeviceState(config); err == nil {
				_ = state.Close()
				t.Fatal("unsafe Redis device-state configuration was accepted")
			}
		})
	}
}
