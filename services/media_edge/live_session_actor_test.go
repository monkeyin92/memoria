package mediaedge

import (
	"errors"
	"fmt"
	"testing"
	"time"
)

func waitForActorEvents(t *testing.T, actor *LiveSessionActor, want uint64) LiveSessionSnapshot {
	t.Helper()
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		snapshot := actor.Snapshot()
		if snapshot.ProcessedEvents >= want {
			return snapshot
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("actor did not process %d events", want)
	return LiveSessionSnapshot{}
}

func TestLiveSessionActorSerializesShadowDecisionsWithoutEffects(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 2, ToolEpoch: 3}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence,
	}); err != nil {
		t.Fatal(err)
	}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventVADStart, StreamEpoch: 1,
	}); err != nil {
		t.Fatal(err)
	}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventAudioDownlink, StreamEpoch: 1, Fence: fence, Audio: true,
	}); err != nil {
		t.Fatal(err)
	}

	snapshot := waitForActorEvents(t, actor, 3)
	if snapshot.LastMailboxSequence != 3 || !snapshot.Generation.Equal(fence) {
		t.Fatalf("unexpected serialized snapshot: %+v", snapshot)
	}
	if snapshot.Floor != ShadowFloorOverlap || snapshot.LastDecision != "drop_stale_event" {
		t.Fatalf("shadow decision not computed: %+v", snapshot)
	}
}

func TestLiveSessionActorSpeechTimelineKeepsOnlyLatestSegmentRevision(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()

	for _, revision := range []uint64{1, 2, 1} {
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind:        LiveEventSpeechSegment,
			StreamEpoch: 1,
			SpeechSegment: &ShadowSpeechSegment{
				SegmentID: "sentence-1", TaskEpoch: 1, Revision: revision,
				CaptureStartSample: 10, CaptureEndSample: 20, Text: "hello",
			},
		}); err != nil {
			t.Fatal(err)
		}
	}

	snapshot := waitForActorEvents(t, actor, 3)
	if len(snapshot.SpeechTimeline.Segments) != 1 ||
		snapshot.SpeechTimeline.Segments[0].Revision != 2 {
		t.Fatalf("timeline did not retain the canonical revision: %+v", snapshot.SpeechTimeline)
	}
	if snapshot.SpeechTimeline.DroppedSegments != 1 {
		t.Fatalf("stale revision was not accounted: %+v", snapshot.SpeechTimeline)
	}
}

func TestLiveSessionActorSpeechTimelineCommitsWatermarkAndRejectsLateSegment(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	segment := ShadowSpeechSegment{
		SegmentID: "sentence-1", TaskEpoch: 1, Revision: 1,
		CaptureStartSample: 10, CaptureEndSample: 20, Text: "hello", Final: true,
	}
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventSpeechSegment, StreamEpoch: 1, SpeechSegment: &segment},
		{Kind: LiveEventSpeechCommit, StreamEpoch: 1, CommitSample: 20},
		{Kind: LiveEventSpeechSegment, StreamEpoch: 1, SpeechSegment: &segment},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}

	snapshot := waitForActorEvents(t, actor, 3)
	if snapshot.SpeechTimeline.CommittedSample != 20 || len(snapshot.SpeechTimeline.Segments) != 0 {
		t.Fatalf("commit did not consume the canonical range: %+v", snapshot.SpeechTimeline)
	}
	if snapshot.SpeechTimeline.DroppedSegments != 1 {
		t.Fatalf("segment behind the watermark was not rejected: %+v", snapshot.SpeechTimeline)
	}
}

func TestLiveSessionActorSpeechTimelineOrdersCanonicalRanges(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	for _, segment := range []ShadowSpeechSegment{
		{SegmentID: "later", TaskEpoch: 1, Revision: 1, CaptureStartSample: 30, CaptureEndSample: 40},
		{SegmentID: "earlier", TaskEpoch: 1, Revision: 1, CaptureStartSample: 10, CaptureEndSample: 20},
	} {
		segment := segment
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind: LiveEventSpeechSegment, StreamEpoch: 1, SpeechSegment: &segment,
		}); err != nil {
			t.Fatal(err)
		}
	}

	segments := waitForActorEvents(t, actor, 2).SpeechTimeline.Segments
	if len(segments) != 2 || segments[0].SegmentID != "earlier" || segments[1].SegmentID != "later" {
		t.Fatalf("canonical timeline is not range ordered: %+v", segments)
	}
}

func TestLiveSessionActorSpeechTimelineRejectsOldTaskAfterTakeover(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	segments := []ShadowSpeechSegment{
		{SegmentID: "old-accepted", TaskEpoch: 1, Revision: 1, CaptureStartSample: 10, CaptureEndSample: 20},
		{SegmentID: "new-task", TaskEpoch: 2, Revision: 1, CaptureStartSample: 30, CaptureEndSample: 40},
		{SegmentID: "old-late", TaskEpoch: 1, Revision: 99, CaptureStartSample: 50, CaptureEndSample: 60},
	}
	for _, segment := range segments {
		segment := segment
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind: LiveEventSpeechSegment, StreamEpoch: 1, SpeechSegment: &segment,
		}); err != nil {
			t.Fatal(err)
		}
	}

	timeline := waitForActorEvents(t, actor, 3).SpeechTimeline
	if timeline.LatestTaskEpoch != 2 || timeline.DroppedSegments != 1 || len(timeline.Segments) != 2 {
		t.Fatalf("task takeover did not fence the old provider task: %+v", timeline)
	}
	for _, segment := range timeline.Segments {
		if segment.SegmentID == "old-late" {
			t.Fatalf("old task wrote back after takeover: %+v", timeline)
		}
	}
}

