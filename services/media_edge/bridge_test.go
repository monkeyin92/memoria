package mediaedge

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"net"
	"testing"
	"time"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

type fakeVoiceCore struct {
	mediav1.UnimplementedVoiceMediaBridgeServer
	received chan *mediav1.MediaToCore
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
	if err := stream.Send(&mediav1.CoreToMedia{
		Event: &mediav1.CoreToMedia_Accepted{
			Accepted: &mediav1.SessionAccepted{Identity: identity, State: mediav1.ConversationState_CONVERSATION_STATE_LISTENING},
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

// Keep the fake independent from generated status helpers; the test only
// needs a non-nil error to terminate the server stream.
type statusError string

func (e statusError) Error() string { return string(e) }

func newBufconnBridge(t *testing.T, service *fakeVoiceCore) (*VoiceCoreBridge, func()) {
	t.Helper()
	listener := bufconn.Listen(1024 * 1024)
	grpcServer := grpc.NewServer()
	mediav1.RegisterVoiceMediaBridgeServer(grpcServer, service)
	go func() { _ = grpcServer.Serve(listener) }()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	conn, err := grpc.DialContext(
		ctx,
		"bufnet",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	cancel()
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
	}
}

func bridgeFormat(rate uint32) BridgeAudioFormat {
	return BridgeAudioFormat{
		Encoding:   mediav1.AudioEncoding_AUDIO_ENCODING_PCM_S16LE,
		SampleRate: rate, Channels: 1, FrameMS: 20,
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
	if err := session.SendStop("stop-1", "first", fence); err != nil {
		t.Fatal(err)
	}
	if err := session.SendStop("stop-2", "second", fence); err != nil {
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
}
