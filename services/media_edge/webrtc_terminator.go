package mediaedge

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	pionopus "github.com/pion/opus"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

const (
	webrtcEventChannelLabel = "memoria.events.v1"
	uplinkSampleRate        = 16_000
	downlinkSampleRate      = 24_000
	opusSampleRate          = 48_000
	opusRTPChannels         = 2
	mediaFrameDuration      = 20 * time.Millisecond
	uplinkFrameSamples      = uplinkSampleRate / 50
	downlinkFrameSamples    = downlinkSampleRate / 50
	opusFrameSamples        = opusSampleRate / 50
	maxPendingDataEvents    = 128
	maxClientEventBytes     = 64 * 1024
	maxSDPBytes             = 1024 * 1024
	maxUplinkGapSamples     = uplinkSampleRate
)

var errWHIPResourceNotFound = errors.New("WHIP resource not found")
var mediaProcessStartedAt = time.Now()

type WebRTCTerminatorConfig struct {
	API                   *webrtc.API
	Configuration         webrtc.Configuration
	VAD                   MediaVADConfig
	RequireRelayCandidate bool
	OnError               func(error)
}

// WebRTCTerminator owns the browser-facing WHIP, RTP/Opus and DataChannel
// lifecycle while the existing Server remains the sole session/bridge owner.
type WebRTCTerminator struct {
	server   *Server
	verifier JWTVerifier
	config   WebRTCTerminatorConfig
	mu       sync.Mutex
	pending  map[string]*webRTCPeer
	active   map[string]*webRTCPeer
	resource map[string]*webRTCPeer
	probeMu  sync.Mutex
	probeAt  time.Time
	probeOK  bool
	closed   bool
}

type webRTCPeer struct {
	terminator   *WebRTCTerminator
	request      OpenSessionRequest
	resourceID   string
	pc           *webrtc.PeerConnection
	downlink     *webrtc.TrackLocalStaticSample
	negotiateMu  sync.Mutex
	runtimeMu    sync.RWMutex
	runtime      *VoiceCoreMediaRuntime
	session      *Session
	runtimeReady chan struct{}
	runtimeOnce  sync.Once
	connectOnce  sync.Once
	channelMu    sync.Mutex
	channel      *webrtc.DataChannel
	pending      [][]byte
	eventSeq     uint64
	clientSeq    uint64
	hasClient    bool
	encoderMu    sync.Mutex
	encoder      *opusEncoder
	encodeBuf    []int16
	encodeFence  Fence
	flushMu      sync.Mutex
	lastFlush    Fence
	closed       atomic.Bool
	closeOnce    sync.Once
}

type mediaDataEnvelope struct {
	Version           int             `json:"v"`
	Protocol          string          `json:"protocol"`
	Type              string          `json:"type"`
	EventID           string          `json:"event_id"`
	SessionID         string          `json:"session_id"`
	StreamEpoch       uint64          `json:"stream_epoch"`
	Sequence          uint64          `json:"sequence"`
	TurnID            uint64          `json:"turn_id"`
	GenerationID      uint64          `json:"generation_id"`
	ToolEpoch         uint64          `json:"tool_epoch"`
	TaskEpoch         uint64          `json:"task_epoch"`
	ContextVersion    uint64          `json:"context_version"`
	ServerMonotonicMS uint64          `json:"server_monotonic_ms"`
	Payload           json.RawMessage `json:"payload"`
}

type playbackProgressPayload struct {
	ReceivedSequence  uint64 `json:"received_sequence"`
	RenderedSampleEnd uint64 `json:"rendered_sample_end"`
	ClientMonotonicMS uint64 `json:"client_monotonic_ms"`
	Approximate       bool   `json:"approximate"`
	TurnID            uint64 `json:"turn_id"`
	GenerationID      uint64 `json:"generation_id"`
	ToolEpoch         uint64 `json:"tool_epoch"`
}

