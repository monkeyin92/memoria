package mediaedge

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/pion/webrtc/v4"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

// WHIP signaling and peer ownership stay together: these methods create,
// authorize, activate, replace, and retire a browser-facing WebRTC resource.

var errWHIPResourceNotFound = errors.New("WHIP resource not found")

func (t *WebRTCTerminator) Ready() bool {
	t.mu.Lock()
	closed := t.closed
	t.mu.Unlock()
	if closed {
		return false
	}
	t.probeMu.Lock()
	defer t.probeMu.Unlock()
	if time.Since(t.probeAt) < 5*time.Second {
		return t.probeOK
	}
	t.probeOK = t.probe()
	t.probeAt = time.Now()
	return t.probeOK
}

func (t *WebRTCTerminator) probe() bool {
	pc, err := t.config.API.NewPeerConnection(t.config.Configuration)
	if err != nil {
		t.report(err)
		return false
	}
	defer func() { _ = pc.Close() }()
	if _, err := pc.CreateDataChannel("readiness", nil); err != nil {
		t.report(err)
		return false
	}
	offer, err := pc.CreateOffer(nil)
	if err != nil {
		t.report(err)
		return false
	}
	gathered := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(offer); err != nil {
		t.report(err)
		return false
	}
	select {
	case <-gathered:
	case <-time.After(3 * time.Second):
		return false
	}
	local := pc.LocalDescription()
	if local == nil || !strings.Contains(local.SDP, "a=candidate:") {
		return false
	}
	return !t.config.RequireRelayCandidate || strings.Contains(local.SDP, " typ relay ") ||
		strings.Contains(local.SDP, " typ relay\r\n")
}

func (t *WebRTCTerminator) Handler() http.Handler { return http.HandlerFunc(t.serveHTTP) }

func (t *WebRTCTerminator) serveHTTP(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Cache-Control", "no-store")
	switch {
	case r.Method == http.MethodPost && strings.TrimRight(r.URL.Path, "/") == "/whip":
		t.createSession(w, r)
	case r.Method == http.MethodDelete && strings.HasPrefix(r.URL.Path, "/whip/"):
		t.deleteSession(w, r)
	case r.Method == http.MethodPatch && strings.HasPrefix(r.URL.Path, "/whip/"):
		t.restartICE(w, r)
	default:
		w.Header().Set("Allow", "POST, PATCH, DELETE")
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
	}
}

func (t *WebRTCTerminator) authorizedResource(r *http.Request) (*webRTCPeer, error) {
	resourceID := strings.TrimPrefix(strings.TrimRight(r.URL.Path, "/"), "/whip/")
	token, err := bearerToken(r)
	if err != nil {
		return nil, err
	}
	identity, err := t.verifier.ParseIdentity(token)
	if err != nil {
		return nil, err
	}
	t.mu.Lock()
	peer := t.resource[resourceID]
	t.mu.Unlock()
	if peer == nil || peer.request.SessionID != identity.SessionID ||
		peer.request.AccountID != identity.AccountID || peer.request.DeviceID != identity.DeviceID ||
		peer.request.ClientType != identity.ClientType || peer.request.StreamEpoch != identity.StreamEpoch {
		return nil, errWHIPResourceNotFound
	}
	return peer, nil
}

func bearerToken(r *http.Request) (string, error) {
	header := strings.TrimSpace(r.Header.Get("Authorization"))
	if !strings.HasPrefix(header, "Bearer ") {
		return "", fmt.Errorf("bearer media token is required")
	}
	token := strings.TrimSpace(strings.TrimPrefix(header, "Bearer "))
	if token == "" {
		return "", fmt.Errorf("bearer media token is required")
	}
	return token, nil
}

