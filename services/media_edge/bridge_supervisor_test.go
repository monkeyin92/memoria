package mediaedge

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/grpc/connectivity"
)

type fakeVoiceCoreBridgeTransport struct {
	mu           sync.RWMutex
	state        connectivity.State
	probeErr     error
	connectCalls atomic.Uint64
	closeCalls   atomic.Uint64
}

func newFakeVoiceCoreBridgeTransport(state connectivity.State) *fakeVoiceCoreBridgeTransport {
	return &fakeVoiceCoreBridgeTransport{state: state}
}

func (f *fakeVoiceCoreBridgeTransport) Ready() bool {
	return f.State() == connectivity.Ready
}

func (f *fakeVoiceCoreBridgeTransport) State() connectivity.State {
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.state
}

func (f *fakeVoiceCoreBridgeTransport) setState(state connectivity.State) {
	f.mu.Lock()
	f.state = state
	f.mu.Unlock()
}

func (f *fakeVoiceCoreBridgeTransport) Probe(ctx context.Context) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	default:
	}
	f.mu.RLock()
	defer f.mu.RUnlock()
	return f.probeErr
}

func (f *fakeVoiceCoreBridgeTransport) setProbeError(err error) {
	f.mu.Lock()
	f.probeErr = err
	f.mu.Unlock()
}

func (f *fakeVoiceCoreBridgeTransport) ConnectWithHandshakeContext(
	context.Context,
	context.Context,
	BridgeIdentity,
	BridgeAudioFormat,
	BridgeAudioFormat,
) (*VoiceCoreSession, error) {
	f.connectCalls.Add(1)
	return nil, nil
}

func (f *fakeVoiceCoreBridgeTransport) Close() error {
	f.closeCalls.Add(1)
	f.setState(connectivity.Shutdown)
	return nil
}

func testVoiceCoreSupervisorSettings(
	dialer voiceCoreBridgeDialer,
) voiceCoreBridgeSupervisorSettings {
	return voiceCoreBridgeSupervisorSettings{
		probeInterval:    time.Millisecond,
		probeTimeout:     10 * time.Millisecond,
		unavailableGrace: time.Millisecond,
		initialBackoff:   5 * time.Millisecond,
		maximumBackoff:   10 * time.Millisecond,
		dialer:           dialer,
	}
}

func waitForSupervisorCondition(t *testing.T, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatal("timed out waiting for Voice Core bridge supervisor condition")
}

func supervisorGeneration(supervisor *VoiceCoreBridgeSupervisor) uint64 {
	supervisor.mu.RLock()
	defer supervisor.mu.RUnlock()
	return supervisor.generation
}

func TestVoiceCoreBridgeSupervisorInitialChannelMustBeReady(t *testing.T) {
	initial := newFakeVoiceCoreBridgeTransport(connectivity.Connecting)
	settings := testVoiceCoreSupervisorSettings(func(
		context.Context, VoiceCoreBridgeConfig,
	) (voiceCoreBridgeTransport, error) {
		return initial, nil
	})

	supervisor, err := newVoiceCoreBridgeSupervisor(
		context.Background(), VoiceCoreBridgeConfig{}, 50*time.Millisecond, settings,
	)
	if err == nil || !strings.Contains(err.Error(), "initial channel is not ready") {
		t.Fatalf("unexpected startup result: supervisor=%v err=%v", supervisor, err)
	}
	if got := initial.closeCalls.Load(); got != 1 {
		t.Fatalf("non-ready initial channel close calls=%d, want 1", got)
	}
}

func TestVoiceCoreBridgeSupervisorInitialChannelMustPassLivenessProbe(t *testing.T) {
	initial := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	initial.setProbeError(errors.New("Voice Core not serving"))
	settings := testVoiceCoreSupervisorSettings(func(
		context.Context, VoiceCoreBridgeConfig,
	) (voiceCoreBridgeTransport, error) {
		return initial, nil
	})

	supervisor, err := newVoiceCoreBridgeSupervisor(
		context.Background(), VoiceCoreBridgeConfig{}, 50*time.Millisecond, settings,
	)
	if err == nil || !strings.Contains(err.Error(), "initial liveness probe") {
		t.Fatalf("unexpected startup result: supervisor=%v err=%v", supervisor, err)
	}
	if got := initial.closeCalls.Load(); got != 1 {
		t.Fatalf("unhealthy initial channel close calls=%d, want 1", got)
	}
}