func NewWebRTCTerminator(
	server *Server,
	verifier JWTVerifier,
	config WebRTCTerminatorConfig,
) (*WebRTCTerminator, error) {
	if server == nil {
		return nil, fmt.Errorf("media edge server is required")
	}
	if config.API == nil {
		config.API = webrtc.NewAPI()
	}
	if config.VAD == (MediaVADConfig{}) {
		config.VAD = DefaultMediaVADConfig()
	}
	if err := config.VAD.validate(); err != nil {
		return nil, err
	}
	encoder, err := newOpusEncoder(opusSampleRate, 1)
	if err != nil {
		return nil, err
	}
	encoder.close()
	return &WebRTCTerminator{
		server: server, verifier: verifier, config: config,
		pending: make(map[string]*webRTCPeer), active: make(map[string]*webRTCPeer),
		resource: make(map[string]*webRTCPeer),
	}, nil
}

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
	t.server.openMu.Lock()
	prepared, err := t.server.prepareWebRTCSessionLocked(peer.request)
	if err != nil {
		t.server.openMu.Unlock()
		t.failPeer(peer, err)
		return
	}
	t.mu.Lock()
	if err := t.canActivateLocked(peer); err != nil {
		t.mu.Unlock()
		t.server.openMu.Unlock()
		_ = prepared.runtime.Close()
		prepared.session.Stop()
		t.failPeer(peer, err)
		return
	}
	replaced, err := t.server.installWebRTCSessionLocked(prepared)
	if err != nil {
		t.mu.Unlock()
		t.server.openMu.Unlock()
		t.failPeer(peer, err)
		return
	}
	peer.attach(prepared.session, prepared.runtime)
	old := t.activateLocked(peer)
	t.mu.Unlock()
	t.server.openMu.Unlock()
	closeReplacedWebRTCSession(replaced)
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

func (p *webRTCPeer) attach(session *Session, runtime *VoiceCoreMediaRuntime) {
	p.runtimeMu.Lock()
	p.session = session
	p.runtime = runtime
	p.runtimeMu.Unlock()
	p.runtimeOnce.Do(func() { close(p.runtimeReady) })
	p.sendSessionReady(runtime.InteractionAuthority())
}

func (p *webRTCPeer) sendSessionReady(authority mediav1.InteractionAuthority) {
	value := "python_authoritative"
	if authority == mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW {
		value = "go_shadow"
	}
	current := p.currentFence()
	p.sendEnvelope(
		"session.ready", current.TurnID, current.GenerationID, current.ToolEpoch,
		0, 0,
		map[string]any{"state": "ready", "interaction_authority": value},
	)
}

func (p *webRTCPeer) onConnectionStateChange(state webrtc.PeerConnectionState) {
	if state == webrtc.PeerConnectionStateConnected {
		p.connectOnce.Do(func() { go p.terminator.completePeer(p) })
	}
	if state == webrtc.PeerConnectionStateFailed || state == webrtc.PeerConnectionStateClosed {
		// Make failure visible before cleanup waits for the activation lock. A
		// failed pending peer must never replace the still-working epoch.
		p.closed.Store(true)
		go p.terminator.peerDisconnected(p)
	}
}

func (p *webRTCPeer) currentRuntime() *VoiceCoreMediaRuntime {
	p.runtimeMu.RLock()
	defer p.runtimeMu.RUnlock()
	return p.runtime
}

func (p *webRTCPeer) waitRuntime(timeout time.Duration) (*VoiceCoreMediaRuntime, error) {
	select {
	case <-p.runtimeReady:
		runtime := p.currentRuntime()
		if runtime == nil || p.closed.Load() {
			return nil, fmt.Errorf("voice core runtime is unavailable")
		}
		return runtime, nil
	case <-time.After(timeout):
		return nil, fmt.Errorf("voice core runtime attach timed out")
	}
}

func (p *webRTCPeer) currentFence() Fence {
	p.runtimeMu.RLock()
	session := p.session
	p.runtimeMu.RUnlock()
	if session == nil {
		return Fence{SessionID: p.request.SessionID}
	}
	fence, _ := session.GenerationSnapshot()
	return fence
}

