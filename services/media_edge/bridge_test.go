package mediaedge

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"net"
	"regexp"
	"testing"
	"time"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"

	"google.golang.org/grpc"
	"google.golang.org/grpc/connectivity"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

type fakeVoiceCore struct {
	mediav1.UnimplementedVoiceMediaBridgeServer
	received           chan *mediav1.MediaToCore
	requestedAuthority chan mediav1.InteractionAuthority
	effectiveAuthority mediav1.InteractionAuthority
	helloReceived      chan *mediav1.SessionHello
}

type withholdingVoiceCore struct {
	mediav1.UnimplementedVoiceMediaBridgeServer
	helloReceived chan struct{}
	streamDone    chan error
}

func (f *withholdingVoiceCore) Connect(stream grpc.BidiStreamingServer[mediav1.MediaToCore, mediav1.CoreToMedia]) error {
	if _, err := stream.Recv(); err != nil {
		return err
	}
	close(f.helloReceived)
	<-stream.Context().Done()
	err := stream.Context().Err()
	f.streamDone <- err
	return err
}

func (f *fakeVoiceCore) Connect(stream grpc.BidiStreamingServer[mediav1.MediaToCore, mediav1.CoreToMedia]) error {
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	if first.GetHello() == nil {
		return statusError("hello is required")
	}
	identity := first.GetHello().GetIdentity()
	if f.helloReceived != nil {
		f.helloReceived <- first.GetHello()
	}
	if f.requestedAuthority != nil {
		f.requestedAuthority <- first.GetHello().GetInteractionAuthority()
	}
	if err := stream.Send(&mediav1.CoreToMedia{
		Event: &mediav1.CoreToMedia_Accepted{
			Accepted: &mediav1.SessionAccepted{
				Identity: identity, State: mediav1.ConversationState_CONVERSATION_STATE_LISTENING,
				InteractionAuthority: f.effectiveAuthority,
			},
		},
	}); err != nil {
		return err
	}
	for {
		message, err := stream.Recv()
		if err != nil {
			return err
		}
		select {
		case f.received <- message:
		default:
		}
		if audio := message.GetAudio(); audio != nil {
			if err := stream.Send(&mediav1.CoreToMedia{
				Event: &mediav1.CoreToMedia_Generation{
					Generation: &mediav1.GenerationControl{
						Identity: identity, TurnId: 1, GenerationId: 1,
						Action: mediav1.GenerationAction_GENERATION_ACTION_START, Sequence: 1,
					},
				},
			}); err != nil {
				return err
			}
			if err := stream.Send(&mediav1.CoreToMedia{
				Event: &mediav1.CoreToMedia_Audio{
					Audio: &mediav1.AssistantAudioFrame{
						Identity: identity, TurnId: 1, GenerationId: 1, Sequence: 0,
						SourceStartSample: 0, FrameSamples: 160, PcmS16Le: make([]byte, 320),
					},
				},
			}); err != nil {
				return err
			}
			// Deliberately replay the same downlink sequence.  The client must
			// reject it instead of clearing its queue and playing it twice.
			if err := stream.Send(&mediav1.CoreToMedia{
				Event: &mediav1.CoreToMedia_Audio{
					Audio: &mediav1.AssistantAudioFrame{
						Identity: identity, TurnId: 1, GenerationId: 1, Sequence: 0,
						SourceStartSample: 0, FrameSamples: 160, PcmS16Le: make([]byte, 320),
					},
				},
			}); err != nil {
				return err
			}
		}
	}
}

