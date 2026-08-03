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
	"fmt"
	"math"
	"os"
	"sync"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"

	"google.golang.org/grpc"
	"google.golang.org/grpc/connectivity"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
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
}

// DialVoiceCore opens the authenticated edge -> Voice Core channel.  Plain
// gRPC is available only with an explicit development opt-in.
func DialVoiceCore(ctx context.Context, config VoiceCoreBridgeConfig) (*VoiceCoreBridge, error) {
	if config.Address == "" {
		return nil, fmt.Errorf("Voice Core bridge address is required")
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
		return nil, fmt.Errorf("Voice Core bridge requires mTLS outside development")
	}
	// A production edge must not announce readiness and only discover an
	// unreachable Voice Core on the first browser session. Callers should pass
	// a bounded context so startup fails closed and predictably.
	opts = append(opts, grpc.WithBlock())
	conn, err := grpc.DialContext(ctx, config.Address, opts...)
	if err != nil {
		return nil, fmt.Errorf("dial Voice Core bridge: %w", err)
	}
	return NewVoiceCoreBridge(conn), nil
}

// VoiceCoreBridge owns a gRPC connection and creates fenced sessions on it.
type VoiceCoreBridge struct {
	conn   *grpc.ClientConn
	client mediav1.VoiceMediaBridgeClient
}

func NewVoiceCoreBridge(conn *grpc.ClientConn) *VoiceCoreBridge {
	if conn == nil {
		panic("nil Voice Core gRPC connection")
	}
	return &VoiceCoreBridge{conn: conn, client: mediav1.NewVoiceMediaBridgeClient(conn)}
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
		return nil, fmt.Errorf("Voice Core bridge requires 16 kHz uplink and 24 kHz downlink")
	}
	streamCtx, cancel := context.WithCancel(ctx)
	stream, err := b.client.Connect(streamCtx)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("open Voice Core stream: %w", err)
	}
	session := &VoiceCoreSession{
		identity: identity,
		stream:   stream,
		cancel:   cancel,
		current:  Fence{SessionID: identity.SessionID},
	}
	if err := session.send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Hello{
		Hello: &mediav1.SessionHello{
			Identity:       identity.proto(),
			UplinkFormat:   uplink.proto(),
			DownlinkFormat: downlink.proto(),
			Capabilities:   map[string]string{"media_only": "true", "generation_gate": "true"},
		},
	}}); err != nil {
		session.Close()
		return nil, err
	}
	accepted, err := stream.Recv()
	if err != nil {
		session.Close()
		return nil, fmt.Errorf("receive Voice Core acceptance: %w", err)
	}
	if accepted == nil || accepted.GetAccepted() == nil || !identity.equal(accepted.GetAccepted().GetIdentity()) {
		session.Close()
		return nil, fmt.Errorf("Voice Core returned an invalid session acceptance")
	}
	if accepted.GetAccepted().GetCurrentGenerationId() > 0 {
		session.current.GenerationID = accepted.GetAccepted().GetCurrentGenerationId()
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
	identity           BridgeIdentity
	stream             grpc.BidiStreamingClient[mediav1.MediaToCore, mediav1.CoreToMedia]
	cancel             context.CancelFunc
	sendMu             sync.Mutex
	stateMu            sync.Mutex
	current            Fence
	lastEventSequence  uint64
	lastAudioSequence  uint64
	hasEventSequence   bool
	hasAudioSequence   bool
	lastAudioEnd       uint64
	nextClientSequence uint64
}

func (s *VoiceCoreSession) Identity() BridgeIdentity { return s.identity }

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
		return fmt.Errorf("PCM payload does not match frame samples")
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
	typeValue := mediav1.VadEventType_VAD_EVENT_SPEECH_END
	if start {
		typeValue = mediav1.VadEventType_VAD_EVENT_SPEECH_START
	}
	return s.send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Vad{
		Vad: &mediav1.VadEvent{
			Identity:       s.identity.proto(),
			Type:           typeValue,
			SamplePosition: sample,
			Probability:    probability,
			Rms:            rms,
			NoiseFloor:     noiseFloor,
		},
	}})
}