func (p *webRTCPeer) close() error {
	var closeErr error
	p.closeOnce.Do(func() {
		p.closed.Store(true)
		p.runtimeOnce.Do(func() { close(p.runtimeReady) })
		p.encoderMu.Lock()
		p.encoder.close()
		p.encoderMu.Unlock()
		p.channelMu.Lock()
		if p.channel != nil {
			_ = p.channel.Close()
		}
		p.pending = nil
		p.channelMu.Unlock()
		closeErr = p.pc.Close()
	})
	return closeErr
}

func (p *webRTCPeer) sendDownlink(ctx context.Context, frame AudioFrame) error {
	payload, err := frame.Payload()
	if err != nil {
		return err
	}
	if len(payload)%2 != 0 {
		return fmt.Errorf("downlink PCM must contain 16-bit samples")
	}
	samples := make([]int16, len(payload)/2)
	for index := range samples {
		samples[index] = int16(binary.LittleEndian.Uint16(payload[index*2:]))
	}
	fence := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch}
	p.encoderMu.Lock()
	defer p.encoderMu.Unlock()
	if p.closed.Load() {
		return io.ErrClosedPipe
	}
	if !p.encodeFence.Equal(fence) {
		p.encodeBuf = nil
		p.encodeFence = fence
		if err := p.encoder.Reset(); err != nil {
			return err
		}
	}
	for _, sample := range samples {
		p.encodeBuf = append(p.encodeBuf, sample, sample)
	}
	for len(p.encodeBuf) >= opusFrameSamples {
		if err := p.writeOpusFrame(ctx, p.encodeBuf[:opusFrameSamples]); err != nil {
			return err
		}
		p.encodeBuf = p.encodeBuf[opusFrameSamples:]
	}
	if frame.Final && len(p.encodeBuf) > 0 {
		padded := make([]int16, opusFrameSamples)
		copy(padded, p.encodeBuf)
		p.encodeBuf = nil
		if err := p.writeOpusFrame(ctx, padded); err != nil {
			return err
		}
	}
	return nil
}

func (p *webRTCPeer) writeOpusFrame(ctx context.Context, samples []int16) error {
	select {
	case <-ctx.Done():
		return ErrStaleDownlinkGeneration
	default:
	}
	packet := make([]byte, 4_000)
	n, err := p.encoder.Encode(samples, packet)
	if err != nil {
		return fmt.Errorf("encode downlink Opus: %w", err)
	}
	select {
	case <-ctx.Done():
		return ErrStaleDownlinkGeneration
	default:
	}
	if err := p.downlink.WriteSample(media.Sample{Data: packet[:n], Duration: mediaFrameDuration}); err != nil {
		return fmt.Errorf("write downlink RTP: %w", err)
	}
	if ctx.Err() != nil {
		return ErrStaleDownlinkGeneration
	}
	return nil
}

