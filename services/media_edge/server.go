package mediaedge

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
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
	s.Directory.CloseAll()
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
	session, ok := s.Directory.Get(sessionID)
	if !ok {
		return false
	}
	if runtime := s.removeBridge(sessionID); runtime != nil {
		_ = runtime.Close()
	}
	session.Stop()
	return s.Directory.Delete(sessionID)
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

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.health)
	mux.HandleFunc("/readyz", s.ready)
	mux.HandleFunc("/metrics", s.metrics)
	mux.HandleFunc("/v1/media/sessions", s.sessions)
	mux.HandleFunc("/v1/media/sessions/", s.session)
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
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	_, _ = fmt.Fprintf(w, "media_edge_requests_total %d\n", s.Requests.Load())
	_, _ = fmt.Fprintf(w, "media_edge_rejected_frames_total %d\n", s.RejectedFrames.Load())
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
	created, err := NewSession(request, s.MaxPendingFrames)
	if err != nil {
		writeStatus(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	runtime, err := s.buildBridge(request, created)
	if err != nil {
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
	if err := s.Directory.Put(created); err != nil {
		if runtime != nil {
			_ = runtime.Close()
		}
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
