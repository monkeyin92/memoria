package mediaedge

import (
	"context"
	"errors"
	"fmt"
	"io"
	"log"
	"sync"
	"sync/atomic"
	"time"

	"google.golang.org/grpc/connectivity"
)

const (
	voiceCoreSupervisorProbeInterval      = time.Second
	voiceCoreSupervisorProbeTimeout       = 750 * time.Millisecond
	voiceCoreSupervisorUnavailableGrace   = 2 * time.Second
	voiceCoreSupervisorInitialBackoff     = 250 * time.Millisecond
	voiceCoreSupervisorMaximumBackoff     = 5 * time.Second
	voiceCoreSupervisorDefaultDialTimeout = 5 * time.Second
)

// VoiceCoreBridgeConnector is the session-opening contract shared by a raw
// VoiceCoreBridge and its process-level supervisor. A replacement transport
// never inherits or replays an existing session; devices reconnect through the
// normal strictly-increasing stream_epoch path after the old channel closes.
type VoiceCoreBridgeConnector interface {
	Ready() bool
	ConnectWithHandshakeContext(
		streamCtx context.Context,
		handshakeCtx context.Context,
		identity BridgeIdentity,
		uplink BridgeAudioFormat,
		downlink BridgeAudioFormat,
	) (*VoiceCoreSession, error)
	Close() error
}

type voiceCoreBridgeTransport interface {
	VoiceCoreBridgeConnector
	State() connectivity.State
	Probe(context.Context) error
}

type voiceCoreBridgeDialer func(context.Context, VoiceCoreBridgeConfig) (voiceCoreBridgeTransport, error)

type voiceCoreBridgeSupervisorSettings struct {
	probeInterval    time.Duration
	probeTimeout     time.Duration
	unavailableGrace time.Duration
	initialBackoff   time.Duration
	maximumBackoff   time.Duration
	dialer           voiceCoreBridgeDialer
}

func defaultVoiceCoreBridgeSupervisorSettings() voiceCoreBridgeSupervisorSettings {
	return voiceCoreBridgeSupervisorSettings{
		probeInterval:    voiceCoreSupervisorProbeInterval,
		probeTimeout:     voiceCoreSupervisorProbeTimeout,
		unavailableGrace: voiceCoreSupervisorUnavailableGrace,
		initialBackoff:   voiceCoreSupervisorInitialBackoff,
		maximumBackoff:   voiceCoreSupervisorMaximumBackoff,
		dialer: func(ctx context.Context, config VoiceCoreBridgeConfig) (voiceCoreBridgeTransport, error) {
			return DialVoiceCore(ctx, config)
		},
	}
}

// VoiceCoreBridgeSupervisor owns the process-level Edge -> Voice Core
// transport. After sustained channel unavailability it constructs a new gRPC
// ClientConn, which forces resolver state to be rebuilt, then atomically swaps
// the transport and closes the old one. Closing the old channel deliberately
// fails its streams closed; no SessionHello, audio, generation, or tool event
// is replayed by the supervisor.
type VoiceCoreBridgeSupervisor struct {
	ctx            context.Context
	cancel         context.CancelFunc
	config         VoiceCoreBridgeConfig
	connectTimeout time.Duration
	settings       voiceCoreBridgeSupervisorSettings
	wake           chan struct{}
	done           chan struct{}

	mu               sync.RWMutex
	bridge           voiceCoreBridgeTransport
	generation       uint64
	lastState        connectivity.State
	lastProbeHealthy bool
	unavailableSince time.Time
	nextAttempt      time.Time
	backoff          time.Duration
	closed           bool

	redialAttempts         atomic.Uint64
	redialSuccesses        atomic.Uint64
	redialFailures         atomic.Uint64
	redialDiscarded        atomic.Uint64
	livenessProbeSuccesses atomic.Uint64
	livenessProbeFailures  atomic.Uint64
	closeOnce              sync.Once
	closeErr               error
}

