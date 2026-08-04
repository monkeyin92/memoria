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
	mu           sync.Mutex
	sent         []AudioFrame
	stops        []Fence
	stopTimes    []uint64
	current      Fence
	keywords     int
	keywordTimes []uint64
	keywordHook  func()
	failKeyword  error
	events       chan *mediav1.CoreToMedia
	closed       chan struct{}
	closeOnce    sync.Once
	failSend     error
	failStop     error
}

func newFakeCoreStream() *fakeCoreStream {
	return &fakeCoreStream{
		events: make(chan *mediav1.CoreToMedia, 256),
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

func (f *fakeCoreStream) CurrentFence() Fence {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.current
}

func (f *fakeCoreStream) setCurrentFence(fence Fence) {
	f.mu.Lock()
	f.current = fence
	f.mu.Unlock()
}

func (f *fakeCoreStream) SendStop(_ string, _ string, fence Fence, detectedAtMs uint64) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.failStop != nil {
		return f.failStop
	}
	if !f.current.Equal(fence) {
		return errors.New("stop fence is stale")
	}
	f.stops = append(f.stops, fence)
	f.stopTimes = append(f.stopTimes, detectedAtMs)
	return nil
}

func (f *fakeCoreStream) setStopError(err error) {
	f.mu.Lock()
	f.failStop = err
	f.mu.Unlock()
}

func (f *fakeCoreStream) stopCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.stops)
}

func (f *fakeCoreStream) SendKeywordAtFence(_ string, _ float32, _, _ uint64, _ bool, fence Fence, detectedAtMs uint64) error {
	f.mu.Lock()
	current := f.current
	hook := f.keywordHook
	fail := f.failKeyword
	f.mu.Unlock()
	if !current.Equal(fence) {
		return errors.New("keyword fence is stale")
	}
	if hook != nil {
		hook()
	}
	if fail != nil {
		return fail
	}
	f.mu.Lock()
	f.keywords++
	f.keywordTimes = append(f.keywordTimes, detectedAtMs)
	f.mu.Unlock()
	return nil
}

func (f *fakeCoreStream) keywordCount() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.keywords
}

