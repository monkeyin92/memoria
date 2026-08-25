package mediaedge

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"time"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

func (s *VoiceCoreSession) Recv() (*mediav1.CoreToMedia, error) {
	for {
		event, err := s.stream.Recv()
		if err != nil {
			return nil, err
		}
		if err := s.validateCoreEvent(event); errors.Is(err, errDropShadowObservation) ||
			errors.Is(err, errDropRealtimeEffect) || errors.Is(err, errDropFloorEffect) {
			continue
		} else if err != nil {
			return nil, err
		}
		return event, nil
	}
}

func (s *VoiceCoreSession) validateCoreEvent(event *mediav1.CoreToMedia) error {
	if event == nil {
		return fmt.Errorf("voice-core bridge returned an empty event")
	}
	if accepted := event.GetAccepted(); accepted != nil {
		if !s.identity.equal(accepted.GetIdentity()) {
			return fmt.Errorf("accepted event identity does not match")
		}
		return nil
	}
	if audio := event.GetAudio(); audio != nil {
		if !s.identity.equal(audio.GetIdentity()) {
			return fmt.Errorf("audio event identity does not match")
		}
		if audio.GetFrameSamples() == 0 || len(audio.GetPcmS16Le())%2 != 0 ||
			uint64(len(audio.GetPcmS16Le())/2) != uint64(audio.GetFrameSamples()) {
			return fmt.Errorf("audio PCM payload does not match frame samples")
		}
		s.stateMu.Lock()
		defer s.stateMu.Unlock()
		actual := Fence{SessionID: s.identity.SessionID, TurnID: audio.GetTurnId(), GenerationID: audio.GetGenerationId(), ToolEpoch: audio.GetToolEpoch(), SessionEpoch: audio.GetSessionEpoch()}
		if !s.current.equal(actual) {
			return fmt.Errorf("stale audio generation")
		}
		if s.hasAudioSequence && audio.GetSequence() <= s.lastAudioSequence {
			return fmt.Errorf("stale audio sequence")
		}
		if s.hasAudioSequence && audio.GetSequence() != s.lastAudioSequence+1 {
			return fmt.Errorf("audio sequence has a gap")
		}
		if s.hasAudioSequence && audio.GetSourceStartSample() < s.lastAudioEnd {
			return fmt.Errorf("audio sample range moved backwards")
		}
		if s.hasAudioSequence && audio.GetSourceStartSample() != s.lastAudioEnd {
			return fmt.Errorf("audio sample range has a gap")
		}
		if !s.hasAudioSequence && s.requireAudioOrigin &&
			(audio.GetSequence() != 0 || audio.GetSourceStartSample() != 0) {
			return fmt.Errorf("first audio frame must start at sequence and sample zero")
		}
		s.lastAudioSequence = audio.GetSequence()
		s.hasAudioSequence = true
		end := audio.GetSourceStartSample() + uint64(audio.GetFrameSamples())
		if end < audio.GetSourceStartSample() {
			return fmt.Errorf("audio sample range overflow")
		}
		s.lastAudioEnd = end
		return nil
	}
	if generation := event.GetGeneration(); generation != nil {
		if !s.identity.equal(generation.GetIdentity()) {
			return fmt.Errorf("generation event identity does not match")
		}
		s.stateMu.Lock()
		defer s.stateMu.Unlock()
		actual := Fence{SessionID: s.identity.SessionID, TurnID: generation.GetTurnId(), GenerationID: generation.GetGenerationId(), ToolEpoch: generation.GetToolEpoch(), SessionEpoch: generation.GetSessionEpoch()}
		if !s.current.monotonic(actual) {
			return fmt.Errorf("generation moved backwards")
		}
		if generation.GetAction() == mediav1.GenerationAction_GENERATION_ACTION_RESUME &&
			!s.current.equal(actual) {
			return fmt.Errorf("generation resume fence does not match acceptance")
		}
		if err := s.acceptEventSequence(generation.GetSequence()); err != nil {
			return err
		}
		if !s.current.equal(actual) {
			// Downlink sequence and sample ranges start at zero for every new
			// generation; never let the prior answer poison the new fence.
			s.hasAudioSequence = false
			s.lastAudioSequence = 0
			s.lastAudioEnd = 0
			// RESUME takes over an in-flight generation after reconnect;
			// START/CANCEL/COMPLETE begin a fresh media range.
			s.requireAudioOrigin = generation.GetAction() != mediav1.GenerationAction_GENERATION_ACTION_RESUME
		}
		s.current = actual
		s.currentActive = generation.GetAction() != mediav1.GenerationAction_GENERATION_ACTION_CANCEL
		return nil
	}
	if effect := event.GetRealtimeEffect(); effect != nil {
		// Candidate values are telemetry even when an upstream implementation
		// accidentally uses the executable oneof. A6B is still fail-closed.
		if effect.GetCandidateOnly() ||
			s.interactionAuthority == mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE {
			return errDropRealtimeEffect
		}
		if !s.identity.equal(effect.GetIdentity()) ||
			effect.GetSessionId() != s.identity.SessionID ||
			effect.GetStreamEpoch() != s.identity.StreamEpoch {
			return fmt.Errorf("realtime effect identity does not match")
		}
		fence, err := validateRealtimeEffect(effect)
		if err != nil {
			return err
		}
		s.stateMu.Lock()
		defer s.stateMu.Unlock()
		if effect.GetEffectKind() == mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION {
			if fence.TurnID != s.current.TurnID || fence.ToolEpoch != s.current.ToolEpoch ||
				s.current.GenerationID == math.MaxUint64 || fence.GenerationID != s.current.GenerationID+1 {
				return fmt.Errorf("realtime cancel effect fence is stale")
			}
		} else if !s.current.Equal(fence) {
			return fmt.Errorf("realtime effect fence is stale")
		}
		if err := s.acceptEventSequence(effect.GetSequence()); err != nil {
			return err
		}
		if effect.GetEffectKind() == mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION {
			s.current = fence
			s.currentActive = false
		}
		return nil
	}
	if effect := event.GetFloorEffect(); effect != nil {
		if effect.GetCandidateOnly() ||
			s.interactionAuthority == mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE {
			return errDropFloorEffect
		}
		if !s.identity.equal(effect.GetIdentity()) {
			return fmt.Errorf("floor effect identity does not match")
		}
		fence, err := validateFloorEffect(effect)
		if err != nil {
			return err
		}
		s.stateMu.Lock()
		defer s.stateMu.Unlock()
		if !s.current.Equal(fence) {
			return fmt.Errorf("floor effect fence is stale")
		}
		if s.hasFloorEpoch && effect.GetFloorEpoch() <= s.lastFloorEpoch {
			// The current floor projection already dominates this duplicate or
			// late event. Drop it without advancing the shared event sequence;
			// closing the stream here would also discard a following typed CLOSED
			// state and strand the device in listening mode.
			return fmt.Errorf(
				"%w: floor effect epoch is stale: got=%d last=%d",
				errDropFloorEffect,
				effect.GetFloorEpoch(),
				s.lastFloorEpoch,
			)
		}
		if err := s.acceptEventSequence(effect.GetSequence()); err != nil {
			return err
		}
		s.lastFloorEpoch = effect.GetFloorEpoch()
		s.hasFloorEpoch = true
		return nil
	}
	if transcript := event.GetTranscript(); transcript != nil {
		if !s.identity.equal(transcript.GetIdentity()) {
			return fmt.Errorf("transcript event identity does not match")
		}
		return s.acceptEventSequence(transcript.GetSequence())
	}
	if observation := event.GetShadowObservation(); observation != nil {
		if s.interactionAuthority != mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW ||
			!s.identity.equal(observation.GetIdentity()) || !observation.GetCandidateOnly() ||
			observation.GetContractVersion() != shadowA6AContractVersion {
			return errDropShadowObservation
		}
		s.stateMu.Lock()
		defer s.stateMu.Unlock()
		if (s.hasEventSequence && observation.GetSequence() <= s.lastEventSequence) ||
			(s.hasShadowSequence && observation.GetShadowSequence() <= s.lastShadowSequence) {
			return errDropShadowObservation
		}
		s.lastEventSequence = observation.GetSequence()
		s.hasEventSequence = true
		s.lastShadowSequence = observation.GetShadowSequence()
		s.hasShadowSequence = true
		return nil
	}
	if state := event.GetState(); state != nil {
		if !s.identity.equal(state.GetIdentity()) {
			return fmt.Errorf("state event identity does not match")
		}
		if len(state.GetReason()) > 128 ||
			(state.GetState() == mediav1.ConversationState_CONVERSATION_STATE_CLOSED &&
				state.GetReason() == "") {
			return fmt.Errorf("state event reason is invalid")
		}
		return s.acceptEventSequence(state.GetSequence())
	}
	if client := event.GetClient(); client != nil {
		if !s.identity.equal(client.GetIdentity()) {
			return fmt.Errorf("client event identity does not match")
		}
		return s.acceptEventSequence(client.GetSequence())
	}
	if coreError := event.GetError(); coreError != nil {
		if !s.identity.equal(coreError.GetIdentity()) {
			return fmt.Errorf("core error identity does not match")
		}
		return nil
	}
	return fmt.Errorf("voice-core bridge returned an unknown event")
}

