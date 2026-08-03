package mediaedge

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

type fakeCoreStream struct {
	mu        sync.Mutex
	sent      []AudioFrame
	events    chan *mediav1.CoreToMedia
	closed    chan struct{}
	closeOnce sync.Once
	failSend  error
}

func newFakeCoreStream() *fakeCoreStream {
	return &fakeCoreStream{
		events: make(chan *mediav1.CoreToMedia, 8),
		closed: make(chan struct{}),
	}
}

func (f *fakeCoreStream) SendAudio(frame AudioFrame) error {
	if f.failSend != nil {
		return f.failSend
	}
	f.mu.Lock()
	f.sent = append(f.sent, frame)
	f.mu.Unlock()
	return nil
}

func (f *fakeCoreStream) Recv() (*mediav1.CoreToMedia, error) {
	select {
	case event := <-f.events:
		return event, nil
	case <-f.closed:
		return nil, errors.New("fake core closed")
	}
}

func (f *fakeCoreStream) Close() error {
	f.closeOnce.Do(func() { close(f.closed) })
	return nil
}

func (f *fakeCoreStream) sentCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.sent)
}

func runtimeSession(t *testing.T, maxPending int) *Session {
	t.Helper()
	session, err := NewSession(OpenSessionRequest{
		SessionID: "s", AccountID: "a", DeviceID: "d", StreamEpoch: 1,
	}, maxPending)
	if err != nil {
		t.Fatal(err)
	}
	return session
}

func runtimeFrame(sequence uint64) AudioFrame {
	return AudioFrame{
		SessionID: "s", StreamEpoch: 1, Sequence: sequence,
		CaptureStartSample: sequence, FrameSamples: 1,
		PayloadB64: "AAE=",
	}
}

func runtimeIdentity() *mediav1.SessionIdentity {
	return &mediav1.SessionIdentity{
		SessionId: "s", AccountId: "a", DeviceId: "d", StreamEpoch: 1,
	}
}

func TestVoiceCoreMediaRuntimeForwardsAndRetiresBoundedUplink(t *testing.T) {
	session := runtimeSession(t, 1)
	core := newFakeCoreStream()
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	runtime.Start()
	if err := runtime.SendUplink(runtimeFrame(0)); err != nil {
		t.Fatal(err)
	}
	// The first frame was retired after SendAudio; a one-frame queue can accept
	// the next contiguous frame instead of reporting a false overflow.
	if err := runtime.SendUplink(runtimeFrame(1)); err != nil {
		t.Fatal(err)
	}
	if core.sentCount() != 2 {
		t.Fatalf("sent=%d, want 2", core.sentCount())
	}
	if err := runtime.Close(); err != nil {
		t.Fatal(err)
	}
	if err := runtime.Wait(); err != nil {
		t.Fatal(err)
	}
}

func TestVoiceCoreMediaRuntimeAppliesGenerationAndDownlinkGate(t *testing.T) {
	session := runtimeSession(t, 2)
	core := newFakeCoreStream()
	callbackEvents := make(chan struct{}, 2)
	runtime, err := NewVoiceCoreMediaRuntime(
		context.Background(), session, core,
		func(*mediav1.CoreToMedia) { callbackEvents <- struct{}{} },
		nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	runtime.Start()
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: runtimeIdentity(), TurnId: 1, GenerationId: 1,
			Sequence: 1, Action: mediav1.GenerationAction_GENERATION_ACTION_START,
		},
	}}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Audio{
		Audio: &mediav1.AssistantAudioFrame{
			Identity: runtimeIdentity(), TurnId: 1, GenerationId: 1,
			Sequence: 0, SourceStartSample: 0, FrameSamples: 1, PcmS16Le: []byte{0, 1},
		},
	}}
	deadline := time.Now().Add(time.Second)
	for range 2 {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			break
		}
		select {
		case <-callbackEvents:
		case <-time.After(remaining):
			break
		}
	}
	if frame, ok := session.PopDownlink(); ok {
		if frame.GenerationID != 1 || frame.Sequence != 0 || frame.FrameSamples != 1 {
			t.Fatalf("unexpected downlink: %+v", frame)
		}
		_ = runtime.Close()
		if err := runtime.Wait(); err != nil {
			t.Fatal(err)
		}
		return
	}
	_ = runtime.Close()
	t.Fatal("downlink was not delivered")
}

func TestVoiceCoreMediaRuntimeRejectsCoreIdentityMismatch(t *testing.T) {
	session := runtimeSession(t, 2)
	core := newFakeCoreStream()
	errorsSeen := make(chan error, 1)
	runtime, err := NewVoiceCoreMediaRuntime(
		context.Background(), session, core, nil,
		func(err error) { errorsSeen <- err },
	)
	if err != nil {
		t.Fatal(err)
	}
	runtime.Start()
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: &mediav1.SessionIdentity{SessionId: "other", AccountId: "a", DeviceId: "d", StreamEpoch: 1},
			TurnId:   1, GenerationId: 1, Sequence: 1,
		},
	}}
	select {
	case err := <-errorsSeen:
		if !strings.Contains(err.Error(), "identity") {
			t.Fatalf("unexpected error: %v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("identity mismatch was not reported")
	}
	if err := runtime.Wait(); err == nil {
		t.Fatal("runtime should stop after identity mismatch")
	}
}
