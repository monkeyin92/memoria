package mediaedge

import (
	"crypto/ed25519"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

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
	releaseOpen, ok := s.beginOpen()
	if !ok {
		writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "edge is draining"})
		return
	}
	defer releaseOpen()
	release := s.acquireOpenSlot()
	defer release()
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
	s.openMu.Lock()
	putErr := s.Directory.Put(created)
	if putErr == nil && runtime != nil {
		s.installBridge(request.SessionID, runtime)
	}
	s.openMu.Unlock()
	if putErr != nil {
		if runtime != nil {
			_ = runtime.Close()
		}
		created.Stop()
		writeStatus(w, http.StatusConflict, map[string]string{"error": "session already exists"})
		return
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
		releaseOpen, opening := s.beginOpen()
		if !opening {
			writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "edge is draining"})
			return
		}
		defer releaseOpen()
		session.lifecycleMu.Lock()
		defer session.lifecycleMu.Unlock()
		release := s.acquireOpenSlot()
		defer release()
		s.openMu.Lock()
		current, currentOK := s.Directory.Get(id)
		_, _, _, currentEpoch := session.IdentitySnapshot()
		if !currentOK || current != session || currentEpoch != epoch {
			s.openMu.Unlock()
			writeStatus(w, http.StatusConflict, map[string]string{"error": "session epoch is stale"})
			return
		}
		if s.Draining.Load() {
			s.openMu.Unlock()
			writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "edge is draining"})
			return
		}
		if !s.externalDownlinkReady() {
			s.openMu.Unlock()
			writeStatus(w, http.StatusServiceUnavailable, map[string]string{"error": "external downlink sender is unavailable"})
			return
		}
		oldRuntime := s.removeBridge(id)
		s.openMu.Unlock()
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
			s.openMu.Lock()
			latest, latestOK := s.Directory.Get(id)
			_, _, _, latestEpoch := session.IdentitySnapshot()
			if !latestOK || latest != session || latestEpoch != epoch {
				s.openMu.Unlock()
				_ = runtime.Close()
				writeStatus(w, http.StatusConflict, map[string]string{"error": "session was replaced during reconnect"})
				return
			}
			s.installBridge(id, runtime)
			s.openMu.Unlock()
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
	configured := len(s.Verifier.Secret) >= 32 ||
		len(s.Verifier.PublicKey) == ed25519.PublicKeySize || len(s.Verifier.PublicKeys) > 0
	if !configured {
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
