package mediaedge

import (
	"encoding/json"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"github.com/pion/webrtc/v4"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

const (
	webrtcEventChannelLabel = "memoria.events.v1"
	webrtcConversationLabel = "memoria.conversation.v1"
	webrtcEphemeralLabel    = "memoria.ephemeral.v1"
	uplinkSampleRate        = 16_000
	downlinkSampleRate      = 24_000
	opusSampleRate          = 48_000
	opusRTPChannels         = 2
	mediaFrameDuration      = 20 * time.Millisecond
	uplinkFrameSamples      = uplinkSampleRate / 50
	downlinkFrameSamples    = downlinkSampleRate / 50
	opusFrameSamples        = opusSampleRate / 50
	maxPendingDataEvents    = 128
	maxPendingEphemeral     = 32
	maxClientEventBytes     = 64 * 1024
	maxSDPBytes             = 1024 * 1024
	maxUplinkGapSamples     = uplinkSampleRate
)

type dataChannelLane uint8

const (
	dataChannelControl dataChannelLane = iota
	dataChannelConversation
	dataChannelEphemeral
)

type pendingDataEvent struct {
	lane dataChannelLane
	raw  []byte
}

func dataChannelLaneForLabel(label string) (dataChannelLane, bool) {
	switch label {
	case webrtcEventChannelLabel:
		return dataChannelControl, true
	case webrtcConversationLabel:
		return dataChannelConversation, true
	case webrtcEphemeralLabel:
		return dataChannelEphemeral, true
	default:
		return dataChannelControl, false
	}
}

func dataChannelLaneForEvent(eventType string) dataChannelLane {
	switch eventType {
	case "turn.provisional.started", "turn.provisional.patch", "turn.provisional.discarded",
		"user.transcript.partial", "transcript_delta", "shadow.observation", "telemetry":
		return dataChannelEphemeral
	case "turn.committed", "user.transcript.final", "assistant.text.delta",
		"assistant.text.final", "assistant.audio.frame":
		return dataChannelConversation
	default:
		return dataChannelControl
	}
}

func dropOldestPendingLane(events []pendingDataEvent, lane dataChannelLane) ([]pendingDataEvent, bool) {
	for index, event := range events {
		if event.lane == lane {
			copy(events[index:], events[index+1:])
			return events[:len(events)-1], true
		}
	}
	return events, false
}

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
	channels     map[dataChannelLane]*webrtc.DataChannel
	pending      []pendingDataEvent
	eventSeq     uint64
	clientSeq    uint64
	hasClient    bool
	encoderMu    sync.Mutex
	encoder      *opusEncoder
	encodeBuf    []int16
	downlinkPCM  []int16
	paddedPCM    []int16
	opusPacket   []byte
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
	SessionEpoch      uint64          `json:"session_epoch"`
	TaskEpoch         uint64          `json:"task_epoch"`
	ContextVersion    uint64          `json:"context_version"`
	ServerMonotonicMS *uint64         `json:"server_monotonic_ms,omitempty"`
	ClientMonotonicMS *uint64         `json:"client_monotonic_ms,omitempty"`
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
	SessionEpoch      uint64 `json:"session_epoch"`
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
		for _, channel := range p.channels {
			_ = channel.Close()
		}
		p.pending = nil
		p.channels = nil
		p.channelMu.Unlock()
		closeErr = p.pc.Close()
	})
	return closeErr
}

func (p *webRTCPeer) onDataChannel(channel *webrtc.DataChannel) {
	lane, ok := dataChannelLaneForLabel(channel.Label())
	if !ok {
		_ = channel.Close()
		return
	}
	p.channelMu.Lock()
	if p.channels == nil {
		p.channels = make(map[dataChannelLane]*webrtc.DataChannel)
	}
	if existing := p.channels[lane]; existing != nil && existing != channel {
		p.channelMu.Unlock()
		_ = channel.Close()
		return
	}
	p.channels[lane] = channel
	p.channelMu.Unlock()
	channel.OnOpen(func() {
		p.channelMu.Lock()
		if p.closed.Load() {
			p.channelMu.Unlock()
			_ = channel.Close()
			return
		}
		remaining := p.pending[:0]
		for _, event := range p.pending {
			target := p.channels[event.lane]
			useOpenedChannel := target == channel ||
				(lane == dataChannelControl &&
					(target == nil || target.ReadyState() != webrtc.DataChannelStateOpen))
			if !useOpenedChannel {
				remaining = append(remaining, event)
				continue
			}
			if err := channel.Send(event.raw); err != nil {
				p.channelMu.Unlock()
				p.terminator.failPeer(p, err)
				return
			}
		}
		p.pending = remaining
		p.channelMu.Unlock()
	})
	if lane == dataChannelControl {
		channel.OnMessage(func(message webrtc.DataChannelMessage) {
			if message.IsString {
				p.handleClientEvent(message.Data)
			}
		})
	}
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
	if envelope.ServerMonotonicMS != nil && envelope.ClientMonotonicMS != nil {
		return mediaDataEnvelope{}, fmt.Errorf("client event contains conflicting monotonic clocks")
	}
	if envelope.ClientMonotonicMS == nil &&
		(envelope.ServerMonotonicMS == nil || *envelope.ServerMonotonicMS != 0) {
		return mediaDataEnvelope{}, fmt.Errorf("client event monotonic clock is invalid")
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
		SessionEpoch: envelope.SessionEpoch,
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
			payload.ToolEpoch != envelope.ToolEpoch || payload.SessionEpoch != envelope.SessionEpoch {
			err = fmt.Errorf("invalid playback progress")
			break
		}
		err = runtime.SendPlaybackProgress(PlaybackProgress{
			SessionID: envelope.SessionID, StreamEpoch: envelope.StreamEpoch,
			TurnID: envelope.TurnID, GenerationID: envelope.GenerationID,
			ToolEpoch: envelope.ToolEpoch, SessionEpoch: payload.SessionEpoch,
			ReceivedSequence:  payload.ReceivedSequence,
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
