package mediaedge

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

func TestVoiceCoreAssistantStateFeedsPythonVsGoShadowComparison(t *testing.T) {
	session := runtimeSession(t, 4)
	defer session.Stop()
	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1, ToolEpoch: 7}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	runtime := &VoiceCoreMediaRuntime{session: session}
	payload, err := json.Marshal(map[string]any{
		"payload": map[string]any{
			"phase": "speaking", "state": "speaking", "tool_epoch": 7,
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := runtime.handleEvent(&mediav1.CoreToMedia{
		Event: &mediav1.CoreToMedia_Client{Client: &mediav1.ClientEvent{
			Identity: runtimeIdentity(), Type: "assistant_state", JsonPayload: payload,
			TurnId: 1, GenerationId: 1, ToolEpoch: 7,
		}},
	}); err != nil {
		t.Fatal(err)
	}
	snapshot := waitForActorEvents(t, session.actor, 2)
	if len(snapshot.RecentComparisons) != 2 {
		t.Fatalf("python authority was not compared: %+v", snapshot)
	}
	comparison := snapshot.RecentComparisons[len(snapshot.RecentComparisons)-1]
	if comparison.Mismatch || comparison.Scenario != "speaking" || comparison.ContractVersion != shadowContractVersion {
		t.Fatalf("python/go shadow comparison is not analyzable: %+v", comparison)
	}

	if err := runtime.handleEvent(&mediav1.CoreToMedia{
		Event: &mediav1.CoreToMedia_Client{Client: &mediav1.ClientEvent{
			Identity: runtimeIdentity(), Type: "assistant_state", JsonPayload: []byte("{"),
		}},
	}); err != nil {
		t.Fatalf("shadow parse failure affected the user path: %v", err)
	}
}

func TestPythonInteractionStateMapsEveryPublishedPhase(t *testing.T) {
	phases := []string{
		"connecting", "listening", "user_speaking", "backchannel", "thinking_silent",
		"speaking", "interrupted", "tool_waiting", "recovering", "closed",
	}
	for _, phase := range phases {
		if _, _, ok := pythonInteractionState(phase); !ok {
			t.Fatalf("published phase %q has no Go shadow mapping", phase)
		}
	}
	if floor, decision, ok := pythonInteractionState("connecting"); !ok ||
		floor != ShadowFloorSilence || decision != "pause_output" {
		t.Fatalf("connecting must be a typed pause decision: floor=%q decision=%q ok=%v", floor, decision, ok)
	}
}

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
	authority    mediav1.InteractionAuthority
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

func (f *fakeCoreStream) InteractionAuthority() mediav1.InteractionAuthority {
	return f.authority
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

func TestVoiceCoreMediaRuntimeMirrorsAuthoritativeSpeechTimelineInGoShadow(t *testing.T) {
	session := runtimeSession(t, 4)
	defer session.Stop()
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	runtime.Start()
	digest := make([]byte, 32)
	segment := &mediav1.ShadowSpeechSegment{
		SegmentId: "sentence-1", TaskEpoch: 2, Revision: 3,
		CaptureStartSample: 10, CaptureEndSample: 20, TextSha256: digest, Final: true,
	}
	core.events <- speechShadowObservation(0, true, segment, 2, []*mediav1.ShadowSpeechSegment{segment})

	snapshot := waitForActorEvents(t, session.actor, 1)
	if len(snapshot.SpeechTimeline.Segments) != 1 ||
		snapshot.SpeechTimeline.Segments[0].TextSHA256 == "" ||
		snapshot.SpeechTimeline.LatestTaskEpoch != 2 {
		t.Fatalf("production observation did not reach the shadow timeline: %+v", snapshot)
	}
	comparison := snapshot.RecentComparisons[len(snapshot.RecentComparisons)-1]
	if comparison.Mismatch || comparison.ContractVersion != shadowA6AContractVersion {
		t.Fatalf("authoritative timeline did not match the Go candidate: %+v", comparison)
	}
	_ = runtime.Close()
	if err := runtime.Wait(); err != nil {
		t.Fatal(err)
	}
}

func TestVoiceCoreMediaRuntimeTaskStartFencesOldResultBeforeFirstNewResult(t *testing.T) {
	session := runtimeSession(t, 4)
	defer session.Stop()
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	runtime.Start()
	digest := make([]byte, 32)
	first := &mediav1.ShadowSpeechSegment{
		SegmentId: "sentence-1", TaskEpoch: 1, Revision: 1,
		CaptureEndSample: 20, TextSha256: digest, Final: true,
	}
	core.events <- speechShadowObservation(0, true, first, 1, []*mediav1.ShadowSpeechSegment{first})
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_ShadowObservation{
		ShadowObservation: &mediav1.ShadowObservation{
			Identity: runtimeIdentity(), Sequence: 2, ShadowSequence: 1,
			ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
			Kind:                  mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_SPEECH_TASK_STARTED,
			Input:                 &mediav1.ShadowObservation_SpeechTaskStarted{SpeechTaskStarted: &mediav1.ShadowSpeechTaskStarted{TaskEpoch: 2}},
			AuthoritativeAccepted: true,
			AuthoritativeTimeline: &mediav1.ShadowSpeechTimelineState{
				LatestTaskEpoch: 2, Segments: []*mediav1.ShadowSpeechSegment{first},
			},
		},
	}}
	late := &mediav1.ShadowSpeechSegment{
		SegmentId: "sentence-1", TaskEpoch: 1, Revision: 2,
		CaptureEndSample: 20, TextSha256: digest, Final: true,
	}
	core.events <- speechShadowObservation(2, false, late, 2, []*mediav1.ShadowSpeechSegment{first})

	snapshot := waitForActorEvents(t, session.actor, 3)
	if snapshot.SpeechTimeline.LatestTaskEpoch != 2 ||
		len(snapshot.SpeechTimeline.Segments) != 1 ||
		snapshot.SpeechTimeline.Segments[0].Revision != 1 {
		t.Fatalf("task-start fence admitted an old result: %+v", snapshot.SpeechTimeline)
	}
	for _, comparison := range snapshot.RecentComparisons {
		if comparison.Mismatch {
			t.Fatalf("Python/Go task-start decision diverged: %+v", comparison)
		}
	}
	_ = runtime.Close()
	if err := runtime.Wait(); err != nil {
		t.Fatal(err)
	}
}

func TestVoiceCoreMediaRuntimeResyncsLossyShadowGapWithoutAlgorithmMismatch(t *testing.T) {
	session := runtimeSession(t, 4)
	defer session.Stop()
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	var forwarded atomic.Int64
	runtime, err := NewVoiceCoreMediaRuntime(
		context.Background(), session, core,
		func(*mediav1.CoreToMedia) { forwarded.Add(1) }, nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	runtime.Start()

	digest := make([]byte, 32)
	first := &mediav1.ShadowSpeechSegment{
		SegmentId: "first", TaskEpoch: 1, Revision: 1,
		CaptureStartSample: 0, CaptureEndSample: 20, TextSha256: digest, Final: true,
	}
	missing := &mediav1.ShadowSpeechSegment{
		SegmentId: "missing", TaskEpoch: 1, Revision: 1,
		CaptureStartSample: 20, CaptureEndSample: 40, TextSha256: digest, Final: true,
	}
	latest := &mediav1.ShadowSpeechSegment{
		SegmentId: "latest", TaskEpoch: 1, Revision: 1,
		CaptureStartSample: 40, CaptureEndSample: 60, TextSha256: digest, Final: true,
	}
	core.events <- speechShadowObservation(0, true, first, 1, []*mediav1.ShadowSpeechSegment{first})
	// shadow_sequence=1 was evicted by the lossy shadow queue. The next
	// observation carries the complete Python after-state and must resync the
	// candidate instead of recording a permanent algorithm mismatch.
	core.events <- speechShadowObservation(2, true, latest, 1, []*mediav1.ShadowSpeechSegment{first, missing, latest})
	// A duplicate after the gap remains a lossy drop.
	core.events <- speechShadowObservation(2, true, latest, 1, []*mediav1.ShadowSpeechSegment{first, missing, latest})

	snapshot := waitForActorEvents(t, session.actor, 2)
	if len(snapshot.SpeechTimeline.Segments) != 3 || snapshot.ShadowMismatchTotal != 0 {
		t.Fatalf("lossy shadow gap was not safely resynchronized: %+v", snapshot)
	}
	comparison := snapshot.RecentComparisons[len(snapshot.RecentComparisons)-1]
	if comparison.Mismatch || comparison.Scenario != "shadow_transport_discontinuity" {
		t.Fatalf("shadow gap was not explicitly classified: %+v", comparison)
	}

	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: runtimeIdentity(), TurnId: 1, GenerationId: 1,
			Action: mediav1.GenerationAction_GENERATION_ACTION_START,
		},
	}}
	waitForActorEvents(t, session.actor, 3)
	actual, active := session.GenerationSnapshot()
	if !active || !actual.Equal(fence) || forwarded.Load() != 1 {
		t.Fatalf("shadow resync affected the authoritative media path: fence=%+v active=%v forwarded=%d", actual, active, forwarded.Load())
	}
	_ = runtime.Close()
	if err := runtime.Wait(); err != nil {
		t.Fatal(err)
	}
}