func (p *webRTCPeer) receiveTrack(track *webrtc.TrackRemote) {
	if !strings.EqualFold(track.Codec().MimeType, webrtc.MimeTypeOpus) || track.Codec().ClockRate != opusSampleRate {
		p.terminator.failPeer(p, fmt.Errorf("uplink must be 48 kHz Opus"))
		return
	}
	decoder, err := pionopus.NewDecoderWithOutput(uplinkSampleRate, 1)
	if err != nil {
		p.terminator.failPeer(p, err)
		return
	}
	vad, err := newMediaVAD(p.terminator.config.VAD)
	if err != nil {
		p.terminator.failPeer(p, err)
		return
	}
	runtime, err := p.waitRuntime(5 * time.Second)
	if err != nil {
		p.terminator.failPeer(p, err)
		return
	}
	var buffer []int16
	var captureSample, frameSequence uint64
	var lastRTPSequence uint16
	var lastTimestamp uint32
	var lastDecoded int
	haveRTP := false
	flushFrame := func(samples []int16, final bool) error {
		payload := make([]byte, len(samples)*2)
		for index, sample := range samples {
			binary.LittleEndian.PutUint16(payload[index*2:], uint16(sample))
		}
		frame := AudioFrame{
			SessionID: p.request.SessionID, StreamEpoch: p.request.StreamEpoch,
			Sequence: frameSequence, CaptureStartSample: captureSample,
			FrameSamples: uint64(len(samples)), PayloadB64: base64.StdEncoding.EncodeToString(payload),
			Final: final,
		}
		if event := vad.observe(captureSample, samples); event != nil {
			if err := runtime.SendVAD(event.Sample, event.VoicedEnd, event.Probability, event.RMS, event.NoiseFloor, event.Start); err != nil {
				return err
			}
		}
		if err := runtime.SendUplink(frame); err != nil {
			return err
		}
		frameSequence++
		captureSample += uint64(len(samples))
		return nil
	}
	appendSamples := func(samples []int16) error {
		buffer = append(buffer, samples...)
		for len(buffer) >= uplinkFrameSamples {
			if err := flushFrame(buffer[:uplinkFrameSamples], false); err != nil {
				return err
			}
			buffer = buffer[uplinkFrameSamples:]
		}
		return nil
	}
	for {
		packet, _, err := track.ReadRTP()
		if err != nil {
			if !errors.Is(err, io.EOF) && !p.closed.Load() {
				p.terminator.report(err)
			}
			break
		}
		if haveRTP {
			delta := uint16(packet.SequenceNumber - lastRTPSequence)
			if delta == 0 || delta > 0x8000 {
				continue
			}
		}
		decoded := make([]int16, 1_920)
		n, err := decoder.DecodeToInt16(packet.Payload, decoded)
		if err != nil {
			p.terminator.failPeer(p, fmt.Errorf("decode uplink Opus: %w", err))
			return
		}
		if n <= 0 {
			p.terminator.failPeer(p, fmt.Errorf("decode uplink Opus returned no samples"))
			return
		}
		if haveRTP {
			timestampDelta := uint32(packet.Timestamp - lastTimestamp)
			if timestampDelta > opusSampleRate*2 || timestampDelta%3 != 0 {
				p.terminator.failPeer(p, fmt.Errorf("uplink RTP timestamp discontinuity"))
				return
			}
			gap := int(timestampDelta/3) - lastDecoded
			if gap < 0 {
				continue
			}
			if gap > maxUplinkGapSamples {
				p.terminator.failPeer(p, fmt.Errorf("uplink RTP gap exceeds one second"))
				return
			}
			if gap > 0 {
				if err := appendSamples(make([]int16, gap)); err != nil {
					p.terminator.failPeer(p, err)
					return
				}
			}
		}
		if err := appendSamples(decoded[:n]); err != nil {
			p.terminator.failPeer(p, err)
			return
		}
		haveRTP = true
		lastRTPSequence = packet.SequenceNumber
		lastTimestamp = packet.Timestamp
		lastDecoded = n
	}
	if len(buffer) > 0 && !p.closed.Load() {
		padded := make([]int16, uplinkFrameSamples)
		copy(padded, buffer)
		if err := flushFrame(padded, true); err != nil {
			p.terminator.failPeer(p, err)
			return
		}
	}
	if event := vad.flush(captureSample); event != nil {
		if err := runtime.SendVAD(event.Sample, event.VoicedEnd, event.Probability, event.RMS, event.NoiseFloor, false); err != nil {
			p.terminator.failPeer(p, err)
		}
	}
}

func (p *webRTCPeer) onDataChannel(channel *webrtc.DataChannel) {
	if channel.Label() != webrtcEventChannelLabel {
		_ = channel.Close()
		return
	}
	channel.OnOpen(func() {
		p.channelMu.Lock()
		if p.closed.Load() {
			p.channelMu.Unlock()
			_ = channel.Close()
			return
		}
		pending := p.pending
		p.pending = nil
		for _, event := range pending {
			if err := channel.Send(event); err != nil {
				p.channelMu.Unlock()
				p.terminator.failPeer(p, err)
				return
			}
		}
		p.channel = channel
		p.channelMu.Unlock()
	})
	channel.OnMessage(func(message webrtc.DataChannelMessage) {
		if message.IsString {
			p.handleClientEvent(message.Data)
		}
	})
}