// NewVoiceCoreBridgeSupervisor dials the initial channel synchronously so
// startup remains fail-closed, then starts the runtime redial supervisor.
func NewVoiceCoreBridgeSupervisor(
	parent context.Context,
	config VoiceCoreBridgeConfig,
	connectTimeout time.Duration,
) (*VoiceCoreBridgeSupervisor, error) {
	return newVoiceCoreBridgeSupervisor(
		parent, config, connectTimeout, defaultVoiceCoreBridgeSupervisorSettings(),
	)
}

func newVoiceCoreBridgeSupervisor(
	parent context.Context,
	config VoiceCoreBridgeConfig,
	connectTimeout time.Duration,
	settings voiceCoreBridgeSupervisorSettings,
) (*VoiceCoreBridgeSupervisor, error) {
	if parent == nil {
		return nil, errors.New("voice-core bridge supervisor context is required")
	}
	if connectTimeout <= 0 {
		connectTimeout = voiceCoreSupervisorDefaultDialTimeout
	}
	settings = normalizeVoiceCoreBridgeSupervisorSettings(settings)
	ctx, cancel := context.WithCancel(parent)
	dialCtx, dialCancel := context.WithTimeout(ctx, connectTimeout)
	initial, err := settings.dialer(dialCtx, config)
	dialCancel()
	if err != nil {
		cancel()
		return nil, err
	}
	if initial == nil || !initial.Ready() {
		if initial != nil {
			_ = initial.Close()
		}
		cancel()
		return nil, errors.New("voice-core bridge supervisor initial channel is not ready")
	}
	probeCtx, probeCancel := context.WithTimeout(ctx, settings.probeTimeout)
	probeErr := initial.Probe(probeCtx)
	probeCancel()
	if probeErr != nil || !initial.Ready() {
		_ = initial.Close()
		cancel()
		if probeErr != nil {
			return nil, fmt.Errorf("voice-core bridge supervisor initial liveness probe: %w", probeErr)
		}
		return nil, errors.New("voice-core bridge supervisor initial channel became unavailable after liveness probe")
	}
	supervisor := &VoiceCoreBridgeSupervisor{
		ctx: ctx, cancel: cancel, config: config, connectTimeout: connectTimeout, settings: settings,
		wake: make(chan struct{}, 1), done: make(chan struct{}), bridge: initial, generation: 1,
		lastState: initial.State(), lastProbeHealthy: true, backoff: settings.initialBackoff,
	}
	go supervisor.run()
	return supervisor, nil
}

func normalizeVoiceCoreBridgeSupervisorSettings(
	settings voiceCoreBridgeSupervisorSettings,
) voiceCoreBridgeSupervisorSettings {
	defaults := defaultVoiceCoreBridgeSupervisorSettings()
	if settings.probeInterval <= 0 {
		settings.probeInterval = defaults.probeInterval
	}
	if settings.probeTimeout <= 0 {
		settings.probeTimeout = defaults.probeTimeout
	}
	if settings.unavailableGrace <= 0 {
		settings.unavailableGrace = defaults.unavailableGrace
	}
	if settings.initialBackoff <= 0 {
		settings.initialBackoff = defaults.initialBackoff
	}
	if settings.maximumBackoff < settings.initialBackoff {
		settings.maximumBackoff = defaults.maximumBackoff
	}
	if settings.dialer == nil {
		settings.dialer = defaults.dialer
	}
	return settings
}

func (s *VoiceCoreBridgeSupervisor) run() {
	defer close(s.done)
	ticker := time.NewTicker(s.settings.probeInterval)
	defer ticker.Stop()
	for {
		select {
		case <-s.ctx.Done():
			return
		case <-ticker.C:
			s.reconcile(false)
		case <-s.wake:
			s.reconcile(true)
		}
	}
}

