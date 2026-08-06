package mediaedge

import (
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"
)

// DownlinkSenderFactory is supplied by the real media terminator. Its sender
// is wired into each Voice Core runtime, so production cannot be made ready
// with an unverified environment flag.
type DownlinkSenderFactory func(OpenSessionRequest, *Session) (DownlinkSender, error)

// BridgeRuntimeFactory is injected by a deployment that has an authenticated
// Voice Core stream. The default HTTP reference server leaves it nil and
// remains provider/media-terminator neutral.
type BridgeRuntimeFactory func(OpenSessionRequest, *Session, DownlinkSender) (*VoiceCoreMediaRuntime, error)

type Server struct {
	Directory             *Directory
	Verifier              JWTVerifier
	MaxPendingFrames      int
	BridgeFactory         BridgeRuntimeFactory
	DownlinkSenderFactory DownlinkSenderFactory
	// DownlinkReadyProbe is supplied by the same media terminator as the
	// sender factory. A factory function alone cannot prove that its RTP/WebRTC
	// transport can accept a new session.
	DownlinkReadyProbe func() bool
	ReadyProbe         func() bool
	WHIPHandler        http.Handler
	SessionCloseHook   func(string)
	// RequireExternalDownlinkSender makes the HTTP reference queue
	// development-only. A production embedding must expose a real media
	// terminator and report its readiness explicitly.
	RequireExternalDownlinkSender bool
	// AllowInsecureDevelopment must be explicitly enabled by a caller that
	// wants an unauthenticated local reference edge.  Leaving it false keeps
	// accidental non-production deployments fail-closed as well.
	AllowInsecureDevelopment bool
	Draining                 atomic.Bool
	Requests                 atomic.Uint64
	RejectedFrames           atomic.Uint64
	OpusFECFrames            atomic.Uint64
	OpusPLCFrames            atomic.Uint64
	OpusSilenceFrames        atomic.Uint64
	bridgeMu                 sync.Mutex
	openMu                   sync.Mutex
	openSemaphore            chan struct{}
	opening                  sync.WaitGroup
	metricsMu                sync.Mutex
	retiredMetrics           SessionStats
	bridges                  map[string]*VoiceCoreMediaRuntime
}

func NewServer(verifier JWTVerifier, maxPendingFrames int) *Server {
	if maxPendingFrames <= 0 {
		maxPendingFrames = 20
	}
	return &Server{
		Directory: NewDirectory(), Verifier: verifier, MaxPendingFrames: maxPendingFrames,
		bridges:       make(map[string]*VoiceCoreMediaRuntime),
		openSemaphore: make(chan struct{}, 32),
	}
}

func (s *Server) acquireOpenSlot() func() {
	s.openSemaphore <- struct{}{}
	return func() { <-s.openSemaphore }
}

func (s *Server) beginOpen() (func(), bool) {
	s.openMu.Lock()
	defer s.openMu.Unlock()
	if s.Draining.Load() {
		return nil, false
	}
	s.opening.Add(1)
	return s.opening.Done, true
}

// Close stops all attached Voice Core streams. It is safe to call on the
// provider-neutral reference server as well.
func (s *Server) Close() error {
	s.Draining.Store(true)
	// Ensure every pre-drain creator has registered before waiting for its
	// expensive bridge/provider work to finish.
	s.openMu.Lock()
	opening := &s.opening
	s.openMu.Unlock()
	opening.Wait()
	s.openMu.Lock()
	defer s.openMu.Unlock()
	s.bridgeMu.Lock()
	bridges := make([]*VoiceCoreMediaRuntime, 0, len(s.bridges))
	for id, runtime := range s.bridges {
		bridges = append(bridges, runtime)
		delete(s.bridges, id)
	}
	s.bridgeMu.Unlock()
	var closeErr error
	for _, runtime := range bridges {
		if err := runtime.Close(); err != nil && closeErr == nil {
			closeErr = err
		}
	}
	s.metricsMu.Lock()
	for _, session := range s.Directory.TakeAll() {
		session.Stop()
		s.archiveSessionMetricsLocked(session.Stats())
	}
	s.metricsMu.Unlock()
	return closeErr
}

func (s *Server) bridgeFor(sessionID string) *VoiceCoreMediaRuntime {
	s.bridgeMu.Lock()
	defer s.bridgeMu.Unlock()
	return s.bridges[sessionID]
}