func TestLiveSessionActorSpeechTimelineFinalCannotRegressToPartial(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	segment := ShadowSpeechSegment{
		SegmentID: "sentence", TaskEpoch: 1, Revision: 1,
		CaptureStartSample: 10, CaptureEndSample: 20,
	}
	for _, final := range []bool{false, true, false} {
		segment.Final = final
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind: LiveEventSpeechSegment, StreamEpoch: 1, SpeechSegment: &segment,
		}); err != nil {
			t.Fatal(err)
		}
	}

	timeline := waitForActorEvents(t, actor, 3).SpeechTimeline
	if len(timeline.Segments) != 1 || !timeline.Segments[0].Final || timeline.DroppedSegments != 1 {
		t.Fatalf("final segment regressed to a partial: %+v", timeline)
	}
}

func TestLiveSessionActorSpeechTimelineRejectsWrongAndMissingStreamEpoch(t *testing.T) {
	actor := NewLiveSessionActor("session", 2)
	defer actor.Close()
	segment := ShadowSpeechSegment{
		SegmentID: "sentence", TaskEpoch: 1, Revision: 1,
		CaptureStartSample: 10, CaptureEndSample: 20,
	}
	for _, streamEpoch := range []uint64{1, 0} {
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind: LiveEventSpeechSegment, StreamEpoch: streamEpoch, SpeechSegment: &segment,
		}); err != nil {
			t.Fatal(err)
		}
	}

	snapshot := waitForActorEvents(t, actor, 2)
	if len(snapshot.SpeechTimeline.Segments) != 0 || snapshot.SpeechTimeline.DroppedSegments != 2 {
		t.Fatalf("incomplete stream fences reached the timeline: %+v", snapshot)
	}
}

func TestLiveSessionActorSpeechTimelineCapacityIsBounded(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	for index := 0; index <= maxShadowSpeechSegments; index++ {
		segment := ShadowSpeechSegment{
			SegmentID: time.Duration(index).String(), TaskEpoch: 1, Revision: 1,
			CaptureStartSample: uint64(index * 10), CaptureEndSample: uint64(index*10 + 5),
		}
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind: LiveEventSpeechSegment, StreamEpoch: 1, SpeechSegment: &segment,
		}); err != nil {
			t.Fatal(err)
		}
		waitForActorEvents(t, actor, uint64(index+1))
	}

	timeline := actor.Snapshot().SpeechTimeline
	if len(timeline.Segments) != maxShadowSpeechSegments || timeline.DroppedSegments != 1 {
		t.Fatalf("timeline exceeded its fixed capacity: %+v", timeline)
	}
}

func TestLiveSessionActorSpeechTimelineCapacityDropStillAdvancesTaskFence(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	for index := 0; index < maxShadowSpeechSegments; index++ {
		segment := ShadowSpeechSegment{
			SegmentID: time.Duration(index).String(), TaskEpoch: 1, Revision: 1,
			CaptureStartSample: uint64(index * 10), CaptureEndSample: uint64(index*10 + 5),
		}
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind: LiveEventSpeechSegment, StreamEpoch: 1, SpeechSegment: &segment,
		}); err != nil {
			t.Fatal(err)
		}
		waitForActorEvents(t, actor, uint64(index+1))
	}
	newTask := ShadowSpeechSegment{
		SegmentID: "new-task", TaskEpoch: 2, Revision: 1,
		CaptureStartSample: 2000, CaptureEndSample: 2010,
	}
	oldRewrite := ShadowSpeechSegment{
		SegmentID: "0s", TaskEpoch: 1, Revision: 2,
		CaptureStartSample: 0, CaptureEndSample: 5,
	}
	for _, segment := range []*ShadowSpeechSegment{&newTask, &oldRewrite} {
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind: LiveEventSpeechSegment, StreamEpoch: 1, SpeechSegment: segment,
		}); err != nil {
			t.Fatal(err)
		}
	}

	timeline := waitForActorEvents(t, actor, maxShadowSpeechSegments+2).SpeechTimeline
	if timeline.LatestTaskEpoch != 2 || timeline.DroppedSegments != 2 ||
		timeline.Segments[0].Revision != 1 {
		t.Fatalf("capacity drop let an old provider task regain authority: %+v", timeline)
	}
}

func TestLiveSessionActorFreezesNestedTimelineInShadowComparison(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	authoritative := &LiveSessionSnapshot{SessionID: "session", StreamEpoch: 1}
	for _, revision := range []uint64{1, 2} {
		segment := ShadowSpeechSegment{
			SegmentID: "sentence", TaskEpoch: 1, Revision: revision,
			CaptureStartSample: 10, CaptureEndSample: 20,
		}
		event := LiveSessionEvent{
			Kind: LiveEventSpeechSegment, StreamEpoch: 1, SpeechSegment: &segment,
		}
		if revision == 1 {
			event.Authoritative = authoritative
		}
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}

	snapshot := waitForActorEvents(t, actor, 2)
	if len(snapshot.RecentComparisons) != 1 ||
		len(snapshot.RecentComparisons[0].Candidate.SpeechTimeline.Segments) != 1 ||
		snapshot.RecentComparisons[0].Candidate.SpeechTimeline.Segments[0].Revision != 1 {
		t.Fatalf("comparison changed after a later timeline revision: %+v", snapshot.RecentComparisons)
	}
	if snapshot.RecentComparisons[0].ContractVersion != "media-v1-a6a" {
		t.Fatalf("timeline comparison used the pre-A6A contract: %+v", snapshot.RecentComparisons[0])
	}
}

func TestLiveSessionActorOutputArbiterKeepsOneHighestRankedDormantCandidate(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1, ToolEpoch: 1}
	expires := time.Now().Add(time.Minute).UnixMilli()
	conversation := ShadowOutputIntent{
		IntentID: "conversation", Fence: fence, Kind: ShadowOutputConversation,
		CreatedAtUnixMillis: 100, ExpiresAtUnixMillis: expires, ContextVersion: 7,
		FloorRequirement:    ShadowOutputFloorAvailable,
		PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}
	acknowledgement := conversation
	acknowledgement.IntentID = "fast-ack"
	acknowledgement.Kind = ShadowOutputFastAck
	acknowledgement.CreatedAtUnixMillis++

	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventContextActivate, StreamEpoch: 1, ContextVersion: 7},
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &conversation},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &acknowledgement},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &conversation},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}

	output := waitForActorEvents(t, actor, 5).OutputArbiter
	if output.Candidate == nil || output.Candidate.IntentID != "fast-ack" {
		t.Fatalf("fast acknowledgement did not supersede conversation output: %+v", output)
	}
	if !output.Candidate.CandidateOnly || output.Candidate.DomainRank <= conversation.DomainRank {
		t.Fatalf("candidate-only rank was not assigned by the actor: %+v", output.Candidate)
	}
	if output.DroppedIntents != 1 {
		t.Fatalf("superseded intent replay was not dropped: %+v", output)
	}
}