func (s *VoiceCoreBridgeSupervisor) reconcile(force bool) {
	now := time.Now()
	s.mu.RLock()
	if s.closed || s.bridge == nil {
		s.mu.RUnlock()
		return
	}
	bridge := s.bridge
	generation := s.generation
	s.mu.RUnlock()

	state, probeErr := s.probe(bridge)
	healthy := probeErr == nil && state == connectivity.Ready

	s.mu.Lock()
	if s.closed || s.generation != generation {
		s.mu.Unlock()
		return
	}
	s.lastState = state
	s.lastProbeHealthy = healthy
	if healthy {
		s.unavailableSince = time.Time{}
		s.nextAttempt = time.Time{}
		s.backoff = s.settings.initialBackoff
		s.mu.Unlock()
		return
	}
	if s.unavailableSince.IsZero() {
		s.unavailableSince = now
	}
	// A failed session open may bypass the initial grace period, but it must
	// never erase a retry window established by a previous failed redial.
	// Otherwise a burst of device reconnects could defeat backoff and turn a
	// Voice Core outage into a channel-creation storm.
	bypassGrace := force && s.nextAttempt.IsZero()
	if (!bypassGrace && now.Sub(s.unavailableSince) < s.settings.unavailableGrace) ||
		(!s.nextAttempt.IsZero() && now.Before(s.nextAttempt)) {
		s.mu.Unlock()
		return
	}
	unavailableFor := now.Sub(s.unavailableSince)
	s.mu.Unlock()

	attempt := s.redialAttempts.Add(1)
	log.Printf(
		"media edge Voice Core channel redial attempt=%d generation=%d state=%s health_err=%v unavailable_ms=%d",
		attempt, generation, state, probeErr, unavailableFor.Milliseconds(),
	)
	dialCtx, cancel := context.WithTimeout(s.ctx, s.connectTimeout)
	candidate, err := s.settings.dialer(dialCtx, s.config)
	cancel()
	if err != nil {
		if s.ctx.Err() != nil {
			s.redialDiscarded.Add(1)
			return
		}
		s.recordRedialFailure(attempt, err)
		return
	}
	if candidate == nil || !candidate.Ready() {
		if candidate != nil {
			_ = candidate.Close()
		}
		if s.ctx.Err() != nil {
			s.redialDiscarded.Add(1)
			return
		}
		s.recordRedialFailure(attempt, errors.New("replacement Voice Core channel is not ready"))
		return
	}
	candidateState, candidateProbeErr := s.probe(candidate)
	if candidateProbeErr != nil || candidateState != connectivity.Ready {
		_ = candidate.Close()
		if s.ctx.Err() != nil {
			s.redialDiscarded.Add(1)
			return
		}
		if candidateProbeErr != nil {
			s.recordRedialFailure(attempt, fmt.Errorf("replacement Voice Core liveness probe: %w", candidateProbeErr))
		} else {
			s.recordRedialFailure(attempt, errors.New("replacement Voice Core channel became unavailable after liveness probe"))
		}
		return
	}

	// The existing connection may have recovered while a replacement was
	// dialing. Verify the application-level Health RPC again instead of
	// trusting its transport state, which can remain READY after the Bridge
	// process has stopped serving media.
	currentState, currentProbeErr := s.probe(bridge)
	currentHealthy := currentProbeErr == nil && currentState == connectivity.Ready

	s.mu.Lock()
	if s.closed || s.generation != generation {
		s.mu.Unlock()
		_ = candidate.Close()
		s.redialDiscarded.Add(1)
		return
	}
	// Preserve a recovered channel and its live streams only after the real
	// Health RPC succeeds.
	if currentHealthy {
		s.lastState = currentState
		s.lastProbeHealthy = true
		s.unavailableSince = time.Time{}
		s.nextAttempt = time.Time{}
		s.backoff = s.settings.initialBackoff
		s.mu.Unlock()
		_ = candidate.Close()
		s.redialDiscarded.Add(1)
		log.Printf(
			"media edge Voice Core channel redial discarded attempt=%d reason=current_channel_recovered",
			attempt,
		)
		return
	}
	old := s.bridge
	s.bridge = candidate
	s.generation++
	newGeneration := s.generation
	s.lastState = candidateState
	s.lastProbeHealthy = true
	s.unavailableSince = time.Time{}
	s.nextAttempt = time.Time{}
	s.backoff = s.settings.initialBackoff
	s.mu.Unlock()

	s.redialSuccesses.Add(1)
	// Closing the old ClientConn terminates old streams. Their owning device
	// transports must reconnect with a higher stream_epoch; the supervisor
	// intentionally has no session registry and cannot replay them.
	_ = old.Close()
	log.Printf(
		"media edge Voice Core channel redial succeeded attempt=%d generation=%d",
		attempt, newGeneration,
	)
}