func (f *fakeCoreStream) setKeywordBehavior(hook func(), err error) {
	f.mu.Lock()
	f.keywordHook = hook
	f.failKeyword = err
	f.mu.Unlock()
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
waitCallbacks:
	for range 2 {
		remaining := time.Until(deadline)
		if remaining <= 0 {
			break
		}
		select {
		case <-callbackEvents:
		case <-time.After(remaining):
			break waitCallbacks
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

func TestVoiceCoreMediaRuntimeExplicitSenderRetiresMoreThanQueueCapacity(t *testing.T) {
	session := runtimeSession(t, 1)
	core := newFakeCoreStream()
	delivered := make(chan AudioFrame, 128)
	runtime, err := NewVoiceCoreMediaRuntimeWithDownlinkSender(
		context.Background(), session, core,
		func(_ context.Context, frame AudioFrame) error {
			// The sender receives a generation-scoped context. A real terminator
			// must stop its enqueue when hard-stop cancellation closes that gate.
			delivered <- frame
			return nil
		},
		nil, nil,
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
	for sequence := range uint64(101) {
		core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Audio{
			Audio: &mediav1.AssistantAudioFrame{
				Identity: runtimeIdentity(), TurnId: 1, GenerationId: 1,
				Sequence: sequence, SourceStartSample: sequence,
				FrameSamples: 1, PcmS16Le: []byte{0, 1},
			},
		}}
	}
	deadline := time.After(2 * time.Second)
	for count := 0; count < 101; count++ {
		select {
		case frame := <-delivered:
			if frame.Sequence != uint64(count) {
				t.Fatalf("delivered sequence=%d, want %d", frame.Sequence, count)
			}
		case <-deadline:
			t.Fatalf("delivered %d frames, want 101", count)
		}
	}
	if _, ok := session.PopDownlink(); ok {
		t.Fatal("sender-acknowledged frame remained queued")
	}
	if stats := session.Stats(); stats.OverflowFrames != 0 || stats.DownlinkFrames != 101 {
		t.Fatalf("unexpected stats: %+v", stats)
	}
	_ = runtime.Close()
	if err := runtime.Wait(); err != nil {
		t.Fatal(err)
	}
}

func TestConcurrentCancelDoesNotWaitForDownlinkSender(t *testing.T) {
	session := runtimeSession(t, 16)
	current := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(current); err != nil {
		t.Fatal(err)
	}
	senderEntered := make(chan struct{})
	release := make(chan struct{})
	delivered := make(chan AudioFrame, 1)
	var senderStarted bool
	sender := func(ctx context.Context, frame AudioFrame) error {
		if !senderStarted {
			senderStarted = true
			close(senderEntered)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-release:
			delivered <- frame
			return nil
		}
	}
	deliveryDone := make(chan struct{})
	var deliveryErr error
	go func() {
		frame := runtimeFrame(0)
		frame.TurnID = current.TurnID
		frame.GenerationID = current.GenerationID
		deliveryErr = session.DeliverDownlink(frame, sender)
		close(deliveryDone)
	}()
	<-senderEntered
	cancelDone := make(chan struct{})
	go func() {
		_, _, _, _ = session.CancelGeneration("atomic-stop", &current)
		close(cancelDone)
	}()
	select {
	case <-cancelDone:
	case <-time.After(50 * time.Millisecond):
		t.Fatal("CancelGeneration waited for the sender")
	}
	<-deliveryDone
	if !errors.Is(deliveryErr, ErrStaleDownlinkGeneration) {
		t.Fatalf("downlink delivery error=%v, want stale generation", deliveryErr)
	}
	select {
	case frame := <-delivered:
		t.Fatalf("cancelled frame reached sender: %+v", frame)
	default:
	}
	senderCalls := 0
	stale := runtimeFrame(0)
	stale.TurnID = current.TurnID
	stale.GenerationID = current.GenerationID
	if err := session.DeliverDownlink(stale, func(context.Context, AudioFrame) error {
		senderCalls++
		return nil
	}); !errors.Is(err, ErrStaleDownlinkGeneration) || senderCalls != 0 {
		t.Fatalf("stale frame reached sender: err=%v senderCalls=%d", err, senderCalls)
	}
}

func TestVoiceCoreMediaRuntimeSenderFailureKeepsFramePending(t *testing.T) {
	session := runtimeSession(t, 1)
	core := newFakeCoreStream()
	sendErr := errors.New("transport backpressure")
	runtime, err := NewVoiceCoreMediaRuntimeWithDownlinkSender(
		context.Background(), session, core,
		func(context.Context, AudioFrame) error { return sendErr }, nil, nil,
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
			Sequence: 0, FrameSamples: 1, PcmS16Le: []byte{0, 1},
		},
	}}
	if err := runtime.Wait(); !errors.Is(err, sendErr) {
		t.Fatalf("wait error=%v, want %v", err, sendErr)
	}
	if frame, ok := session.PopDownlink(); !ok || frame.Sequence != 0 {
		t.Fatalf("failed delivery was not retained: frame=%+v ok=%v", frame, ok)
	}
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

func TestVoiceCoreMediaRuntimeCancelUsesAuthoritativeFenceAndKeepsSessionActive(t *testing.T) {
	session := runtimeSession(t, 2)
	current := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(current); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.setCurrentFence(current)
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	stale := Fence{SessionID: "s", TurnID: 1, GenerationID: 0}
	if _, err := runtime.CancelGeneration("stale-stop", "user_button", &stale, 111); err == nil {
		t.Fatal("stale expected stop fence was accepted")
	}
	stopErr := errors.New("core temporarily unavailable")
	core.setStopError(stopErr)
	if _, err := runtime.CancelGeneration("stop-1", "user_button", &current, 222); !errors.Is(err, stopErr) {
		t.Fatalf("first stop error=%v, want %v", err, stopErr)
	}
	core.setStopError(nil)
	cancelled, err := runtime.CancelGeneration("stop-1", "user_button", &current, 222)
	if err != nil {
		t.Fatalf("failed stop retry: %v", err)
	}
	if cancelled.GenerationID != 2 {
		t.Fatalf("cancelled=%+v", cancelled)
	}
	core.setCurrentFence(cancelled)
	if _, err := runtime.CancelGeneration("stop-1", "user_button", &current, 222); err != nil {
		t.Fatalf("idempotent completed retry failed: %v", err)
	}
	if core.stopCount() != 1 {
		t.Fatalf("stop count=%d, want 1", core.stopCount())
	}
	if len(core.stopTimes) != 1 || core.stopTimes[0] != 222 {
		t.Fatalf("detection timestamp was not forwarded: %v", core.stopTimes)
	}
	if stats := session.Stats(); stats.State != SessionActive {
		t.Fatalf("state=%s, want active", stats.State)
	}
	if err := session.AcceptUplink(runtimeFrame(0)); err != nil {
		t.Fatalf("uplink after cancel: %v", err)
	}
}

func TestVoiceCoreMediaRuntimeReplaysOlderStopAfterLaterCancellation(t *testing.T) {
	session := runtimeSession(t, 2)
	firstCurrent := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(firstCurrent); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.setCurrentFence(firstCurrent)
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	firstCancelled, err := runtime.CancelGeneration("stop-1", "first", &firstCurrent, 333)
	if err != nil {
		t.Fatal(err)
	}
	secondCurrent := firstCancelled
	secondCurrent.GenerationID++
	if err := session.AdvanceGeneration(secondCurrent); err != nil {
		t.Fatal(err)
	}
	core.setCurrentFence(secondCurrent)
	if _, err := runtime.CancelGeneration("stop-2", "second", &secondCurrent, 444); err != nil {
		t.Fatal(err)
	}
	replayed, err := runtime.CancelGeneration("stop-1", "changed-retry-reason", &firstCurrent, 333)
	if err != nil {
		t.Fatalf("out-of-order retry failed: %v", err)
	}
	if !replayed.Equal(firstCancelled) {
		t.Fatalf("replayed=%+v, want %+v", replayed, firstCancelled)
	}
	if core.stopCount() != 2 {
		t.Fatalf("stop count=%d, want 2", core.stopCount())
	}
}

func TestVoiceCoreMediaRuntimeKeywordUsesLocalGenerationGate(t *testing.T) {
	session := runtimeSession(t, 2)
	current := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(current); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.setCurrentFence(current)
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := runtime.SendKeyword("提示词", 0.9, 10, 20, false, current); err != nil {
		t.Fatal(err)
	}
	if fence, active := session.GenerationSnapshot(); !active || !fence.Equal(current) {
		t.Fatalf("ordinary keyword changed generation: fence=%+v active=%v", fence, active)
	}
	if _, err := runtime.CancelGeneration("stop-1", "user_button", &current, 555); err != nil {
		t.Fatal(err)
	}
	if err := runtime.SendKeyword("提示词", 0.9, 20, 30, false, current); err == nil {
		t.Fatal("cancelled generation accepted delayed keyword")
	}
	if core.keywordCount() != 1 {
		t.Fatalf("keyword count=%d, want 1", core.keywordCount())
	}
}

func TestVoiceCoreMediaRuntimeHardStopKeywordClosesGateBeforeCoreSend(t *testing.T) {
	session := runtimeSession(t, 2)
	current := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(current); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.setCurrentFence(current)
	gateChecked := false
	core.setKeywordBehavior(func() {
		fence, active := session.GenerationSnapshot()
		if active || fence.GenerationID != 2 {
			t.Fatalf("gate was not closed before sender: fence=%+v active=%v", fence, active)
		}
		gateChecked = true
	}, nil)
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := runtime.SendKeyword("停一下", 0.9, 10, 20, true, current); err != nil {
		t.Fatal(err)
	}
	if !gateChecked {
		t.Fatal("sender did not observe the closed gate")
	}
	old := runtimeFrame(0)
	old.TurnID = current.TurnID
	old.GenerationID = current.GenerationID
	if err := session.AcceptDownlink(old); !errors.Is(err, ErrStaleDownlinkGeneration) {
		t.Fatalf("old audio error=%v, want stale generation", err)
	}
	cancelled := Fence{SessionID: "s", TurnID: 1, GenerationID: 2}
	if err := runtime.handleEvent(&mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: runtimeIdentity(), TurnId: 1, GenerationId: 2,
			Sequence: 2, Action: mediav1.GenerationAction_GENERATION_ACTION_CANCEL,
		},
	}}); err != nil {
		t.Fatal(err)
	}
	if fence, active := session.GenerationSnapshot(); active || !fence.Equal(cancelled) {
		t.Fatalf("Core cancel was not idempotent: fence=%+v active=%v", fence, active)
	}
	next := Fence{SessionID: "s", TurnID: 2, GenerationID: 3}
	if err := runtime.handleEvent(&mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: runtimeIdentity(), TurnId: 2, GenerationId: 3,
			Sequence: 3, Action: mediav1.GenerationAction_GENERATION_ACTION_START,
		},
	}}); err != nil {
		t.Fatal(err)
	}
	fresh := runtimeFrame(0)
	fresh.TurnID = next.TurnID
	fresh.GenerationID = next.GenerationID
	if err := session.AcceptDownlink(fresh); err != nil {
		t.Fatalf("new generation did not continue: %v", err)
	}
}