func TestVoiceCoreMediaRuntimeRetriesShadowResyncAfterActorMailboxRejectsIt(t *testing.T) {
	session := runtimeSession(t, 4)
	session.actor.Close()
	session.actor = newLiveSessionActor("s", 1, 2, 1, time.Second, 100*time.Millisecond)
	defer session.Stop()
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}

	digest := make([]byte, 32)
	first := &mediav1.ShadowSpeechSegment{
		SegmentId: "first", TaskEpoch: 1, Revision: 1,
		CaptureStartSample: 0, CaptureEndSample: 20, TextSha256: digest, Final: true,
	}
	runtime.handleShadowObservation(
		speechShadowObservation(0, true, first, 1, []*mediav1.ShadowSpeechSegment{first}).GetShadowObservation(),
	)
	waitForActorEvents(t, session.actor, 1)

	if err := session.actor.TrySubmit(LiveSessionEvent{Kind: LiveEventVADStart, StreamEpoch: 1}); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	for session.actor.Snapshot().MailboxDepth != 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if err := session.actor.TrySubmit(LiveSessionEvent{Kind: LiveEventVADEnd, StreamEpoch: 1}); err != nil {
		t.Fatal(err)
	}

	latest := &mediav1.ShadowSpeechSegment{
		SegmentId: "latest", TaskEpoch: 1, Revision: 1,
		CaptureStartSample: 20, CaptureEndSample: 40, TextSha256: digest, Final: true,
	}
	runtime.handleShadowObservation(
		speechShadowObservation(2, true, latest, 1, []*mediav1.ShadowSpeechSegment{first, latest}).GetShadowObservation(),
	)
	if runtime.lastShadowSequence != 0 || !runtime.resyncShadowSpeech {
		t.Fatalf(
			"rejected resync was marked delivered: sequence=%d resync=%v",
			runtime.lastShadowSequence,
			runtime.resyncShadowSpeech,
		)
	}
}

