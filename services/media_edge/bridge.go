package mediaedge

// This file is the self-authored Media Edge -> Voice Core adapter for the
// media-v1 contract.  It deliberately contains no WebRTC/RTP codec code: a
// reviewed media terminator converts its frames to AudioFrame and uses this
// narrow, fenced bridge.  Keeping the bridge independent of that terminator
// makes the protocol testable without copying a third-party media stack.

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"os"
	"sync"
	"time"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"

	"google.golang.org/grpc"
	"google.golang.org/grpc/connectivity"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
)

const (
	KWSHardStopMinConfidence      float32 = 0.8
	maxRealtimeEffectPayloadBytes         = 4 * 1024
	maxFloorEffectTTLMS           uint64  = 60_000
)

var (
	errDropShadowObservation = errors.New("drop shadow observation")
	errDropRealtimeEffect    = errors.New("drop realtime effect")
	errDropFloorEffect       = errors.New("drop floor effect")
)

// BridgeIdentity is the identity carried on every media-v1 message.
type BridgeIdentity struct {
	SessionID     string
	AccountID     string
	ParticipantID string
	DeviceID      string
	ClientType    string
	StreamEpoch   uint64
}

func (i BridgeIdentity) validate() error {
	if i.SessionID == "" || i.AccountID == "" || i.DeviceID == "" {
		return fmt.Errorf("session, account and device identity are required")
	}
	if i.ClientType == "" {
		return fmt.Errorf("client type is required")
	}
	if i.StreamEpoch == 0 {
		return fmt.Errorf("stream epoch must be positive")
	}
	return nil
}

func (i BridgeIdentity) proto() *mediav1.SessionIdentity {
	return &mediav1.SessionIdentity{
		SessionId:     i.SessionID,
		AccountId:     i.AccountID,
		ParticipantId: i.ParticipantID,
		DeviceId:      i.DeviceID,
		ClientType:    i.ClientType,
		StreamEpoch:   i.StreamEpoch,
	}
}

func identityFromProto(value *mediav1.SessionIdentity) BridgeIdentity {
	if value == nil {
		return BridgeIdentity{}
	}
	return BridgeIdentity{
		SessionID:     value.GetSessionId(),
		AccountID:     value.GetAccountId(),
		ParticipantID: value.GetParticipantId(),
		DeviceID:      value.GetDeviceId(),
		ClientType:    value.GetClientType(),
		StreamEpoch:   value.GetStreamEpoch(),
	}
}

func (i BridgeIdentity) equal(other *mediav1.SessionIdentity) bool {
	return i == identityFromProto(other)
}

// BridgeAudioFormat is kept small so a WebRTC adapter cannot accidentally
// negotiate a format that the Voice Core does not validate.
type BridgeAudioFormat struct {
	Encoding   mediav1.AudioEncoding
	SampleRate uint32
	Channels   uint32
	FrameMS    uint32
}

func (f BridgeAudioFormat) validate() error {
	if f.Encoding != mediav1.AudioEncoding_AUDIO_ENCODING_PCM_S16LE {
		return fmt.Errorf("only PCM_S16LE is accepted by the Voice Core bridge")
	}
	if f.SampleRate == 0 || f.Channels == 0 || f.FrameMS == 0 {
		return fmt.Errorf("audio format values must be positive")
	}
	if f.Channels != 1 || f.FrameMS != 20 {
		return fmt.Errorf("audio format must be mono 20 ms PCM")
	}
	return nil
}

func (f BridgeAudioFormat) proto() *mediav1.AudioFormat {
	return &mediav1.AudioFormat{
		Encoding:   f.Encoding,
		SampleRate: f.SampleRate,
		Channels:   f.Channels,
		FrameMs:    f.FrameMS,
	}
}

// BridgeTLSConfig is the internal mTLS material used for the edge -> core
// hop.  A client certificate and a pinned CA are both required.
type BridgeTLSConfig struct {
	RootCAPEM     []byte
	ClientCertPEM []byte
	ClientKeyPEM  []byte
	ServerName    string
}