func TestVoiceCoreMediaRuntimeHardStopKeywordFailureRetriesWithoutAdvancingAgain(t *testing.T) {
	session := runtimeSession(t, 2)
	current := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(current); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.setCurrentFence(current)
	sendErr := errors.New("keyword transport failed")
	core.setKeywordBehavior(nil, sendErr)
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := runtime.SendKeyword("停一下", 0.9, 10, 20, true, current); !errors.Is(err, sendErr) {
		t.Fatalf("first send error=%v, want %v", err, sendErr)
	}
	if fence, active := session.GenerationSnapshot(); active || fence.GenerationID != 2 {
		t.Fatalf("failed send did not stay fail-closed: fence=%+v active=%v", fence, active)
	}
	core.setKeywordBehavior(nil, nil)
	if err := runtime.SendKeyword("停一下", 0.9, 10, 20, true, current); err != nil {
		t.Fatalf("keyword retry failed: %v", err)
	}
	if err := runtime.SendKeyword("停一下", 0.9, 10, 20, true, current); err != nil {
		t.Fatalf("completed keyword retry failed: %v", err)
	}
	if core.keywordCount() != 1 {
		t.Fatalf("keyword count=%d, want 1", core.keywordCount())
	}
	if fence, _ := session.GenerationSnapshot(); fence.GenerationID != 2 {
		t.Fatalf("keyword retry advanced generation again: %+v", fence)
	}
}