func (s *VoiceCoreSession) acceptEventSequence(sequence uint64) error {
	if s.hasEventSequence && sequence <= s.lastEventSequence {
		return fmt.Errorf("stale core event sequence")
	}
	s.lastEventSequence = sequence
	s.hasEventSequence = true
	return nil
}

func validateRealtimeEffect(effect *mediav1.RealtimeEffect) (Fence, error) {
	if effect == nil || effect.GetEffectId() == "" || len(effect.GetEffectId()) > 256 ||
		effect.GetSourceEventId() == "" || len(effect.GetSourceEventId()) > 128 {
		return Fence{}, fmt.Errorf("realtime effect id and source event are required")
	}
	switch effect.GetEffectKind() {
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT,
		mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION,
		mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_PAUSE_OUTPUT,
		mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_RESUME_OUTPUT:
	default:
		return Fence{}, fmt.Errorf("realtime effect kind is not executable")
	}
	payload := effect.GetPayload()
	if len(payload) == 0 || len(payload) > maxRealtimeEffectPayloadBytes || !json.Valid(payload) {
		return Fence{}, fmt.Errorf("realtime effect payload is invalid")
	}
	var decoded map[string]json.RawMessage
	if err := json.Unmarshal(payload, &decoded); err != nil || decoded == nil {
		return Fence{}, fmt.Errorf("realtime effect payload must be an object")
	}
	return Fence{
		SessionID: effect.GetSessionId(), TurnID: effect.GetTurnId(),
		GenerationID: effect.GetGenerationId(), ToolEpoch: effect.GetToolEpoch(),
		SessionEpoch: effect.GetSessionEpoch(),
	}, nil
}