func (c BridgeTLSConfig) credentials() (credentials.TransportCredentials, error) {
	if len(c.RootCAPEM) == 0 || len(c.ClientCertPEM) == 0 || len(c.ClientKeyPEM) == 0 {
		return nil, fmt.Errorf("bridge mTLS requires CA, client certificate and private key")
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(c.RootCAPEM) {
		return nil, fmt.Errorf("bridge CA does not contain a certificate")
	}
	cert, err := tls.X509KeyPair(c.ClientCertPEM, c.ClientKeyPEM)
	if err != nil {
		return nil, fmt.Errorf("load bridge client certificate: %w", err)
	}
	return credentials.NewTLS(&tls.Config{
		MinVersion:   tls.VersionTLS13,
		RootCAs:      pool,
		Certificates: []tls.Certificate{cert},
		ServerName:   c.ServerName,
	}), nil
}

// LoadBridgeTLSConfig reads mTLS files without ever returning their contents
// in an error message.
func LoadBridgeTLSConfig(caFile, certFile, keyFile, serverName string) (BridgeTLSConfig, error) {
	read := func(path, label string) ([]byte, error) {
		if path == "" {
			return nil, fmt.Errorf("bridge %s file is required", label)
		}
		value, err := os.ReadFile(path)
		if err != nil {
			return nil, fmt.Errorf("read bridge %s file: %w", label, err)
		}
		if len(value) == 0 {
			return nil, fmt.Errorf("bridge %s file is empty", label)
		}
		return value, nil
	}
	ca, err := read(caFile, "CA")
	if err != nil {
		return BridgeTLSConfig{}, err
	}
	cert, err := read(certFile, "client certificate")
	if err != nil {
		return BridgeTLSConfig{}, err
	}
	key, err := read(keyFile, "client key")
	if err != nil {
		return BridgeTLSConfig{}, err
	}
	return BridgeTLSConfig{RootCAPEM: ca, ClientCertPEM: cert, ClientKeyPEM: key, ServerName: serverName}, nil
}

// VoiceCoreBridgeConfig controls one edge process connection.
type VoiceCoreBridgeConfig struct {
	Address                  string
	TLS                      *BridgeTLSConfig
	AllowInsecureDevelopment bool
	InteractionAuthority     mediav1.InteractionAuthority
}

func normalizeRequestedInteractionAuthority(value mediav1.InteractionAuthority) mediav1.InteractionAuthority {
	if value == mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW {
		return value
	}
	return mediav1.InteractionAuthority_INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE
}

func normalizeEffectiveInteractionAuthority(value mediav1.InteractionAuthority) (mediav1.InteractionAuthority, error) {
	switch value {
	case mediav1.InteractionAuthority_INTERACTION_AUTHORITY_UNSPECIFIED,
		mediav1.InteractionAuthority_INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE:
		return mediav1.InteractionAuthority_INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE, nil
	case mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW:
		return value, nil
	case mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE:
		return mediav1.InteractionAuthority_INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE,
			fmt.Errorf("voice core selected Go authority before the parity gate")
	default:
		return mediav1.InteractionAuthority_INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE,
			fmt.Errorf("voice core selected an unknown interaction authority")
	}
}

// DialVoiceCore opens the authenticated edge -> Voice Core channel.  Plain
// gRPC is available only with an explicit development opt-in.
func DialVoiceCore(ctx context.Context, config VoiceCoreBridgeConfig) (*VoiceCoreBridge, error) {
	if config.Address == "" {
		return nil, fmt.Errorf("voice-core bridge address is required")
	}
	var opts []grpc.DialOption
	if config.TLS != nil {
		transport, err := config.TLS.credentials()
		if err != nil {
			return nil, err
		}
		opts = append(opts, grpc.WithTransportCredentials(transport))
	} else if config.AllowInsecureDevelopment {
		opts = append(opts, grpc.WithTransportCredentials(insecure.NewCredentials()))
	} else {
		return nil, fmt.Errorf("voice-core bridge requires mTLS outside development")
	}
	// grpc.NewClient connects lazily; callers pass a bounded context so
	// startup fails closed and predictably instead of announcing readiness
	// and only discovering an unreachable Voice Core on the first session.
	conn, err := grpc.NewClient(config.Address, opts...)
	if err != nil {
		return nil, fmt.Errorf("dial Voice Core bridge: %w", err)
	}
	conn.Connect()
	if err := waitForReady(ctx, conn); err != nil {
		_ = conn.Close()
		return nil, err
	}
	bridge := NewVoiceCoreBridge(conn)
	bridge.interactionAuthority = normalizeRequestedInteractionAuthority(config.InteractionAuthority)
	return bridge, nil
}

func waitForReady(ctx context.Context, conn *grpc.ClientConn) error {
	for {
		state := conn.GetState()
		if state == connectivity.Ready {
			return nil
		}
		if !conn.WaitForStateChange(ctx, state) {
			return fmt.Errorf("voice-core bridge did not become ready: %w", ctx.Err())
		}
	}
}

// VoiceCoreBridge owns a gRPC connection and creates fenced sessions on it.
type VoiceCoreBridge struct {
	conn                 *grpc.ClientConn
	client               mediav1.VoiceMediaBridgeClient
	interactionAuthority mediav1.InteractionAuthority
}

func NewVoiceCoreBridge(conn *grpc.ClientConn) *VoiceCoreBridge {
	if conn == nil {
		panic("nil Voice Core gRPC connection")
	}
	return &VoiceCoreBridge{
		conn: conn, client: mediav1.NewVoiceMediaBridgeClient(conn),
		interactionAuthority: mediav1.InteractionAuthority_INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE,
	}
}

func (b *VoiceCoreBridge) Close() error {
	if b == nil || b.conn == nil {
		return nil
	}
	return b.conn.Close()
}

func (b *VoiceCoreBridge) Ready() bool {
	return b != nil && b.conn != nil && b.conn.GetState() == connectivity.Ready
}

// Connect starts one bidirectional media-v1 stream and consumes the accepted
// event before returning.  There is no implicit retry: a reconnect must use a
// strictly larger stream epoch supplied by the control plane.
func (b *VoiceCoreBridge) Connect(
	ctx context.Context,
	identity BridgeIdentity,
	uplink BridgeAudioFormat,
	downlink BridgeAudioFormat,
) (*VoiceCoreSession, error) {
	if err := identity.validate(); err != nil {
		return nil, err
	}
	if err := uplink.validate(); err != nil {
		return nil, fmt.Errorf("uplink format: %w", err)
	}
	if err := downlink.validate(); err != nil {
		return nil, fmt.Errorf("downlink format: %w", err)
	}
	if uplink.SampleRate != 16_000 || downlink.SampleRate != 24_000 {
		return nil, fmt.Errorf("voice-core bridge requires 16 kHz uplink and 24 kHz downlink")
	}
	streamCtx, cancel := context.WithCancel(ctx)
	stream, err := b.client.Connect(streamCtx)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("open Voice Core stream: %w", err)
	}
	session := &VoiceCoreSession{
		identity:           identity,
		stream:             stream,
		cancel:             cancel,
		current:            Fence{SessionID: identity.SessionID},
		requireAudioOrigin: true,
	}
	if err := session.send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Hello{
		Hello: &mediav1.SessionHello{
			Identity:             identity.proto(),
			UplinkFormat:         uplink.proto(),
			DownlinkFormat:       downlink.proto(),
			Capabilities:         map[string]string{"media_only": "true", "generation_gate": "true"},
			InteractionAuthority: b.interactionAuthority,
		},
	}}); err != nil {
		_ = session.Close()
		return nil, err
	}
	accepted, err := stream.Recv()
	if err != nil {
		_ = session.Close()
		return nil, fmt.Errorf("receive Voice Core acceptance: %w", err)
	}
	if accepted == nil || accepted.GetAccepted() == nil || !identity.equal(accepted.GetAccepted().GetIdentity()) {
		_ = session.Close()
		return nil, fmt.Errorf("voice-core bridge returned an invalid session acceptance")
	}
	effectiveAuthority, err := normalizeEffectiveInteractionAuthority(
		accepted.GetAccepted().GetInteractionAuthority(),
	)
	if err != nil {
		_ = session.Close()
		return nil, err
	}
	session.interactionAuthority = effectiveAuthority
	if accepted.GetAccepted().GetCurrentGenerationId() > 0 {
		session.current.GenerationID = accepted.GetAccepted().GetCurrentGenerationId()
		// SessionAccepted predates the full fence fields.  A reconnect with a
		// non-zero generation therefore carries one ordered GenerationControl
		// resume event immediately after acceptance; consume it before exposing
		// the session so callers cannot send a stop/playback fact against the
		// generation-only placeholder fence.
		resume, resumeErr := stream.Recv()
		if resumeErr != nil {
			_ = session.Close()
			return nil, fmt.Errorf("receive Voice Core reconnect fence: %w", resumeErr)
		}
		if resumeErr := session.validateCoreEvent(resume); resumeErr != nil || resume.GetGeneration() == nil {
			_ = session.Close()
			if resumeErr != nil {
				return nil, fmt.Errorf("invalid Voice Core reconnect fence: %w", resumeErr)
			}
			return nil, fmt.Errorf("voice-core reconnect acceptance omitted full generation fence")
		}
	}
	return session, nil
}

