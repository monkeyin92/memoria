package mediaedge

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
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
	bridgeMu                 sync.Mutex
	openMu                   sync.Mutex
	metricsMu                sync.Mutex
	retiredMetrics           SessionStats
	bridges                  map[string]*VoiceCoreMediaRuntime
}

func NewServer(verifier JWTVerifier, maxPendingFrames int) *Server {
	if maxPendingFrames <= 0 {
		maxPendingFrames = 100
	}
	return &Server{
		Directory: NewDirectory(), Verifier: verifier, MaxPendingFrames: maxPendingFrames,
		bridges: make(map[string]*VoiceCoreMediaRuntime),
	}
}

// Close stops all attached Voice Core streams. It is safe to call on the
// provider-neutral reference server as well.
func (s *Server) Close() error {
	s.Draining.Store(true)
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
	s.openMu.Lock()
	defer s.openMu.Unlock()
	return s.closeSessionLocked(sessionID)
}

// CloseWebRTCSession prevents a delayed close from an old peer from deleting
// the replacement epoch that now owns the same session id.
func (s *Server) CloseWebRTCSession(request OpenSessionRequest) bool {
	s.openMu.Lock()
	defer s.openMu.Unlock()
	current, ok := s.Directory.Get(request.SessionID)
	if !ok {
		return false
	}
	sessionID, accountID, deviceID, streamEpoch := current.IdentitySnapshot()
	if sessionID != request.SessionID || accountID != request.AccountID ||
		deviceID != request.DeviceID || streamEpoch != request.StreamEpoch ||
		current.ClientTypeValue() != defaultClientType(request.ClientType) {
		return false
	}
	return s.closeSessionLocked(request.SessionID)
}

func (s *Server) closeSessionLocked(sessionID string) bool {
	session, ok := s.Directory.Get(sessionID)
	if !ok {
		return false
	}
	if runtime := s.removeBridge(sessionID); runtime != nil {
		_ = runtime.Close()
	}
	s.metricsMu.Lock()
	session.Stop()
	deleted := s.Directory.Delete(sessionID)
	if deleted {
		s.archiveSessionMetricsLocked(session.Stats())
	}
	s.metricsMu.Unlock()
	if deleted && s.SessionCloseHook != nil {
		s.SessionCloseHook(sessionID)
	}
	return deleted
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
	s.openMu.Lock()
	prepared, err := s.prepareWebRTCSessionLocked(request)
	if err != nil {
		s.openMu.Unlock()
		return nil, err
	}
	replaced, err := s.installWebRTCSessionLocked(prepared)
	if err != nil {
		s.openMu.Unlock()
		return nil, err
	}
	s.openMu.Unlock()
	closeReplacedWebRTCSession(replaced)
	return prepared.session, nil
}

func (s *Server) prepareWebRTCSessionLocked(request OpenSessionRequest) (*preparedWebRTCSession, error) {
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
	s.metricsMu.Lock()
	defer s.metricsMu.Unlock()
	oldSession, err := s.Directory.ReplaceNewer(prepared.session)
	if err != nil {
		_ = prepared.runtime.Close()
		prepared.session.Stop()
		return nil, err
	}
	oldRuntime := s.replaceBridge(prepared.session.ID, prepared.runtime)
	if oldSession != nil {
		oldSession.Stop()
		s.archiveSessionMetricsLocked(oldSession.Stats())
	}
	return &replacedWebRTCSession{session: oldSession, runtime: oldRuntime}, nil
}

func closeReplacedWebRTCSession(replaced *replacedWebRTCSession) {
	if replaced == nil {
		return
	}
	if replaced.runtime != nil {
		_ = replaced.runtime.Close()
	}
	if replaced.session != nil {
		replaced.session.Stop()
	}
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.health)
	mux.HandleFunc("/readyz", s.ready)
	mux.HandleFunc("/metrics", s.metrics)
	mux.HandleFunc("/v1/media/sessions", s.sessions)
	mux.HandleFunc("/v1/media/sessions/", s.session)
	if s.WHIPHandler != nil {
		mux.Handle("/whip", s.WHIPHandler)
		mux.Handle("/whip/", s.WHIPHandler)
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.Requests.Add(1)
		w.Header().Set("Cache-Control", "no-store")
		mux.ServeHTTP(w, r)
	})
}