func TestLiveSessionActorOutputArbiterClearsAudibleCandidateWhenUserTakesFloor(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	intent := ShadowOutputIntent{
		IntentID: "conversation", Fence: fence, Kind: ShadowOutputConversation,
		CreatedAtUnixMillis: 100, ExpiresAtUnixMillis: time.Now().Add(time.Minute).UnixMilli(),
		ContextVersion: 1, FloorRequirement: ShadowOutputFloorAvailable,
		PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventContextActivate, StreamEpoch: 1, ContextVersion: 1},
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &intent},
		{Kind: LiveEventVADStart, StreamEpoch: 1},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}

	snapshot := waitForActorEvents(t, actor, 4)
	if snapshot.Floor != ShadowFloorOverlap || snapshot.OutputArbiter.Candidate != nil {
		t.Fatalf("user floor retained an audible candidate: %+v", snapshot)
	}
}

func TestLiveSessionActorOutputArbiterDoesNotExposeExpiredCandidate(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	intent := ShadowOutputIntent{
		IntentID: "short-lived", Fence: fence, Kind: ShadowOutputConversation,
		CreatedAtUnixMillis: time.Now().Add(-time.Second).UnixMilli(),
		ExpiresAtUnixMillis: time.Now().Add(200 * time.Millisecond).UnixMilli(),
		ContextVersion:      1, FloorRequirement: ShadowOutputFloorAvailable,
		PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventContextActivate, StreamEpoch: 1, ContextVersion: 1},
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &intent},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}
	if candidate := waitForActorEvents(t, actor, 3).OutputArbiter.Candidate; candidate == nil {
		t.Fatal("unexpired candidate was rejected")
	}
	time.Sleep(250 * time.Millisecond)
	if candidate := actor.Snapshot().OutputArbiter.Candidate; candidate != nil {
		t.Fatalf("expired candidate remained observable: %+v", candidate)
	}
}

func TestLiveSessionActorOutputArbiterRejectsStaleAuthorityInputs(t *testing.T) {
	current := Fence{SessionID: "session", TurnID: 2, GenerationID: 3, ToolEpoch: 4}
	now := time.Now()
	tests := []struct {
		name           string
		streamEpoch    uint64
		fence          Fence
		context        uint64
		expires        int64
		floor          ShadowOutputFloorRequirement
		playback       ShadowOutputPlaybackRequirement
		userTakesFloor bool
	}{
		{name: "missing stream", streamEpoch: 0, fence: current, context: 7, expires: now.Add(time.Minute).UnixMilli(), floor: ShadowOutputFloorAvailable, playback: ShadowOutputPlaybackCurrentGeneration},
		{name: "stale fence", streamEpoch: 1, fence: Fence{SessionID: "session", TurnID: 2, GenerationID: 3, ToolEpoch: 3}, context: 7, expires: now.Add(time.Minute).UnixMilli(), floor: ShadowOutputFloorAvailable, playback: ShadowOutputPlaybackCurrentGeneration},
		{name: "stale context", streamEpoch: 1, fence: current, context: 6, expires: now.Add(time.Minute).UnixMilli(), floor: ShadowOutputFloorAvailable, playback: ShadowOutputPlaybackCurrentGeneration},
		{name: "expired", streamEpoch: 1, fence: current, context: 7, expires: now.Add(-time.Second).UnixMilli(), floor: ShadowOutputFloorAvailable, playback: ShadowOutputPlaybackCurrentGeneration},
		{name: "future created", streamEpoch: 1, fence: current, context: 7, expires: now.Add(time.Minute).UnixMilli(), floor: ShadowOutputFloorAvailable, playback: ShadowOutputPlaybackCurrentGeneration},
		{name: "user floor", streamEpoch: 1, fence: current, context: 7, expires: now.Add(time.Minute).UnixMilli(), floor: ShadowOutputFloorAvailable, playback: ShadowOutputPlaybackCurrentGeneration, userTakesFloor: true},
		{name: "invalid floor requirement", streamEpoch: 1, fence: current, context: 7, expires: now.Add(time.Minute).UnixMilli(), floor: "any", playback: ShadowOutputPlaybackCurrentGeneration},
		{name: "invalid playback requirement", streamEpoch: 1, fence: current, context: 7, expires: now.Add(time.Minute).UnixMilli(), floor: ShadowOutputFloorAvailable, playback: "idle"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			actor := NewLiveSessionActor("session", 1)
			defer actor.Close()
			for _, event := range []LiveSessionEvent{
				{Kind: LiveEventContextActivate, StreamEpoch: 1, ContextVersion: 7},
				{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: current},
			} {
				if err := actor.TrySubmit(event); err != nil {
					t.Fatal(err)
				}
			}
			processed := uint64(2)
			if test.userTakesFloor {
				if err := actor.TrySubmit(LiveSessionEvent{Kind: LiveEventVADStart, StreamEpoch: 1}); err != nil {
					t.Fatal(err)
				}
				processed++
			}
			intent := ShadowOutputIntent{
				IntentID: test.name, Fence: test.fence, Kind: ShadowOutputConversation,
				CreatedAtUnixMillis: now.Add(-2 * time.Second).UnixMilli(),
				ExpiresAtUnixMillis: test.expires, ContextVersion: test.context,
				FloorRequirement: test.floor, PlaybackRequirement: test.playback,
			}
			if test.name == "future created" {
				intent.CreatedAtUnixMillis = now.Add(30 * time.Second).UnixMilli()
			}
			if err := actor.TrySubmit(LiveSessionEvent{
				Kind: LiveEventOutputIntent, StreamEpoch: test.streamEpoch, OutputIntent: &intent,
			}); err != nil {
				t.Fatal(err)
			}
			snapshot := waitForActorEvents(t, actor, processed+1)
			if snapshot.OutputArbiter.Candidate != nil || snapshot.OutputArbiter.DroppedIntents != 1 {
				t.Fatalf("stale authority input was admitted: %+v", snapshot)
			}
		})
	}
}