func (s *VoiceCoreBridgeSupervisor) probe(bridge voiceCoreBridgeTransport) (connectivity.State, error) {
	probeCtx, cancel := context.WithTimeout(s.ctx, s.settings.probeTimeout)
	err := bridge.Probe(probeCtx)
	cancel()
	state := bridge.State()
	if err != nil {
		s.livenessProbeFailures.Add(1)
		return state, err
	}
	s.livenessProbeSuccesses.Add(1)
	return state, nil
}

func (s *VoiceCoreBridgeSupervisor) recordRedialFailure(attempt uint64, err error) {
	if s.ctx.Err() != nil {
		s.redialDiscarded.Add(1)
		return
	}
	s.mu.Lock()
	if s.closed || s.ctx.Err() != nil {
		s.mu.Unlock()
		s.redialDiscarded.Add(1)
		return
	}
	delay := s.backoff
	s.nextAttempt = time.Now().Add(delay)
	s.backoff *= 2
	if s.backoff > s.settings.maximumBackoff {
		s.backoff = s.settings.maximumBackoff
	}
	s.mu.Unlock()
	s.redialFailures.Add(1)
	log.Printf(
		"media edge Voice Core channel redial failed attempt=%d retry_in_ms=%d err=%v",
		attempt, delay.Milliseconds(), err,
	)
}

func (s *VoiceCoreBridgeSupervisor) Ready() bool {
	s.mu.RLock()
	bridge, closed, healthy := s.bridge, s.closed, s.lastProbeHealthy
	s.mu.RUnlock()
	return !closed && healthy && bridge != nil && bridge.Ready()
}

func (s *VoiceCoreBridgeSupervisor) ConnectWithHandshakeContext(
	streamCtx context.Context,
	handshakeCtx context.Context,
	identity BridgeIdentity,
	uplink BridgeAudioFormat,
	downlink BridgeAudioFormat,
) (*VoiceCoreSession, error) {
	s.mu.RLock()
	bridge, closed, healthy := s.bridge, s.closed, s.lastProbeHealthy
	s.mu.RUnlock()
	if closed || bridge == nil {
		return nil, errors.New("voice-core bridge supervisor is closed")
	}
	if !healthy || !bridge.Ready() {
		s.requestRedial()
		return nil, fmt.Errorf("voice-core bridge channel is unavailable: %s", bridge.State())
	}
	session, err := bridge.ConnectWithHandshakeContext(
		streamCtx, handshakeCtx, identity, uplink, downlink,
	)
	if err != nil {
		s.requestRedial()
	}
	return session, err
}

func (s *VoiceCoreBridgeSupervisor) requestRedial() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

