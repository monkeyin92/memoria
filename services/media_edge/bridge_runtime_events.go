package mediaedge

import (
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"time"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

// Core-to-edge dispatch owns identity validation and candidate-only shadow
// observation handling. Runtime construction and edge-to-core writes stay in
// bridge_runtime.go.

func (r *VoiceCoreMediaRuntime) receiveLoop() {
	err := r.receive()
	r.done <- err
}

func (r *VoiceCoreMediaRuntime) receive() error {
	for {
		select {
		case <-r.ctx.Done():
			return nil
		default:
		}
		event, err := r.core.Recv()
		if err != nil {
			if r.ctx.Err() != nil {
				return nil
			}
			r.report(err)
			return err
		}
		if err := r.handleEvent(event); err != nil {
			r.report(err)
			return err
		}
	}
}

func (r *VoiceCoreMediaRuntime) handleEvent(event *mediav1.CoreToMedia) error {
	if event == nil {
		return fmt.Errorf("voice-core returned an empty event")
	}
	if observation := event.GetShadowObservation(); observation != nil {
		r.handleShadowObservation(observation)
		return nil
	}
	if effect := event.GetFloorEffect(); effect != nil {
		// Floor remains Python-owned through A6A. Candidate values and the
		// future Go-authoritative mode are never executable on this path.
		if effect.GetCandidateOnly() {
			return nil
		}
		if source, ok := r.core.(interactionAuthorityStream); ok &&
			source.InteractionAuthority() == mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE {
			return nil
		}
		if !r.session.IdentityMatches(effect.GetIdentity()) {
			return fmt.Errorf("voice-core floor effect identity does not match edge session")
		}
		fence, err := validateFloorEffect(effect)
		if err != nil {
			return err
		}
		floor, valid := floorStateFromProto(effect.GetFloorState())
		if !valid {
			return fmt.Errorf("voice-core floor effect state is invalid")
		}
		if err := r.session.ApplyFloorEffect(
			fence,
			floor,
			effect.GetFloorEpoch(),
			effect.GetSourceEventId(),
		); err != nil {
			return err
		}
		if r.onEvent != nil {
			r.onEvent(event)
		}
		return nil
	}
	if effect := event.GetRealtimeEffect(); effect != nil {
		// A candidate can never enter the executable path. Go authority remains
		// disabled until the A6B rollout gate is explicitly opened.
		if effect.GetCandidateOnly() {
			return nil
		}
		if source, ok := r.core.(interactionAuthorityStream); ok &&
			source.InteractionAuthority() == mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE {
			return nil
		}
		if !r.session.IdentityMatches(effect.GetIdentity()) {
			return fmt.Errorf("voice-core realtime effect identity does not match edge session")
		}
		sessionID, _, _, streamEpoch := r.session.IdentitySnapshot()
		if effect.GetSessionId() != sessionID || effect.GetStreamEpoch() != streamEpoch {
			return fmt.Errorf("voice-core realtime effect legacy identity does not match edge session")
		}
		fence, err := validateRealtimeEffect(effect)
		if err != nil {
			return err
		}
		if effect.GetEffectKind() == mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION {
			if err := r.session.ApplyCancelledGeneration(fence); err != nil {
				return err
			}
		} else if err := r.session.withActiveGeneration(fence, func() error { return nil }); err != nil {
			return err
		}
		if r.onEvent != nil {
			r.onEvent(event)
		}
		return nil
	}
	if generation := event.GetGeneration(); generation != nil {
		if !r.session.IdentityMatches(generation.GetIdentity()) {
			return fmt.Errorf("voice-core generation identity does not match edge session")
		}
		fence := Fence{
			SessionID:    r.sessionID(),
			TurnID:       generation.GetTurnId(),
			GenerationID: generation.GetGenerationId(),
			ToolEpoch:    generation.GetToolEpoch(),
		}
		if generation.GetAction() == mediav1.GenerationAction_GENERATION_ACTION_CANCEL {
			if err := r.session.ApplyCancelledGeneration(fence); err != nil {
				return err
			}
		} else if err := r.session.AdvanceGeneration(fence); err != nil {
			return err
		}
	}
	if audio := event.GetAudio(); audio != nil {
		if !r.session.IdentityMatches(audio.GetIdentity()) {
			return fmt.Errorf("voice-core audio identity does not match edge session")
		}
		sessionID, _, _, streamEpoch := r.session.IdentitySnapshot()
		frame := AudioFrame{
			SessionID:          sessionID,
			StreamEpoch:        streamEpoch,
			Sequence:           audio.GetSequence(),
			CaptureStartSample: audio.GetSourceStartSample(),
			FrameSamples:       uint64(audio.GetFrameSamples()),
			TurnID:             audio.GetTurnId(),
			GenerationID:       audio.GetGenerationId(),
			ToolEpoch:          audio.GetToolEpoch(),
			PayloadB64:         base64.StdEncoding.EncodeToString(audio.GetPcmS16Le()),
			Final:              audio.GetFinalFrame(),
		}
		if err := r.session.DeliverDownlink(frame, r.downlinkSender); errors.Is(err, ErrStaleDownlinkGeneration) {
			return nil
		} else if err != nil {
			return err
		}
	}
	if client := event.GetClient(); client != nil && client.GetType() == "assistant_state" {
		if !r.session.IdentityMatches(client.GetIdentity()) {
			return fmt.Errorf("voice-core client identity does not match edge session")
		}
		var envelope struct {
			Payload struct {
				Phase string `json:"phase"`
				State string `json:"state"`
			} `json:"payload"`
		}
		if err := json.Unmarshal(client.GetJsonPayload(), &envelope); err == nil {
			phase := envelope.Payload.Phase
			if phase == "" {
				phase = envelope.Payload.State
			}
			floor, decision, ok := pythonInteractionState(phase)
			if ok {
				r.session.MirrorPythonInteraction(
					floor,
					decision,
					phase,
					Fence{
						SessionID: r.sessionID(), TurnID: client.GetTurnId(),
						GenerationID: client.GetGenerationId(), ToolEpoch: client.GetToolEpoch(),
					},
				)
			}
		}
	}
	if r.onEvent != nil {
		r.onEvent(event)
	}
	return nil
}

func (r *VoiceCoreMediaRuntime) handleShadowObservation(observation *mediav1.ShadowObservation) {
	if r.InteractionAuthority() != mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW ||
		observation == nil || !observation.GetCandidateOnly() ||
		observation.GetContractVersion() != shadowA6AContractVersion ||
		!r.session.IdentityMatches(observation.GetIdentity()) ||
		(r.hasShadowSequence && observation.GetShadowSequence() <= r.lastShadowSequence) {
		return
	}
	shadowSequence := observation.GetShadowSequence()
	if (!r.hasShadowSequence && shadowSequence != 0) ||
		(r.hasShadowSequence && shadowSequence-r.lastShadowSequence > 1) {
		// The Python shadow lane is intentionally lossy. A sequence gap is a
		// transport discontinuity, not evidence that the Go algorithm diverged.
		// Each domain resynchronizes independently from its next complete
		// authoritative after-state so a dropped context observation cannot be
		// hidden by an unrelated Timeline update.
		r.resyncShadowSpeech = true
		r.resyncShadowOutput = true
		r.resyncShadowFloor = true
	}
	sessionID, _, _, streamEpoch := r.session.IdentitySnapshot()
	event := LiveSessionEvent{
		StreamEpoch: streamEpoch, ContractVersion: shadowA6AContractVersion,
		AuthoritativeReason: observation.GetAuthoritativeReason(),
	}
	switch observation.GetKind() {
	case mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_SPEECH_TASK_STARTED:
		expected, valid := shadowSpeechTimelineSnapshotFromObservation(
			sessionID, streamEpoch, observation,
		)
		if !valid {
			return
		}
		input := observation.GetSpeechTaskStarted()
		if input == nil || input.GetTaskEpoch() == 0 {
			return
		}
		event.Kind = LiveEventSpeechTaskStart
		event.TaskEpoch = input.GetTaskEpoch()
		event.Scenario = "speech_task_started"
		event.Authoritative = expected
		event.CompareTimeline = true
	case mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_SPEECH_SEGMENT:
		expected, valid := shadowSpeechTimelineSnapshotFromObservation(
			sessionID, streamEpoch, observation,
		)
		if !valid {
			return
		}
		input, valid := shadowSpeechSegmentFromProto(observation.GetSpeechSegment())
		if !valid {
			return
		}
		event.Kind = LiveEventSpeechSegment
		event.SpeechSegment = &input
		if observation.GetAuthoritativeAccepted() {
			event.Scenario = "speech_segment_accepted"
		} else {
			event.Scenario = "speech_segment_rejected"
		}
		event.Authoritative = expected
		event.CompareTimeline = true
	case mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_SPEECH_COMMIT:
		expected, valid := shadowSpeechTimelineSnapshotFromObservation(
			sessionID, streamEpoch, observation,
		)
		if !valid {
			return
		}
		input := observation.GetSpeechCommit()
		if input == nil || input.GetCommittedSample() == 0 {
			return
		}
		event.Kind = LiveEventSpeechCommit
		event.CommitSample = input.GetCommittedSample()
		event.Scenario = "speech_commit"
		event.Authoritative = expected
		event.CompareTimeline = true
	case mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_CONTEXT_ACTIVATED:
		input := observation.GetContextActivated()
		if input == nil || input.GetContextVersion() != observation.GetAuthoritativeContextVersion() {
			return
		}
		event.Kind = LiveEventContextActivate
		event.ContextVersion = input.GetContextVersion()
		event.Scenario = "context_activated"
		event.Authoritative = &LiveSessionSnapshot{
			SessionID: sessionID, StreamEpoch: streamEpoch,
			OutputArbiter: ShadowOutputArbiter{ContextVersion: input.GetContextVersion()},
		}
		event.CompareOutput = true
	case mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_FLOOR_DECISION:
		input := observation.GetFloorDecision()
		floor, decision, valid := shadowFloorDecisionFromProto(input)
		if !valid {
			return
		}
		fence := Fence{
			SessionID:    sessionID,
			TurnID:       input.GetTurnId(),
			GenerationID: input.GetGenerationId(),
			ToolEpoch:    input.GetToolEpoch(),
		}
		event.Kind = LiveEventAuthoritySnapshot
		event.Fence = fence
		event.Scenario = "floor_decision"
		event.Authoritative = &LiveSessionSnapshot{
			SessionID: sessionID, StreamEpoch: streamEpoch,
			Floor: floor, Generation: fence, LastDecision: decision,
		}
		event.AuthoritativeReason = observation.GetAuthoritativeReason()
		event.CompareGeneration = false
	case mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_OUTPUT_INTENT:
		input, inputValid := shadowOutputIntentFromProto(sessionID, observation.GetOutputIntent())
		accepted := observation.GetAuthoritativeAccepted()
		consumed := observation.GetAuthoritativeConsumed()
		if accepted && consumed {
			return
		}
		arbiter := observation.GetAuthoritativeOutputArbiter()
		observedAt := observation.GetObservedAtMs()
		expected := ShadowOutputArbiter{ContextVersion: observation.GetAuthoritativeContextVersion()}
		if arbiter != nil {
			var valid bool
			expected, valid = shadowOutputArbiterFromProto(sessionID, arbiter)
			if !valid || expected.ContextVersion != observation.GetAuthoritativeContextVersion() ||
				observedAt == 0 || observedAt > math.MaxInt64 ||
				!shadowOutputArbiterActiveAt(expected, int64(observedAt)) {
				return
			}
		} else if !inputValid || observation.GetObservedAtMs() == 0 || observation.GetObservedAtMs() > math.MaxInt64 {
			// Legacy observations have no after-state from which to compare a
			// malformed rejection. Keep the old fail-closed behavior.
			return
		}
		if observedAt == 0 || observedAt > math.MaxInt64 {
			if accepted {
				return
			}
			observedAt = uint64(time.Now().UnixMilli())
		}
		if consumed && (!inputValid || arbiter == nil || !expected.CandidatesComplete ||
			input.CreatedAtUnixMillis <= 0 || input.CreatedAtUnixMillis > int64(observedAt) ||
			input.ExpiresAtUnixMillis <= input.CreatedAtUnixMillis ||
			input.ContextVersion != observation.GetAuthoritativeContextVersion() ||
			input.FloorRequirement != ShadowOutputFloorAvailable ||
			input.PlaybackRequirement != ShadowOutputPlaybackCurrentGeneration) {
			return
		}
		event.Kind = LiveEventOutputIntent
		event.ObservedAtUnixMS = int64(observedAt)
		event.Scenario = "output_intent_accepted"
		if consumed {
			event.Scenario = "output_intent_consumed"
		} else if !accepted {
			event.Scenario = "output_intent_rejected"
		}
		if !inputValid {
			if accepted || arbiter == nil {
				return
			}
			currentFence, _ := r.session.GenerationSnapshot()
			event.Fence = currentFence
			event.OutputIntentInvalid = true
			event.AuthoritativeOutputState = true
		} else {
			event.Fence = input.Fence
			event.OutputIntent = &input
		}
		if arbiter != nil {
			event.AuthoritativeOutputState = true
			event.AuthoritativeOutputAccepted = accepted
			event.AuthoritativeOutputConsumed = consumed
		} else if accepted {
			candidate := input
			rank, ranked := shadowOutputDomainRank(&candidate)
			if !ranked {
				return
			}
			candidate.DomainRank = rank
			candidate.CandidateOnly = true
			expected.Candidate = &candidate
			event.AuthoritativeOutputAccepted = true
			event.ConsumeOutput = true
		} else {
			event.ConsumeOutput = true
		}
		event.Authoritative = &LiveSessionSnapshot{
			SessionID: sessionID, StreamEpoch: streamEpoch, OutputArbiter: expected,
		}
		event.CompareOutput = true
	default:
		return
	}
	resyncSpeech := event.CompareTimeline && r.resyncShadowSpeech
	if resyncSpeech {
		event.ResyncFromAuthoritative = true
		event.Scenario = "shadow_transport_discontinuity"
	}
	resyncOutput := event.CompareOutput && r.resyncShadowOutput
	if resyncOutput && !event.Authoritative.OutputArbiter.CandidatesComplete {
		// A winner-only snapshot cannot reconstruct the candidates that may
		// have been lost in transit. Keep the domain pending until Python sends
		// a complete bounded after-state.
		return
	}
	if resyncOutput {
		event.ResyncFromAuthoritative = true
		event.Scenario = "shadow_transport_discontinuity"
	}
	resyncFloor := event.Authoritative != nil && event.Authoritative.Floor != "" && r.resyncShadowFloor
	if resyncFloor {
		event.ResyncFromAuthoritative = true
		event.Scenario = "shadow_transport_discontinuity"
	}
	if err := r.session.ObserveShadow(event); err != nil {
		return
	}
	if resyncSpeech {
		r.resyncShadowSpeech = false
	}
	if resyncOutput {
		r.resyncShadowOutput = false
	}
	if resyncFloor {
		r.resyncShadowFloor = false
	}
	r.lastShadowSequence = observation.GetShadowSequence()
	r.hasShadowSequence = true
}