func TestVoiceCoreBridgeSupervisorSwapsTransportWithoutReplayingSessions(t *testing.T) {
	initial := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	replacement := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	var dialCalls atomic.Uint64
	settings := testVoiceCoreSupervisorSettings(func(
		context.Context, VoiceCoreBridgeConfig,
	) (voiceCoreBridgeTransport, error) {
		if dialCalls.Add(1) == 1 {
			return initial, nil
		}
		return replacement, nil
	})
	supervisor, err := newVoiceCoreBridgeSupervisor(
		context.Background(), VoiceCoreBridgeConfig{}, 50*time.Millisecond, settings,
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = supervisor.Close() })

	if _, err := supervisor.ConnectWithHandshakeContext(
		context.Background(), context.Background(), BridgeIdentity{},
		BridgeAudioFormat{}, BridgeAudioFormat{},
	); err != nil {
		t.Fatal(err)
	}
	initial.setState(connectivity.TransientFailure)
	supervisor.requestRedial()
	waitForSupervisorCondition(t, func() bool { return supervisorGeneration(supervisor) == 2 })

	if got := initial.closeCalls.Load(); got != 1 {
		t.Fatalf("retired channel close calls=%d, want 1", got)
	}
	if got := initial.connectCalls.Load(); got != 1 {
		t.Fatalf("initial channel session opens=%d, want 1", got)
	}
	if got := replacement.connectCalls.Load(); got != 0 {
		t.Fatalf("replacement channel replayed %d sessions", got)
	}
	if got := supervisor.redialAttempts.Load(); got != 1 {
		t.Fatalf("redial attempts=%d, want 1", got)
	}
	if got := supervisor.redialSuccesses.Load(); got != 1 {
		t.Fatalf("redial successes=%d, want 1", got)
	}
	if got := supervisor.redialDiscarded.Load(); got != 0 {
		t.Fatalf("redial discarded=%d, want 0", got)
	}
}

func TestVoiceCoreBridgeSupervisorReplacesReadyButUnhealthyTransport(t *testing.T) {
	initial := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	replacement := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	var dialCalls atomic.Uint64
	settings := testVoiceCoreSupervisorSettings(func(
		context.Context, VoiceCoreBridgeConfig,
	) (voiceCoreBridgeTransport, error) {
		if dialCalls.Add(1) == 1 {
			return initial, nil
		}
		return replacement, nil
	})
	settings.unavailableGrace = 50 * time.Millisecond
	supervisor, err := newVoiceCoreBridgeSupervisor(
		context.Background(), VoiceCoreBridgeConfig{}, 50*time.Millisecond, settings,
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = supervisor.Close() })

	initial.setProbeError(errors.New("Voice Core application stopped serving"))
	if got := initial.State(); got != connectivity.Ready {
		t.Fatalf("test setup transport state=%s, want READY", got)
	}
	waitForSupervisorCondition(t, func() bool { return supervisor.livenessProbeFailures.Load() > 0 })
	if supervisor.Ready() {
		t.Fatal("supervisor stayed ready after a failed application liveness probe")
	}
	if got := supervisorGeneration(supervisor); got != 1 {
		t.Fatalf("generation=%d before grace elapsed, want 1", got)
	}
	waitForSupervisorCondition(t, func() bool { return supervisorGeneration(supervisor) == 2 })

	if got := initial.closeCalls.Load(); got != 1 {
		t.Fatalf("unhealthy READY channel close calls=%d, want 1", got)
	}
	if got := initial.connectCalls.Load(); got != 0 {
		t.Fatalf("initial channel session opens=%d, want 0", got)
	}
	if got := replacement.connectCalls.Load(); got != 0 {
		t.Fatalf("replacement channel replayed %d sessions", got)
	}
	if !supervisor.Ready() {
		t.Fatal("supervisor did not become ready after a healthy replacement")
	}
	if got := supervisor.redialSuccesses.Load(); got != 1 {
		t.Fatalf("redial successes=%d, want 1", got)
	}
}