func TestLiveSessionActorInvalidOutputIntentCannotReviveWithTheSameID(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	now := time.Now()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	intent := ShadowOutputIntent{
		IntentID: "one-shot", Fence: fence, Kind: ShadowOutputConversation,
		CreatedAtUnixMillis: now.Add(-time.Second).UnixMilli(),
		ExpiresAtUnixMillis: now.Add(time.Minute).UnixMilli(), ContextVersion: 1,
		FloorRequirement: "invalid", PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventContextActivate, StreamEpoch: 1, ContextVersion: 1},
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &intent},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}
	intent.FloorRequirement = ShadowOutputFloorAvailable
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &intent,
	}); err != nil {
		t.Fatal(err)
	}
	snapshot := waitForActorEvents(t, actor, 4)
	if snapshot.OutputArbiter.Candidate != nil || snapshot.OutputArbiter.DroppedIntents != 2 {
		t.Fatalf("invalid one-shot intent revived after its gates changed: %+v", snapshot.OutputArbiter)
	}
}

func TestLiveSessionActorAcceptedOutputIntentRemainsQueuedWithoutDropTelemetry(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	base := ShadowOutputIntent{
		Fence: fence, CreatedAtUnixMillis: 100, ExpiresAtUnixMillis: time.Now().Add(time.Minute).UnixMilli(),
		ContextVersion: 1, FloorRequirement: ShadowOutputFloorAvailable,
		PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}
	conversation := base
	conversation.IntentID = "conversation"
	conversation.Kind = ShadowOutputConversation
	reminder := base
	reminder.IntentID = "reminder"
	reminder.Kind = ShadowOutputReminder
	reminder.Priority = ^uint32(0)
	reminder.CreatedAtUnixMillis++
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventContextActivate, StreamEpoch: 1, ContextVersion: 1},
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &conversation},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &reminder},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}

	snapshot := waitForActorEvents(t, actor, 4)
	output := snapshot.OutputArbiter
	if output.Candidate == nil || output.Candidate.IntentID != "conversation" ||
		len(output.Candidates) != 2 || output.Candidates[1].IntentID != "reminder" {
		t.Fatalf("accepted reminder was not queued behind the conversation winner: %+v", output)
	}
	if snapshot.DroppedEvents != 0 || output.DroppedIntents != 0 ||
		snapshot.LastDecision != "record_output_candidate" {
		t.Fatalf("queued reminder was reported as dropped: %+v", snapshot)
	}
}

func TestLiveSessionActorOutputArbiterUsesFixedDomainOrder(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventContextActivate, StreamEpoch: 1, ContextVersion: 1},
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}
	expires := time.Now().Add(time.Minute).UnixMilli()
	for index, kind := range []ShadowOutputIntentKind{
		ShadowOutputReminder, ShadowOutputDeep, ShadowOutputConversation, ShadowOutputFastAck,
	} {
		intent := ShadowOutputIntent{
			IntentID: string(kind), Fence: fence, Kind: kind,
			Priority: uint32(100 - index), CreatedAtUnixMillis: int64(100 + index),
			ExpiresAtUnixMillis: expires, ContextVersion: 1,
			FloorRequirement:    ShadowOutputFloorAvailable,
			PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
		}
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &intent,
		}); err != nil {
			t.Fatal(err)
		}
		snapshot := waitForActorEvents(t, actor, uint64(index+3))
		if snapshot.OutputArbiter.Candidate == nil || snapshot.OutputArbiter.Candidate.IntentID != string(kind) {
			t.Fatalf("domain %s did not supersede its lower rank: %+v", kind, snapshot.OutputArbiter)
		}
	}
}

func TestLiveSessionActorRestoresPendingOutputAfterHigherPriorityExpiry(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	now := time.Now()
	conversation := ShadowOutputIntent{
		IntentID: "conversation", Fence: fence, Kind: ShadowOutputConversation,
		Priority: 10, CreatedAtUnixMillis: now.Add(-time.Second).UnixMilli(),
		ExpiresAtUnixMillis: now.Add(time.Minute).UnixMilli(), ContextVersion: 1,
		FloorRequirement:    ShadowOutputFloorAvailable,
		PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}
	acknowledgement := ShadowOutputIntent{
		IntentID: "fast-ack", Fence: fence, Kind: ShadowOutputFastAck,
		Priority: 1, CreatedAtUnixMillis: now.UnixMilli(),
		ExpiresAtUnixMillis: now.Add(20 * time.Millisecond).UnixMilli(), ContextVersion: 1,
		FloorRequirement:    ShadowOutputFloorAvailable,
		PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventContextActivate, StreamEpoch: 1, ContextVersion: 1},
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &conversation},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &acknowledgement},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}
	if snapshot := waitForActorEvents(t, actor, 4); snapshot.OutputArbiter.Candidate == nil ||
		snapshot.OutputArbiter.Candidate.IntentID != "fast-ack" {
		t.Fatalf("higher-priority output did not win while valid: %+v", snapshot.OutputArbiter)
	}
	time.Sleep(30 * time.Millisecond)
	notification := ShadowOutputIntent{
		IntentID: "notification", Fence: fence, Kind: ShadowOutputNotification,
		Priority: 100, CreatedAtUnixMillis: time.Now().UnixMilli(),
		ExpiresAtUnixMillis: time.Now().Add(time.Minute).UnixMilli(), ContextVersion: 1,
		FloorRequirement:    ShadowOutputFloorAvailable,
		PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &notification,
	}); err != nil {
		t.Fatal(err)
	}
	snapshot := waitForActorEvents(t, actor, 5)
	if snapshot.OutputArbiter.Candidate == nil ||
		snapshot.OutputArbiter.Candidate.IntentID != "conversation" {
		t.Fatalf("expired winner did not reveal pending conversation output: %+v", snapshot.OutputArbiter)
	}
}