func TestVoiceCoreMediaRuntimeResyncsShadowDomainsIndependentlyAfterLossyGap(t *testing.T) {
	session := runtimeSession(t, 4)
	defer session.Stop()
	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1, ToolEpoch: 2}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	runtime.Start()

	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_ShadowObservation{
		ShadowObservation: &mediav1.ShadowObservation{
			Identity: runtimeIdentity(), Sequence: 1, ShadowSequence: 0,
			ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
			Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_CONTEXT_ACTIVATED,
			Input: &mediav1.ShadowObservation_ContextActivated{
				ContextActivated: &mediav1.ShadowContextActivated{ContextVersion: 7},
			},
			AuthoritativeAccepted:       true,
			AuthoritativeContextVersion: 7,
		},
	}}
	// shadow_sequence=1 was lost. The output observation can only restore the
	// Output domain; Speech must remain pending until its own complete state.
	core.events <- outputShadowObservation(2, true, fence, 7)

	digest := make([]byte, 32)
	segment := &mediav1.ShadowSpeechSegment{
		SegmentId: "latest", TaskEpoch: 1, Revision: 1,
		CaptureStartSample: 0, CaptureEndSample: 20, TextSha256: digest, Final: true,
	}
	core.events <- speechShadowObservation(3, true, segment, 1, []*mediav1.ShadowSpeechSegment{segment})
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_ShadowObservation{
		ShadowObservation: &mediav1.ShadowObservation{
			Identity: runtimeIdentity(), Sequence: 4, ShadowSequence: 4,
			ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
			Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_FLOOR_DECISION,
			Input: &mediav1.ShadowObservation_FloorDecision{
				FloorDecision: &mediav1.ShadowFloorDecision{
					FloorState: mediav1.FloorState_FLOOR_STATE_ASSISTANT_HOLDS_FLOOR,
					EffectKind: mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_ENQUEUE_OUTPUT_INTENT,
					TurnId:     1, GenerationId: 1, ToolEpoch: 2,
				},
			},
			AuthoritativeAccepted: true, AuthoritativeReason: "speaking",
		},
	}}

	snapshot := waitForActorEvents(t, session.actor, 5)
	if snapshot.ShadowMismatchTotal != 0 || len(snapshot.SpeechTimeline.Segments) != 1 {
		t.Fatalf("shadow domains did not recover independently: %+v", snapshot)
	}
	comparisons := snapshot.RecentComparisons
	if len(comparisons) < 4 ||
		comparisons[len(comparisons)-2].Scenario != "shadow_transport_discontinuity" ||
		comparisons[len(comparisons)-1].Scenario != "shadow_transport_discontinuity" ||
		comparisons[len(comparisons)-1].Kind != LiveEventAuthoritySnapshot {
		t.Fatalf("each recovered domain must classify the transport gap: %+v", comparisons)
	}
	_ = runtime.Close()
	if err := runtime.Wait(); err != nil {
		t.Fatal(err)
	}
}

func TestVoiceCoreMediaRuntimeObservesContextAndOutputIntentWithoutForwardingEffect(t *testing.T) {
	session := runtimeSession(t, 4)
	defer session.Stop()
	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1, ToolEpoch: 2}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	forwarded := 0
	runtime, err := NewVoiceCoreMediaRuntime(
		context.Background(), session, core,
		func(*mediav1.CoreToMedia) { forwarded++ }, nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	runtime.Start()
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_ShadowObservation{
		ShadowObservation: &mediav1.ShadowObservation{
			Identity: runtimeIdentity(), Sequence: 1, ShadowSequence: 0,
			ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
			Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_CONTEXT_ACTIVATED,
			Input: &mediav1.ShadowObservation_ContextActivated{
				ContextActivated: &mediav1.ShadowContextActivated{ContextVersion: 7},
			},
			AuthoritativeAccepted:       true,
			AuthoritativeContextVersion: 7,
		},
	}}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_ShadowObservation{
		ShadowObservation: &mediav1.ShadowObservation{
			Identity: runtimeIdentity(), Sequence: 2, ShadowSequence: 1,
			ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
			Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_OUTPUT_INTENT,
			Input: &mediav1.ShadowObservation_OutputIntent{OutputIntent: &mediav1.ShadowOutputIntent{
				IntentId: "deep-1", TurnId: 1, GenerationId: 1, ToolEpoch: 2,
				Kind:     mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_DEEP_RESULT,
				Priority: 50, CreatedAtMs: 1_000, ExpiresAtMs: 2_000,
				FloorRequirement: mediav1.FloorRequirement_FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
				ContextVersion:   7,
			}},
			ObservedAtMs: 1_500, AuthoritativeAccepted: true,
			AuthoritativeContextVersion: 7,
		},
	}}

	snapshot := waitForActorEvents(t, session.actor, 3)
	if snapshot.OutputArbiter.ContextVersion != 7 || snapshot.OutputArbiter.Candidate != nil {
		t.Fatalf("consumed output candidate leaked into live state: %+v", snapshot.OutputArbiter)
	}
	comparison := snapshot.RecentComparisons[len(snapshot.RecentComparisons)-1]
	if comparison.Mismatch || comparison.Candidate.OutputArbiter.Candidate == nil ||
		comparison.Candidate.OutputArbiter.Candidate.IntentID != "deep-1" {
		t.Fatalf("output admission did not reach atomic parity: %+v", comparison)
	}
	if forwarded != 0 {
		t.Fatalf("internal shadow observations were forwarded to the client: %d", forwarded)
	}
	_ = runtime.Close()
	if err := runtime.Wait(); err != nil {
		t.Fatal(err)
	}
}