func (t *WebRTCTerminator) createSession(w http.ResponseWriter, r *http.Request) {
	if t.server.Draining.Load() || !t.server.externalDownlinkReady() ||
		(t.server.ReadyProbe != nil && !t.server.ReadyProbe()) ||
		t.server.BridgeFactory == nil || t.server.DownlinkSenderFactory == nil {
		http.Error(w, "media edge is not ready", http.StatusServiceUnavailable)
		return
	}
	token, err := bearerToken(r)
	if err != nil {
		http.Error(w, err.Error(), http.StatusUnauthorized)
		return
	}
	identity, err := t.verifier.ParseIdentity(token)
	if err != nil {
		http.Error(w, err.Error(), http.StatusUnauthorized)
		return
	}
	if mediaType := strings.ToLower(strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0])); mediaType != "application/sdp" {
		http.Error(w, "Content-Type must be application/sdp", http.StatusUnsupportedMediaType)
		return
	}
	offer, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxSDPBytes))
	if err != nil || len(strings.TrimSpace(string(offer))) == 0 {
		http.Error(w, "invalid SDP offer", http.StatusBadRequest)
		return
	}
	request := OpenSessionRequest(identity)
	peer, answer, err := t.negotiate(request, string(offer))
	if err != nil {
		t.report(err)
		http.Error(w, "WebRTC negotiation failed", http.StatusBadRequest)
		return
	}
	if err := t.registerPending(peer); err != nil {
		t.closePeer(peer)
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	w.Header().Set("Content-Type", "application/sdp")
	w.Header().Set("Location", "/whip/"+peer.resourceID)
	w.Header().Set("ETag", `"`+peer.resourceID+`"`)
	w.Header().Set("Access-Control-Expose-Headers", "Location, ETag")
	w.Header().Set("Accept-Patch", "application/sdp")
	w.WriteHeader(http.StatusCreated)
	if _, err := io.WriteString(w, answer); err != nil {
		t.failPeer(peer, err)
	}
}

// completePeer preserves the working epoch until the replacement has passed
// ICE/DTLS and its exact sender + Voice Core bridge are installed.
func (t *WebRTCTerminator) completePeer(peer *webRTCPeer) {
	release := t.server.acquireOpenSlot()
	prepared, err := t.server.prepareWebRTCSession(peer.request)
	release()
	if err != nil {
		t.failPeer(peer, err)
		return
	}
	t.mu.Lock()
	if err := t.canActivateLocked(peer); err != nil {
		t.mu.Unlock()
		_ = prepared.runtime.Close()
		prepared.session.Stop()
		t.failPeer(peer, err)
		return
	}
	t.server.openMu.Lock()
	replaced, err := t.server.installWebRTCSessionLocked(prepared)
	if err != nil {
		t.mu.Unlock()
		t.failPeer(peer, err)
		return
	}
	peer.attach(prepared.session, prepared.runtime)
	old := t.activateLocked(peer)
	t.mu.Unlock()
	t.server.openMu.Unlock()
	t.server.closeReplacedWebRTCSession(replaced)
	if old != nil {
		t.closePeer(old)
	}
}

func (t *WebRTCTerminator) deleteSession(w http.ResponseWriter, r *http.Request) {
	peer, err := t.authorizedResource(r)
	if err != nil {
		status := http.StatusUnauthorized
		if errors.Is(err, errWHIPResourceNotFound) {
			status = http.StatusNotFound
		}
		http.Error(w, err.Error(), status)
		return
	}
	t.mu.Lock()
	if t.resource[peer.resourceID] != peer {
		t.mu.Unlock()
		http.Error(w, "WHIP resource not found", http.StatusNotFound)
		return
	}
	t.removePeerLocked(peer)
	wasActive := t.active[peer.request.SessionID] == peer
	if wasActive {
		delete(t.active, peer.request.SessionID)
	}
	t.mu.Unlock()
	t.closePeer(peer)
	if wasActive {
		t.server.CloseWebRTCSession(peer.request)
	}
	w.WriteHeader(http.StatusNoContent)
}