func (s *Server) health(w http.ResponseWriter, _ *http.Request) {
	writeStatus(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) externalDownlinkReady() bool {
	return !s.RequireExternalDownlinkSender || (s.DownlinkSenderFactory != nil &&
		s.BridgeFactory != nil && s.DownlinkReadyProbe != nil && s.DownlinkReadyProbe())
}

func (s *Server) ready(w http.ResponseWriter, _ *http.Request) {
	if s.Draining.Load() {
		writeStatus(w, http.StatusServiceUnavailable, map[string]string{"status": "draining"})
		return
	}
	if s.ReadyProbe != nil && !s.ReadyProbe() {
		writeStatus(w, http.StatusServiceUnavailable, map[string]string{"status": "voice_core_unavailable"})
		return
	}
	if !s.externalDownlinkReady() {
		writeStatus(w, http.StatusServiceUnavailable, map[string]string{"status": "downlink_sender_unavailable"})
		return
	}
	writeStatus(w, http.StatusOK, map[string]string{"status": "ready"})
}

func (s *Server) metrics(w http.ResponseWriter, _ *http.Request) {
	s.metricsMu.Lock()
	defer s.metricsMu.Unlock()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	_, _ = fmt.Fprintf(w, "media_edge_requests_total %d\n", s.Requests.Load())
	_, _ = fmt.Fprintf(w, "media_edge_rejected_frames_total %d\n", s.RejectedFrames.Load())
	stats := s.Directory.Snapshots()
	deadlineMisses := s.retiredMetrics.ActorDeadlineMisses
	audioFrames := s.retiredMetrics.ActorAudioFrames
	maxMailboxAge := s.retiredMetrics.ActorMailboxAgeMS
	maxIngressQueueAge := 0.0
	maxEgressQueueAge := 0.0
	maxFloorDecisionLatency := s.retiredMetrics.FloorDecisionLatencyMS
	maxGenerationCancelLatency := s.retiredMetrics.GenerationCancelLatencyMS
	maxPlayoutBuffer := 0.0
	playoutUnderruns := s.retiredMetrics.PlayoutUnderruns
	maxSessionDuration := s.retiredMetrics.SessionDurationMS
	shadowMismatches := make(map[string]ShadowMismatchCount)
	for _, count := range s.retiredMetrics.ShadowMismatchCounts {
		shadowMismatches[count.Scenario+"\x00"+count.ContractVersion] = count
	}
	for _, session := range stats {
		deadlineMisses += session.ActorDeadlineMisses
		audioFrames += session.ActorAudioFrames
		for _, count := range session.ShadowMismatchCounts {
			key := count.Scenario + "\x00" + count.ContractVersion
			total := shadowMismatches[key]
			total.Scenario = count.Scenario
			total.ContractVersion = count.ContractVersion
			total.Count += count.Count
			shadowMismatches[key] = total
		}
		if session.ActorMailboxAgeMS > maxMailboxAge {
			maxMailboxAge = session.ActorMailboxAgeMS
		}
		if session.IngressQueueAgeMS > maxIngressQueueAge {
			maxIngressQueueAge = session.IngressQueueAgeMS
		}
		if session.EgressQueueAgeMS > maxEgressQueueAge {
			maxEgressQueueAge = session.EgressQueueAgeMS
		}
		if session.FloorDecisionLatencyMS > maxFloorDecisionLatency {
			maxFloorDecisionLatency = session.FloorDecisionLatencyMS
		}
		if session.GenerationCancelLatencyMS > maxGenerationCancelLatency {
			maxGenerationCancelLatency = session.GenerationCancelLatencyMS
		}
		if session.PlayoutBufferMS > maxPlayoutBuffer {
			maxPlayoutBuffer = session.PlayoutBufferMS
		}
		playoutUnderruns += session.PlayoutUnderruns
		if session.SessionDurationMS > maxSessionDuration {
			maxSessionDuration = session.SessionDurationMS
		}
	}
	_, _ = fmt.Fprintf(w, "active_media_sessions %d\n", len(stats))
	_, _ = fmt.Fprintf(w, "audio_frame_deadline_miss_total %d\n", deadlineMisses)
	deadlineRatio := 0.0
	if audioFrames > 0 {
		deadlineRatio = float64(deadlineMisses) / float64(audioFrames)
	}
	_, _ = fmt.Fprintf(w, "audio_frame_deadline_miss_ratio %g\n", deadlineRatio)
	_, _ = fmt.Fprintf(w, "actor_mailbox_age_ms %g\n", maxMailboxAge)
	_, _ = fmt.Fprintf(w, "ingress_queue_age_ms %g\n", maxIngressQueueAge)
	_, _ = fmt.Fprintf(w, "egress_queue_age_ms %g\n", maxEgressQueueAge)
	_, _ = fmt.Fprintf(w, "floor_decision_latency_ms %g\n", maxFloorDecisionLatency)
	_, _ = fmt.Fprintf(w, "generation_cancel_latency_ms %g\n", maxGenerationCancelLatency)
	_, _ = fmt.Fprintf(w, "playout_buffer_ms %g\n", maxPlayoutBuffer)
	_, _ = fmt.Fprintf(w, "playout_underrun_total %d\n", playoutUnderruns)
	_, _ = fmt.Fprintf(w, "session_duration_ms %g\n", maxSessionDuration)
	_, _ = fmt.Fprintln(w, "# TYPE shadow_decision_mismatch_total counter")
	keys := make([]string, 0, len(shadowMismatches))
	for key := range shadowMismatches {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		count := shadowMismatches[key]
		_, _ = fmt.Fprintf(
			w,
			"shadow_decision_mismatch_total{scenario=%s,contract_version=%s} %d\n",
			strconv.Quote(count.Scenario),
			strconv.Quote(count.ContractVersion),
			count.Count,
		)
	}
}

func (s *Server) archiveSessionMetricsLocked(stats SessionStats) {
	s.retiredMetrics.ActorDeadlineMisses += stats.ActorDeadlineMisses
	s.retiredMetrics.ActorAudioFrames += stats.ActorAudioFrames
	if stats.ActorMailboxAgeMS > s.retiredMetrics.ActorMailboxAgeMS {
		s.retiredMetrics.ActorMailboxAgeMS = stats.ActorMailboxAgeMS
	}
	if stats.FloorDecisionLatencyMS > s.retiredMetrics.FloorDecisionLatencyMS {
		s.retiredMetrics.FloorDecisionLatencyMS = stats.FloorDecisionLatencyMS
	}
	if stats.GenerationCancelLatencyMS > s.retiredMetrics.GenerationCancelLatencyMS {
		s.retiredMetrics.GenerationCancelLatencyMS = stats.GenerationCancelLatencyMS
	}
	if stats.SessionDurationMS > s.retiredMetrics.SessionDurationMS {
		s.retiredMetrics.SessionDurationMS = stats.SessionDurationMS
	}
	s.retiredMetrics.ShadowMismatches += stats.ShadowMismatches
	s.retiredMetrics.PlayoutUnderruns += stats.PlayoutUnderruns
	s.retiredMetrics.ShadowMismatchCounts = mergeShadowMismatchCounts(
		s.retiredMetrics.ShadowMismatchCounts,
		stats.ShadowMismatchCounts,
	)
}

func (s *Server) sessions(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if s.Draining.Load() {
		writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "edge is draining"})
		return
	}
	if !s.externalDownlinkReady() {
		writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "external downlink sender is unavailable"})
		return
	}
	var request OpenSessionRequest
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64*1024)).Decode(&request); err != nil {
		writeStatus(w, http.StatusBadRequest, map[string]string{"error": "invalid request"})
		return
	}
	if err := request.Validate(); err != nil {
		writeStatus(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	if err := s.authorizeIdentity(r, MediaTokenIdentity{
		SessionID:   request.SessionID,
		AccountID:   request.AccountID,
		DeviceID:    request.DeviceID,
		ClientType:  defaultClientType(request.ClientType),
		StreamEpoch: request.StreamEpoch,
	}); err != nil {
		writeStatus(w, http.StatusUnauthorized, map[string]string{"error": err.Error()})
		return
	}
	s.openMu.Lock()
	defer s.openMu.Unlock()
	if s.Draining.Load() {
		writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "edge is draining"})
		return
	}
	created, err := NewSession(request, s.MaxPendingFrames)
	if err != nil {
		writeStatus(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	runtime, err := s.buildBridge(request, created)
	if err != nil {
		created.Stop()
		writeStatus(w, http.StatusBadGateway, map[string]string{"error": "Voice Core bridge unavailable"})
		return
	}
	if s.RequireExternalDownlinkSender && (runtime == nil || !runtime.HasDownlinkSender()) {
		if runtime != nil {
			_ = runtime.Close()
		}
		created.Stop()
		writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "Voice Core bridge has no external downlink sender"})
		return
	}
	if err := s.Directory.Put(created); err != nil {
		if runtime != nil {
			_ = runtime.Close()
		}
		created.Stop()
		writeStatus(w, http.StatusConflict, map[string]string{"error": "session already exists"})
		return
	}
	if runtime != nil {
		s.installBridge(request.SessionID, runtime)
	}
	writeStatus(w, http.StatusCreated, SessionResponse{
		SessionID: request.SessionID, MediaRuntime: "media-edge-reference",
		StreamEpoch: request.StreamEpoch, Protocol: "media-v1",
	})
}