func TestLiveSessionActorRestoresSameDomainOutputAfterWinnerExpiry(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	now := time.Now()
	fallback := ShadowOutputIntent{
		IntentID: "fallback", Fence: fence, Kind: ShadowOutputDeep,
		Priority: 1, CreatedAtUnixMillis: now.Add(-time.Second).UnixMilli(),
		ExpiresAtUnixMillis: now.Add(time.Minute).UnixMilli(), ContextVersion: 1,
		FloorRequirement:    ShadowOutputFloorAvailable,
		PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}
	winner := fallback
	winner.IntentID = "winner"
	winner.Priority = 100
	winner.CreatedAtUnixMillis++
	winner.ExpiresAtUnixMillis = now.Add(20 * time.Millisecond).UnixMilli()
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventContextActivate, StreamEpoch: 1, ContextVersion: 1},
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &fallback},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &winner},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}
	if snapshot := waitForActorEvents(t, actor, 4); snapshot.OutputArbiter.Candidate == nil ||
		snapshot.OutputArbiter.Candidate.IntentID != "winner" {
		t.Fatalf("same-domain winner did not supersede fallback: %+v", snapshot.OutputArbiter)
	}
	time.Sleep(30 * time.Millisecond)
	if snapshot := actor.Snapshot(); snapshot.OutputArbiter.Candidate == nil ||
		snapshot.OutputArbiter.Candidate.IntentID != "fallback" {
		t.Fatalf("same-domain fallback was not restored: %+v", snapshot.OutputArbiter)
	}
}

func TestShadowOutputArbiterComparisonIncludesCompleteFallbackSet(t *testing.T) {
	winner := ShadowOutputIntent{IntentID: "winner", DomainRank: 2, Priority: 100}
	fallback := ShadowOutputIntent{IntentID: "fallback", DomainRank: 2, Priority: 1}
	left := ShadowOutputArbiter{
		ContextVersion: 1, Candidate: &winner,
		Candidates: []ShadowOutputIntent{winner, fallback}, CandidatesComplete: true,
	}
	right := ShadowOutputArbiter{
		ContextVersion: 1, Candidate: &winner,
		Candidates: []ShadowOutputIntent{winner}, CandidatesComplete: true,
	}
	if equalShadowOutputArbiter(left, right) {
		t.Fatal("matching winner hid a missing fallback candidate")
	}
	left.CandidatesComplete = false
	if !equalShadowOutputArbiter(left, right) {
		t.Fatal("legacy winner-only state required a full candidate set")
	}
}

func TestLiveSessionActorOutputCandidatesAreBoundedPerDomain(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventContextActivate, StreamEpoch: 1, ContextVersion: 1},
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}
	for priority := 0; priority < 5; priority++ {
		intent := ShadowOutputIntent{
			IntentID: fmt.Sprintf("candidate-%d", priority), Fence: fence, Kind: ShadowOutputDeep,
			Priority: uint32(priority), CreatedAtUnixMillis: 100,
			ExpiresAtUnixMillis: time.Now().Add(time.Minute).UnixMilli(), ContextVersion: 1,
			FloorRequirement:    ShadowOutputFloorAvailable,
			PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
		}
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &intent,
		}); err != nil {
			t.Fatal(err)
		}
	}
	snapshot := waitForActorEvents(t, actor, 7)
	if len(snapshot.OutputArbiter.Candidates) != maxShadowOutputPerDomain {
		t.Fatalf("output candidate domain exceeded its bound: %+v", snapshot.OutputArbiter)
	}
	for index, candidate := range snapshot.OutputArbiter.Candidates {
		expected := fmt.Sprintf("candidate-%d", 4-index)
		if candidate.IntentID != expected {
			t.Fatalf("bounded candidates were not canonical: %+v", snapshot.OutputArbiter.Candidates)
		}
	}
}

func TestLiveSessionActorOutputCandidateTrimReportsCurrentIntentAsDropped(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventContextActivate, StreamEpoch: 1, ContextVersion: 1},
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}
	expires := time.Now().Add(time.Minute).UnixMilli()
	for priority := 10; priority < 14; priority++ {
		intent := ShadowOutputIntent{
			IntentID: fmt.Sprintf("retained-%d", priority), Fence: fence, Kind: ShadowOutputDeep,
			Priority: uint32(priority), CreatedAtUnixMillis: int64(priority),
			ExpiresAtUnixMillis: expires, ContextVersion: 1,
			FloorRequirement:    ShadowOutputFloorAvailable,
			PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
		}
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &intent,
		}); err != nil {
			t.Fatal(err)
		}
	}
	trimmed := ShadowOutputIntent{
		IntentID: "trimmed", Fence: fence, Kind: ShadowOutputDeep,
		Priority: 1, CreatedAtUnixMillis: 100,
		ExpiresAtUnixMillis: expires, ContextVersion: 1,
		FloorRequirement:    ShadowOutputFloorAvailable,
		PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &trimmed,
	}); err != nil {
		t.Fatal(err)
	}

	snapshot := waitForActorEvents(t, actor, 7)
	if len(snapshot.OutputArbiter.Candidates) != 4 ||
		snapshot.OutputArbiter.Candidates[0].IntentID != "retained-13" {
		t.Fatalf("capacity trim changed the retained candidate set: %+v", snapshot.OutputArbiter)
	}
	for _, candidate := range snapshot.OutputArbiter.Candidates {
		if candidate.IntentID == trimmed.IntentID {
			t.Fatalf("trimmed intent remained active: %+v", snapshot.OutputArbiter)
		}
	}
	if snapshot.DroppedEvents != 1 || snapshot.OutputArbiter.DroppedIntents != 1 ||
		snapshot.LastDecision != "drop_superseded_output_intent" {
		t.Fatalf("trimmed intent was not reported as superseded: %+v", snapshot)
	}
}