func TestVoiceCoreMediaRuntimeDropsLateCancelledAudioAndAcceptsNextGeneration(t *testing.T) {
	session := runtimeSession(t, 2)
	current := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(current); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.setCurrentFence(current)
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := runtime.CancelGeneration("stop-1", "user_button", &current, 0); err != nil {
		t.Fatal(err)
	}
	runtime.Start()
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Audio{
		Audio: &mediav1.AssistantAudioFrame{
			Identity: runtimeIdentity(), TurnId: 1, GenerationId: 1,
			Sequence: 0, FrameSamples: 1, PcmS16Le: []byte{0, 1},
		},
	}}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: runtimeIdentity(), TurnId: 2, GenerationId: 0,
			Sequence: 2, Action: mediav1.GenerationAction_GENERATION_ACTION_START,
		},
	}}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Audio{
		Audio: &mediav1.AssistantAudioFrame{
			Identity: runtimeIdentity(), TurnId: 2, GenerationId: 0,
			Sequence: 0, FrameSamples: 1, PcmS16Le: []byte{0, 1},
		},
	}}
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if frame, ok := session.PopDownlink(); ok {
			if frame.TurnID != 2 {
				t.Fatalf("late cancelled audio escaped: %+v", frame)
			}
			_ = runtime.Close()
			if err := runtime.Wait(); err != nil {
				t.Fatal(err)
			}
			return
		}
		time.Sleep(time.Millisecond)
	}
	_ = runtime.Close()
	t.Fatal("next generation audio was not accepted")
}

func TestVoiceCoreMediaRuntimeCancelEventClosesLocalGenerationGate(t *testing.T) {
	session := runtimeSession(t, 2)
	core := newFakeCoreStream()
	callback := make(chan struct{}, 1)
	errorsSeen := make(chan error, 1)
	runtime, err := NewVoiceCoreMediaRuntime(
		context.Background(), session, core,
		func(*mediav1.CoreToMedia) { callback <- struct{}{} },
		func(err error) { errorsSeen <- err },
	)
	if err != nil {
		t.Fatal(err)
	}
	runtime.Start()
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: runtimeIdentity(), TurnId: 0, GenerationId: 1,
			Sequence: 1, Action: mediav1.GenerationAction_GENERATION_ACTION_CANCEL,
		},
	}}
	select {
	case <-callback:
	case err := <-errorsSeen:
		t.Fatalf("cancel generation failed: %v", err)
	case <-time.After(time.Second):
		t.Fatal("cancel generation was not processed")
	}
	frame := runtimeFrame(0)
	frame.TurnID = 0
	frame.GenerationID = 1
	if err := session.AcceptDownlink(frame); err == nil {
		t.Fatal("cancelled generation accepted downlink")
	}
	_ = runtime.Close()
	if err := runtime.Wait(); err != nil {
		t.Fatal(err)
	}
}