func TestVoiceCoreBridgeNegotiatesAndRecordsEffectiveInteractionAuthority(t *testing.T) {
	service := &fakeVoiceCore{
		received: make(chan *mediav1.MediaToCore, 1), requestedAuthority: make(chan mediav1.InteractionAuthority, 1),
		effectiveAuthority: mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW,
		helloReceived:      make(chan *mediav1.SessionHello, 1),
	}
	bridge, cleanup := newBufconnBridge(t, service)
	defer cleanup()
	bridge.interactionAuthority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	session, err := bridge.Connect(ctx, bridgeIdentity(), bridgeFormat(16_000), bridgeFormat(24_000))
	if err != nil {
		t.Fatal(err)
	}
	if requested := <-service.requestedAuthority; requested != mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW {
		t.Fatalf("unexpected requested authority: %v", requested)
	}
	if session.InteractionAuthority() != mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW {
		t.Fatalf("effective authority was not retained: %v", session.InteractionAuthority())
	}
	hello := <-service.helloReceived
	if !regexp.MustCompile(`^00-[0-9a-f]{32}-[0-9a-f]{16}-01$`).MatchString(hello.GetTraceparent()) {
		t.Fatalf("invalid W3C traceparent: %q", hello.GetTraceparent())
	}
	if session.traceparent != hello.GetTraceparent() {
		t.Fatalf("session traceparent %q does not match hello %q", session.traceparent, hello.GetTraceparent())
	}
}

func TestVoiceCoreBridgeHandshakeContextDoesNotOwnAcceptedStream(t *testing.T) {
	service := &fakeVoiceCore{received: make(chan *mediav1.MediaToCore, 1)}
	bridge, cleanup := newBufconnBridge(t, service)
	defer cleanup()
	streamCtx, cancelStream := context.WithCancel(context.Background())
	defer cancelStream()
	handshakeCtx, cancelHandshake := context.WithCancel(context.Background())
	session, err := bridge.ConnectWithHandshakeContext(
		streamCtx, handshakeCtx, bridgeIdentity(), bridgeFormat(16_000), bridgeFormat(24_000),
	)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	cancelHandshake()
	frame := AudioFrame{
		SessionID: "s", StreamEpoch: 1, Sequence: 0, CaptureStartSample: 0,
		FrameSamples: 160, PayloadB64: base64.StdEncoding.EncodeToString(make([]byte, 320)),
	}
	if err := session.SendAudio(frame); err != nil {
		t.Fatalf("accepted stream was cancelled with its handshake context: %v", err)
	}
	select {
	case received := <-service.received:
		if received.GetAudio() == nil {
			t.Fatalf("expected audio after handshake cancellation, got %v", received)
		}
	case <-time.After(time.Second):
		t.Fatal("accepted stream stopped delivering after handshake cancellation")
	}
}

func TestVoiceCoreBridgeHandshakeTimeoutCancelsUnacceptedStream(t *testing.T) {
	service := &withholdingVoiceCore{
		helloReceived: make(chan struct{}),
		streamDone:    make(chan error, 1),
	}
	bridge, cleanup := newBufconnBridge(t, service)
	defer cleanup()
	streamCtx, cancelStream := context.WithCancel(context.Background())
	defer cancelStream()
	handshakeCtx, cancelHandshake := context.WithTimeout(context.Background(), 100*time.Millisecond)
	defer cancelHandshake()
	_, err := bridge.ConnectWithHandshakeContext(
		streamCtx, handshakeCtx, bridgeIdentity(), bridgeFormat(16_000), bridgeFormat(24_000),
	)
	if !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("handshake timeout error=%v, want deadline exceeded", err)
	}
	select {
	case <-service.helloReceived:
	default:
		t.Fatal("Voice Core did not receive the hello before handshake timeout")
	}
	select {
	case streamErr := <-service.streamDone:
		if !errors.Is(streamErr, context.Canceled) {
			t.Fatalf("timed-out stream ended with %v, want context canceled", streamErr)
		}
	case <-time.After(time.Second):
		t.Fatal("timed-out handshake did not cancel its gRPC stream")
	}
}