// WriteMetrics emits bounded-cardinality Prometheus metrics suitable for
// readiness alerting and hot-replacement acceptance evidence.
func (s *VoiceCoreBridgeSupervisor) WriteMetrics(w io.Writer) {
	now := time.Now()
	s.mu.RLock()
	bridge := s.bridge
	generation := s.generation
	state := s.lastState
	lastProbeHealthy := s.lastProbeHealthy
	unavailableSince := s.unavailableSince
	closed := s.closed
	s.mu.RUnlock()
	if bridge != nil && !closed {
		state = bridge.State()
	}
	livenessHealthy := !closed && state == connectivity.Ready && lastProbeHealthy
	unavailableSeconds := 0.0
	if !livenessHealthy && !unavailableSince.IsZero() {
		unavailableSeconds = now.Sub(unavailableSince).Seconds()
	}
	_, _ = fmt.Fprintln(w, "# TYPE media_edge_voice_core_redial_attempts_total counter")
	_, _ = fmt.Fprintf(w, "media_edge_voice_core_redial_attempts_total %d\n", s.redialAttempts.Load())
	_, _ = fmt.Fprintln(w, "# TYPE media_edge_voice_core_redial_successes_total counter")
	_, _ = fmt.Fprintf(w, "media_edge_voice_core_redial_successes_total %d\n", s.redialSuccesses.Load())
	_, _ = fmt.Fprintln(w, "# TYPE media_edge_voice_core_redial_failures_total counter")
	_, _ = fmt.Fprintf(w, "media_edge_voice_core_redial_failures_total %d\n", s.redialFailures.Load())
	_, _ = fmt.Fprintln(w, "# TYPE media_edge_voice_core_redial_discarded_total counter")
	_, _ = fmt.Fprintf(w, "media_edge_voice_core_redial_discarded_total %d\n", s.redialDiscarded.Load())
	_, _ = fmt.Fprintln(w, "# TYPE media_edge_voice_core_liveness_probe_successes_total counter")
	_, _ = fmt.Fprintf(w, "media_edge_voice_core_liveness_probe_successes_total %d\n", s.livenessProbeSuccesses.Load())
	_, _ = fmt.Fprintln(w, "# TYPE media_edge_voice_core_liveness_probe_failures_total counter")
	_, _ = fmt.Fprintf(w, "media_edge_voice_core_liveness_probe_failures_total %d\n", s.livenessProbeFailures.Load())
	_, _ = fmt.Fprintln(w, "# TYPE media_edge_voice_core_liveness_healthy gauge")
	_, _ = fmt.Fprintf(w, "media_edge_voice_core_liveness_healthy %d\n", map[bool]int{true: 1, false: 0}[livenessHealthy])
	_, _ = fmt.Fprintln(w, "# TYPE media_edge_voice_core_channel_generation gauge")
	_, _ = fmt.Fprintf(w, "media_edge_voice_core_channel_generation %d\n", generation)
	_, _ = fmt.Fprintln(w, "# TYPE media_edge_voice_core_channel_state gauge")
	for _, candidateState := range []connectivity.State{
		connectivity.Idle,
		connectivity.Connecting,
		connectivity.Ready,
		connectivity.TransientFailure,
		connectivity.Shutdown,
	} {
		value := 0
		if state == candidateState {
			value = 1
		}
		_, _ = fmt.Fprintf(
			w, "media_edge_voice_core_channel_state{state=%q} %d\n",
			candidateState.String(), value,
		)
	}
	_, _ = fmt.Fprintln(w, "# TYPE media_edge_voice_core_consecutive_unavailable_seconds gauge")
	_, _ = fmt.Fprintf(w, "media_edge_voice_core_consecutive_unavailable_seconds %g\n", unavailableSeconds)
}

func (s *VoiceCoreBridgeSupervisor) Close() error {
	s.closeOnce.Do(func() {
		s.mu.Lock()
		s.closed = true
		cancel := s.cancel
		done := s.done
		s.mu.Unlock()
		if cancel != nil {
			cancel()
		}
		if done != nil {
			<-done
		}
		s.mu.Lock()
		bridge := s.bridge
		s.bridge = nil
		s.lastState = connectivity.Shutdown
		s.lastProbeHealthy = false
		s.unavailableSince = time.Time{}
		s.nextAttempt = time.Time{}
		s.mu.Unlock()
		if bridge != nil {
			s.closeErr = bridge.Close()
		}
	})
	return s.closeErr
}