func (s *Server) installBridge(sessionID string, runtime *VoiceCoreMediaRuntime) {
	s.bridgeMu.Lock()
	s.bridges[sessionID] = runtime
	s.bridgeMu.Unlock()
	runtime.Start()
}

func (s *Server) replaceBridge(
	sessionID string,
	runtime *VoiceCoreMediaRuntime,
) *VoiceCoreMediaRuntime {
	s.bridgeMu.Lock()
	old := s.bridges[sessionID]
	if runtime == nil {
		delete(s.bridges, sessionID)
	} else {
		s.bridges[sessionID] = runtime
	}
	s.bridgeMu.Unlock()
	if runtime != nil {
		runtime.Start()
	}
	return old
}

func (s *Server) removeBridge(sessionID string) *VoiceCoreMediaRuntime {
	s.bridgeMu.Lock()
	runtime := s.bridges[sessionID]
	delete(s.bridges, sessionID)
	s.bridgeMu.Unlock()
	return runtime
}

// CloseSession is the explicit lifecycle hook used by the control plane when
// a media-directory route expires or a user session is deleted.  Generation
// stop intentionally does not call this method: stopping an answer must leave
// the session reusable for the next turn.
func (s *Server) CloseSession(sessionID string) bool {
	for {
		current, ok := s.Directory.Get(sessionID)
		if !ok {
			return false
		}
		current.lifecycleMu.Lock()
		s.openMu.Lock()
		latest, stillCurrent := s.Directory.Get(sessionID)
		if !stillCurrent || latest != current {
			s.openMu.Unlock()
			current.lifecycleMu.Unlock()
			continue
		}
		detached, ok := s.detachSessionLocked(sessionID)
		s.openMu.Unlock()
		if !ok {
			current.lifecycleMu.Unlock()
			return false
		}
		s.closeDetachedSessionLocked(detached)
		current.lifecycleMu.Unlock()
		if s.SessionCloseHook != nil {
			s.SessionCloseHook(detached.session.ID)
		}
		return true
	}
}

// CloseWebRTCSession prevents a delayed close from an old peer from deleting
// the replacement epoch that now owns the same session id.
func (s *Server) CloseWebRTCSession(request OpenSessionRequest) bool {
	current, ok := s.Directory.Get(request.SessionID)
	if !ok {
		return false
	}
	current.lifecycleMu.Lock()
	s.openMu.Lock()
	latest, ok := s.Directory.Get(request.SessionID)
	if !ok || latest != current {
		s.openMu.Unlock()
		current.lifecycleMu.Unlock()
		return false
	}
	sessionID, accountID, deviceID, streamEpoch := current.IdentitySnapshot()
	if sessionID != request.SessionID || accountID != request.AccountID ||
		deviceID != request.DeviceID || streamEpoch != request.StreamEpoch ||
		current.ClientTypeValue() != defaultClientType(request.ClientType) {
		s.openMu.Unlock()
		current.lifecycleMu.Unlock()
		return false
	}
	detached, ok := s.detachSessionLocked(request.SessionID)
	s.openMu.Unlock()
	if !ok {
		current.lifecycleMu.Unlock()
		return false
	}
	s.closeDetachedSessionLocked(detached)
	current.lifecycleMu.Unlock()
	if s.SessionCloseHook != nil {
		s.SessionCloseHook(detached.session.ID)
	}
	return true
}

type detachedSession struct {
	session *Session
	runtime *VoiceCoreMediaRuntime
}

func (s *Server) detachSessionLocked(sessionID string) (detachedSession, bool) {
	session, ok := s.Directory.Get(sessionID)
	if !ok {
		return detachedSession{}, false
	}
	runtime := s.removeBridge(sessionID)
	deleted := s.Directory.Delete(sessionID)
	if !deleted {
		return detachedSession{}, false
	}
	return detachedSession{session: session, runtime: runtime}, true
}

// closeDetachedSessionLocked performs slow shutdown after the directory entry
// has been removed. The caller holds detached.session.lifecycleMu.
func (s *Server) closeDetachedSessionLocked(detached detachedSession) {
	if detached.runtime != nil {
		_ = detached.runtime.Close()
	}
	detached.session.Stop()
	s.metricsMu.Lock()
	s.archiveSessionMetricsLocked(detached.session.Stats())
	s.metricsMu.Unlock()
}