// restartICE is the non-trickle restart companion to the initial full-SDP
// WHIP exchange. It keeps the same authenticated resource, session epoch,
// sender and Voice Core stream while replacing only ICE credentials.
func (t *WebRTCTerminator) restartICE(w http.ResponseWriter, r *http.Request) {
	peer, err := t.authorizedResource(r)
	if err != nil {
		status := http.StatusUnauthorized
		if errors.Is(err, errWHIPResourceNotFound) {
			status = http.StatusNotFound
		}
		http.Error(w, err.Error(), status)
		return
	}
	if r.Header.Get("If-Match") != `"`+peer.resourceID+`"` {
		http.Error(w, "WHIP resource ETag does not match", http.StatusPreconditionFailed)
		return
	}
	if mediaType := strings.ToLower(strings.TrimSpace(strings.Split(r.Header.Get("Content-Type"), ";")[0])); mediaType != "application/sdp" {
		http.Error(w, "Content-Type must be application/sdp", http.StatusUnsupportedMediaType)
		return
	}
	offer, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxSDPBytes))
	if err != nil || len(strings.TrimSpace(string(offer))) == 0 {
		http.Error(w, "invalid SDP offer", http.StatusBadRequest)
		return
	}
	peer.negotiateMu.Lock()
	defer peer.negotiateMu.Unlock()
	if peer.closed.Load() {
		http.Error(w, "WHIP resource not found", http.StatusNotFound)
		return
	}
	if err := peer.pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: string(offer)}); err != nil {
		http.Error(w, "invalid ICE restart offer", http.StatusBadRequest)
		return
	}
	answer, err := peer.pc.CreateAnswer(nil)
	if err != nil {
		t.failPeer(peer, fmt.Errorf("create ICE restart answer: %w", err))
		http.Error(w, "ICE restart failed", http.StatusConflict)
		return
	}
	gatherComplete := webrtc.GatheringCompletePromise(peer.pc)
	if err := peer.pc.SetLocalDescription(answer); err != nil {
		t.failPeer(peer, fmt.Errorf("install ICE restart answer: %w", err))
		http.Error(w, "ICE restart failed", http.StatusConflict)
		return
	}
	select {
	case <-gatherComplete:
	case <-time.After(5 * time.Second):
		t.failPeer(peer, fmt.Errorf("ICE restart gathering timed out"))
		http.Error(w, "ICE gathering timed out", http.StatusGatewayTimeout)
		return
	}
	local := peer.pc.LocalDescription()
	if local == nil || strings.TrimSpace(local.SDP) == "" {
		t.failPeer(peer, fmt.Errorf("ICE restart produced no answer"))
		http.Error(w, "ICE restart produced no answer", http.StatusConflict)
		return
	}
	w.Header().Set("Content-Type", "application/sdp")
	w.Header().Set("ETag", `"`+peer.resourceID+`"`)
	w.WriteHeader(http.StatusOK)
	if _, err := io.WriteString(w, local.SDP); err != nil {
		t.failPeer(peer, err)
	}
}