func (s *VoiceCoreSession) SendKeyword(keyword string, confidence float32, start, end uint64, hardStop bool) error {
	if keyword == "" || end <= start {
		return fmt.Errorf("keyword and sample range are required")
	}
	return s.send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Keyword{
		Keyword: &mediav1.KeywordEvent{
			Identity:    s.identity.proto(),
			Keyword:     keyword,
			Confidence:  confidence,
			StartSample: start,
			EndSample:   end,
			HardStop:    hardStop,
		},
	}})
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

func (s *VoiceCoreSession) SendStop(eventID, reason string, fence Fence) error {
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
		"v": 1, "type": "client.stop_assistant", "event_id": eventID,
		"session_id": s.identity.SessionID, "stream_epoch": s.identity.StreamEpoch,
		"sequence": clientSequence, "turn_id": fence.TurnID, "generation_id": fence.GenerationID,
		"tool_epoch": fence.ToolEpoch, "server_monotonic_ms": 0,
		"payload": map[string]any{"idempotency_key": eventID, "reason": reason},
	}
	raw, err := json.Marshal(envelope)
	if err != nil {
		return fmt.Errorf("encode stop envelope: %w", err)
	}
	if err := s.stream.Send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Device{
		Device: &mediav1.DeviceEvent{Identity: s.identity.proto(), EventType: "client.stop_assistant", JsonPayload: raw},
	}}); err != nil {
		return fmt.Errorf("send media-v1 event: %w", err)
	}
	return nil
}

// Recv returns the next core event after applying identity, event sequence and
// complete generation checks.  A stale event is an error, not a silent queue
// clear, so the media terminator can reconnect with a fresh stream epoch.
func (s *VoiceCoreSession) Recv() (*mediav1.CoreToMedia, error) {
	event, err := s.stream.Recv()
	if err != nil {
		return nil, err
	}
	if err := s.validateCoreEvent(event); err != nil {
		return nil, err
	}
	return event, nil
}

func (s *VoiceCoreSession) validateCoreEvent(event *mediav1.CoreToMedia) error {
	if event == nil {
		return fmt.Errorf("Voice Core returned an empty event")
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
		s.stateMu.Lock()
		defer s.stateMu.Unlock()
		actual := Fence{SessionID: s.identity.SessionID, TurnID: audio.GetTurnId(), GenerationID: audio.GetGenerationId(), ToolEpoch: audio.GetToolEpoch()}
		if !s.current.equal(actual) {
			return fmt.Errorf("stale audio generation")
		}
		if s.hasAudioSequence && audio.GetSequence() <= s.lastAudioSequence {
			return fmt.Errorf("stale audio sequence")
		}
		if s.hasAudioSequence && audio.GetSourceStartSample() < s.lastAudioEnd {
			return fmt.Errorf("audio sample range moved backwards")
		}
		s.lastAudioSequence = audio.GetSequence()
		s.hasAudioSequence = true
		s.lastAudioEnd = audio.GetSourceStartSample() + uint64(audio.GetFrameSamples())
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
		}
		s.current = actual
		return nil
	}
	if transcript := event.GetTranscript(); transcript != nil {
		if !s.identity.equal(transcript.GetIdentity()) {
			return fmt.Errorf("transcript event identity does not match")
		}
		return s.acceptEventSequence(transcript.GetSequence())
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
	if coreError := event.GetError(); coreError != nil && coreError.GetIdentity() != nil && !s.identity.equal(coreError.GetIdentity()) {
		return fmt.Errorf("core error identity does not match")
	}
	return nil
}

func (s *VoiceCoreSession) acceptEventSequence(sequence uint64) error {
	if s.hasEventSequence && sequence <= s.lastEventSequence {
		return fmt.Errorf("stale core event sequence")
	}
	s.lastEventSequence = sequence
	s.hasEventSequence = true
	return nil
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
