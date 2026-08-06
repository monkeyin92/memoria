package mediaedge

import (
	"encoding/hex"
	"math"
	"slices"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

// These helpers keep generated protobuf details at the bridge boundary. The
// runtime loop consumes the bounded shadow-domain types defined by the actor.
func shadowFloorDecisionFromProto(
	input *mediav1.ShadowFloorDecision,
) (ShadowFloorState, string, bool) {
	if input == nil {
		return "", "", false
	}
	floor, valid := floorStateFromProto(input.GetFloorState())
	if !valid {
		return "", "", false
	}
	decision := ""
	switch input.GetEffectKind() {
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT:
		decision = "duck_output"
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_ENQUEUE_OUTPUT_INTENT:
		decision = "enqueue_output_intent"
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_RESUME_OUTPUT:
		decision = "continue_output"
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DROP_STALE_EVENT:
		decision = "drop_stale_event"
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_PAUSE_OUTPUT:
		decision = "pause_output"
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION:
		decision = "cancel_generation"
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_UNSPECIFIED:
	default:
		return "", "", false
	}
	return floor, decision, true
}

func floorStateFromProto(input mediav1.FloorState) (ShadowFloorState, bool) {
	switch input {
	case mediav1.FloorState_FLOOR_STATE_SILENCE:
		return ShadowFloorSilence, true
	case mediav1.FloorState_FLOOR_STATE_USER_HOLDS_FLOOR:
		return ShadowFloorUser, true
	case mediav1.FloorState_FLOOR_STATE_ASSISTANT_HOLDS_FLOOR:
		return ShadowFloorAssistant, true
	case mediav1.FloorState_FLOOR_STATE_OVERLAP:
		return ShadowFloorOverlap, true
	case mediav1.FloorState_FLOOR_STATE_UNCERTAIN:
		return ShadowFloorUncertain, true
	default:
		return "", false
	}
}

func shadowSpeechTimelineSnapshotFromObservation(
	sessionID string,
	streamEpoch uint64,
	observation *mediav1.ShadowObservation,
) (*LiveSessionSnapshot, bool) {
	authoritative, ok := shadowSpeechTimelineStateFromProto(observation.GetAuthoritativeTimeline())
	if !ok {
		return nil, false
	}
	return &LiveSessionSnapshot{
		SessionID: sessionID, StreamEpoch: streamEpoch, SpeechTimeline: authoritative,
	}, true
}

func shadowSpeechSegmentFromProto(source *mediav1.ShadowSpeechSegment) (ShadowSpeechSegment, bool) {
	if source == nil || source.GetSegmentId() == "" || source.GetTaskEpoch() == 0 ||
		source.GetRevision() == 0 || source.GetCaptureEndSample() <= source.GetCaptureStartSample() ||
		len(source.GetTextSha256()) != 32 {
		return ShadowSpeechSegment{}, false
	}
	return ShadowSpeechSegment{
		SegmentID: source.GetSegmentId(), CaptureStartSample: source.GetCaptureStartSample(),
		CaptureEndSample: source.GetCaptureEndSample(), TaskEpoch: source.GetTaskEpoch(),
		Revision: source.GetRevision(), TextSHA256: hex.EncodeToString(source.GetTextSha256()),
		Final: source.GetFinal(),
	}, true
}

func shadowSpeechTimelineStateFromProto(source *mediav1.ShadowSpeechTimelineState) (ShadowSpeechTimeline, bool) {
	if source == nil {
		return ShadowSpeechTimeline{}, false
	}
	segments := make([]ShadowSpeechSegment, 0, len(source.GetSegments()))
	for _, input := range source.GetSegments() {
		segment, ok := shadowSpeechSegmentFromProto(input)
		if !ok {
			return ShadowSpeechTimeline{}, false
		}
		segments = append(segments, segment)
	}
	return ShadowSpeechTimeline{
		CommittedSample: source.GetCommittedSample(), LatestTaskEpoch: source.GetLatestTaskEpoch(),
		Segments: segments,
	}, true
}

func shadowOutputIntentFromProto(sessionID string, source *mediav1.ShadowOutputIntent) (ShadowOutputIntent, bool) {
	if source == nil || source.GetIntentId() == "" || source.GetCreatedAtMs() > math.MaxInt64 ||
		source.GetExpiresAtMs() > math.MaxInt64 {
		return ShadowOutputIntent{}, false
	}
	kind := ShadowOutputIntentKind("")
	switch source.GetKind() {
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
		kind = ShadowOutputFastAck
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_BACKCHANNEL:
		kind = ShadowOutputConversation
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_CONVERSATION_REPLY:
		kind = ShadowOutputConversation
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_DEEP_RESULT:
		kind = ShadowOutputDeep
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_TOOL_RESULT:
		kind = ShadowOutputTool
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_REMINDER:
		kind = ShadowOutputReminder
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_NOTIFICATION:
		kind = ShadowOutputNotification
	default:
		return ShadowOutputIntent{}, false
	}
	floor := ShadowOutputFloorRequirement("")
	if source.GetFloorRequirement() == mediav1.FloorRequirement_FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK {
		floor = ShadowOutputFloorAvailable
	}
	return ShadowOutputIntent{
		IntentID: source.GetIntentId(),
		Fence: Fence{SessionID: sessionID, TurnID: source.GetTurnId(),
			GenerationID: source.GetGenerationId(), ToolEpoch: source.GetToolEpoch()},
		Kind: kind, Priority: source.GetPriority(), CreatedAtUnixMillis: int64(source.GetCreatedAtMs()),
		ExpiresAtUnixMillis: int64(source.GetExpiresAtMs()), ContextVersion: source.GetContextVersion(),
		FloorRequirement: floor, PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}, true
}

func shadowOutputArbiterFromProto(
	sessionID string,
	source *mediav1.ShadowOutputArbiterState,
) (ShadowOutputArbiter, bool) {
	if source == nil {
		return ShadowOutputArbiter{}, false
	}
	decode := func(candidate *mediav1.ShadowOutputIntent) (ShadowOutputIntent, bool) {
		intent, valid := shadowOutputIntentFromProto(sessionID, candidate)
		if !valid {
			return ShadowOutputIntent{}, false
		}
		rank, ranked := shadowOutputDomainRank(&intent)
		if !ranked {
			return ShadowOutputIntent{}, false
		}
		intent.DomainRank = rank
		intent.CandidateOnly = true
		if intent.CreatedAtUnixMillis <= 0 || intent.ExpiresAtUnixMillis <= intent.CreatedAtUnixMillis ||
			intent.FloorRequirement != ShadowOutputFloorAvailable ||
			intent.PlaybackRequirement != ShadowOutputPlaybackCurrentGeneration {
			return ShadowOutputIntent{}, false
		}
		return intent, true
	}
	state := ShadowOutputArbiter{
		ContextVersion: source.GetContextVersion(), CandidatesComplete: source.GetActiveCandidatesComplete(),
	}
	if !state.CandidatesComplete {
		if len(source.GetActiveCandidates()) != 0 {
			return ShadowOutputArbiter{}, false
		}
		if candidate := source.GetCandidate(); candidate != nil {
			intent, valid := decode(candidate)
			if !valid {
				return ShadowOutputArbiter{}, false
			}
			state.Candidate = &intent
		}
		return state, true
	}

	inputs := source.GetActiveCandidates()
	if len(inputs) > maxShadowOutputPerDomain*4 {
		return ShadowOutputArbiter{}, false
	}
	seen := make(map[string]struct{}, len(inputs))
	domainCounts := [5]int{}
	var fence Fence
	for index, candidate := range inputs {
		intent, valid := decode(candidate)
		if !valid || intent.ContextVersion != state.ContextVersion {
			return ShadowOutputArbiter{}, false
		}
		if _, duplicate := seen[intent.IntentID]; duplicate {
			return ShadowOutputArbiter{}, false
		}
		seen[intent.IntentID] = struct{}{}
		domainCounts[intent.DomainRank]++
		if domainCounts[intent.DomainRank] > maxShadowOutputPerDomain {
			return ShadowOutputArbiter{}, false
		}
		if index == 0 {
			fence = intent.Fence
		} else if !intent.Fence.Equal(fence) {
			return ShadowOutputArbiter{}, false
		}
		state.Candidates = append(state.Candidates, intent)
	}
	canonical := append([]ShadowOutputIntent(nil), state.Candidates...)
	sortShadowOutputCandidates(canonical)
	if !slices.Equal(canonical, state.Candidates) {
		return ShadowOutputArbiter{}, false
	}
	winner := source.GetCandidate()
	if len(state.Candidates) == 0 {
		if winner != nil {
			return ShadowOutputArbiter{}, false
		}
		return state, true
	}
	decodedWinner, valid := decode(winner)
	if !valid || decodedWinner != state.Candidates[0] {
		return ShadowOutputArbiter{}, false
	}
	state.Candidate = &decodedWinner
	return state, true
}

func shadowOutputArbiterActiveAt(state ShadowOutputArbiter, observedAtUnixMillis int64) bool {
	if state.CandidatesComplete {
		for _, candidate := range state.Candidates {
			if candidate.CreatedAtUnixMillis > observedAtUnixMillis ||
				candidate.ExpiresAtUnixMillis <= observedAtUnixMillis {
				return false
			}
		}
		return true
	}
	return state.Candidate == nil ||
		(state.Candidate.CreatedAtUnixMillis <= observedAtUnixMillis &&
			state.Candidate.ExpiresAtUnixMillis > observedAtUnixMillis)
}

func pythonInteractionState(phase string) (ShadowFloorState, string, bool) {
	switch phase {
	case "connecting":
		return ShadowFloorSilence, "pause_output", true
	case "ready", "speaker_enroll", "listening":
		return ShadowFloorSilence, "continue_output", true
	case "user_speaking":
		return ShadowFloorUser, "duck_output", true
	case "backchannel":
		return ShadowFloorOverlap, "continue_output", true
	case "thinking", "thinking_silent", "tool_waiting":
		return ShadowFloorSilence, "continue_output", true
	case "speaking":
		return ShadowFloorAssistant, "enqueue_output_intent", true
	case "interrupted":
		return ShadowFloorUser, "drop_stale_event", true
	case "recovering", "closed":
		return ShadowFloorSilence, "drop_stale_event", true
	default:
		return ShadowFloorSilence, "", false
	}
}