func (s *Server) session(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/v1/media/sessions/")
	parts := strings.Split(strings.Trim(path, "/"), "/")
	if len(parts) < 1 || parts[0] == "" {
		writeStatus(w, http.StatusNotFound, map[string]string{"error": "session not found"})
		return
	}
	id := parts[0]
	if err := s.authorize(r, id); err != nil {
		writeStatus(w, http.StatusUnauthorized, map[string]string{"error": err.Error()})
		return
	}
	session, ok := s.Directory.Get(id)
	if !ok {
		writeStatus(w, http.StatusNotFound, map[string]string{"error": "session not found"})
		return
	}
	accountID, deviceID := "", ""
	_, accountID, deviceID, epoch := session.IdentitySnapshot()
	if err := s.authorizeIdentity(r, MediaTokenIdentity{
		SessionID:   id,
		AccountID:   accountID,
		DeviceID:    deviceID,
		ClientType:  session.ClientTypeValue(),
		StreamEpoch: epoch,
	}); err != nil {
		writeStatus(w, http.StatusUnauthorized, map[string]string{"error": err.Error()})
		return
	}
	operation := ""
	if len(parts) == 2 {
		operation = parts[1]
	}
	switch operation {
	case "":
		if r.Method != http.MethodDelete {
			if r.Method != http.MethodGet {
				writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
				return
			}
			writeStatus(w, http.StatusOK, session.Stats())
			return
		}
		if !s.CloseSession(id) {
			writeStatus(w, http.StatusNotFound, map[string]string{"error": "session not found"})
			return
		}
		writeStatus(w, http.StatusOK, map[string]string{"status": "closed"})
	case "reconnect":
		if r.Method != http.MethodPost {
			writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
			return
		}
		s.openMu.Lock()
		defer s.openMu.Unlock()
		if s.Draining.Load() {
			writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "edge is draining"})
			return
		}
		if !s.externalDownlinkReady() {
			writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "external downlink sender is unavailable"})
			return
		}
		oldRuntime := s.removeBridge(id)
		if oldRuntime != nil {
			_ = oldRuntime.Close()
		}
		epoch, err := session.Reconnect()
		if err != nil {
			writeStatus(w, http.StatusConflict, map[string]string{"error": err.Error()})
			return
		}
		if s.BridgeFactory != nil {
			sessionID, accountID, deviceID, _ := session.IdentitySnapshot()
			reconnectRequest := OpenSessionRequest{
				SessionID: sessionID, AccountID: accountID, DeviceID: deviceID,
				ClientType:  session.ClientTypeValue(),
				StreamEpoch: epoch,
			}
			runtime, bridgeErr := s.buildBridge(reconnectRequest, session)
			if bridgeErr != nil {
				writeStatus(w, http.StatusBadGateway, map[string]string{"error": "Voice Core bridge unavailable"})
				return
			}
			if s.RequireExternalDownlinkSender && (runtime == nil || !runtime.HasDownlinkSender()) {
				if runtime != nil {
					_ = runtime.Close()
				}
				writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "Voice Core bridge has no external downlink sender"})
				return
			}
			s.installBridge(id, runtime)
		}
		writeStatus(w, http.StatusOK, map[string]any{"session_id": id, "stream_epoch": epoch})
	case "shadow":
		if r.Method != http.MethodGet {
			writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
			return
		}
		snapshot, available := session.ShadowSnapshot()
		if !available {
			writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "shadow snapshot unavailable"})
			return
		}
		writeStatus(w, http.StatusOK, snapshot)
	case "stop":
		if r.Method != http.MethodPost {
			writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
			return
		}
		var request StopGenerationRequest
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64*1024)).Decode(&request); err != nil {
			writeStatus(w, http.StatusBadRequest, map[string]string{"error": "invalid stop request"})
			return
		}
		if request.StreamEpoch != 0 && request.StreamEpoch != epoch {
			writeStatus(w, http.StatusConflict, map[string]string{"error": "stop stream epoch is stale"})
			return
		}
		expected, err := request.ExpectedFence(id)
		if err != nil {
			writeStatus(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
			return
		}
		eventID := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
		if eventID == "" || len(eventID) > 128 {
			writeStatus(w, http.StatusBadRequest, map[string]string{"error": "valid Idempotency-Key is required"})
			return
		}
		reason := strings.TrimSpace(request.Reason)
		if reason == "" {
			reason = "client_stop"
		}
		if len(reason) > 128 {
			writeStatus(w, http.StatusBadRequest, map[string]string{"error": "stop reason is too long"})
			return
		}
		// Edge-side detection time (interrupt.detect). Voice Core uses it as
		// the SLO anchor so the measurement includes local receipt, gate and
		// the Edge→Core hop rather than only Core-side processing.
		detectedAtMs := uint64(time.Now().UnixMilli())
		var cancelled Fence
		if runtime := s.bridgeFor(id); runtime != nil {
			cancelled, err = runtime.CancelGeneration(eventID, reason, expected, detectedAtMs)
		} else {
			_, cancelled, _, err = session.CancelGeneration(eventID, expected)
		}
		if err != nil {
			writeStatus(w, http.StatusConflict, map[string]string{"error": err.Error()})
			return
		}
		writeStatus(w, http.StatusOK, map[string]any{
			"status": "cancelled", "stream_epoch": epoch,
			"turn_id": cancelled.TurnID, "generation_id": cancelled.GenerationID,
			"tool_epoch": cancelled.ToolEpoch,
		})
	case "uplink":
		s.acceptFrame(w, r, session, false)
	case "downlink":
		if r.Method == http.MethodGet {
			frame, ok := session.PopDownlink()
			if !ok {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			writeStatus(w, http.StatusOK, frame)
			return
		}
		s.acceptFrame(w, r, session, true)
	default:
		if r.Method != http.MethodGet {
			writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
			return
		}
		writeStatus(w, http.StatusOK, session.Stats())
	}
}

