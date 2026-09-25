package mediaedge

import (
	"encoding/base64"
	"errors"
	"fmt"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

// Core-to-edge dispatch owns identity validation. Runtime construction and
// edge-to-core writes stay in bridge_runtime.go.

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
	if event.GetShadowObservation() != nil {
		// Python stays authoritative; candidate-only shadow observations have
		// no consumer on this edge and are ignored.
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
		if err := r.session.ApplyFloorEffect(fence, floor, effect.GetFloorEpoch()); err != nil {
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
			SessionEpoch: generation.GetSessionEpoch(),
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
			SessionEpoch:       audio.GetSessionEpoch(),
			PayloadB64:         base64.StdEncoding.EncodeToString(audio.GetPcmS16Le()),
			Final:              audio.GetFinalFrame(),
		}
		if err := r.session.DeliverDownlink(frame, r.downlinkSender); errors.Is(err, ErrStaleDownlinkGeneration) {
			return nil
		} else if err != nil {
			return err
		}
	}
	if client := event.GetClient(); client != nil && client.GetType() == "assistant_state" &&
		!r.session.IdentityMatches(client.GetIdentity()) {
		return fmt.Errorf("voice-core client identity does not match edge session")
	}
	if r.onEvent != nil {
		r.onEvent(event)
	}
	return nil
}

func floorStateFromProto(input mediav1.FloorState) (FloorState, bool) {
	switch input {
	case mediav1.FloorState_FLOOR_STATE_SILENCE:
		return FloorSilence, true
	case mediav1.FloorState_FLOOR_STATE_USER_HOLDS_FLOOR:
		return FloorUser, true
	case mediav1.FloorState_FLOOR_STATE_ASSISTANT_HOLDS_FLOOR:
		return FloorAssistant, true
	case mediav1.FloorState_FLOOR_STATE_OVERLAP:
		return FloorOverlap, true
	case mediav1.FloorState_FLOOR_STATE_UNCERTAIN:
		return FloorUncertain, true
	default:
		return "", false
	}
}