func TestLiveSessionActorSnapshotDoesNotExposeMutableCandidateState(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	segment := ShadowSpeechSegment{
		SegmentID: "sentence", TaskEpoch: 1, Revision: 1,
		CaptureStartSample: 10, CaptureEndSample: 20, Text: "original",
	}
	intent := ShadowOutputIntent{
		IntentID: "original", Fence: fence, Kind: ShadowOutputConversation,
		CreatedAtUnixMillis: 100, ExpiresAtUnixMillis: time.Now().Add(time.Minute).UnixMilli(),
		ContextVersion: 1, FloorRequirement: ShadowOutputFloorAvailable,
		PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventContextActivate, StreamEpoch: 1, ContextVersion: 1},
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence},
		{Kind: LiveEventSpeechSegment, StreamEpoch: 1, SpeechSegment: &segment},
		{Kind: LiveEventOutputIntent, StreamEpoch: 1, OutputIntent: &intent},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}
	segment.Text = "caller mutation"
	intent.IntentID = "caller mutation"

	snapshot := waitForActorEvents(t, actor, 4)
	snapshot.SpeechTimeline.Segments[0].Text = "snapshot mutation"
	snapshot.OutputArbiter.Candidate.IntentID = "snapshot mutation"
	snapshot.OutputArbiter.Candidates[0].IntentID = "snapshot mutation"
	second := actor.Snapshot()
	if second.SpeechTimeline.Segments[0].Text != "original" ||
		second.OutputArbiter.Candidate == nil || second.OutputArbiter.Candidate.IntentID != "original" ||
		len(second.OutputArbiter.Candidates) != 1 || second.OutputArbiter.Candidates[0].IntentID != "original" {
		t.Fatalf("caller mutated actor candidate state: %+v", second)
	}
}

func TestLiveSessionActorRestoresTheRemainingFloorHolder(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence},
		{Kind: LiveEventVADStart, StreamEpoch: 1},
		{Kind: LiveEventVADEnd, StreamEpoch: 1},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}
	if snapshot := waitForActorEvents(t, actor, 3); snapshot.Floor != ShadowFloorAssistant {
		t.Fatalf("assistant floor was not restored after user VAD ended: %+v", snapshot)
	}
	if err := actor.TrySubmit(LiveSessionEvent{Kind: LiveEventVADStart, StreamEpoch: 1}); err != nil {
		t.Fatal(err)
	}
	cancelled := fence
	cancelled.GenerationID++
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventGenerationCancel, StreamEpoch: 1, Fence: cancelled,
	}); err != nil {
		t.Fatal(err)
	}
	if snapshot := waitForActorEvents(t, actor, 5); snapshot.Floor != ShadowFloorUser {
		t.Fatalf("user floor was not preserved after generation cancellation: %+v", snapshot)
	}
}

func TestLiveSessionActorRejectsStaleShadowOutput(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	current := Fence{SessionID: "session", TurnID: 2, GenerationID: 1}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: current,
	}); err != nil {
		t.Fatal(err)
	}
	stale := current
	stale.GenerationID--
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventAudioDownlink, StreamEpoch: 1, Fence: stale, Audio: true,
	}); err != nil {
		t.Fatal(err)
	}
	if snapshot := waitForActorEvents(t, actor, 2); snapshot.LastDecision != "drop_stale_event" {
		t.Fatalf("stale output was admitted by the shadow arbiter: %+v", snapshot)
	}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventAudioDownlink, StreamEpoch: 1, Fence: current, Audio: true,
	}); err != nil {
		t.Fatal(err)
	}
	if snapshot := waitForActorEvents(t, actor, 3); snapshot.LastDecision != "enqueue_output_intent" {
		t.Fatalf("current output was rejected by the shadow arbiter: %+v", snapshot)
	}
}

func TestLiveSessionActorKeepsPlayoutMonotonicWithinOneGeneration(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	current := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	stale := current
	stale.GenerationID--
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: current},
		{Kind: LiveEventPlaybackProgress, StreamEpoch: 1, Fence: current, SamplePosition: 100},
		{Kind: LiveEventPlaybackProgress, StreamEpoch: 1, Fence: stale, SamplePosition: 200},
		{Kind: LiveEventPlaybackProgress, StreamEpoch: 1, Fence: current, SamplePosition: 90},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}
	if snapshot := waitForActorEvents(t, actor, 4); snapshot.PlayoutSample != 100 {
		t.Fatalf("stale playback progress moved the watermark: %+v", snapshot)
	}
	next := current
	next.GenerationID++
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: next,
	}); err != nil {
		t.Fatal(err)
	}
	if snapshot := waitForActorEvents(t, actor, 5); snapshot.PlayoutSample != 0 {
		t.Fatalf("new generation retained the previous playout watermark: %+v", snapshot)
	}
}

func TestLiveSessionActorRejectsEventsFromOlderEpochsAndFences(t *testing.T) {
	actor := NewLiveSessionActor("session", 2)
	defer actor.Close()
	current := Fence{SessionID: "session", TurnID: 2, GenerationID: 2}
	for _, event := range []LiveSessionEvent{
		{Kind: LiveEventGenerationStart, StreamEpoch: 2, Fence: current},
		{Kind: LiveEventVADStart, StreamEpoch: 1},
		{Kind: LiveEventGenerationCancel, StreamEpoch: 2, Fence: Fence{SessionID: "session", TurnID: 1, GenerationID: 9}},
	} {
		if err := actor.TrySubmit(event); err != nil {
			t.Fatal(err)
		}
	}
	snapshot := waitForActorEvents(t, actor, 3)
	if snapshot.StreamEpoch != 2 || !snapshot.Generation.Equal(current) || !snapshot.GenerationActive {
		t.Fatalf("stale event moved actor authority: %+v", snapshot)
	}
	if snapshot.DroppedEvents != 2 {
		t.Fatalf("stale events were not accounted: %+v", snapshot)
	}
	cancelled := current
	cancelled.GenerationID++
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventGenerationCancel, StreamEpoch: 2, Fence: cancelled,
	}); err != nil {
		t.Fatal(err)
	}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventGenerationStart, StreamEpoch: 2, Fence: cancelled,
	}); err != nil {
		t.Fatal(err)
	}
	snapshot = waitForActorEvents(t, actor, 5)
	if snapshot.GenerationActive || !snapshot.Generation.Equal(cancelled) || snapshot.DroppedEvents != 3 {
		t.Fatalf("cancelled generation was reactivated: %+v", snapshot)
	}
}