func TestVoiceCoreBridgeSupervisorFailureBackoffDoublesAndCaps(t *testing.T) {
	supervisor := &VoiceCoreBridgeSupervisor{
		ctx: context.Background(),
		settings: voiceCoreBridgeSupervisorSettings{
			maximumBackoff: 20 * time.Millisecond,
		},
		backoff: 10 * time.Millisecond,
	}

	for attempt, expectedNextBackoff := range []time.Duration{
		20 * time.Millisecond,
		20 * time.Millisecond,
		20 * time.Millisecond,
	} {
		supervisor.recordRedialFailure(uint64(attempt+1), errors.New("unavailable"))
		supervisor.mu.RLock()
		backoff := supervisor.backoff
		nextAttempt := supervisor.nextAttempt
		supervisor.mu.RUnlock()
		if backoff != expectedNextBackoff {
			t.Fatalf("attempt %d next backoff=%s, want %s", attempt+1, backoff, expectedNextBackoff)
		}
		if !nextAttempt.After(time.Now()) {
			t.Fatalf("attempt %d did not schedule a future retry", attempt+1)
		}
	}
	if got := supervisor.redialFailures.Load(); got != 3 {
		t.Fatalf("redial failures=%d, want 3", got)
	}
}

func TestVoiceCoreBridgeSupervisorHonorsBackoffAndEventuallyRecovers(t *testing.T) {
	initial := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	replacement := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	var dialCalls atomic.Uint64
	settings := testVoiceCoreSupervisorSettings(func(
		context.Context, VoiceCoreBridgeConfig,
	) (voiceCoreBridgeTransport, error) {
		switch dialCalls.Add(1) {
		case 1:
			return initial, nil
		case 2:
			return nil, errors.New("Voice Core unavailable")
		default:
			return replacement, nil
		}
	})
	settings.initialBackoff = 100 * time.Millisecond
	settings.maximumBackoff = 100 * time.Millisecond
	supervisor, err := newVoiceCoreBridgeSupervisor(
		context.Background(), VoiceCoreBridgeConfig{}, 50*time.Millisecond, settings,
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = supervisor.Close() })

	initial.setState(connectivity.TransientFailure)
	supervisor.requestRedial()
	waitForSupervisorCondition(t, func() bool { return supervisor.redialFailures.Load() == 1 })
	for range 20 {
		supervisor.requestRedial()
	}
	time.Sleep(25 * time.Millisecond)
	if got := supervisor.redialAttempts.Load(); got != 1 {
		t.Fatalf("forced reconnect bypassed backoff: attempts=%d, want 1", got)
	}
	waitForSupervisorCondition(t, func() bool { return supervisorGeneration(supervisor) == 2 })
	if got := supervisor.redialAttempts.Load(); got != 2 {
		t.Fatalf("redial attempts=%d, want 2", got)
	}
	if got := supervisor.redialSuccesses.Load(); got != 1 {
		t.Fatalf("redial successes=%d, want 1", got)
	}
}

func TestVoiceCoreBridgeSupervisorPreservesRecoveredOldChannel(t *testing.T) {
	initial := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	replacement := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	dialStarted := make(chan struct{})
	releaseDial := make(chan struct{})
	var dialCalls atomic.Uint64
	settings := testVoiceCoreSupervisorSettings(func(
		ctx context.Context, _ VoiceCoreBridgeConfig,
	) (voiceCoreBridgeTransport, error) {
		if dialCalls.Add(1) == 1 {
			return initial, nil
		}
		close(dialStarted)
		select {
		case <-releaseDial:
			return replacement, nil
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	})
	supervisor, err := newVoiceCoreBridgeSupervisor(
		context.Background(), VoiceCoreBridgeConfig{}, 50*time.Millisecond, settings,
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = supervisor.Close() })

	initial.setState(connectivity.TransientFailure)
	supervisor.requestRedial()
	select {
	case <-dialStarted:
	case <-time.After(time.Second):
		t.Fatal("replacement dial did not start")
	}
	initial.setState(connectivity.Ready)
	close(releaseDial)
	waitForSupervisorCondition(t, func() bool { return replacement.closeCalls.Load() == 1 })
	if got := supervisorGeneration(supervisor); got != 1 {
		t.Fatalf("generation=%d, want recovered original generation 1", got)
	}
	if got := initial.closeCalls.Load(); got != 0 {
		t.Fatalf("recovered original channel close calls=%d, want 0", got)
	}
	if got := supervisor.redialSuccesses.Load(); got != 0 {
		t.Fatalf("redial successes=%d, want 0 without swap", got)
	}
	if got := supervisor.redialDiscarded.Load(); got != 1 {
		t.Fatalf("redial discarded=%d, want recovered candidate discard", got)
	}
	if attempts := supervisor.redialAttempts.Load(); attempts !=
		supervisor.redialSuccesses.Load()+supervisor.redialFailures.Load()+supervisor.redialDiscarded.Load() {
		t.Fatalf("redial terminal counters do not match attempts=%d", attempts)
	}
}