func (f Fence) equal(other Fence) bool {
	return f.Equal(other)
}

func (f Fence) monotonic(other Fence) bool {
	return other.TurnID > f.TurnID ||
		(other.TurnID == f.TurnID && other.GenerationID > f.GenerationID) ||
		(other.TurnID == f.TurnID && other.GenerationID == f.GenerationID && other.ToolEpoch >= f.ToolEpoch)
}

// VoiceCoreSession serializes client sends and validates the dual-end
// generation/sequence gates on all core output.
type VoiceCoreSession struct {
	identity             BridgeIdentity
	stream               grpc.BidiStreamingClient[mediav1.MediaToCore, mediav1.CoreToMedia]
	cancel               context.CancelFunc
	sendMu               sync.Mutex
	stateMu              sync.Mutex
	current              Fence
	lastEventSequence    uint64
	lastShadowSequence   uint64
	lastFloorEpoch       uint64
	lastAudioSequence    uint64
	hasEventSequence     bool
	hasShadowSequence    bool
	hasFloorEpoch        bool
	hasAudioSequence     bool
	requireAudioOrigin   bool
	lastAudioEnd         uint64
	nextClientSequence   uint64
	interactionAuthority mediav1.InteractionAuthority
}

func (s *VoiceCoreSession) Identity() BridgeIdentity { return s.identity }