func (t *WebRTCTerminator) negotiate(
	request OpenSessionRequest,
	offer string,
) (*webRTCPeer, string, error) {
	pc, err := t.config.API.NewPeerConnection(t.config.Configuration)
	if err != nil {
		return nil, "", err
	}
	resourceID, err := randomResourceID()
	if err != nil {
		_ = pc.Close()
		return nil, "", err
	}
	track, err := webrtc.NewTrackLocalStaticSample(
		webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus, ClockRate: opusSampleRate, Channels: opusRTPChannels},
		"assistant", request.SessionID,
	)
	if err != nil {
		_ = pc.Close()
		return nil, "", err
	}
	sender, err := pc.AddTrack(track)
	if err != nil {
		_ = pc.Close()
		return nil, "", err
	}
	encoder, err := newOpusEncoder(opusSampleRate, 1)
	if err != nil {
		_ = pc.Close()
		return nil, "", fmt.Errorf("create Opus encoder: %w", err)
	}
	peer := &webRTCPeer{
		terminator: t, request: request, resourceID: resourceID, pc: pc,
		downlink: track, encoder: encoder, runtimeReady: make(chan struct{}),
		channels:    make(map[dataChannelLane]*webrtc.DataChannel),
		encodeBuf:   make([]int16, 0, opusFrameSamples*2),
		downlinkPCM: make([]int16, opusFrameSamples*2),
		opusPacket:  make([]byte, 4_000),
		paddedPCM:   make([]int16, opusFrameSamples),
	}
	pc.OnDataChannel(peer.onDataChannel)
	pc.OnTrack(func(track *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		go peer.receiveTrack(track)
	})
	pc.OnConnectionStateChange(peer.onConnectionStateChange)
	go func() {
		for {
			if _, _, err := sender.ReadRTCP(); err != nil {
				return
			}
		}
	}()
	if err := pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: offer}); err != nil {
		t.closePeer(peer)
		return nil, "", err
	}
	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		t.closePeer(peer)
		return nil, "", err
	}
	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(answer); err != nil {
		t.closePeer(peer)
		return nil, "", err
	}
	select {
	case <-gatherComplete:
	case <-time.After(5 * time.Second):
		t.closePeer(peer)
		return nil, "", fmt.Errorf("ICE gathering timed out")
	}
	local := pc.LocalDescription()
	if local == nil || strings.TrimSpace(local.SDP) == "" {
		t.closePeer(peer)
		return nil, "", fmt.Errorf("empty SDP answer")
	}
	return peer, local.SDP, nil
}

func randomResourceID() (string, error) {
	value := make([]byte, 16)
	if _, err := rand.Read(value); err != nil {
		return "", err
	}
	return hex.EncodeToString(value), nil
}

func peerKey(request OpenSessionRequest) string {
	return fmt.Sprintf("%s\x00%d", request.SessionID, request.StreamEpoch)
}

func (t *WebRTCTerminator) registerPending(peer *webRTCPeer) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed {
		return fmt.Errorf("WebRTC terminator is closed")
	}
	key := peerKey(peer.request)
	if _, exists := t.pending[key]; exists {
		return fmt.Errorf("WHIP negotiation is already pending")
	}
	if current := t.active[peer.request.SessionID]; current != nil &&
		peer.request.StreamEpoch <= current.request.StreamEpoch {
		return fmt.Errorf("WHIP stream epoch did not advance")
	}
	t.pending[key] = peer
	t.resource[peer.resourceID] = peer
	return nil
}

func (t *WebRTCTerminator) canActivateLocked(peer *webRTCPeer) error {
	key := peerKey(peer.request)
	if t.closed || t.pending[key] != peer || peer.closed.Load() ||
		peer.pc.ConnectionState() != webrtc.PeerConnectionStateConnected {
		delete(t.pending, key)
		delete(t.resource, peer.resourceID)
		return fmt.Errorf("WHIP peer unavailable during negotiation")
	}
	if current := t.active[peer.request.SessionID]; current != nil &&
		peer.request.StreamEpoch <= current.request.StreamEpoch {
		delete(t.pending, key)
		delete(t.resource, peer.resourceID)
		return fmt.Errorf("WHIP stream epoch did not advance")
	}
	return nil
}

func (t *WebRTCTerminator) activateLocked(peer *webRTCPeer) *webRTCPeer {
	key := peerKey(peer.request)
	delete(t.pending, key)
	old := t.active[peer.request.SessionID]
	t.active[peer.request.SessionID] = peer
	t.resource[peer.resourceID] = peer
	if old != nil {
		delete(t.resource, old.resourceID)
	}
	return old
}