func TestVoiceCoreBridgeSupervisorForcedRedialPreservesUnavailableStart(t *testing.T) {
	initial := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	replacement := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	dialStarted := make(chan struct{})
	releaseDial := make(chan struct{})
	var dialCalls atomic.Uint64
	settings := testVoiceCoreSupervisorSettings(func(
		ctx context.Context, _ VoiceCoreBridgeConfig,
	) (voiceCoreBridgeTransport, error) {
		if dialCalls.Add(1) == 1 {
			return initial, nil
		}
		close(dialStarted)
		select {
		case <-releaseDial:
			return replacement, nil
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	})
	settings.unavailableGrace = time.Second
	supervisor, err := newVoiceCoreBridgeSupervisor(
		context.Background(), VoiceCoreBridgeConfig{}, 50*time.Millisecond, settings,
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = supervisor.Close() })

	started := time.Now()
	initial.setState(connectivity.TransientFailure)
	supervisor.requestRedial()
	select {
	case <-dialStarted:
	case <-time.After(time.Second):
		t.Fatal("forced replacement dial did not start")
	}
	supervisor.mu.RLock()
	unavailableSince := supervisor.unavailableSince
	supervisor.mu.RUnlock()
	if unavailableSince.Before(started.Add(-100 * time.Millisecond)) {
		t.Fatalf("forced redial backdated unavailable start: %s", unavailableSince)
	}
	close(releaseDial)
}

func TestVoiceCoreBridgeSupervisorConnectingFailsFastWithoutBypassingBackoff(t *testing.T) {
	initial := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	var dialCalls atomic.Uint64
	settings := testVoiceCoreSupervisorSettings(func(
		context.Context, VoiceCoreBridgeConfig,
	) (voiceCoreBridgeTransport, error) {
		if dialCalls.Add(1) == 1 {
			return initial, nil
		}
		return nil, errors.New("Voice Core connecting")
	})
	settings.initialBackoff = 100 * time.Millisecond
	settings.maximumBackoff = 100 * time.Millisecond
	supervisor, err := newVoiceCoreBridgeSupervisor(
		context.Background(), VoiceCoreBridgeConfig{}, 50*time.Millisecond, settings,
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = supervisor.Close() })

	initial.setState(connectivity.Connecting)
	for range 20 {
		if _, err := supervisor.ConnectWithHandshakeContext(
			context.Background(), context.Background(), BridgeIdentity{},
			BridgeAudioFormat{}, BridgeAudioFormat{},
		); err == nil || !strings.Contains(err.Error(), connectivity.Connecting.String()) {
			t.Fatalf("unexpected Connecting result: %v", err)
		}
	}
	waitForSupervisorCondition(t, func() bool { return supervisor.redialFailures.Load() == 1 })
	time.Sleep(25 * time.Millisecond)
	if got := supervisor.redialAttempts.Load(); got != 1 {
		t.Fatalf("Connecting requests bypassed backoff: attempts=%d, want 1", got)
	}
}

func TestVoiceCoreBridgeSupervisorCloseCancelsInFlightRedial(t *testing.T) {
	initial := newFakeVoiceCoreBridgeTransport(connectivity.Ready)
	dialStarted := make(chan struct{})
	var dialCalls atomic.Uint64
	settings := testVoiceCoreSupervisorSettings(func(
		ctx context.Context, _ VoiceCoreBridgeConfig,
	) (voiceCoreBridgeTransport, error) {
		if dialCalls.Add(1) == 1 {
			return initial, nil
		}
		close(dialStarted)
		<-ctx.Done()
		return nil, ctx.Err()
	})
	supervisor, err := newVoiceCoreBridgeSupervisor(
		context.Background(), VoiceCoreBridgeConfig{}, time.Second, settings,
	)
	if err != nil {
		t.Fatal(err)
	}
	initial.setState(connectivity.TransientFailure)
	supervisor.requestRedial()
	select {
	case <-dialStarted:
	case <-time.After(time.Second):
		t.Fatal("replacement dial did not start")
	}

	closed := make(chan error, 1)
	go func() { closed <- supervisor.Close() }()
	select {
	case err := <-closed:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(time.Second):
		t.Fatal("supervisor close deadlocked with in-flight redial")
	}
	if err := supervisor.Close(); err != nil {
		t.Fatal(err)
	}
	if got := initial.closeCalls.Load(); got != 1 {
		t.Fatalf("initial channel close calls=%d, want idempotent 1", got)
	}
	if got := supervisor.redialAttempts.Load(); got != 1 {
		t.Fatalf("redial attempts=%d, want 1", got)
	}
	if got := supervisor.redialDiscarded.Load(); got != 1 {
		t.Fatalf("cancelled redial discarded=%d, want 1", got)
	}
}