func TestLiveSessionActorReservesMailboxCapacityForAudio(t *testing.T) {
	actor := newLiveSessionActor("session", 1, 4, 2, time.Millisecond, 100*time.Millisecond)
	defer actor.Close()
	full := false
	for i := 0; i < 20; i++ {
		err := actor.TrySubmit(LiveSessionEvent{Kind: LiveEventVADStart, StreamEpoch: 1})
		if errors.Is(err, ErrActorMailboxFull) {
			full = true
			break
		}
	}
	if !full {
		t.Fatal("business events did not reach reserved mailbox threshold")
	}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventAudioUplink, StreamEpoch: 1, Audio: true,
	}); err != nil {
		t.Fatalf("audio reserve was unavailable: %v", err)
	}
	if actor.Snapshot().DroppedEvents == 0 {
		t.Fatal("mailbox overload was not recorded")
	}
}

func TestLiveSessionActorCountsMailboxRejectedAudioInDeadlineRatioDenominator(t *testing.T) {
	actor := newLiveSessionActor("session", 1, 3, 1, time.Second, 100*time.Millisecond)
	defer actor.Close()
	rejected := false
	for i := 0; i < 20; i++ {
		err := actor.TrySubmit(LiveSessionEvent{Kind: LiveEventAudioUplink, StreamEpoch: 1})
		if errors.Is(err, ErrActorMailboxFull) {
			rejected = true
			break
		}
		if err != nil {
			t.Fatal(err)
		}
	}
	if !rejected {
		t.Fatal("audio mailbox never reached overload")
	}
	snapshot := actor.Snapshot()
	if snapshot.FrameDeadlineMisses == 0 || snapshot.ProcessedAudioFrames < snapshot.FrameDeadlineMisses {
		t.Fatalf("audio deadline ratio can be zero or exceed one: %+v", snapshot)
	}
}

func TestLiveSessionActorCloseAccountsForQueuedAndInFlightAudio(t *testing.T) {
	actor := newLiveSessionActor("session", 1, 4, 1, time.Second, 100*time.Millisecond)
	accepted := uint64(0)
	for i := 0; i < 4; i++ {
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind: LiveEventAudioUplink, StreamEpoch: 1,
		}); err == nil {
			accepted++
		} else if !errors.Is(err, ErrActorMailboxFull) {
			t.Fatal(err)
		}
	}
	actor.Close()
	snapshot := actor.Snapshot()
	if snapshot.ProcessedAudioFrames != accepted || snapshot.DroppedEvents < accepted {
		t.Fatalf("close omitted queued audio accounting: accepted=%d snapshot=%+v", accepted, snapshot)
	}
	if snapshot.FrameDeadlineMisses > snapshot.ProcessedAudioFrames {
		t.Fatalf("close produced an invalid deadline ratio: %+v", snapshot)
	}
}

func TestLiveSessionActorDerivesAudioPriorityFromEventKind(t *testing.T) {
	actor := newLiveSessionActor("session", 1, 3, 1, time.Millisecond, 100*time.Millisecond)
	defer actor.Close()
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventAudioUplink, StreamEpoch: 1, Audio: true,
	}); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	for actor.Snapshot().MailboxDepth != 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	for i := 0; i < 2; i++ {
		if err := actor.TrySubmit(LiveSessionEvent{
			Kind: LiveEventVADStart, StreamEpoch: 1, Audio: true,
		}); err != nil {
			t.Fatalf("business event %d reached reserve too early: %v", i, err)
		}
	}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventVADStart, StreamEpoch: 1, Audio: true,
	}); !errors.Is(err, ErrActorMailboxFull) {
		t.Fatalf("business event bypassed audio reserve by setting Audio=true: %v", err)
	}
}

func TestLiveSessionActorDropsAudioThatMissesItsFrameDeadline(t *testing.T) {
	actor := newLiveSessionActor("session", 1, 4, 1, time.Millisecond, 20*time.Millisecond)
	defer actor.Close()
	fence := Fence{SessionID: "session", TurnID: 1, GenerationID: 1}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence,
	}); err != nil {
		t.Fatal(err)
	}
	waitForActorEvents(t, actor, 1)
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventAudioDownlink, StreamEpoch: 1, Fence: fence,
	}); err != nil {
		t.Fatal(err)
	}
	snapshot := waitForActorEvents(t, actor, 2)
	if snapshot.FrameDeadlineMisses != 1 || snapshot.DroppedEvents != 1 {
		t.Fatalf("expired audio was not accounted as an overload drop: %+v", snapshot)
	}
	if snapshot.MaxMailboxAgeMillis < 1 {
		t.Fatalf("expired frame age was omitted from actor telemetry: %+v", snapshot)
	}
	if snapshot.LastDecision != "drop_stale_event" {
		t.Fatalf("expired audio changed the shadow output decision: %+v", snapshot)
	}
}

func TestLiveSessionActorRecordsBoundedShadowComparisons(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	for i := uint64(1); i <= 100; i++ {
		fence := Fence{SessionID: "session", TurnID: i, GenerationID: 1}
		authoritative := &LiveSessionSnapshot{
			SessionID: "session", StreamEpoch: 1,
			Generation: fence, GenerationActive: true,
		}
		for {
			err := actor.TrySubmit(LiveSessionEvent{
				Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence,
				Authoritative: authoritative, CompareGeneration: true,
			})
			if err == nil {
				break
			}
			if !errors.Is(err, ErrActorMailboxFull) {
				t.Fatal(err)
			}
			time.Sleep(time.Millisecond)
		}
	}
	snapshot := waitForActorEvents(t, actor, 100)
	if snapshot.ShadowMismatchTotal != 0 {
		t.Fatalf("matching shadow decisions diverged: %+v", snapshot)
	}
	if len(snapshot.RecentComparisons) != maxShadowComparisons {
		t.Fatalf("comparisons are not bounded: %d", len(snapshot.RecentComparisons))
	}
	latest := snapshot.RecentComparisons[len(snapshot.RecentComparisons)-1]
	if latest.ObservedAtUnixMillis <= 0 || latest.MailboxAgeMillis < 0 {
		t.Fatalf("comparison lacks per-event timing: %+v", latest)
	}
}