func (s *VoiceCoreSession) InteractionAuthority() mediav1.InteractionAuthority {
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	return s.interactionAuthority
}

func (s *VoiceCoreSession) CurrentFence() Fence {
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	return s.current
}

func (s *VoiceCoreSession) send(message *mediav1.MediaToCore) error {
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	if err := s.stream.Send(message); err != nil {
		return fmt.Errorf("send media-v1 event: %w", err)
	}
	return nil
}

func (s *VoiceCoreSession) SendAudio(frame AudioFrame) error {
	if frame.SessionID != s.identity.SessionID || frame.StreamEpoch != s.identity.StreamEpoch {
		return fmt.Errorf("audio identity or stream epoch does not match")
	}
	payload, err := frame.Payload()
	if err != nil {
		return err
	}
	if frame.FrameSamples > math.MaxUint32 || len(payload)%2 != 0 || uint64(len(payload)/2) != frame.FrameSamples {
		return fmt.Errorf("pcm payload does not match frame samples")
	}
	return s.send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Audio{
		Audio: &mediav1.AudioFrame{
			Identity:           s.identity.proto(),
			Sequence:           frame.Sequence,
			CaptureStartSample: frame.CaptureStartSample,
			FrameSamples:       uint32(frame.FrameSamples),
			Payload:            payload,
			Discontinuity:      frame.Discontinuity,
		},
	}})
}

func (s *VoiceCoreSession) SendVad(sample uint64, probability, rms, noiseFloor float32, start bool) error {
	return s.SendVadWithVoicedEnd(sample, sample, probability, rms, noiseFloor, start)
}

// SendVadWithVoicedEnd distinguishes the transport event time from the last
// voiced sample. SPEECH_END must carry this boundary so Voice Core can wait
// for late ASR text without treating VAD tail silence as missing speech.
func (s *VoiceCoreSession) SendVadWithVoicedEnd(sample, voicedEnd uint64, probability, rms, noiseFloor float32, start bool) error {
	typeValue := mediav1.VadEventType_VAD_EVENT_SPEECH_END
	if start {
		typeValue = mediav1.VadEventType_VAD_EVENT_SPEECH_START
	} else if voicedEnd > sample {
		return fmt.Errorf("vad voiced end cannot exceed event sample")
	}
	var voicedEndSample *uint64
	if !start {
		voicedEndSample = &voicedEnd
	}
	return s.send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Vad{
		Vad: &mediav1.VadEvent{
			Identity:        s.identity.proto(),
			Type:            typeValue,
			SamplePosition:  sample,
			Probability:     probability,
			Rms:             rms,
			NoiseFloor:      noiseFloor,
			VoicedEndSample: voicedEndSample,
		},
	}})
}

func (s *VoiceCoreSession) SendKeyword(keyword string, confidence float32, start, end uint64, hardStop bool) error {
	return s.SendKeywordAtFence(
		keyword, confidence, start, end, hardStop, s.CurrentFence(),
		uint64(time.Now().UnixMilli()),
	)
}