func TestVoiceCoreMediaRuntimeAppliesAuthoritativeOutputAfterState(t *testing.T) {
	session := runtimeSession(t, 8)
	defer session.Stop()
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1, ToolEpoch: 2}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	contextObservation := &mediav1.ShadowObservation{
		Identity: runtimeIdentity(), Sequence: 1, ShadowSequence: 0,
		ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
		Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_CONTEXT_ACTIVATED,
		Input: &mediav1.ShadowObservation_ContextActivated{
			ContextActivated: &mediav1.ShadowContextActivated{ContextVersion: 7},
		},
		AuthoritativeAccepted: true, AuthoritativeContextVersion: 7,
	}
	runtime.handleShadowObservation(contextObservation)
	waitForActorEvents(t, session.actor, 2)
	createdAt := uint64(time.Now().UnixMilli())
	expiresAt := createdAt + 60_000

	outputObservation := func(sequence uint64, accepted bool, intentID string) *mediav1.ShadowObservation {
		inputCreatedAt := createdAt
		authoritativeReason := "accepted"
		if !accepted {
			inputCreatedAt = createdAt + 1
			authoritativeReason = "invalid_created_at"
		}
		winner := &mediav1.ShadowOutputIntent{
			IntentId: "deep-winner", TurnId: 1, GenerationId: 1, ToolEpoch: 2,
			Kind:     mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_DEEP_RESULT,
			Priority: 50, CreatedAtMs: createdAt, ExpiresAtMs: expiresAt,
			FloorRequirement: mediav1.FloorRequirement_FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
			ContextVersion:   7,
		}
		return &mediav1.ShadowObservation{
			Identity: runtimeIdentity(), Sequence: sequence + 1, ShadowSequence: sequence,
			ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
			Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_OUTPUT_INTENT,
			Input: &mediav1.ShadowObservation_OutputIntent{OutputIntent: &mediav1.ShadowOutputIntent{
				IntentId: intentID, TurnId: 1, GenerationId: 1, ToolEpoch: 2,
				Kind:     mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_DEEP_RESULT,
				Priority: 50, CreatedAtMs: inputCreatedAt, ExpiresAtMs: expiresAt,
				FloorRequirement: mediav1.FloorRequirement_FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
				ContextVersion:   7,
			}},
			ObservedAtMs: createdAt, AuthoritativeAccepted: accepted,
			AuthoritativeReason: authoritativeReason, AuthoritativeContextVersion: 7,
			AuthoritativeOutputArbiter: &mediav1.ShadowOutputArbiterState{
				ContextVersion: 7, Candidate: winner,
				ActiveCandidates: []*mediav1.ShadowOutputIntent{winner}, ActiveCandidatesComplete: true,
			},
		}
	}

	runtime.handleShadowObservation(outputObservation(1, true, "deep-winner"))
	accepted := waitForActorEvents(t, session.actor, 3)
	if accepted.OutputArbiter.Candidate == nil || accepted.OutputArbiter.Candidate.IntentID != "deep-winner" {
		t.Fatalf("authoritative winner was consumed: %+v", accepted.OutputArbiter)
	}
	if comparison := accepted.RecentComparisons[len(accepted.RecentComparisons)-1]; comparison.Mismatch {
		t.Fatalf("accepted output after-state diverged: %+v", comparison)
	}

	runtime.handleShadowObservation(outputObservation(2, false, "rejected-input"))
	rejected := waitForActorEvents(t, session.actor, 4)
	if rejected.OutputArbiter.Candidate == nil || rejected.OutputArbiter.Candidate.IntentID != "deep-winner" {
		t.Fatalf("rejected input replaced the authoritative winner: %+v", rejected.OutputArbiter)
	}
	if comparison := rejected.RecentComparisons[len(rejected.RecentComparisons)-1]; comparison.Mismatch {
		t.Fatalf("rejected output after-state diverged: %+v", comparison)
	}

	consumedObservation := outputObservation(3, false, "deep-winner")
	consumedObservation.GetOutputIntent().CreatedAtMs = createdAt
	consumedObservation.AuthoritativeConsumed = true
	consumedObservation.AuthoritativeReason = "completed"
	consumedObservation.AuthoritativeOutputArbiter = &mediav1.ShadowOutputArbiterState{
		ContextVersion: 7, ActiveCandidatesComplete: true,
	}
	runtime.handleShadowObservation(consumedObservation)
	consumed := waitForActorEvents(t, session.actor, 5)
	if consumed.OutputArbiter.Candidate != nil || len(consumed.OutputArbiter.Candidates) != 0 {
		t.Fatalf("consumed output candidate remained active: %+v", consumed.OutputArbiter)
	}
	comparison := consumed.RecentComparisons[len(consumed.RecentComparisons)-1]
	if comparison.Mismatch || comparison.Scenario != "output_intent_consumed" ||
		comparison.CandidateReason != "consume_output_intent" {
		t.Fatalf("consumed output after-state diverged: %+v", comparison)
	}
}

func TestVoiceCoreMediaRuntimeRejectsInvalidConsumedOutputObservations(t *testing.T) {
	session := runtimeSession(t, 4)
	defer session.Stop()
	if err := session.AdvanceGeneration(Fence{SessionID: "s", TurnID: 1, GenerationID: 1}); err != nil {
		t.Fatal(err)
	}
	baseline := waitForActorEvents(t, session.actor, 1).ProcessedEvents
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	base := &mediav1.ShadowObservation{
		Identity: runtimeIdentity(), Sequence: 1, ShadowSequence: 0,
		ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
		Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_OUTPUT_INTENT,
		Input: &mediav1.ShadowObservation_OutputIntent{OutputIntent: &mediav1.ShadowOutputIntent{
			IntentId: "conversation", TurnId: 1, GenerationId: 1,
			Kind:     mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_CONVERSATION_REPLY,
			Priority: 50, CreatedAtMs: 1_000, ExpiresAtMs: 2_000,
			FloorRequirement: mediav1.FloorRequirement_FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
			ContextVersion:   7,
		}},
		ObservedAtMs: 1_500, AuthoritativeConsumed: true,
		AuthoritativeReason: "completed", AuthoritativeContextVersion: 7,
		AuthoritativeOutputArbiter: &mediav1.ShadowOutputArbiterState{
			ContextVersion: 7, ActiveCandidatesComplete: true,
		},
	}
	tests := map[string]func(*mediav1.ShadowObservation){
		"accepted and consumed": func(observation *mediav1.ShadowObservation) {
			observation.AuthoritativeAccepted = true
		},
		"missing after-state": func(observation *mediav1.ShadowObservation) {
			observation.AuthoritativeOutputArbiter = nil
		},
		"incomplete after-state": func(observation *mediav1.ShadowObservation) {
			observation.AuthoritativeOutputArbiter.ActiveCandidatesComplete = false
		},
		"invalid input": func(observation *mediav1.ShadowObservation) {
			observation.GetOutputIntent().Kind = mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_UNSPECIFIED
		},
		"missing observation time": func(observation *mediav1.ShadowObservation) {
			observation.ObservedAtMs = 0
		},
	}
	for name, mutate := range tests {
		t.Run(name, func(t *testing.T) {
			observation := proto.Clone(base).(*mediav1.ShadowObservation)
			mutate(observation)
			runtime.handleShadowObservation(observation)
			if runtime.hasShadowSequence {
				t.Fatal("invalid consumed observation advanced the shadow sequence")
			}
		})
	}
	time.Sleep(10 * time.Millisecond)
	if processed := session.actor.Snapshot().ProcessedEvents; processed != baseline {
		t.Fatalf("invalid consumed observations reached the actor: processed=%d, want %d", processed, baseline)
	}
}