func decodeClientEnvelope(raw []byte, request OpenSessionRequest) (mediaDataEnvelope, error) {
	if len(raw) == 0 || len(raw) > maxClientEventBytes {
		return mediaDataEnvelope{}, fmt.Errorf("client event exceeds bound")
	}
	var envelope mediaDataEnvelope
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return mediaDataEnvelope{}, fmt.Errorf("invalid client event")
	}
	if envelope.Version != ProtocolVersion || envelope.Protocol != "media-v1" ||
		envelope.SessionID != request.SessionID || envelope.StreamEpoch != request.StreamEpoch ||
		envelope.Type == "" || len(envelope.Type) > 96 || envelope.EventID == "" ||
		len(envelope.EventID) > 128 || len(envelope.Payload) < 2 || envelope.Payload[0] != '{' {
		return mediaDataEnvelope{}, fmt.Errorf("client event identity or envelope is invalid")
	}
	return envelope, nil
}

func (p *webRTCPeer) handleClientEvent(raw []byte) {
	envelope, err := decodeClientEnvelope(raw, p.request)
	if err != nil {
		p.terminator.report(err)
		return
	}
	p.channelMu.Lock()
	if p.hasClient && envelope.Sequence <= p.clientSeq {
		p.channelMu.Unlock()
		p.terminator.report(fmt.Errorf("client event sequence is stale"))
		return
	}
	p.clientSeq = envelope.Sequence
	p.hasClient = true
	p.channelMu.Unlock()
	runtime, err := p.waitRuntime(5 * time.Second)
	if err != nil {
		p.terminator.failPeer(p, err)
		return
	}
	fence := Fence{
		SessionID: envelope.SessionID, TurnID: envelope.TurnID,
		GenerationID: envelope.GenerationID, ToolEpoch: envelope.ToolEpoch,
	}
	switch envelope.Type {
	case "client.stop_assistant":
		var payload struct {
			Reason string `json:"reason"`
		}
		_ = json.Unmarshal(envelope.Payload, &payload)
		if payload.Reason == "" {
			payload.Reason = "client_stop"
		}
		var cancelled Fence
		cancelled, err = runtime.CancelGeneration(envelope.EventID, payload.Reason, &fence, uint64(time.Now().UnixMilli()))
		if err == nil {
			p.sendPlaybackFlush(cancelled, payload.Reason)
		}
	case "client.playback.progress":
		var payload playbackProgressPayload
		if decodeErr := json.Unmarshal(envelope.Payload, &payload); decodeErr != nil ||
			payload.TurnID != envelope.TurnID || payload.GenerationID != envelope.GenerationID ||
			payload.ToolEpoch != envelope.ToolEpoch {
			err = fmt.Errorf("invalid playback progress")
			break
		}
		err = runtime.SendPlaybackProgress(PlaybackProgress{
			SessionID: envelope.SessionID, StreamEpoch: envelope.StreamEpoch,
			TurnID: envelope.TurnID, GenerationID: envelope.GenerationID,
			ToolEpoch: envelope.ToolEpoch, ReceivedSequence: payload.ReceivedSequence,
			RenderedSampleEnd: payload.RenderedSampleEnd,
			ClientMonotonicMS: payload.ClientMonotonicMS, Approximate: payload.Approximate,
		})
	default:
		err = runtime.SendClientEvent(raw, envelope.Type, fence, uint64(time.Now().UnixMilli()))
	}
	if err != nil {
		p.terminator.report(err)
	}
}