func validateKeyword(keyword string, confidence float32, start, end uint64, hardStop bool) error {
	if keyword == "" || end <= start {
		return fmt.Errorf("keyword and sample range are required")
	}
	if math.IsNaN(float64(confidence)) || math.IsInf(float64(confidence), 0) || confidence < 0 || confidence > 1 {
		return fmt.Errorf("keyword confidence must be between 0 and 1")
	}
	if hardStop && confidence < KWSHardStopMinConfidence {
		return fmt.Errorf("hard-stop keyword confidence must be at least %.1f", KWSHardStopMinConfidence)
	}
	return nil
}

// SendKeywordAtFence rejects delayed KWS evidence before it can stop a newer
// generation and stamps the edge-side detection time so Voice Core can
// measure interrupt.detect → interrupt.cancel. SendKeyword remains as the
// current-fence compatibility helper.
func (s *VoiceCoreSession) SendKeywordAtFence(keyword string, confidence float32, start, end uint64, hardStop bool, fence Fence, detectedAtMs uint64) error {
	if err := validateKeyword(keyword, confidence, start, end, hardStop); err != nil {
		return err
	}
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	if !s.current.Equal(fence) {
		return fmt.Errorf("keyword belongs to a stale generation")
	}
	keywordEvent := &mediav1.KeywordEvent{
		Identity:    s.identity.proto(),
		Keyword:     keyword,
		Confidence:  confidence,
		StartSample: start,
		EndSample:   end,
		HardStop:    hardStop,
	}
	if detectedAtMs > 0 {
		keywordEvent.DetectedMonotonicMs = &detectedAtMs
	}
	if err := s.stream.Send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Keyword{
		Keyword: keywordEvent,
	}}); err != nil {
		return fmt.Errorf("send media-v1 event: %w", err)
	}
	return nil
}

func (s *VoiceCoreSession) SendPlaybackProgress(progress PlaybackProgress) error {
	if progress.SessionID != s.identity.SessionID || progress.StreamEpoch != s.identity.StreamEpoch {
		return fmt.Errorf("playback identity or stream epoch does not match")
	}
	s.stateMu.Lock()
	current := s.current
	s.stateMu.Unlock()
	if !current.Equal(Fence{SessionID: s.identity.SessionID, TurnID: progress.TurnID, GenerationID: progress.GenerationID, ToolEpoch: progress.ToolEpoch}) {
		return fmt.Errorf("playback progress belongs to a stale generation")
	}
	return s.send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Playback{
		Playback: &mediav1.PlaybackProgress{
			Identity:          s.identity.proto(),
			GenerationId:      progress.GenerationID,
			ReceivedSequence:  progress.ReceivedSequence,
			RenderedSampleEnd: progress.RenderedSampleEnd,
			ClientMonotonicMs: progress.ClientMonotonicMS,
			Approximate:       progress.Approximate,
			TurnId:            progress.TurnID,
			ToolEpoch:         progress.ToolEpoch,
		},
	}})
}

// SendClientEvent re-sequences a validated browser envelope onto the single
// Media Edge -> Voice Core client-event stream. Browser sequence numbers are
// transport-local and cannot be mixed with Edge-generated stop events.
func (s *VoiceCoreSession) SendClientEvent(
	raw []byte,
	eventType string,
	fence Fence,
	monotonicMS uint64,
) error {
	if len(raw) == 0 || eventType == "" || fence.SessionID != s.identity.SessionID {
		return fmt.Errorf("client event and matching fence are required")
	}
	var envelope map[string]any
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return fmt.Errorf("decode client envelope: %w", err)
	}
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	s.stateMu.Lock()
	if !s.current.Equal(fence) {
		s.stateMu.Unlock()
		return fmt.Errorf("client event belongs to a stale generation")
	}
	sequence := s.nextClientSequence
	s.nextClientSequence++
	s.stateMu.Unlock()
	envelope["sequence"] = sequence
	encoded, err := json.Marshal(envelope)
	if err != nil {
		return fmt.Errorf("encode client envelope: %w", err)
	}
	if err := s.stream.Send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Device{
		Device: &mediav1.DeviceEvent{
			Identity: s.identity.proto(), EventType: eventType,
			JsonPayload: encoded, MonotonicMs: monotonicMS,
		},
	}}); err != nil {
		return fmt.Errorf("send media-v1 event: %w", err)
	}
	return nil
}