func validateFloorEffect(effect *mediav1.FloorEffect) (Fence, error) {
	if effect == nil || effect.GetEffectId() == "" || len(effect.GetEffectId()) > 256 ||
		effect.GetSourceEventId() == "" || len(effect.GetSourceEventId()) > 128 ||
		effect.GetFloorEpoch() == 0 {
		return Fence{}, fmt.Errorf("floor effect id, source event and epoch are required")
	}
	switch effect.GetFloorState() {
	case mediav1.FloorState_FLOOR_STATE_USER_HOLDS_FLOOR,
		mediav1.FloorState_FLOOR_STATE_ASSISTANT_HOLDS_FLOOR,
		mediav1.FloorState_FLOOR_STATE_OVERLAP,
		mediav1.FloorState_FLOOR_STATE_UNCERTAIN,
		mediav1.FloorState_FLOOR_STATE_SILENCE:
	default:
		return Fence{}, fmt.Errorf("floor effect state is invalid")
	}
	now := uint64(time.Now().UnixMilli())
	if effect.GetExpiresAtMs() <= now || effect.GetExpiresAtMs()-now > maxFloorEffectTTLMS {
		return Fence{}, fmt.Errorf("floor effect is expired or exceeds TTL")
	}
	return Fence{
		SessionID: effect.GetIdentity().GetSessionId(), TurnID: effect.GetTurnId(),
		GenerationID: effect.GetGenerationId(), ToolEpoch: effect.GetToolEpoch(),
		SessionEpoch: effect.GetSessionEpoch(),
	}, nil
}

func (s *VoiceCoreSession) CloseSend() error {
	return s.stream.CloseSend()
}

// Close cancels this stream.  The parent bridge connection remains reusable.
func (s *VoiceCoreSession) Close() error {
	if s.cancel != nil {
		s.cancel()
	}
	return s.stream.CloseSend()
}

// PlaybackProgress is the Go-side equivalent of the media-v1 playback ACK.