func TestVoiceCoreBridgeSupervisorMetricsAreExposedByServer(t *testing.T) {
	bridge := newFakeVoiceCoreBridgeTransport(connectivity.TransientFailure)
	supervisor := &VoiceCoreBridgeSupervisor{
		bridge:           bridge,
		generation:       3,
		lastState:        connectivity.TransientFailure,
		unavailableSince: time.Now().Add(-2 * time.Second),
	}
	supervisor.redialAttempts.Store(6)
	supervisor.redialSuccesses.Store(2)
	supervisor.redialFailures.Store(3)
	supervisor.redialDiscarded.Store(1)
	supervisor.livenessProbeSuccesses.Store(8)
	supervisor.livenessProbeFailures.Store(4)

	server := NewServer(JWTVerifier{}, 4)
	server.BridgeMetricsWriter = supervisor.WriteMetrics
	recorder := httptest.NewRecorder()
	server.metrics(recorder, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	body := recorder.Body.String()
	for _, expected := range []string{
		"media_edge_voice_core_redial_attempts_total 6",
		"media_edge_voice_core_redial_successes_total 2",
		"media_edge_voice_core_redial_failures_total 3",
		"media_edge_voice_core_redial_discarded_total 1",
		"media_edge_voice_core_liveness_probe_successes_total 8",
		"media_edge_voice_core_liveness_probe_failures_total 4",
		"media_edge_voice_core_liveness_healthy 0",
		"media_edge_voice_core_channel_generation 3",
		"media_edge_voice_core_channel_state{state=\"TRANSIENT_FAILURE\"} 1",
		"media_edge_voice_core_consecutive_unavailable_seconds",
	} {
		if !strings.Contains(body, expected) {
			t.Fatalf("metrics missing %q:\n%s", expected, body)
		}
	}
	for _, state := range []connectivity.State{
		connectivity.Idle, connectivity.Connecting, connectivity.Ready,
		connectivity.TransientFailure, connectivity.Shutdown,
	} {
		if !strings.Contains(body, "state=\""+state.String()+"\"") {
			t.Fatalf("metrics omitted channel state label %s", state)
		}
	}
	for _, line := range strings.Split(body, "\n") {
		if !strings.HasPrefix(line, "media_edge_voice_core_consecutive_unavailable_seconds ") {
			continue
		}
		value, err := strconv.ParseFloat(strings.TrimSpace(strings.TrimPrefix(
			line, "media_edge_voice_core_consecutive_unavailable_seconds ",
		)), 64)
		if err != nil || value < 1.9 {
			t.Fatalf("unexpected unavailable duration %q", line)
		}
		return
	}
	t.Fatal("unavailable duration metric was not emitted")
}

func TestVoiceCoreBridgeSupervisorCloseClearsUnavailableMetricsAndSupportsZeroValue(t *testing.T) {
	supervisor := &VoiceCoreBridgeSupervisor{
		lastState:        connectivity.TransientFailure,
		unavailableSince: time.Now().Add(-2 * time.Second),
	}
	if err := supervisor.Close(); err != nil {
		t.Fatal(err)
	}
	var body strings.Builder
	supervisor.WriteMetrics(&body)
	if !strings.Contains(body.String(), "media_edge_voice_core_channel_state{state=\"SHUTDOWN\"} 1") {
		t.Fatalf("closed supervisor did not report shutdown state:\n%s", body.String())
	}
	if !strings.Contains(body.String(), "media_edge_voice_core_consecutive_unavailable_seconds 0\n") {
		t.Fatalf("closed supervisor retained unavailable duration:\n%s", body.String())
	}
}