func (s *VoiceCoreSession) SendStop(eventID, reason string, fence Fence, detectedAtMs uint64) error {
	if eventID == "" || fence.SessionID != s.identity.SessionID {
		return fmt.Errorf("stop event id and matching fence are required")
	}
	s.stateMu.Lock()
	current := s.current
	s.stateMu.Unlock()
	if !current.Equal(fence) {
		return fmt.Errorf("stop fence is stale")
	}
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	s.stateMu.Lock()
	clientSequence := s.nextClientSequence
	s.nextClientSequence++
	s.stateMu.Unlock()
	envelope := map[string]any{
		"v": 1, "protocol": "media-v1", "type": "client.stop_assistant", "event_id": eventID,
		"session_id": s.identity.SessionID, "stream_epoch": s.identity.StreamEpoch,
		"sequence": clientSequence, "turn_id": fence.TurnID, "generation_id": fence.GenerationID,
		"tool_epoch": fence.ToolEpoch, "server_monotonic_ms": 0,
		"payload": map[string]any{"idempotency_key": eventID, "reason": reason},
	}
	raw, err := json.Marshal(envelope)
	if err != nil {
		return fmt.Errorf("encode stop envelope: %w", err)
	}
	deviceEvent := &mediav1.DeviceEvent{
		Identity:    s.identity.proto(),
		EventType:   "client.stop_assistant",
		JsonPayload: raw,
		MonotonicMs: detectedAtMs,
	}
	if err := s.stream.Send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Device{
		Device: deviceEvent,
	}}); err != nil {
		return fmt.Errorf("send media-v1 event: %w", err)
	}
	return nil
}

// Recv returns the next core event after applying identity, event sequence and
// complete generation checks.  A stale event is an error, not a silent queue
// clear, so the media terminator can reconnect with a fresh stream epoch.
func (s *VoiceCoreSession) Recv() (*mediav1.CoreToMedia, error) {
	for {
		event, err := s.stream.Recv()
		if err != nil {
			return nil, err
		}
		if err := s.validateCoreEvent(event); errors.Is(err, errDropShadowObservation) ||
			errors.Is(err, errDropRealtimeEffect) || errors.Is(err, errDropFloorEffect) {
			continue
		} else if err != nil {
			return nil, err
		}
		return event, nil
	}
}

