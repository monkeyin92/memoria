package mediaedge

import "net/http"

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
