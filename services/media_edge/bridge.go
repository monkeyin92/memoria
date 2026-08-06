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
	"errors"
	"fmt"
	"os"
	"sync"

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