func (s *VoiceCoreSession) validateCoreEvent(event *mediav1.CoreToMedia) error {
	if event == nil {
		return fmt.Errorf("voice-core bridge returned an empty event")
	}
	if accepted := event.GetAccepted(); accepted != nil {
		if !s.identity.equal(accepted.GetIdentity()) {
			return fmt.Errorf("accepted event identity does not match")
		}
		return nil
	}
	if audio := event.GetAudio(); audio != nil {
		if !s.identity.equal(audio.GetIdentity()) {
			return fmt.Errorf("audio event identity does not match")
		}
		if audio.GetFrameSamples() == 0 || len(audio.GetPcmS16Le())%2 != 0 ||
			uint64(len(audio.GetPcmS16Le())/2) != uint64(audio.GetFrameSamples()) {
			return fmt.Errorf("audio PCM payload does not match frame samples")
		}
		s.stateMu.Lock()
		defer s.stateMu.Unlock()
		actual := Fence{SessionID: s.identity.SessionID, TurnID: audio.GetTurnId(), GenerationID: audio.GetGenerationId(), ToolEpoch: audio.GetToolEpoch()}
		if !s.current.equal(actual) {
			return fmt.Errorf("stale audio generation")
		}
		if s.hasAudioSequence && audio.GetSequence() <= s.lastAudioSequence {
			return fmt.Errorf("stale audio sequence")
		}
		if s.hasAudioSequence && audio.GetSequence() != s.lastAudioSequence+1 {
			return fmt.Errorf("audio sequence has a gap")
		}
		if s.hasAudioSequence && audio.GetSourceStartSample() < s.lastAudioEnd {
			return fmt.Errorf("audio sample range moved backwards")
		}
		if s.hasAudioSequence && audio.GetSourceStartSample() != s.lastAudioEnd {
			return fmt.Errorf("audio sample range has a gap")
		}
		if !s.hasAudioSequence && s.requireAudioOrigin &&
			(audio.GetSequence() != 0 || audio.GetSourceStartSample() != 0) {
			return fmt.Errorf("first audio frame must start at sequence and sample zero")
		}
		s.lastAudioSequence = audio.GetSequence()
		s.hasAudioSequence = true
		end := audio.GetSourceStartSample() + uint64(audio.GetFrameSamples())
		if end < audio.GetSourceStartSample() {
			return fmt.Errorf("audio sample range overflow")
		}
		s.lastAudioEnd = end
		return nil
	}
	if generation := event.GetGeneration(); generation != nil {
		if !s.identity.equal(generation.GetIdentity()) {
			return fmt.Errorf("generation event identity does not match")
		}
		s.stateMu.Lock()
		defer s.stateMu.Unlock()
		actual := Fence{SessionID: s.identity.SessionID, TurnID: generation.GetTurnId(), GenerationID: generation.GetGenerationId(), ToolEpoch: generation.GetToolEpoch()}
		if !s.current.monotonic(actual) {
			return fmt.Errorf("generation moved backwards")
		}
		if err := s.acceptEventSequence(generation.GetSequence()); err != nil {
			return err
		}
		if !s.current.equal(actual) {
			// Downlink sequence and sample ranges start at zero for every new
			// generation; never let the prior answer poison the new fence.
			s.hasAudioSequence = false
			s.lastAudioSequence = 0
			s.lastAudioEnd = 0
			// RESUME takes over an in-flight generation after reconnect;
			// START/CANCEL/COMPLETE begin a fresh media range.
			s.requireAudioOrigin = generation.GetAction() != mediav1.GenerationAction_GENERATION_ACTION_RESUME
		}
		s.current = actual
		return nil
	}
	if effect := event.GetRealtimeEffect(); effect != nil {
		// Candidate values are telemetry even when an upstream implementation
		// accidentally uses the executable oneof. A6B is still fail-closed.
		if effect.GetCandidateOnly() ||
			s.interactionAuthority == mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE {
			return errDropRealtimeEffect
		}
		if !s.identity.equal(effect.GetIdentity()) ||
			effect.GetSessionId() != s.identity.SessionID ||
			effect.GetStreamEpoch() != s.identity.StreamEpoch {
			return fmt.Errorf("realtime effect identity does not match")
		}
		fence, err := validateRealtimeEffect(effect)
		if err != nil {
			return err
		}
		s.stateMu.Lock()
		defer s.stateMu.Unlock()
		if effect.GetEffectKind() == mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION {
			if fence.TurnID != s.current.TurnID || fence.ToolEpoch != s.current.ToolEpoch ||
				s.current.GenerationID == math.MaxUint64 || fence.GenerationID != s.current.GenerationID+1 {
				return fmt.Errorf("realtime cancel effect fence is stale")
			}
		} else if !s.current.Equal(fence) {
			return fmt.Errorf("realtime effect fence is stale")
		}
		if err := s.acceptEventSequence(effect.GetSequence()); err != nil {
			return err
		}
		if effect.GetEffectKind() == mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION {
			s.current = fence
		}
		return nil
	}
	if effect := event.GetFloorEffect(); effect != nil {
		if effect.GetCandidateOnly() ||
			s.interactionAuthority == mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE {
			return errDropFloorEffect
		}
		if !s.identity.equal(effect.GetIdentity()) {
			return fmt.Errorf("floor effect identity does not match")
		}
		fence, err := validateFloorEffect(effect)
		if err != nil {
			return err
		}
		s.stateMu.Lock()
		defer s.stateMu.Unlock()
		if !s.current.Equal(fence) {
			return fmt.Errorf("floor effect fence is stale")
		}
		if s.hasFloorEpoch && effect.GetFloorEpoch() <= s.lastFloorEpoch {
			return fmt.Errorf("floor effect epoch is stale")
		}
		if err := s.acceptEventSequence(effect.GetSequence()); err != nil {
			return err
		}
		s.lastFloorEpoch = effect.GetFloorEpoch()
		s.hasFloorEpoch = true
		return nil
	}
	if transcript := event.GetTranscript(); transcript != nil {
		if !s.identity.equal(transcript.GetIdentity()) {
			return fmt.Errorf("transcript event identity does not match")
		}
		return s.acceptEventSequence(transcript.GetSequence())
	}
	if observation := event.GetShadowObservation(); observation != nil {
		if s.interactionAuthority != mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW ||
			!s.identity.equal(observation.GetIdentity()) || !observation.GetCandidateOnly() ||
			observation.GetContractVersion() != shadowA6AContractVersion {
			return errDropShadowObservation
		}
		s.stateMu.Lock()
		defer s.stateMu.Unlock()
		if (s.hasEventSequence && observation.GetSequence() <= s.lastEventSequence) ||
			(s.hasShadowSequence && observation.GetShadowSequence() <= s.lastShadowSequence) {
			return errDropShadowObservation
		}
		s.lastEventSequence = observation.GetSequence()
		s.hasEventSequence = true
		s.lastShadowSequence = observation.GetShadowSequence()
		s.hasShadowSequence = true
		return nil
	}
	if state := event.GetState(); state != nil {
		if !s.identity.equal(state.GetIdentity()) {
			return fmt.Errorf("state event identity does not match")
		}
		return s.acceptEventSequence(state.GetSequence())
	}
	if client := event.GetClient(); client != nil {
		if !s.identity.equal(client.GetIdentity()) {
			return fmt.Errorf("client event identity does not match")
		}
		return s.acceptEventSequence(client.GetSequence())
	}
	if coreError := event.GetError(); coreError != nil {
		if !s.identity.equal(coreError.GetIdentity()) {
			return fmt.Errorf("core error identity does not match")
		}
		return nil
	}
	return fmt.Errorf("voice-core bridge returned an unknown event")
}