func TestLiveSessionActorBoundsShadowMismatchCardinality(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	for index := uint64(1); index <= 100; index++ {
		fence := Fence{SessionID: "session", TurnID: index, GenerationID: 1}
		authoritative := &LiveSessionSnapshot{
			SessionID: "session", StreamEpoch: 1, Generation: fence,
			GenerationActive: true, Floor: ShadowFloorUser,
		}
		for {
			err := actor.TrySubmit(LiveSessionEvent{
				Kind: LiveEventGenerationStart, StreamEpoch: 1, Fence: fence,
				Authoritative: authoritative, CompareGeneration: true,
				Scenario: time.Duration(index).String(),
			})
			if err == nil {
				break
			}
			if !errors.Is(err, ErrActorMailboxFull) {
				t.Fatal(err)
			}
			time.Sleep(time.Millisecond)
		}
	}

	snapshot := waitForActorEvents(t, actor, 100)
	if len(snapshot.ShadowMismatchCounts) > maxShadowMismatchCounts {
		t.Fatalf("mismatch cardinality is unbounded: %d", len(snapshot.ShadowMismatchCounts))
	}
	var counted uint64
	for _, count := range snapshot.ShadowMismatchCounts {
		counted += count.Count
	}
	if snapshot.ShadowMismatchTotal != 100 || counted != snapshot.ShadowMismatchTotal {
		t.Fatalf("bounded mismatch aggregation lost counts: %+v", snapshot)
	}
}

func TestLiveSessionActorComparesFloorAndOutputDecisionWhenSupplied(t *testing.T) {
	actor := NewLiveSessionActor("session", 1)
	defer actor.Close()
	authoritative := &LiveSessionSnapshot{
		SessionID: "session", StreamEpoch: 1,
		Floor: ShadowFloorAssistant, LastDecision: "continue_output",
	}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventVADStart, StreamEpoch: 1, Authoritative: authoritative,
	}); err != nil {
		t.Fatal(err)
	}
	snapshot := waitForActorEvents(t, actor, 1)
	if len(snapshot.RecentComparisons) != 1 || !snapshot.RecentComparisons[0].Mismatch {
		t.Fatalf("floor mismatch was not recorded: %+v", snapshot)
	}
	if snapshot.RecentComparisons[0].Reason != "floor" {
		t.Fatalf("unexpected mismatch reason: %+v", snapshot.RecentComparisons[0])
	}
	authoritative.Floor = ShadowFloorUser
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventVADStart, StreamEpoch: 1, Authoritative: authoritative,
	}); err != nil {
		t.Fatal(err)
	}
	snapshot = waitForActorEvents(t, actor, 2)
	if snapshot.RecentComparisons[1].Reason != "decision" {
		t.Fatalf("output decision mismatch was not recorded: %+v", snapshot.RecentComparisons[1])
	}
}

func TestLiveSessionActorFreezesAuthoritativeSnapshotAtSubmission(t *testing.T) {
	actor := newLiveSessionActor("session", 1, 4, 1, time.Second, 100*time.Millisecond)
	defer actor.Close()
	if err := actor.TrySubmit(LiveSessionEvent{Kind: LiveEventVADStart, StreamEpoch: 1}); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	for actor.Snapshot().MailboxDepth != 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	authoritative := &LiveSessionSnapshot{
		SessionID: "session", StreamEpoch: 1, Floor: ShadowFloorUser,
	}
	if err := actor.TrySubmit(LiveSessionEvent{
		Kind: LiveEventVADStart, StreamEpoch: 1, Authoritative: authoritative,
	}); err != nil {
		t.Fatal(err)
	}
	authoritative.Floor = ShadowFloorAssistant
	snapshot := waitForActorEvents(t, actor, 2)
	comparison := snapshot.RecentComparisons[0]
	if comparison.Authoritative.Floor != ShadowFloorUser || comparison.Mismatch {
		t.Fatalf("queued authority changed after caller mutation: %+v", comparison)
	}
}

func TestSessionReconnectAndStopRecycleActor(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{
		SessionID: "session", AccountID: "account", DeviceID: "device", StreamEpoch: 1,
	}, 8)
	if err != nil {
		t.Fatal(err)
	}
	old := session.actor
	if _, err := session.Reconnect(); err != nil {
		t.Fatal(err)
	}
	if old == session.actor {
		t.Fatal("reconnect reused the old actor")
	}
	if err := old.TrySubmit(LiveSessionEvent{Kind: LiveEventVADStart}); !errors.Is(err, ErrActorClosed) {
		t.Fatalf("old actor remained active: %v", err)
	}
	current := session.actor
	session.Stop()
	if err := current.TrySubmit(LiveSessionEvent{Kind: LiveEventVADStart}); !errors.Is(err, ErrActorClosed) {
		t.Fatalf("stopped session actor remained active: %v", err)
	}
}

func TestLiveSessionActorCloseReleasesQueuedEvents(t *testing.T) {
	actor := newLiveSessionActor("session", 1, 8, 2, time.Second, time.Second)
	if err := actor.TrySubmit(LiveSessionEvent{Kind: LiveEventVADStart, StreamEpoch: 1}); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(time.Second)
	for actor.Snapshot().MailboxDepth != 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	for i := 0; i < 4; i++ {
		if err := actor.TrySubmit(LiveSessionEvent{Kind: LiveEventVADStart, StreamEpoch: 1}); err != nil {
			t.Fatal(err)
		}
	}
	actor.Close()
	if depth := actor.Snapshot().MailboxDepth; depth != 0 {
		t.Fatalf("closed actor retained %d queued events", depth)
	}
}