func TestVoiceCoreSessionDropsStaleShadowWithoutPoisoningAuthoritativeEvents(t *testing.T) {
	identity := BridgeIdentity{
		SessionID: "s", AccountID: "a", DeviceID: "d", ClientType: "h5", StreamEpoch: 1,
	}
	session := &VoiceCoreSession{
		identity:             identity,
		interactionAuthority: mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW,
	}
	observation := func(sequence, shadowSequence uint64, value *mediav1.SessionIdentity) *mediav1.CoreToMedia {
		return &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_ShadowObservation{
			ShadowObservation: &mediav1.ShadowObservation{
				Identity: value, Sequence: sequence, ShadowSequence: shadowSequence,
				ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
			},
		}}
	}
	if err := session.validateCoreEvent(observation(1, 0, identity.proto())); err != nil {
		t.Fatalf("valid shadow observation failed: %v", err)
	}
	if err := session.validateCoreEvent(observation(2, 0, identity.proto())); !errors.Is(err, errDropShadowObservation) {
		t.Fatalf("duplicate shadow sequence was not classified as a lossy drop: %v", err)
	}
	other := identity.proto()
	other.SessionId = "other"
	if err := session.validateCoreEvent(observation(2, 1, other)); !errors.Is(err, errDropShadowObservation) {
		t.Fatalf("mismatched shadow identity was not classified as a lossy drop: %v", err)
	}
	if err := session.validateCoreEvent(&mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Transcript{
		Transcript: &mediav1.TranscriptEvent{Identity: identity.proto(), Sequence: 3},
	}}); err != nil {
		t.Fatalf("shadow drop poisoned the next authoritative event: %v", err)
	}
}

func TestVoiceCoreSessionEmptyRuntimeSubjectIsNotAWildcard(t *testing.T) {
	identity := BridgeIdentity{
		SessionID: "s", AccountID: "a", DeviceID: "d", ClientType: "device",
		StreamEpoch: 2, BindingID: "binding", BindingVersion: 3, RuntimeProfileVersion: 27,
	}
	session := &VoiceCoreSession{identity: identity}
	event := func(subject string) *mediav1.CoreToMedia {
		candidate := identity.proto()
		candidate.SubjectId = subject
		return &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Transcript{
			Transcript: &mediav1.TranscriptEvent{Identity: candidate, Sequence: 1},
		}}
	}
	// An empty subject must equal the expected empty subject.
	if err := session.validateCoreEvent(event("")); err != nil {
		t.Fatalf("matching empty subject was rejected: %v", err)
	}
	// A populated subject is not absorbed by an expected empty subject.
	if err := session.validateCoreEvent(event("subject_1")); err == nil {
		t.Fatal("empty runtime subject was treated as a wildcard")
	}
}

func TestVoiceCoreSessionAdmitsOnlyCurrentPythonRealtimeEffects(t *testing.T) {
	identity := BridgeIdentity{
		SessionID: "s", AccountID: "a", DeviceID: "d", ClientType: "h5", StreamEpoch: 1,
	}
	fence := Fence{SessionID: "s", TurnID: 4, GenerationID: 5, ToolEpoch: 6}
	session := &VoiceCoreSession{
		identity: identity, current: fence,
		interactionAuthority: mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW,
	}
	effect := func(sequence uint64) *mediav1.RealtimeEffect {
		return &mediav1.RealtimeEffect{
			EffectId: "effect-1", SessionId: identity.SessionID, StreamEpoch: identity.StreamEpoch,
			Sequence: sequence, EffectKind: mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT,
			SourceEventId: "assistant_audio:duck", TurnId: fence.TurnID,
			GenerationId: fence.GenerationID, ToolEpoch: fence.ToolEpoch,
			Payload: []byte(`{"action":"duck","gain":0}`), Identity: identity.proto(),
		}
	}

	candidate := effect(99)
	candidate.CandidateOnly = true
	if err := session.validateCoreEvent(&mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_RealtimeEffect{
		RealtimeEffect: candidate,
	}}); !errors.Is(err, errDropRealtimeEffect) {
		t.Fatalf("candidate effect error=%v, want drop", err)
	}
	if err := session.validateCoreEvent(&mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_RealtimeEffect{
		RealtimeEffect: effect(1),
	}}); err != nil {
		t.Fatalf("current Python effect rejected: %v", err)
	}
	if session.lastEventSequence != 1 {
		t.Fatalf("candidate effect poisoned authoritative sequence: %d", session.lastEventSequence)
	}

	stale := effect(2)
	stale.GenerationId--
	if err := session.validateCoreEvent(&mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_RealtimeEffect{
		RealtimeEffect: stale,
	}}); err == nil {
		t.Fatal("stale effect fence was accepted")
	}
	illegal := effect(3)
	illegal.EffectKind = mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_START_DELEGATION
	if err := session.validateCoreEvent(&mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_RealtimeEffect{
		RealtimeEffect: illegal,
	}}); err == nil {
		t.Fatal("non-media effect kind was accepted")
	}

	session.interactionAuthority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE
	if err := session.validateCoreEvent(&mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_RealtimeEffect{
		RealtimeEffect: effect(4),
	}}); !errors.Is(err, errDropRealtimeEffect) {
		t.Fatalf("unproven Go-authoritative effect error=%v, want drop", err)
	}
}

