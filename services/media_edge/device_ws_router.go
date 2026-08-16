package mediaedge

// HTTP entry for /v1/device/media. Only WSS upgrades with a valid strict
// Media Edge token and matching X-Client-ID are accepted; every rejection is
// a generic 401 so no claim or key detail leaks. The old
// memoria-device-media-gateway ticket types fail the typ check here.

import (
	"errors"
	"net/http"
	"strings"

	"github.com/gorilla/websocket"
)

const DeviceMediaEndpoint = "/v1/device/media"

var deviceUpgrader = websocket.Upgrader{
	ReadBufferSize:  4096,
	WriteBufferSize: 4096,
	// Native device firmware does not send a browser Origin; the bearer
	// token and per-device lease are the security boundary.
	CheckOrigin: func(*http.Request) bool { return true },
}

func (s *DeviceWSServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeStatus(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	if r.URL.Path != DeviceMediaEndpoint {
		writeStatus(w, http.StatusNotFound, map[string]string{"error": "not found"})
		return
	}
	if s.draining.Load() {
		writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "edge is draining"})
		return
	}
	authorization := strings.TrimSpace(r.Header.Get("Authorization"))
	const bearerPrefix = "Bearer "
	if !strings.HasPrefix(authorization, bearerPrefix) {
		s.reject(w, "missing_bearer_token")
		return
	}
	token := strings.TrimSpace(strings.TrimPrefix(authorization, bearerPrefix))
	clientID := strings.TrimSpace(r.Header.Get("X-Client-ID"))
	if strings.TrimSpace(r.Header.Get("Protocol-Version")) != "2" {
		s.reject(w, "protocol_version_mismatch")
		return
	}
	claims, err := s.Verifier.Verify(token, clientID)
	if err != nil {
		s.reject(w, deviceAuthReason(err))
		return
	}
	if err := s.Tickets.Consume(claims.JTI, claims.Expiry); err != nil {
		if errors.Is(err, ErrDeviceSharedStateUnavailable) {
			s.unavailable(w, "shared_state_unavailable")
			return
		}
		s.reject(w, "ticket_replay")
		return
	}
	connection, err := newDeviceConnection(s, nil, claims)
	if err != nil {
		s.reject(w, "internal")
		return
	}
	ws, err := deviceUpgrader.Upgrade(w, r, nil)
	if err != nil {
		return
	}
	connection.ws = ws
	go connection.run()
}

func (s *DeviceWSServer) unavailable(w http.ResponseWriter, reason string) {
	s.metrics.runtimeErrors.Add(1)
	s.rejectedReasons.add(reason)
	writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "service unavailable"})
}

func (s *DeviceWSServer) reject(w http.ResponseWriter, reason string) {
	s.metrics.authRejected.Add(1)
	s.rejectedReasons.add(reason)
	w.Header().Set("WWW-Authenticate", "Bearer")
	writeStatus(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
}

func deviceAuthReason(err error) string {
	if err == nil {
		return "invalid_token"
	}
	message := err.Error()
	switch {
	case strings.Contains(message, "expired"), strings.Contains(message, "not active"),
		strings.Contains(message, "issued in the future"), strings.Contains(message, "ttl exceeds"):
		return "token_time_invalid"
	case strings.Contains(message, "client mismatch"):
		return "client_mismatch"
	case strings.Contains(message, "not a device media token"), strings.Contains(message, "client_type must be device"):
		return "not_device_token"
	case strings.Contains(message, "signature"), strings.Contains(message, "algorithm"),
		strings.Contains(message, "key id"), strings.Contains(message, "header"):
		return "bad_signature"
	case strings.Contains(message, "issuer"), strings.Contains(message, "audience"):
		return "issuer_audience_mismatch"
	case strings.Contains(message, "not configured"):
		return "verifier_not_configured"
	default:
		return "invalid_claims"
	}
}