func (t *WebRTCTerminator) removePeerLocked(peer *webRTCPeer) {
	delete(t.resource, peer.resourceID)
	if t.pending[peerKey(peer.request)] == peer {
		delete(t.pending, peerKey(peer.request))
	}
}

func (t *WebRTCTerminator) peerDisconnected(peer *webRTCPeer) {
	t.mu.Lock()
	t.removePeerLocked(peer)
	wasActive := t.active[peer.request.SessionID] == peer
	if wasActive {
		delete(t.active, peer.request.SessionID)
	}
	t.mu.Unlock()
	t.closePeer(peer)
	if wasActive {
		t.server.CloseWebRTCSession(peer.request)
	}
}

func (t *WebRTCTerminator) failPeer(peer *webRTCPeer, err error) {
	// Publish terminal state before report/cleanup can block behind activation.
	peer.closed.Store(true)
	t.report(err)
	t.peerDisconnected(peer)
}

func (t *WebRTCTerminator) report(err error) {
	if err != nil && t.config.OnError != nil {
		t.config.OnError(err)
	}
}

func (t *WebRTCTerminator) closePeer(peer *webRTCPeer) {
	if err := peer.close(); err != nil {
		t.report(fmt.Errorf("close WHIP peer: %w", err))
	}
}

func (t *WebRTCTerminator) DownlinkSender(request OpenSessionRequest, _ *Session) (DownlinkSender, error) {
	t.mu.Lock()
	peer := t.peerForRequestLocked(request)
	t.mu.Unlock()
	if peer == nil || peer.closed.Load() {
		return nil, fmt.Errorf("WHIP downlink peer is unavailable")
	}
	return peer.sendDownlink, nil
}

func (t *WebRTCTerminator) ForwardCoreEvent(request OpenSessionRequest, event *mediav1.CoreToMedia) {
	t.mu.Lock()
	peer := t.peerForRequestLocked(request)
	t.mu.Unlock()
	if peer != nil {
		peer.forwardCoreEvent(event)
	}
}

func (t *WebRTCTerminator) HandleBridgeError(request OpenSessionRequest, err error) {
	t.mu.Lock()
	peer := t.peerForRequestLocked(request)
	t.mu.Unlock()
	if peer != nil {
		t.failPeer(peer, err)
	} else {
		t.report(err)
	}
}

func (t *WebRTCTerminator) peerForRequestLocked(request OpenSessionRequest) *webRTCPeer {
	if peer := t.pending[peerKey(request)]; peer != nil {
		return peer
	}
	peer := t.active[request.SessionID]
	if peer == nil || peer.request.StreamEpoch != request.StreamEpoch {
		return nil
	}
	return peer
}

func (t *WebRTCTerminator) CloseSession(sessionID string) {
	t.mu.Lock()
	var peers []*webRTCPeer
	if peer := t.active[sessionID]; peer != nil {
		peers = append(peers, peer)
		delete(t.active, sessionID)
		t.removePeerLocked(peer)
	}
	for key, peer := range t.pending {
		if peer.request.SessionID == sessionID {
			peers = append(peers, peer)
			delete(t.pending, key)
		}
	}
	t.mu.Unlock()
	for _, peer := range peers {
		t.closePeer(peer)
	}
}

func (t *WebRTCTerminator) Close() error {
	t.mu.Lock()
	if t.closed {
		t.mu.Unlock()
		return nil
	}
	t.closed = true
	seen := make(map[*webRTCPeer]struct{})
	for _, peer := range t.active {
		seen[peer] = struct{}{}
	}
	for _, peer := range t.pending {
		seen[peer] = struct{}{}
	}
	t.active = make(map[string]*webRTCPeer)
	t.pending = make(map[string]*webRTCPeer)
	t.resource = make(map[string]*webRTCPeer)
	t.mu.Unlock()
	var closeErr error
	for peer := range seen {
		if err := peer.close(); err != nil && closeErr == nil {
			closeErr = err
		}
	}
	return closeErr

}