func (s *VoiceCoreSession) acceptEventSequence(sequence uint64) error {
	if s.hasEventSequence && sequence <= s.lastEventSequence {
		return fmt.Errorf("stale core event sequence")
	}
	s.lastEventSequence = sequence
	s.hasEventSequence = true
	return nil
}

func validateRealtimeEffect(effect *mediav1.RealtimeEffect) (Fence, error) {
	if effect == nil || effect.GetEffectId() == "" || len(effect.GetEffectId()) > 256 ||
		effect.GetSourceEventId() == "" || len(effect.GetSourceEventId()) > 128 {
		return Fence{}, fmt.Errorf("realtime effect id and source event are required")
	}
	switch effect.GetEffectKind() {
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT,
		mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION,
		mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_PAUSE_OUTPUT,
		mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_RESUME_OUTPUT:
	default:
		return Fence{}, fmt.Errorf("realtime effect kind is not executable")
	}
	payload := effect.GetPayload()
	if len(payload) == 0 || len(payload) > maxRealtimeEffectPayloadBytes || !json.Valid(payload) {
		return Fence{}, fmt.Errorf("realtime effect payload is invalid")
	}
	var decoded map[string]json.RawMessage
	if err := json.Unmarshal(payload, &decoded); err != nil || decoded == nil {
		return Fence{}, fmt.Errorf("realtime effect payload must be an object")
	}
	return Fence{
		SessionID: effect.GetSessionId(), TurnID: effect.GetTurnId(),
		GenerationID: effect.GetGenerationId(), ToolEpoch: effect.GetToolEpoch(),
	}, nil
}

func validateFloorEffect(effect *mediav1.FloorEffect) (Fence, error) {
	if effect == nil || effect.GetEffectId() == "" || len(effect.GetEffectId()) > 256 ||
		effect.GetSourceEventId() == "" || len(effect.GetSourceEventId()) > 128 ||
		effect.GetFloorEpoch() == 0 {
		return Fence{}, fmt.Errorf("floor effect id, source event and epoch are required")
	}
	switch effect.GetFloorState() {
	case mediav1.FloorState_FLOOR_STATE_USER_HOLDS_FLOOR,
		mediav1.FloorState_FLOOR_STATE_ASSISTANT_HOLDS_FLOOR,
		mediav1.FloorState_FLOOR_STATE_OVERLAP,
		mediav1.FloorState_FLOOR_STATE_UNCERTAIN,
		mediav1.FloorState_FLOOR_STATE_SILENCE:
	default:
		return Fence{}, fmt.Errorf("floor effect state is invalid")
	}
	now := uint64(time.Now().UnixMilli())
	if effect.GetExpiresAtMs() <= now || effect.GetExpiresAtMs()-now > maxFloorEffectTTLMS {
		return Fence{}, fmt.Errorf("floor effect is expired or exceeds TTL")
	}
	return Fence{
		SessionID: effect.GetIdentity().GetSessionId(), TurnID: effect.GetTurnId(),
		GenerationID: effect.GetGenerationId(), ToolEpoch: effect.GetToolEpoch(),
	}, nil
}

func (s *VoiceCoreSession) CloseSend() error {
	return s.stream.CloseSend()
}

// Close cancels this stream.  The parent bridge connection remains reusable.
func (s *VoiceCoreSession) Close() error {
	if s.cancel != nil {
		s.cancel()
	}
	return s.stream.CloseSend()
}

// PlaybackProgress is the Go-side equivalent of the media-v1 playback ACK.
type PlaybackProgress struct {
	SessionID         string
	StreamEpoch       uint64
	GenerationID      uint64
	ReceivedSequence  uint64
	RenderedSampleEnd uint64
	ClientMonotonicMS uint64
	Approximate       bool
	TurnID            uint64
	ToolEpoch         uint64
}
