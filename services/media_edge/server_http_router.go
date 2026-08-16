package mediaedge

import (
	"crypto/subtle"
	"encoding/json"
	"io"
	"net/http"
)

const internalControlTokenHeader = "X-Memoria-Edge-Control-Token"

func (s *Server) Handler() http.Handler {
	return s.handler(true, true)
}

// PublicHandler exposes authenticated media/control routes and liveness.
// Readiness and metrics belong on the private listener in production.
func (s *Server) PublicHandler() http.Handler {
	return s.handler(true, false)
}

// InternalHandler exposes operational endpoints for a private listener.
func (s *Server) InternalHandler() http.Handler {
	return s.handler(false, true)
}

func (s *Server) handler(public, internal bool) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", s.health)
	if internal {
		mux.HandleFunc("/readyz", s.ready)
		mux.HandleFunc("/metrics", s.metrics)
		if s.DeviceWSS != nil {
			mux.HandleFunc("/v1/internal/device-runtime/invalidate", s.invalidateDeviceRuntime)
			mux.HandleFunc("/v1/internal/device-runtime/status", s.deviceRuntimeStatus)
			mux.HandleFunc("/v1/internal/device-runtime/session-close", s.closeDeviceSession)
		}
	}
	if public {
		mux.HandleFunc("/v1/media/sessions", s.sessions)
		mux.HandleFunc("/v1/media/sessions/", s.session)
	}
	if public && s.WHIPHandler != nil {
		mux.Handle("/whip", s.WHIPHandler)
		mux.Handle("/whip/", s.WHIPHandler)
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		s.Requests.Add(1)
		w.Header().Set("Cache-Control", "no-store")
		mux.ServeHTTP(w, r)
	})
}

func (s *Server) internalControlAuthorized(r *http.Request) bool {
	expected := []byte(s.InternalControlToken)
	presented := []byte(r.Header.Get(internalControlTokenHeader))
	return len(expected) >= 32 && len(presented) == len(expected) &&
		subtle.ConstantTimeCompare(presented, expected) == 1
}

func (s *Server) invalidateDeviceRuntime(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if !s.internalControlAuthorized(r) {
		writeStatus(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
		return
	}
	var request struct {
		DeviceID       string `json:"device_id"`
		ProfileVersion uint64 `json:"profile_version"`
		ApplyAt        string `json:"apply_at"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil || request.DeviceID == "" {
		writeStatus(w, http.StatusBadRequest, map[string]string{"error": "invalid request"})
		return
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		writeStatus(w, http.StatusBadRequest, map[string]string{"error": "invalid request"})
		return
	}
	delivered, err := s.DeviceWSS.InvalidateRuntimeProfile(
		request.DeviceID, request.ProfileVersion, request.ApplyAt,
	)
	if err != nil {
		writeStatus(w, http.StatusBadRequest, map[string]string{"error": err.Error()})
		return
	}
	writeStatus(w, http.StatusOK, map[string]any{"delivered": delivered})
}

func (s *Server) deviceRuntimeStatus(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if !s.internalControlAuthorized(r) {
		writeStatus(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
		return
	}
	query := r.URL.Query()
	deviceIDs, hasDeviceID := query["device_id"]
	deviceID := query.Get("device_id")
	if err := validateDeviceIdentifier(deviceID, "device_id"); err != nil ||
		!hasDeviceID || len(deviceIDs) != 1 || len(query) != 1 {
		writeStatus(w, http.StatusBadRequest, map[string]string{"error": "invalid request"})
		return
	}
	writeStatus(w, http.StatusOK, s.DeviceWSS.RuntimeStatus(deviceID))
}

// closeDeviceSession is the Control-to-Edge socket-close port used by the
// owner DELETE pipeline. It authenticates with the runtime-control token
// (the same secret as invalidate/status; the Edge-to-Control close-report
// token is a separate secret and never used here).
func (s *Server) closeDeviceSession(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if !s.internalControlAuthorized(r) {
		writeStatus(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
		return
	}
	var request struct {
		DeviceID  string `json:"device_id"`
		SessionID string `json:"session_id"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&request); err != nil || request.DeviceID == "" ||
		request.SessionID == "" {
		writeStatus(w, http.StatusBadRequest, map[string]string{"error": "invalid request"})
		return
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		writeStatus(w, http.StatusBadRequest, map[string]string{"error": "invalid request"})
		return
	}
	delivered := s.DeviceWSS.CloseSession(request.SessionID, SessionCloseReasonDeviceClose)
	writeStatus(w, http.StatusOK, map[string]any{
		"device_id":  request.DeviceID,
		"session_id": request.SessionID,
		"delivered":  delivered,
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
	if s.DeviceWSS != nil && !s.DeviceWSS.Ready() {
		writeStatus(w, http.StatusServiceUnavailable, map[string]string{"status": "device_media_unavailable"})
		return
	}
	writeStatus(w, http.StatusOK, map[string]string{"status": "ready"})
}