func TestVoiceCoreMediaRuntimeResyncRestoresCompleteSameDomainFallback(t *testing.T) {
	session := runtimeSession(t, 8)
	defer session.Stop()
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1, ToolEpoch: 2}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	runtime.handleShadowObservation(&mediav1.ShadowObservation{
		Identity: runtimeIdentity(), Sequence: 1, ShadowSequence: 0,
		ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
		Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_CONTEXT_ACTIVATED,
		Input: &mediav1.ShadowObservation_ContextActivated{
			ContextActivated: &mediav1.ShadowContextActivated{ContextVersion: 7},
		},
		AuthoritativeAccepted: true, AuthoritativeContextVersion: 7,
	})
	waitForActorEvents(t, session.actor, 2)
	now := uint64(time.Now().UnixMilli())
	fallback := &mediav1.ShadowOutputIntent{
		IntentId: "fallback", TurnId: 1, GenerationId: 1, ToolEpoch: 2,
		Kind:     mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_DEEP_RESULT,
		Priority: 1, CreatedAtMs: now - 1_000, ExpiresAtMs: now + 60_000,
		FloorRequirement: mediav1.FloorRequirement_FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
		ContextVersion:   7,
	}
	winner := proto.Clone(fallback).(*mediav1.ShadowOutputIntent)
	winner.IntentId = "winner"
	winner.Priority = 100
	winner.CreatedAtMs++
	winner.ExpiresAtMs = now + 50
	runtime.handleShadowObservation(&mediav1.ShadowObservation{
		Identity: runtimeIdentity(), Sequence: 3, ShadowSequence: 2,
		ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
		Kind:         mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_OUTPUT_INTENT,
		Input:        &mediav1.ShadowObservation_OutputIntent{OutputIntent: winner},
		ObservedAtMs: now, AuthoritativeAccepted: true, AuthoritativeReason: "accepted",
		AuthoritativeContextVersion: 7,
		AuthoritativeOutputArbiter: &mediav1.ShadowOutputArbiterState{
			ContextVersion: 7, Candidate: winner,
			ActiveCandidates: []*mediav1.ShadowOutputIntent{winner, fallback}, ActiveCandidatesComplete: true,
		},
	})
	snapshot := waitForActorEvents(t, session.actor, 3)
	if snapshot.OutputArbiter.Candidate == nil || snapshot.OutputArbiter.Candidate.IntentID != "winner" ||
		len(snapshot.OutputArbiter.Candidates) != 2 {
		t.Fatalf("complete output after-state was not restored: %+v", snapshot.OutputArbiter)
	}
	comparison := snapshot.RecentComparisons[len(snapshot.RecentComparisons)-1]
	if comparison.Mismatch || comparison.Scenario != "shadow_transport_discontinuity" {
		t.Fatalf("transport resync was counted as a mismatch: %+v", comparison)
	}
	time.Sleep(70 * time.Millisecond)
	snapshot = session.actor.Snapshot()
	if snapshot.OutputArbiter.Candidate == nil || snapshot.OutputArbiter.Candidate.IntentID != "fallback" {
		t.Fatalf("resynced same-domain fallback was lost: %+v", snapshot.OutputArbiter)
	}
}

func TestVoiceCoreMediaRuntimeOutputResyncWaitsForCompleteAfterState(t *testing.T) {
	session := runtimeSession(t, 4)
	defer session.Stop()
	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1, ToolEpoch: 2}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	runtime.handleShadowObservation(&mediav1.ShadowObservation{
		Identity: runtimeIdentity(), Sequence: 1, ShadowSequence: 0,
		ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
		Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_CONTEXT_ACTIVATED,
		Input: &mediav1.ShadowObservation_ContextActivated{
			ContextActivated: &mediav1.ShadowContextActivated{ContextVersion: 7},
		},
		AuthoritativeAccepted: true, AuthoritativeContextVersion: 7,
	})
	waitForActorEvents(t, session.actor, 2)

	incomplete := outputShadowObservation(2, true, fence, 7).GetShadowObservation()
	incomplete.AuthoritativeOutputArbiter.ActiveCandidatesComplete = false
	incomplete.AuthoritativeOutputArbiter.ActiveCandidates = nil
	runtime.handleShadowObservation(incomplete)
	time.Sleep(10 * time.Millisecond)
	if processed := session.actor.Snapshot().ProcessedEvents; processed != 2 ||
		runtime.lastShadowSequence != 0 || !runtime.resyncShadowOutput {
		t.Fatalf(
			"incomplete output after-state resolved the gap: processed=%d sequence=%d resync=%v",
			processed, runtime.lastShadowSequence, runtime.resyncShadowOutput,
		)
	}

	runtime.handleShadowObservation(outputShadowObservation(3, true, fence, 7).GetShadowObservation())
	snapshot := waitForActorEvents(t, session.actor, 3)
	comparison := snapshot.RecentComparisons[len(snapshot.RecentComparisons)-1]
	if comparison.Mismatch || comparison.Scenario != "shadow_transport_discontinuity" ||
		snapshot.OutputArbiter.Candidate == nil || snapshot.OutputArbiter.Candidate.IntentID != "deep-gap" ||
		runtime.resyncShadowOutput {
		t.Fatalf("complete output after-state did not resolve the gap: %+v", snapshot)
	}
}

func TestShadowOutputIntentProtoPreservesFastAcknowledgementPriorityDomain(t *testing.T) {
	fast, ok := shadowOutputIntentFromProto("s", &mediav1.ShadowOutputIntent{
		IntentId: "fast",
		Kind:     mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT,
	})
	if !ok {
		t.Fatal("fast acknowledgement was rejected")
	}
	fastRank, ok := shadowOutputDomainRank(&fast)
	if !ok || fastRank != 4 {
		t.Fatalf("fast acknowledgement rank=%d, ok=%v; want rank 4", fastRank, ok)
	}
	backchannel, ok := shadowOutputIntentFromProto("s", &mediav1.ShadowOutputIntent{
		IntentId: "backchannel",
		Kind:     mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_BACKCHANNEL,
	})
	if !ok {
		t.Fatal("backchannel was rejected")
	}
	backchannelRank, ok := shadowOutputDomainRank(&backchannel)
	if !ok || backchannelRank != 3 {
		t.Fatalf("backchannel rank=%d, ok=%v; want rank 3", backchannelRank, ok)
	}
	conversation, ok := shadowOutputIntentFromProto("s", &mediav1.ShadowOutputIntent{
		IntentId: "conversation",
		Kind:     mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_CONVERSATION_REPLY,
	})
	if !ok || conversation.Kind != ShadowOutputConversation {
		t.Fatalf("conversation reply was not mapped to conversation: %+v, ok=%v", conversation, ok)
	}
	conversationRank, ok := shadowOutputDomainRank(&conversation)
	if !ok || conversationRank != 3 {
		t.Fatalf("conversation reply rank=%d, ok=%v; want rank 3", conversationRank, ok)
	}
}