func TestVoiceCoreBridgeFailsClosedForUnprovenGoAuthority(t *testing.T) {
	service := &fakeVoiceCore{
		received:           make(chan *mediav1.MediaToCore, 1),
		effectiveAuthority: mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE,
	}
	bridge, cleanup := newBufconnBridge(t, service)
	defer cleanup()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if _, err := bridge.Connect(ctx, bridgeIdentity(), bridgeFormat(16_000), bridgeFormat(24_000)); err == nil {
		t.Fatal("bridge accepted unproven Go authority")
	}
}

func TestBridgeIdentityRuntimeAuthorityFence(t *testing.T) {
	device := BridgeIdentity{
		SessionID: "s", AccountID: "a", DeviceID: "d", ClientType: "device",
		StreamEpoch: 2, BindingID: "binding", BindingVersion: 3, RuntimeProfileVersion: 27,
	}
	cases := []struct {
		name     string
		mutate   func(*BridgeIdentity)
		rejected bool
	}{
		{"device empty subject valid", func(identity *BridgeIdentity) {}, false},
		{"device explicit subject valid", func(identity *BridgeIdentity) { identity.SubjectID = "subject_1" }, false},
		{"device missing binding_id", func(identity *BridgeIdentity) { identity.BindingID = "" }, true},
		{"device zero binding_version", func(identity *BridgeIdentity) { identity.BindingVersion = 0 }, true},
		{"device zero runtime_profile_version", func(identity *BridgeIdentity) {
			identity.RuntimeProfileVersion = 0
		}, true},
		{"device malformed subject", func(identity *BridgeIdentity) { identity.SubjectID = "bad subject" }, true},
		{"h5 with fence rejected", func(identity *BridgeIdentity) {
			identity.ClientType = "h5"
			identity.SubjectID = "subject_1"
		}, true},
		{"h5 empty fence valid", func(identity *BridgeIdentity) {
			identity.ClientType = "h5"
			identity.SubjectID = ""
			identity.BindingID = ""
			identity.BindingVersion = 0
			identity.RuntimeProfileVersion = 0
		}, false},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			identity := device
			testCase.mutate(&identity)
			err := identity.validate()
			if testCase.rejected && err == nil {
				t.Fatalf("identity was accepted: %+v", identity)
			}
			if !testCase.rejected && err != nil {
				t.Fatalf("identity was rejected: %v", err)
			}
		})
	}
}