func (p *webRTCPeer) forwardCoreEvent(event *mediav1.CoreToMedia) {
	if event == nil || p.closed.Load() {
		return
	}
	typeValue := ""
	turnID, generationID, toolEpoch := uint64(0), uint64(0), uint64(0)
	taskEpoch, contextVersion := uint64(0), uint64(0)
	payload := map[string]any{}
	switch {
	case event.GetAccepted() != nil:
		accepted := event.GetAccepted()
		typeValue = "session.ready"
		turnID, generationID, toolEpoch = accepted.GetCurrentTurnId(), accepted.GetCurrentGenerationId(), accepted.GetCurrentToolEpoch()
		taskEpoch, contextVersion = accepted.GetTaskEpoch(), accepted.GetContextVersion()
		payload["state"] = "ready"
		switch accepted.GetInteractionAuthority() {
		case mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW:
			payload["interaction_authority"] = "go_shadow"
		case mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE:
			// This value must never be produced before A6, but exposing it keeps
			// the client diagnostic honest if a mismatched Core is deployed.
			payload["interaction_authority"] = "go_authoritative"
		default:
			payload["interaction_authority"] = "python_authoritative"
		}
	case event.GetAudio() != nil:
		audio := event.GetAudio()
		typeValue = "assistant.audio.frame"
		turnID, generationID, toolEpoch = audio.GetTurnId(), audio.GetGenerationId(), audio.GetToolEpoch()
		taskEpoch, contextVersion = audio.GetTaskEpoch(), audio.GetContextVersion()
		payload = map[string]any{
			"sequence": audio.GetSequence(), "source_start_sample": audio.GetSourceStartSample(),
			"frame_samples": audio.GetFrameSamples(), "final": audio.GetFinalFrame(),
		}
	case event.GetGeneration() != nil:
		generation := event.GetGeneration()
		turnID, generationID, toolEpoch = generation.GetTurnId(), generation.GetGenerationId(), generation.GetToolEpoch()
		taskEpoch, contextVersion = generation.GetTaskEpoch(), generation.GetContextVersion()
		if generation.GetAction() == mediav1.GenerationAction_GENERATION_ACTION_CANCEL {
			p.sendPlaybackFlush(Fence{
				SessionID: p.request.SessionID, TurnID: turnID,
				GenerationID: generationID, ToolEpoch: toolEpoch,
			}, generation.GetReason())
			return
		} else {
			typeValue = "assistant.generation"
			payload = map[string]any{"action": generation.GetAction().String(), "reason": generation.GetReason()}
		}
	case event.GetRealtimeEffect() != nil:
		effect := event.GetRealtimeEffect()
		turnID, generationID, toolEpoch = effect.GetTurnId(), effect.GetGenerationId(), effect.GetToolEpoch()
		taskEpoch, contextVersion = effect.GetTaskEpoch(), effect.GetContextVersion()
		switch effect.GetEffectKind() {
		case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT,
			mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_PAUSE_OUTPUT:
			typeValue = "playback.duck"
		case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_RESUME_OUTPUT:
			typeValue = "playback.restore"
		case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION:
			p.sendPlaybackFlush(Fence{
				SessionID: p.request.SessionID, TurnID: turnID,
				GenerationID: generationID, ToolEpoch: toolEpoch,
			}, effect.GetSourceEventId())
			return
		default:
			return
		}
		payload = map[string]any{"source_event_id": effect.GetSourceEventId()}
		if gain, ok := realtimeEffectGain(effect); ok {
			payload["gain"] = gain
		}
	case event.GetFloorEffect() != nil:
		effect := event.GetFloorEffect()
		floorState, ok := floorStateName(effect.GetFloorState())
		if !ok || effect.GetFloorEpoch() == 0 {
			return
		}
		typeValue = "floor.state"
		turnID, generationID, toolEpoch = effect.GetTurnId(), effect.GetGenerationId(), effect.GetToolEpoch()
		taskEpoch, contextVersion = effect.GetTaskEpoch(), effect.GetContextVersion()
		payload = map[string]any{
			"floor_state":     floorState,
			"floor_epoch":     effect.GetFloorEpoch(),
			"source_event_id": effect.GetSourceEventId(),
		}
	case event.GetTranscript() != nil:
		transcript := event.GetTranscript()
		turnID, generationID, toolEpoch = transcript.GetTurnId(), transcript.GetGenerationId(), transcript.GetToolEpoch()
		taskEpoch, contextVersion = transcript.GetTaskEpoch(), transcript.GetContextVersion()
		typeValue = "user.transcript.partial"
		if transcript.GetFinal() {
			typeValue = "user.transcript.final"
		}
		payload = map[string]any{
			"text": transcript.GetText(), "revision": transcript.GetRevision(),
			"capture_start_sample": transcript.GetCaptureStartSample(),
			"capture_end_sample":   transcript.GetCaptureEndSample(), "final": transcript.GetFinal(),
			"confidence": transcript.GetConfidence(), "speaker_class": transcript.GetSpeakerClass(),
		}
	case event.GetState() != nil:
		state := event.GetState()
		typeValue = "assistant.state"
		turnID, generationID, toolEpoch = state.GetTurnId(), state.GetGenerationId(), state.GetToolEpoch()
		taskEpoch, contextVersion = state.GetTaskEpoch(), state.GetContextVersion()
		current := p.currentFence()
		if toolEpoch == 0 && current.TurnID == turnID && current.GenerationID == generationID {
			toolEpoch = current.ToolEpoch
		}
		payload = map[string]any{"state": conversationStateName(state.GetState()), "reason": state.GetReason()}
	case event.GetClient() != nil:
		p.forwardClientEvent(event.GetClient())
		return
	case event.GetError() != nil:
		coreError := event.GetError()
		typeValue = "error"
		turnID, generationID, toolEpoch = coreError.GetTurnId(), coreError.GetGenerationId(), coreError.GetToolEpoch()
		taskEpoch, contextVersion = coreError.GetTaskEpoch(), coreError.GetContextVersion()
		payload = map[string]any{"code": coreError.GetCode(), "message": coreError.GetMessage(), "retryable": coreError.GetRetryable()}
	}
	if typeValue != "" {
		p.sendEnvelope(typeValue, turnID, generationID, toolEpoch, taskEpoch, contextVersion, payload)
	}
}