func TestShadowOutputArbiterProtoValidatesCompleteCandidateSet(t *testing.T) {
	winner := &mediav1.ShadowOutputIntent{
		IntentId: "winner", TurnId: 1, GenerationId: 2, ToolEpoch: 3,
		Kind:     mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_DEEP_RESULT,
		Priority: 100, CreatedAtMs: 1_000, ExpiresAtMs: 2_000,
		FloorRequirement: mediav1.FloorRequirement_FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
		ContextVersion:   7,
	}
	fallback := proto.Clone(winner).(*mediav1.ShadowOutputIntent)
	fallback.IntentId = "fallback"
	fallback.Priority = 1
	valid := &mediav1.ShadowOutputArbiterState{
		ContextVersion: 7, Candidate: winner,
		ActiveCandidates: []*mediav1.ShadowOutputIntent{winner, fallback}, ActiveCandidatesComplete: true,
	}
	state, ok := shadowOutputArbiterFromProto("s", valid)
	if !ok || !state.CandidatesComplete || len(state.Candidates) != 2 ||
		state.Candidate == nil || state.Candidate.IntentID != "winner" {
		t.Fatalf("valid complete candidate set was rejected: %+v", state)
	}
	if !shadowOutputArbiterActiveAt(state, 1_500) || shadowOutputArbiterActiveAt(state, 999) ||
		shadowOutputArbiterActiveAt(state, 2_000) {
		t.Fatal("complete candidate set ignored its observation-time validity")
	}

	noncanonical := &mediav1.ShadowOutputArbiterState{
		ContextVersion: 7, Candidate: winner,
		ActiveCandidates: []*mediav1.ShadowOutputIntent{fallback, winner}, ActiveCandidatesComplete: true,
	}
	if _, ok := shadowOutputArbiterFromProto("s", noncanonical); ok {
		t.Fatal("non-canonical candidate order was accepted")
	}
	legacyWithList := &mediav1.ShadowOutputArbiterState{
		ContextVersion: 7, Candidate: winner,
		ActiveCandidates: []*mediav1.ShadowOutputIntent{winner},
	}
	if _, ok := shadowOutputArbiterFromProto("s", legacyWithList); ok {
		t.Fatal("incomplete state with an active list was accepted")
	}
	overCapacity := &mediav1.ShadowOutputArbiterState{
		ContextVersion: 7, ActiveCandidatesComplete: true,
	}
	for priority := maxShadowOutputPerDomain; priority >= 0; priority-- {
		candidate := proto.Clone(winner).(*mediav1.ShadowOutputIntent)
		candidate.IntentId = fmt.Sprintf("candidate-%d", priority)
		candidate.Priority = uint32(priority)
		overCapacity.ActiveCandidates = append(overCapacity.ActiveCandidates, candidate)
	}
	overCapacity.Candidate = overCapacity.ActiveCandidates[0]
	if _, ok := shadowOutputArbiterFromProto("s", overCapacity); ok {
		t.Fatal("over-capacity candidate domain was accepted")
	}
	empty, ok := shadowOutputArbiterFromProto("s", &mediav1.ShadowOutputArbiterState{
		ContextVersion: 7, ActiveCandidatesComplete: true,
	})
	if !ok || !empty.CandidatesComplete || empty.Candidate != nil || len(empty.Candidates) != 0 {
		t.Fatalf("complete empty candidate set was rejected: %+v", empty)
	}
}

func TestVoiceCoreMediaRuntimeRecordsMalformedRejectedOutputAfterState(t *testing.T) {
	session := runtimeSession(t, 4)
	defer session.Stop()
	if err := session.AdvanceGeneration(Fence{SessionID: "s", TurnID: 1, GenerationID: 1}); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	runtime.handleShadowObservation(&mediav1.ShadowObservation{
		Identity: runtimeIdentity(), Sequence: 1, ShadowSequence: 2,
		ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
		Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_OUTPUT_INTENT,
		Input: &mediav1.ShadowObservation_OutputIntent{OutputIntent: &mediav1.ShadowOutputIntent{
			Kind: mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_UNSPECIFIED,
		}},
		AuthoritativeAccepted: false, AuthoritativeReason: "missing_intent_id",
		ObservedAtMs: 1, AuthoritativeContextVersion: 7,
		AuthoritativeOutputArbiter: &mediav1.ShadowOutputArbiterState{
			ContextVersion: 7, ActiveCandidatesComplete: true,
		},
	})
	snapshot := waitForActorEvents(t, session.actor, 2)
	if snapshot.OutputArbiter.ContextVersion != 7 || snapshot.OutputArbiter.Candidate != nil {
		t.Fatalf("malformed rejected output changed candidate state: %+v", snapshot.OutputArbiter)
	}
	comparison := snapshot.RecentComparisons[len(snapshot.RecentComparisons)-1]
	if comparison.Mismatch || comparison.AuthoritativeReason != "missing_intent_id" ||
		comparison.CandidateReason != "drop_invalid_output_intent" {
		t.Fatalf("malformed rejected output was not recorded safely: %+v", comparison)
	}
}

func TestVoiceCoreMediaRuntimeObservesTypedFloorDecisionWithoutExecutingEffect(t *testing.T) {
	session := runtimeSession(t, 4)
	defer session.Stop()
	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1, ToolEpoch: 2}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	forwarded := 0
	runtime, err := NewVoiceCoreMediaRuntime(
		context.Background(), session, core,
		func(*mediav1.CoreToMedia) { forwarded++ }, nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	runtime.Start()
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_ShadowObservation{
		ShadowObservation: &mediav1.ShadowObservation{
			Identity: runtimeIdentity(), Sequence: 1, ShadowSequence: 0,
			ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
			Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_FLOOR_DECISION,
			Input: &mediav1.ShadowObservation_FloorDecision{
				FloorDecision: &mediav1.ShadowFloorDecision{
					FloorState: mediav1.FloorState_FLOOR_STATE_ASSISTANT_HOLDS_FLOOR,
					EffectKind: mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_ENQUEUE_OUTPUT_INTENT,
					TurnId:     1, GenerationId: 1, ToolEpoch: 2,
				},
			},
			AuthoritativeAccepted: true, AuthoritativeReason: "speaking",
		},
	}}

	snapshot := waitForActorEvents(t, session.actor, 2)
	comparison := snapshot.RecentComparisons[len(snapshot.RecentComparisons)-1]
	if comparison.Mismatch || comparison.Scenario != "floor_decision" ||
		comparison.Candidate.Floor != ShadowFloorAssistant ||
		comparison.Candidate.LastDecision != "enqueue_output_intent" {
		t.Fatalf("typed floor parity was not recorded: %+v", comparison)
	}
	if forwarded != 0 {
		t.Fatalf("typed shadow evidence reached the client effect path: %d", forwarded)
	}
	_ = runtime.Close()
	if err := runtime.Wait(); err != nil {
		t.Fatal(err)
	}
}