func TestVoiceCoreBridgeAcceptsDeviceWithEmptyRuntimeSubject(t *testing.T) {
	service := &fakeVoiceCore{received: make(chan *mediav1.MediaToCore, 1)}
	bridge, cleanup := newBufconnBridge(t, service)
	defer cleanup()
	identity := BridgeIdentity{
		SessionID: "s", AccountID: "a", DeviceID: "d", ClientType: "device",
		StreamEpoch: 2, BindingID: "binding", BindingVersion: 3, RuntimeProfileVersion: 27,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	session, err := bridge.Connect(ctx, identity, bridgeFormat(16_000), bridgeFormat(24_000))
	if err != nil {
		t.Fatalf("device bridge with empty subject was rejected: %v", err)
	}
	defer session.Close()
	if !session.identity.equal(identity.proto()) {
		t.Fatalf("accepted stream does not own the empty-subject identity: %+v", session.identity)
	}
}

func TestDialVoiceCoreKeepsLongLivedBridgeOutOfIdle(t *testing.T) {
	if voiceCoreBridgeIdleTimeout != 0 {
		t.Fatalf("production Voice Core bridge idle timeout=%s, want disabled", voiceCoreBridgeIdleTimeout)
	}

	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	grpcServer := grpc.NewServer()
	go func() { _ = grpcServer.Serve(listener) }()
	defer grpcServer.Stop()

	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	bridge, err := dialVoiceCoreWithIdleTimeout(ctx, VoiceCoreBridgeConfig{
		Address:                  listener.Addr().String(),
		AllowInsecureDevelopment: true,
	}, 20*time.Millisecond)
	if err != nil {
		t.Fatal(err)
	}
	defer bridge.Close()

	// Use a short test-only timeout to prove that this option controls the
	// grpc-go channel. The production wrapper passes zero, which disables the
	// idle transition for this process-level dependency.
	waitCtx, waitCancel := context.WithTimeout(context.Background(), time.Second)
	defer waitCancel()
	state := bridge.conn.GetState()
	if state != connectivity.Ready {
		t.Fatalf("bridge state=%s, want Ready before idle probe", state)
	}
	if !bridge.conn.WaitForStateChange(waitCtx, state) {
		t.Fatalf("bridge state did not change during idle probe: %v", waitCtx.Err())
	}
	if state = bridge.conn.GetState(); state != connectivity.Idle {
		t.Fatalf("bridge state=%s after idle timeout, want Idle", state)
	}
}

// Keep the fake independent from generated status helpers; the test only
// needs a non-nil error to terminate the server stream.
type statusError string

func (e statusError) Error() string { return string(e) }

func newBufconnBridge(t *testing.T, service mediav1.VoiceMediaBridgeServer) (*VoiceCoreBridge, func()) {
	t.Helper()
	listener := bufconn.Listen(1024 * 1024)
	grpcServer := grpc.NewServer()
	mediav1.RegisterVoiceMediaBridgeServer(grpcServer, service)
	go func() { _ = grpcServer.Serve(listener) }()
	conn, err := grpc.NewClient(
		"passthrough:///bufnet",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err == nil {
		conn.Connect()
		readyCtx, readyCancel := context.WithTimeout(context.Background(), 3*time.Second)
		for conn.GetState() != connectivity.Ready {
			if !conn.WaitForStateChange(readyCtx, conn.GetState()) {
				err = readyCtx.Err()
				break
			}
		}
		readyCancel()
	}
	if err != nil {
		grpcServer.Stop()
		_ = listener.Close()
		t.Fatal(err)
	}
	bridge := NewVoiceCoreBridge(conn)
	cleanup := func() {
		_ = bridge.Close()
		grpcServer.Stop()
		_ = listener.Close()
	}
	return bridge, cleanup
}

func bridgeIdentity() BridgeIdentity {
	return BridgeIdentity{
		SessionID: "s", AccountID: "a", ParticipantID: "p", DeviceID: "d", ClientType: "device", StreamEpoch: 1,
		SubjectID: "subject", BindingID: "binding", BindingVersion: 1, RuntimeProfileVersion: 1,
	}
}

func bridgeFormat(rate uint32) BridgeAudioFormat {
	return BridgeAudioFormat{
		Encoding:   mediav1.AudioEncoding_AUDIO_ENCODING_PCM_S16LE,
		SampleRate: rate, Channels: 1, FrameMS: 20,
	}
}

func TestVoiceCoreSessionSendsExplicitVoicedEndBoundary(t *testing.T) {
	service := &fakeVoiceCore{received: make(chan *mediav1.MediaToCore, 1)}
	bridge, cleanup := newBufconnBridge(t, service)
	defer cleanup()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	session, err := bridge.Connect(ctx, bridgeIdentity(), bridgeFormat(16_000), bridgeFormat(24_000))
	if err != nil {
		t.Fatal(err)
	}
	if err := session.SendVadWithVoicedEnd(400, 320, 0.1, 10, 2, false); err != nil {
		t.Fatal(err)
	}
	select {
	case message := <-service.received:
		vad := message.GetVad()
		if vad == nil || vad.VoicedEndSample == nil || vad.GetVoicedEndSample() != 320 {
			t.Fatalf("missing explicit voiced end: %+v", vad)
		}
	case <-time.After(time.Second):
		t.Fatal("Voice Core did not receive VAD end")
	}
	if err := session.SendVadWithVoicedEnd(400, 401, 0.1, 10, 2, false); err == nil {
		t.Fatal("VAD accepted voiced end after event sample")
	}
}

func TestVoiceCoreBridgeConnectsAndAppliesDualGenerationGate(t *testing.T) {
	service := &fakeVoiceCore{received: make(chan *mediav1.MediaToCore, 4)}
	bridge, cleanup := newBufconnBridge(t, service)
	defer cleanup()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	session, err := bridge.Connect(ctx, bridgeIdentity(), bridgeFormat(16_000), bridgeFormat(24_000))
	if err != nil {
		t.Fatal(err)
	}
	payload := make([]byte, 320)
	frame := AudioFrame{
		SessionID: "s", StreamEpoch: 1, Sequence: 0, CaptureStartSample: 0,
		FrameSamples: 160, PayloadB64: base64.StdEncoding.EncodeToString(payload),
	}
	if err := session.SendAudio(frame); err != nil {
		t.Fatal(err)
	}
	if _, err := session.Recv(); err != nil {
		t.Fatal(err)
	}
	event, err := session.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if event.GetAudio() == nil || session.CurrentFence().GenerationID != 1 {
		t.Fatalf("expected generation-gated audio, fence=%+v event=%v", session.CurrentFence(), event)
	}

	received := <-service.received
	if received.GetAudio() == nil || received.GetAudio().GetFrameSamples() != 160 {
		t.Fatalf("fake core did not receive PCM frame: %v", received)
	}
}

func TestVoiceCoreBridgeRejectsStaleDownlinkAndRequiresMTLSOutsideDevelopment(t *testing.T) {
	service := &fakeVoiceCore{received: make(chan *mediav1.MediaToCore, 4)}
	bridge, cleanup := newBufconnBridge(t, service)
	defer cleanup()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	session, err := bridge.Connect(ctx, bridgeIdentity(), bridgeFormat(16_000), bridgeFormat(24_000))
	if err != nil {
		t.Fatal(err)
	}
	payload := make([]byte, 320)
	if err := session.SendAudio(AudioFrame{SessionID: "s", StreamEpoch: 1, FrameSamples: 160, PayloadB64: base64.StdEncoding.EncodeToString(payload)}); err != nil {
		t.Fatal(err)
	}
	_, _ = session.Recv()
	if _, err := session.Recv(); err != nil {
		t.Fatal(err)
	}
	if _, err := session.Recv(); err == nil {
		t.Fatal("stale downlink sequence was accepted")
	}
	if _, err := DialVoiceCore(context.Background(), VoiceCoreBridgeConfig{Address: "127.0.0.1:1"}); err == nil {
		t.Fatal("plain bridge dial must fail closed outside development")
	}
}

func TestVoiceCoreBridgeStopSequenceIsMonotonic(t *testing.T) {
	service := &fakeVoiceCore{received: make(chan *mediav1.MediaToCore, 4)}
	bridge, cleanup := newBufconnBridge(t, service)
	defer cleanup()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	session, err := bridge.Connect(ctx, bridgeIdentity(), bridgeFormat(16_000), bridgeFormat(24_000))
	if err != nil {
		t.Fatal(err)
	}
	fence := Fence{SessionID: "s"}
	if err := session.SendStop("stop-1", "first", fence, 1234); err != nil {
		t.Fatal(err)
	}
	if err := session.SendStop("stop-2", "second", fence, 2234); err != nil {
		t.Fatal(err)
	}
	first := (<-service.received).GetDevice().GetJsonPayload()
	second := (<-service.received).GetDevice().GetJsonPayload()
	var firstEnvelope, secondEnvelope map[string]any
	if err := json.Unmarshal(first, &firstEnvelope); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(second, &secondEnvelope); err != nil {
		t.Fatal(err)
	}
	if firstEnvelope["sequence"] != float64(0) || secondEnvelope["sequence"] != float64(1) {
		t.Fatalf("stop sequence did not advance: first=%v second=%v", firstEnvelope["sequence"], secondEnvelope["sequence"])
	}
	if firstEnvelope["protocol"] != "media-v1" || secondEnvelope["protocol"] != "media-v1" {
		t.Fatalf("stop envelope protocol missing: first=%v second=%v", firstEnvelope["protocol"], secondEnvelope["protocol"])
	}
}

func TestVoiceCoreBridgeResequencesBrowserEventAfterEdgeStop(t *testing.T) {
	service := &fakeVoiceCore{received: make(chan *mediav1.MediaToCore, 4)}
	bridge, cleanup := newBufconnBridge(t, service)
	defer cleanup()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	session, err := bridge.Connect(ctx, bridgeIdentity(), bridgeFormat(16_000), bridgeFormat(24_000))
	if err != nil {
		t.Fatal(err)
	}
	fence := Fence{SessionID: "s"}
	if err := session.SendStop("stop-1", "first", fence, 1234); err != nil {
		t.Fatal(err)
	}
	raw := []byte(`{"v":1,"protocol":"media-v1","type":"client.text","event_id":"text-1","session_id":"s","stream_epoch":1,"sequence":99,"turn_id":0,"generation_id":0,"tool_epoch":0,"server_monotonic_ms":0,"payload":{"text":"继续"}}`)
	if err := session.SendClientEvent(raw, "client.text", fence, 2345); err != nil {
		t.Fatal(err)
	}
	<-service.received
	device := (<-service.received).GetDevice()
	if device == nil || device.GetEventType() != "client.text" || device.GetMonotonicMs() != 2345 {
		t.Fatalf("unexpected client device event: %v", device)
	}
	var envelope map[string]any
	if err := json.Unmarshal(device.GetJsonPayload(), &envelope); err != nil {
		t.Fatal(err)
	}
	if envelope["sequence"] != float64(1) || envelope["event_id"] != "text-1" {
		t.Fatalf("browser event was not safely re-sequenced: %v", envelope)
	}
}

func TestVoiceCoreBridgeKeywordHardStopRequiresConfidenceAndPreservesFlag(t *testing.T) {
	service := &fakeVoiceCore{received: make(chan *mediav1.MediaToCore, 4)}
	bridge, cleanup := newBufconnBridge(t, service)
	defer cleanup()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	session, err := bridge.Connect(ctx, bridgeIdentity(), bridgeFormat(16_000), bridgeFormat(24_000))
	if err != nil {
		t.Fatal(err)
	}
	if err := session.SendKeyword("停一下", 0.79, 10, 20, true); err == nil {
		t.Fatal("low-confidence hard stop was accepted")
	}
	if err := session.SendKeywordAtFence(
		"停一下", 0.9, 10, 20, true,
		Fence{SessionID: "s", GenerationID: 99},
		1234,
	); err == nil {
		t.Fatal("stale hard-stop keyword fence was accepted")
	}
	if err := session.SendKeyword("停一下", 0.8, 10, 20, true); err != nil {
		t.Fatal(err)
	}
	keyword := (<-service.received).GetKeyword()
	if keyword == nil || !keyword.GetHardStop() || keyword.GetConfidence() != float32(0.8) {
		t.Fatalf("unexpected keyword event: %v", keyword)
	}
	if keyword.GetDetectedMonotonicMs() == 0 {
		t.Fatal("hard-stop keyword event is missing the edge detection timestamp")
	}
}