func realtimeEffectGain(effect *mediav1.RealtimeEffect) (float64, bool) {
	if effect == nil || effect.GetEffectKind() != mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT {
		return 0, false
	}
	var payload struct {
		Gain *float64 `json:"gain"`
	}
	if err := json.Unmarshal(effect.GetPayload(), &payload); err != nil || payload.Gain == nil ||
		*payload.Gain < 0 || *payload.Gain > 1 {
		return 0, false
	}
	return *payload.Gain, true
}

func (p *webRTCPeer) sendPlaybackFlush(fence Fence, reason string) {
	p.flushMu.Lock()
	if p.lastFlush.Equal(fence) {
		p.flushMu.Unlock()
		return
	}
	p.lastFlush = fence
	p.flushMu.Unlock()
	p.sendEnvelope(
		"playback.flush", fence.TurnID, fence.GenerationID, fence.ToolEpoch,
		0, 0,
		map[string]any{"reason": reason},
	)
}

func (p *webRTCPeer) forwardClientEvent(event *mediav1.ClientEvent) {
	if event == nil || len(event.GetJsonPayload()) == 0 {
		return
	}
	var upstream mediaDataEnvelope
	if err := json.Unmarshal(event.GetJsonPayload(), &upstream); err != nil || upstream.Type != event.GetType() {
		p.terminator.report(fmt.Errorf("invalid Voice Core client event"))
		return
	}
	var payload map[string]any
	if err := json.Unmarshal(upstream.Payload, &payload); err != nil {
		p.terminator.report(fmt.Errorf("invalid Voice Core client payload"))
		return
	}
	taskEpoch, contextVersion := event.GetTaskEpoch(), event.GetContextVersion()
	if upstream.TaskEpoch != 0 {
		if taskEpoch != 0 && taskEpoch != upstream.TaskEpoch {
			p.terminator.report(fmt.Errorf("mismatched Voice Core task epoch"))
			return
		}
		taskEpoch = upstream.TaskEpoch
	}
	if upstream.ContextVersion != 0 {
		if contextVersion != 0 && contextVersion != upstream.ContextVersion {
			p.terminator.report(fmt.Errorf("mismatched Voice Core context version"))
			return
		}
		contextVersion = upstream.ContextVersion
	}
	p.sendEnvelope(
		event.GetType(), event.GetTurnId(), event.GetGenerationId(), event.GetToolEpoch(),
		taskEpoch, contextVersion, payload,
	)
}