func TestVoiceCoreMediaRuntimeRecordsRejectedOutputIntentReasons(t *testing.T) {
	session := runtimeSession(t, 4)
	defer session.Stop()
	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1, ToolEpoch: 2}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	runtime, err := NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	runtime.Start()
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_ShadowObservation{
		ShadowObservation: &mediav1.ShadowObservation{
			Identity: runtimeIdentity(), Sequence: 1, ShadowSequence: 0,
			ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
			Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_CONTEXT_ACTIVATED,
			Input: &mediav1.ShadowObservation_ContextActivated{
				ContextActivated: &mediav1.ShadowContextActivated{ContextVersion: 7},
			},
			AuthoritativeAccepted:       true,
			AuthoritativeReason:         "context_activated",
			AuthoritativeContextVersion: 7,
		},
	}}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_ShadowObservation{
		ShadowObservation: &mediav1.ShadowObservation{
			Identity: runtimeIdentity(), Sequence: 2, ShadowSequence: 1,
			ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
			Kind: mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_OUTPUT_INTENT,
			Input: &mediav1.ShadowObservation_OutputIntent{OutputIntent: &mediav1.ShadowOutputIntent{
				IntentId: "future-deep", TurnId: 1, GenerationId: 1, ToolEpoch: 2,
				Kind:     mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_DEEP_RESULT,
				Priority: 50, CreatedAtMs: 2_000, ExpiresAtMs: 3_000,
				FloorRequirement: mediav1.FloorRequirement_FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
				ContextVersion:   7,
			}},
			ObservedAtMs: 1_500, AuthoritativeAccepted: false,
			AuthoritativeReason: "invalid_created_at", AuthoritativeContextVersion: 7,
		},
	}}

	snapshot := waitForActorEvents(t, session.actor, 3)
	comparison := snapshot.RecentComparisons[len(snapshot.RecentComparisons)-1]
	if comparison.Mismatch || comparison.Scenario != "output_intent_rejected" ||
		comparison.AuthoritativeReason != "invalid_created_at" ||
		comparison.CandidateReason != "drop_invalid_output_intent" {
		t.Fatalf("rejected output intent parity was not analyzable: %+v", comparison)
	}
	_ = runtime.Close()
	if err := runtime.Wait(); err != nil {
		t.Fatal(err)
	}
}

func speechShadowObservation(
	shadowSequence uint64,
	accepted bool,
	segment *mediav1.ShadowSpeechSegment,
	latestTaskEpoch uint64,
	authoritative []*mediav1.ShadowSpeechSegment,
) *mediav1.CoreToMedia {
	return &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_ShadowObservation{
		ShadowObservation: &mediav1.ShadowObservation{
			Identity: runtimeIdentity(), Sequence: shadowSequence + 1, ShadowSequence: shadowSequence,
			ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
			Kind:                  mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_SPEECH_SEGMENT,
			Input:                 &mediav1.ShadowObservation_SpeechSegment{SpeechSegment: segment},
			AuthoritativeAccepted: accepted,
			AuthoritativeTimeline: &mediav1.ShadowSpeechTimelineState{
				LatestTaskEpoch: latestTaskEpoch, Segments: authoritative,
			},
		},
	}}
}

func outputShadowObservation(
	shadowSequence uint64,
	accepted bool,
	fence Fence,
	contextVersion uint64,
) *mediav1.CoreToMedia {
	now := uint64(time.Now().UnixMilli())
	intent := &mediav1.ShadowOutputIntent{
		IntentId: "deep-gap", TurnId: fence.TurnID, GenerationId: fence.GenerationID,
		ToolEpoch: fence.ToolEpoch, Kind: mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_DEEP_RESULT,
		Priority: 50, CreatedAtMs: now - 1_000, ExpiresAtMs: now + 60_000,
		FloorRequirement: mediav1.FloorRequirement_FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK,
		ContextVersion:   contextVersion,
	}
	return &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_ShadowObservation{
		ShadowObservation: &mediav1.ShadowObservation{
			Identity: runtimeIdentity(), Sequence: shadowSequence + 1, ShadowSequence: shadowSequence,
			ContractVersion: shadowA6AContractVersion, CandidateOnly: true,
			Kind:         mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_OUTPUT_INTENT,
			Input:        &mediav1.ShadowObservation_OutputIntent{OutputIntent: intent},
			ObservedAtMs: now, AuthoritativeAccepted: accepted,
			AuthoritativeContextVersion: contextVersion,
			AuthoritativeOutputArbiter: &mediav1.ShadowOutputArbiterState{
				ContextVersion: contextVersion, Candidate: intent,
				ActiveCandidates: []*mediav1.ShadowOutputIntent{intent}, ActiveCandidatesComplete: true,
			},
		},
	}}
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
	// Delivery is observable before the receive loop reacquires the Session
	// mutex to retire the acknowledged frame. Wait for that post-send commit
	// instead of racing it under the race detector's slower scheduler.
	deadlineAt := time.Now().Add(2 * time.Second)
	for {
		session.mu.Lock()
		pending := session.downlink.Len()
		session.mu.Unlock()
		if pending == 0 {
			break
		}
		if time.Now().After(deadlineAt) {
			t.Fatal("sender-acknowledged frame remained queued")
		}
		time.Sleep(time.Millisecond)
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

func TestVoiceCoreMediaRuntimeExecutesOnlyCurrentPythonRealtimeEffects(t *testing.T) {
	session := runtimeSession(t, 2)
	defer session.Stop()
	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1, ToolEpoch: 2}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	forwarded := make(chan *mediav1.CoreToMedia, 4)
	runtime, err := NewVoiceCoreMediaRuntime(
		context.Background(), session, core,
		func(event *mediav1.CoreToMedia) { forwarded <- event }, nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	effect := func(kind mediav1.RealtimeEffectKind, value Fence) *mediav1.CoreToMedia {
		return &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_RealtimeEffect{
			RealtimeEffect: &mediav1.RealtimeEffect{
				EffectId: "effect", SessionId: value.SessionID, StreamEpoch: 1,
				Sequence: 1, EffectKind: kind, SourceEventId: "runtime-test",
				TurnId: value.TurnID, GenerationId: value.GenerationID, ToolEpoch: value.ToolEpoch,
				Payload: []byte(`{"reason":"test"}`), Identity: runtimeIdentity(),
			},
		}}
	}

	if err := runtime.handleEvent(effect(mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT, fence)); err != nil {
		t.Fatalf("current effect rejected: %v", err)
	}
	select {
	case event := <-forwarded:
		if event.GetRealtimeEffect() == nil {
			t.Fatalf("unexpected forwarded event: %v", event)
		}
	default:
		t.Fatal("current effect was not forwarded to the terminator")
	}

	candidate := effect(mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT, fence)
	candidate.GetRealtimeEffect().CandidateOnly = true
	if err := runtime.handleEvent(candidate); err != nil {
		t.Fatalf("candidate effect should be dropped, got %v", err)
	}
	select {
	case <-forwarded:
		t.Fatal("candidate effect reached the terminator")
	default:
	}

	stale := fence
	stale.GenerationID--
	if err := runtime.handleEvent(effect(mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT, stale)); err == nil {
		t.Fatal("stale effect fence was accepted")
	}

	cancelled := fence
	cancelled.GenerationID++
	if err := runtime.handleEvent(effect(mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION, cancelled)); err != nil {
		t.Fatalf("cancel effect rejected: %v", err)
	}
	if current, active := session.GenerationSnapshot(); active || !current.Equal(cancelled) {
		t.Fatalf("cancel effect did not close generation gate: current=%+v active=%v", current, active)
	}
	select {
	case event := <-forwarded:
		if event.GetRealtimeEffect() == nil ||
			event.GetRealtimeEffect().GetEffectKind() != mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION {
			t.Fatalf("cancel effect was not forwarded to the terminator: %v", event)
		}
	default:
		t.Fatal("cancel effect was not forwarded to the terminator")
	}
	old := runtimeFrame(0)
	old.TurnID, old.GenerationID, old.ToolEpoch = fence.TurnID, fence.GenerationID, fence.ToolEpoch
	if err := session.AcceptDownlink(old); !errors.Is(err, ErrStaleDownlinkGeneration) {
		t.Fatalf("cancelled generation accepted old PCM: %v", err)
	}

	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE
	if err := runtime.handleEvent(effect(mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_RESUME_OUTPUT, cancelled)); err != nil {
		t.Fatalf("unproven Go-authoritative effect should be dropped, got %v", err)
	}
	select {
	case <-forwarded:
		t.Fatal("unproven Go-authoritative effect reached the terminator")
	default:
	}
}