func (s *Server) acceptFrame(w http.ResponseWriter, r *http.Request, session *Session, downlink bool) {
	if r.Method != http.MethodPost {
		writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	var frame AudioFrame
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, MaxPayloadBytes+64*1024)).Decode(&frame); err != nil {
		writeStatus(w, http.StatusBadRequest, map[string]string{"error": "invalid frame"})
		return
	}
	var err error
	runtime := s.bridgeFor(session.ID)
	if downlink {
		if runtime != nil {
			s.RejectedFrames.Add(1)
			writeStatus(w, http.StatusConflict, map[string]string{"error": "downlink is owned by Voice Core bridge"})
			return
		}
		err = session.AcceptDownlink(frame)
	} else {
		if runtime != nil {
			err = runtime.SendUplink(frame)
		} else {
			err = session.AcceptUplink(frame)
		}
	}
	if err != nil {
		s.RejectedFrames.Add(1)
		writeStatus(w, http.StatusConflict, map[string]string{"error": err.Error()})
		return
	}
	writeStatus(w, http.StatusAccepted, map[string]string{"status": "accepted"})
}

func (s *Server) authorize(r *http.Request, sessionID string, expectedEpoch ...uint64) error {
	identity := MediaTokenIdentity{SessionID: sessionID}
	if len(expectedEpoch) > 0 {
		identity.StreamEpoch = expectedEpoch[0]
	}
	return s.authorizeIdentity(r, identity)
}

func (s *Server) authorizeIdentity(r *http.Request, identity MediaTokenIdentity) error {
	if len(s.Verifier.Secret) == 0 {
		// Development-only mode is explicit; production and every other
		// environment fail closed unless the embedding test/dev process opts in.
		if os.Getenv("ENVIRONMENT") == "production" || !s.AllowInsecureDevelopment {
			return fmt.Errorf("media token verifier is not configured")
		}
		return nil
	}
	header := r.Header.Get("Authorization")
	if !strings.HasPrefix(header, "Bearer ") {
		return fmt.Errorf("bearer media token is required")
	}
	return s.Verifier.VerifyIdentity(
		strings.TrimSpace(strings.TrimPrefix(header, "Bearer ")),
		identity,
	)
}

func writeStatus(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := writeJSON(w, value); err != nil {
		log.Printf("media edge response write failed: %v", err)
	}
}