func conversationStateName(state mediav1.ConversationState) string {
	switch state {
	case mediav1.ConversationState_CONVERSATION_STATE_CONNECTING:
		return "connecting"
	case mediav1.ConversationState_CONVERSATION_STATE_LISTENING:
		return "listening"
	case mediav1.ConversationState_CONVERSATION_STATE_USER_SPEAKING:
		return "user_speaking"
	case mediav1.ConversationState_CONVERSATION_STATE_FINALIZING:
		return "eot_pending"
	case mediav1.ConversationState_CONVERSATION_STATE_THINKING:
		return "thinking"
	case mediav1.ConversationState_CONVERSATION_STATE_ASSISTANT_SPEAKING:
		return "speaking"
	case mediav1.ConversationState_CONVERSATION_STATE_INTERRUPT_PENDING:
		return "interruption_pending"
	case mediav1.ConversationState_CONVERSATION_STATE_RECOVERING:
		return "recovering"
	case mediav1.ConversationState_CONVERSATION_STATE_CLOSED:
		return "closed"
	default:
		return "connecting"
	}
}

func floorStateName(state mediav1.FloorState) (string, bool) {
	switch state {
	case mediav1.FloorState_FLOOR_STATE_SILENCE:
		return "silence", true
	case mediav1.FloorState_FLOOR_STATE_USER_HOLDS_FLOOR:
		return "user_holds_floor", true
	case mediav1.FloorState_FLOOR_STATE_ASSISTANT_HOLDS_FLOOR:
		return "assistant_holds_floor", true
	case mediav1.FloorState_FLOOR_STATE_OVERLAP:
		return "overlap", true
	case mediav1.FloorState_FLOOR_STATE_UNCERTAIN:
		return "uncertain", true
	default:
		return "", false
	}
}

func (p *webRTCPeer) sendEnvelope(
	typeValue string,
	turnID, generationID, toolEpoch, taskEpoch, contextVersion uint64,
	payload map[string]any,
) {
	p.channelMu.Lock()
	sequence := p.eventSeq
	p.eventSeq++
	envelope := map[string]any{
		"v": ProtocolVersion, "protocol": "media-v1", "type": typeValue,
		"event_id":   fmt.Sprintf("%s:%d:%d", p.request.SessionID, p.request.StreamEpoch, sequence),
		"session_id": p.request.SessionID, "stream_epoch": p.request.StreamEpoch,
		"sequence": sequence, "turn_id": turnID, "generation_id": generationID,
		"tool_epoch": toolEpoch, "task_epoch": taskEpoch, "context_version": contextVersion,
		"server_monotonic_ms": uint64(time.Since(mediaProcessStartedAt) / time.Millisecond),
		"payload":             payload,
	}
	raw, err := json.Marshal(envelope)
	if err != nil {
		p.channelMu.Unlock()
		p.terminator.report(err)
		return
	}
	channel := p.channel
	if channel == nil || channel.ReadyState() != webrtc.DataChannelStateOpen {
		if len(p.pending) == maxPendingDataEvents {
			p.channelMu.Unlock()
			go p.terminator.failPeer(p, fmt.Errorf("DataChannel event queue is full"))
			return
		}
		p.pending = append(p.pending, raw)
		p.channelMu.Unlock()
		return
	}
	p.channelMu.Unlock()
	if err := channel.Send(raw); err != nil {
		p.terminator.failPeer(p, err)
	}
}