func (s *Server) buildBridge(request OpenSessionRequest, session *Session) (*VoiceCoreMediaRuntime, error) {
	if s.BridgeFactory == nil {
		return nil, nil
	}
	var sender DownlinkSender
	if s.DownlinkSenderFactory != nil {
		var err error
		sender, err = s.DownlinkSenderFactory(request, session)
		if err != nil {
			return nil, err
		}
	}
	if s.RequireExternalDownlinkSender && sender == nil {
		return nil, fmt.Errorf("external downlink sender is unavailable")
	}
	runtime, err := s.BridgeFactory(request, session, sender)
	if err != nil {
		return nil, err
	}
	if runtime == nil {
		return nil, fmt.Errorf("bridge factory returned a nil runtime")
	}
	return runtime, nil
}

// OpenWebRTCSession installs a higher-epoch WHIP peer and Voice Core bridge as
// one server-owned lifecycle. The terminator must register its exact
// session/epoch sender before calling this method.
type preparedWebRTCSession struct {
	session *Session
	runtime *VoiceCoreMediaRuntime
}

type replacedWebRTCSession struct {
	session *Session
	runtime *VoiceCoreMediaRuntime
}

func (s *Server) OpenWebRTCSession(request OpenSessionRequest) (*Session, error) {
	releaseOpen, ok := s.beginOpen()
	if !ok {
		return nil, fmt.Errorf("media edge is draining")
	}
	defer releaseOpen()
	release := s.acquireOpenSlot()
	defer release()
	prepared, err := s.prepareWebRTCSession(request)
	if err != nil {
		return nil, err
	}
	s.openMu.Lock()
	replaced, err := s.installWebRTCSessionLocked(prepared)
	s.openMu.Unlock()
	if err != nil {
		_ = prepared.runtime.Close()
		prepared.session.Stop()
		return nil, err
	}
	s.closeReplacedWebRTCSession(replaced)
	return prepared.session, nil
}

func (s *Server) prepareWebRTCSession(request OpenSessionRequest) (*preparedWebRTCSession, error) {
	if err := request.Validate(); err != nil {
		return nil, err
	}
	if s.Draining.Load() {
		return nil, fmt.Errorf("media edge is draining")
	}
	if s.ReadyProbe != nil && !s.ReadyProbe() {
		return nil, fmt.Errorf("voice core bridge is unavailable")
	}
	if !s.externalDownlinkReady() {
		return nil, fmt.Errorf("WebRTC downlink is unavailable")
	}
	if s.BridgeFactory == nil || s.DownlinkSenderFactory == nil {
		return nil, fmt.Errorf("WebRTC Voice Core bridge is unavailable")
	}
	if current, ok := s.Directory.Get(request.SessionID); ok {
		sessionID, accountID, deviceID, streamEpoch := current.IdentitySnapshot()
		if sessionID != request.SessionID || accountID != request.AccountID ||
			deviceID != request.DeviceID || current.ClientTypeValue() != defaultClientType(request.ClientType) {
			return nil, fmt.Errorf("WebRTC session identity changed")
		}
		if request.StreamEpoch <= streamEpoch {
			return nil, fmt.Errorf("WebRTC stream epoch did not advance")
		}
	}

	created, err := NewSession(request, s.MaxPendingFrames)
	if err != nil {
		return nil, err
	}
	runtime, err := s.buildBridge(request, created)
	if err != nil {
		created.Stop()
		return nil, err
	}
	if runtime == nil || !runtime.HasDownlinkSender() {
		if runtime != nil {
			_ = runtime.Close()
		}
		created.Stop()
		return nil, fmt.Errorf("WebRTC bridge has no downlink sender")
	}
	return &preparedWebRTCSession{session: created, runtime: runtime}, nil
}

func (s *Server) installWebRTCSessionLocked(
	prepared *preparedWebRTCSession,
) (*replacedWebRTCSession, error) {
	oldSession, err := s.Directory.ReplaceNewer(prepared.session)
	if err != nil {
		return nil, err
	}
	oldRuntime := s.replaceBridge(prepared.session.ID, prepared.runtime)
	return &replacedWebRTCSession{session: oldSession, runtime: oldRuntime}, nil
}

func (s *Server) closeReplacedWebRTCSession(replaced *replacedWebRTCSession) {
	if replaced == nil {
		return
	}
	if replaced.session != nil {
		replaced.session.lifecycleMu.Lock()
		defer replaced.session.lifecycleMu.Unlock()
	}
	if replaced.runtime != nil {
		_ = replaced.runtime.Close()
	}
	if replaced.session != nil {
		replaced.session.Stop()
		s.metricsMu.Lock()
		s.archiveSessionMetricsLocked(replaced.session.Stats())
		s.metricsMu.Unlock()
	}
}
