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
)

// BridgeRuntimeFactory is injected by a deployment that has an authenticated
// Voice Core stream.  The default HTTP reference server leaves it nil and
// remains provider/media-terminator neutral.
type BridgeRuntimeFactory func(OpenSessionRequest, *Session) (*VoiceCoreMediaRuntime, error)

type Server struct {
	Directory        *Directory
	Verifier         JWTVerifier
	MaxPendingFrames int
	BridgeFactory    BridgeRuntimeFactory
	ReadyProbe       func() bool
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
	for _, runtime := range bridges {
		if err := runtime.Close(); err != nil {
			return err
		}
	}
	return nil
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

func (s *Server) buildBridge(request OpenSessionRequest, session *Session) (*VoiceCoreMediaRuntime, error) {
	if s.BridgeFactory == nil {
		return nil, nil
	}
	runtime, err := s.BridgeFactory(request, session)
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

func (s *Server) ready(w http.ResponseWriter, _ *http.Request) {
	if s.Draining.Load() {
		writeStatus(w, http.StatusServiceUnavailable, map[string]string{"status": "draining"})
		return
	}
	if s.ReadyProbe != nil && !s.ReadyProbe() {
		writeStatus(w, http.StatusServiceUnavailable, map[string]string{"status": "voice_core_unavailable"})
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
	case "reconnect":
		if r.Method != http.MethodPost {
			writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
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
			s.installBridge(id, runtime)
		}
		writeStatus(w, http.StatusOK, map[string]any{"session_id": id, "stream_epoch": epoch})
	case "stop":
		if r.Method != http.MethodPost {
			writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
			return
		}
		session.Stop()
		if runtime := s.removeBridge(id); runtime != nil {
			_ = runtime.Close()
		}
		writeStatus(w, http.StatusOK, map[string]string{"status": "stopped"})
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