func TestVoiceCoreMediaRuntimeExecutesOnlyCurrentTypedFloorEffects(t *testing.T) {
	session := runtimeSession(t, 2)
	defer session.Stop()
	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1, ToolEpoch: 2}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	core := newFakeCoreStream()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	forwarded := make(chan *mediav1.CoreToMedia, 4)
	runtime, err := NewVoiceCoreMediaRuntime(
		context.Background(), session, core,
		func(event *mediav1.CoreToMedia) { forwarded <- event }, nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	effect := func(value Fence, state mediav1.FloorState, epoch uint64) *mediav1.CoreToMedia {
		return &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_FloorEffect{
			FloorEffect: &mediav1.FloorEffect{
				EffectId: "floor-effect", Identity: runtimeIdentity(), Sequence: epoch,
				FloorState: state, FloorEpoch: epoch, SourceEventId: "runtime-test",
				TurnId: value.TurnID, GenerationId: value.GenerationID, ToolEpoch: value.ToolEpoch,
				ExpiresAtMs: uint64(time.Now().Add(time.Minute).UnixMilli()),
			},
		}}
	}

	if err := runtime.handleEvent(effect(fence, mediav1.FloorState_FLOOR_STATE_USER_HOLDS_FLOOR, 1)); err != nil {
		t.Fatalf("current floor effect rejected: %v", err)
	}
	if session.floorState != ShadowFloorUser || session.floorEpoch != 1 {
		t.Fatalf("typed floor was not installed: state=%q epoch=%d", session.floorState, session.floorEpoch)
	}
	select {
	case event := <-forwarded:
		if event.GetFloorEffect() == nil {
			t.Fatalf("unexpected forwarded event: %v", event)
		}
	default:
		t.Fatal("current floor effect was not forwarded to the terminator")
	}

	stale := fence
	stale.GenerationID--
	if err := runtime.handleEvent(effect(stale, mediav1.FloorState_FLOOR_STATE_SILENCE, 2)); err == nil {
		t.Fatal("stale floor effect fence was accepted")
	}
	if err := runtime.handleEvent(effect(fence, mediav1.FloorState_FLOOR_STATE_SILENCE, 1)); err == nil {
		t.Fatal("stale floor epoch was accepted")
	}

	candidate := effect(fence, mediav1.FloorState_FLOOR_STATE_SILENCE, 2)
	candidate.GetFloorEffect().CandidateOnly = true
	if err := runtime.handleEvent(candidate); err != nil {
		t.Fatalf("candidate floor effect should be dropped, got %v", err)
	}
	select {
	case <-forwarded:
		t.Fatal("candidate floor effect reached the terminator")
	default:
	}

	cancelled := fence
	cancelled.GenerationID++
	if err := session.ApplyCancelledGeneration(cancelled); err != nil {
		t.Fatal(err)
	}
	if err := runtime.handleEvent(effect(cancelled, mediav1.FloorState_FLOOR_STATE_USER_HOLDS_FLOOR, 2)); err != nil {
		t.Fatalf("current cancelled floor effect rejected: %v", err)
	}
	if session.floorState != ShadowFloorUser || session.floorEpoch != 2 {
		t.Fatalf("cancelled fence did not retain current floor: state=%q epoch=%d", session.floorState, session.floorEpoch)
	}
	<-forwarded

	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE
	if err := runtime.handleEvent(effect(cancelled, mediav1.FloorState_FLOOR_STATE_SILENCE, 3)); err != nil {
		t.Fatalf("unproven Go-authoritative floor effect should be dropped, got %v", err)
	}
	select {
	case <-forwarded:
		t.Fatal("unproven Go-authoritative floor effect reached the terminator")
	default:
	}
}
