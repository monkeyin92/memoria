package mediaedge

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/pion/webrtc/v4"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

// forwardCoreEvent is the browser projection seam. WHIP/RTP lifecycle stays
// in the terminator; this file owns the Core-to-client event mapping only.
func (p *webRTCPeer) forwardCoreEvent(event *mediav1.CoreToMedia) {
	if event == nil || p.closed.Load() {
		return
	}
	typeValue := ""
	turnID, generationID, toolEpoch := uint64(0), uint64(0), uint64(0)
	taskEpoch, contextVersion := uint64(0), uint64(0)
	payload := map[string]any{}
	switch {
	case event.GetAccepted() != nil:
		accepted := event.GetAccepted()
		typeValue = "session.ready"
		turnID, generationID, toolEpoch = accepted.GetCurrentTurnId(), accepted.GetCurrentGenerationId(), accepted.GetCurrentToolEpoch()
		taskEpoch, contextVersion = accepted.GetTaskEpoch(), accepted.GetContextVersion()
		payload["state"] = "ready"
		switch accepted.GetInteractionAuthority() {
		case mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW:
			payload["interaction_authority"] = "go_shadow"
		case mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE:
			payload["interaction_authority"] = "go_authoritative"
		default:
			payload["interaction_authority"] = "python_authoritative"
		}
	case event.GetAudio() != nil:
		audio := event.GetAudio()
		typeValue = "assistant.audio.frame"
		turnID, generationID, toolEpoch = audio.GetTurnId(), audio.GetGenerationId(), audio.GetToolEpoch()
		taskEpoch, contextVersion = audio.GetTaskEpoch(), audio.GetContextVersion()
		payload = map[string]any{
			"sequence": audio.GetSequence(), "source_start_sample": audio.GetSourceStartSample(),
			"frame_samples": audio.GetFrameSamples(), "final": audio.GetFinalFrame(),
		}
	case event.GetGeneration() != nil:
		generation := event.GetGeneration()
		turnID, generationID, toolEpoch = generation.GetTurnId(), generation.GetGenerationId(), generation.GetToolEpoch()
		taskEpoch, contextVersion = generation.GetTaskEpoch(), generation.GetContextVersion()
		if generation.GetAction() == mediav1.GenerationAction_GENERATION_ACTION_CANCEL {
			p.sendPlaybackFlush(Fence{
				SessionID: p.request.SessionID, TurnID: turnID,
				GenerationID: generationID, ToolEpoch: toolEpoch,
				SessionEpoch: generation.GetSessionEpoch(),
			}, generation.GetReason())
			return
		}
		typeValue = "assistant.generation"
		payload = map[string]any{"action": generation.GetAction().String(), "reason": generation.GetReason()}
	case event.GetRealtimeEffect() != nil:
		effect := event.GetRealtimeEffect()
		turnID, generationID, toolEpoch = effect.GetTurnId(), effect.GetGenerationId(), effect.GetToolEpoch()
		taskEpoch, contextVersion = effect.GetTaskEpoch(), effect.GetContextVersion()
		switch effect.GetEffectKind() {
		case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT,
			mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_PAUSE_OUTPUT:
			typeValue = "playback.duck"
		case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_RESUME_OUTPUT:
			typeValue = "playback.restore"
		case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION:
			p.sendPlaybackFlush(Fence{
				SessionID: p.request.SessionID, TurnID: turnID,
				GenerationID: generationID, ToolEpoch: toolEpoch,
				SessionEpoch: effect.GetSessionEpoch(),
			}, effect.GetSourceEventId())
			return
		default:
			return
		}
		payload = map[string]any{"source_event_id": effect.GetSourceEventId()}
		if gain, ok := realtimeEffectGain(effect); ok {
			payload["gain"] = gain
		}
	case event.GetFloorEffect() != nil:
		effect := event.GetFloorEffect()
		floorState, ok := floorStateName(effect.GetFloorState())
		if !ok || effect.GetFloorEpoch() == 0 {
			return
		}
		typeValue = "floor.state"
		turnID, generationID, toolEpoch = effect.GetTurnId(), effect.GetGenerationId(), effect.GetToolEpoch()
		taskEpoch, contextVersion = effect.GetTaskEpoch(), effect.GetContextVersion()
		payload = map[string]any{
			"floor_state": floorState, "floor_epoch": effect.GetFloorEpoch(),
			"source_event_id": effect.GetSourceEventId(),
		}
	case event.GetTranscript() != nil:
		transcript := event.GetTranscript()
		turnID, generationID, toolEpoch = transcript.GetTurnId(), transcript.GetGenerationId(), transcript.GetToolEpoch()
		taskEpoch, contextVersion = transcript.GetTaskEpoch(), transcript.GetContextVersion()
		typeValue = "user.transcript.partial"
		if transcript.GetFinal() {
			typeValue = "user.transcript.final"
		}
		payload = map[string]any{
			"text": transcript.GetText(), "revision": transcript.GetRevision(),
			"capture_start_sample": transcript.GetCaptureStartSample(),
			"capture_end_sample":   transcript.GetCaptureEndSample(), "final": transcript.GetFinal(),
			"confidence": transcript.GetConfidence(), "speaker_class": transcript.GetSpeakerClass(),
			"loss_concealed": transcript.GetLossConcealed(),
		}
	case event.GetState() != nil:
		state := event.GetState()
		typeValue = "assistant.state"
		turnID, generationID, toolEpoch = state.GetTurnId(), state.GetGenerationId(), state.GetToolEpoch()
		taskEpoch, contextVersion = state.GetTaskEpoch(), state.GetContextVersion()
		current := p.currentFence()
		if toolEpoch == 0 && current.TurnID == turnID && current.GenerationID == generationID {
			toolEpoch = current.ToolEpoch
		}
		payload = map[string]any{"state": conversationStateName(state.GetState()), "reason": state.GetReason()}
	case event.GetClient() != nil:
		p.forwardClientEvent(event.GetClient())
		return
	case event.GetError() != nil:
		coreError := event.GetError()
		typeValue = "error"
		turnID, generationID, toolEpoch = coreError.GetTurnId(), coreError.GetGenerationId(), coreError.GetToolEpoch()
		taskEpoch, contextVersion = coreError.GetTaskEpoch(), coreError.GetContextVersion()
		payload = map[string]any{"code": coreError.GetCode(), "message": coreError.GetMessage(), "retryable": coreError.GetRetryable()}
	}
	if typeValue != "" {
		p.sendEnvelope(typeValue, turnID, generationID, toolEpoch, taskEpoch, contextVersion, payload)
	}
}

func realtimeEffectGain(effect *mediav1.RealtimeEffect) (float64, bool) {
	if effect == nil || effect.GetEffectKind() != mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT {
		return 0, false
	}
	var payload struct {
		Gain *float64 `json:"gain"`
	}
	if err := json.Unmarshal(effect.GetPayload(), &payload); err != nil || payload.Gain == nil ||
		*payload.Gain < 0 || *payload.Gain > 1 {
		return 0, false
	}
	return *payload.Gain, true
}

func (p *webRTCPeer) sendPlaybackFlush(fence Fence, reason string) {
	p.flushMu.Lock()
	if p.lastFlush.Equal(fence) {
		p.flushMu.Unlock()
		return
	}
	p.lastFlush = fence
	p.flushMu.Unlock()
	p.sendEnvelope(
		"playback.flush", fence.TurnID, fence.GenerationID, fence.ToolEpoch,
		0, 0, map[string]any{"reason": reason},
	)
}

func (p *webRTCPeer) forwardClientEvent(event *mediav1.ClientEvent) {
	if event == nil || len(event.GetJsonPayload()) == 0 {
		return
	}
	var upstream mediaDataEnvelope
	if err := json.Unmarshal(event.GetJsonPayload(), &upstream); err != nil || upstream.Type != event.GetType() {
		p.terminator.report(fmt.Errorf("invalid Voice Core client event"))
		return
	}
	var payload map[string]any
	if err := json.Unmarshal(upstream.Payload, &payload); err != nil {
		p.terminator.report(fmt.Errorf("invalid Voice Core client payload"))
		return
	}
	taskEpoch, contextVersion := event.GetTaskEpoch(), event.GetContextVersion()
	if upstream.TaskEpoch != 0 {
		if taskEpoch != 0 && taskEpoch != upstream.TaskEpoch {
			p.terminator.report(fmt.Errorf("mismatched Voice Core task epoch"))
			return
		}
		taskEpoch = upstream.TaskEpoch
	}
	if upstream.ContextVersion != 0 {
		if contextVersion != 0 && contextVersion != upstream.ContextVersion {
			p.terminator.report(fmt.Errorf("mismatched Voice Core context version"))
			return
		}
		contextVersion = upstream.ContextVersion
	}
	p.sendEnvelope(
		event.GetType(), event.GetTurnId(), event.GetGenerationId(), event.GetToolEpoch(),
		taskEpoch, contextVersion, payload,
	)
}

func conversationStateName(state mediav1.ConversationState) string {
	switch state {
	case mediav1.ConversationState_CONVERSATION_STATE_CONNECTING:
		return "connecting"
	case mediav1.ConversationState_CONVERSATION_STATE_LISTENING:
		return "listening"
	case mediav1.ConversationState_CONVERSATION_STATE_USER_SPEAKING:
		return "user_speaking"
	case mediav1.ConversationState_CONVERSATION_STATE_FINALIZING:
		return "eot_pending"
	case mediav1.ConversationState_CONVERSATION_STATE_THINKING:
		return "thinking"
	case mediav1.ConversationState_CONVERSATION_STATE_ASSISTANT_SPEAKING:
		return "speaking"
	case mediav1.ConversationState_CONVERSATION_STATE_INTERRUPT_PENDING:
		return "interruption_pending"
	case mediav1.ConversationState_CONVERSATION_STATE_RECOVERING:
		return "recovering"
	case mediav1.ConversationState_CONVERSATION_STATE_CLOSED:
		return "closed"
	default:
		return "connecting"
	}
}

func floorStateName(state mediav1.FloorState) (string, bool) {
	switch state {
	case mediav1.FloorState_FLOOR_STATE_SILENCE:
		return "silence", true
	case mediav1.FloorState_FLOOR_STATE_USER_HOLDS_FLOOR:
		return "user_holds_floor", true
	case mediav1.FloorState_FLOOR_STATE_ASSISTANT_HOLDS_FLOOR:
		return "assistant_holds_floor", true
	case mediav1.FloorState_FLOOR_STATE_OVERLAP:
		return "overlap", true
	case mediav1.FloorState_FLOOR_STATE_UNCERTAIN:
		return "uncertain", true
	default:
		return "", false
	}
}

func (p *webRTCPeer) sendEnvelope(
	typeValue string,
	turnID, generationID, toolEpoch, taskEpoch, contextVersion uint64,
	payload map[string]any,
) {
	current := p.currentFence()
	sessionEpoch := uint64(0)
	if current.TurnID == turnID && current.GenerationID == generationID && current.ToolEpoch == toolEpoch {
		sessionEpoch = current.SessionEpoch
	}
	p.channelMu.Lock()
	lane := dataChannelLaneForEvent(typeValue)
	sequence := p.eventSeq
	p.eventSeq++
	envelope := map[string]any{
		"v": ProtocolVersion, "protocol": "media-v1", "type": typeValue,
		"event_id":   fmt.Sprintf("%s:%d:%d", p.request.SessionID, p.request.StreamEpoch, sequence),
		"session_id": p.request.SessionID, "stream_epoch": p.request.StreamEpoch,
		"sequence": sequence, "turn_id": turnID, "generation_id": generationID,
		"tool_epoch": toolEpoch, "session_epoch": sessionEpoch,
		"task_epoch": taskEpoch, "context_version": contextVersion,
		"server_monotonic_ms": uint64(time.Since(mediaProcessStartedAt) / time.Millisecond),
		"payload":             payload,
	}
	raw, err := json.Marshal(envelope)
	if err != nil {
		p.channelMu.Unlock()
		p.terminator.report(err)
		return
	}
	channel := p.channels[lane]
	if (channel == nil || channel.ReadyState() != webrtc.DataChannelStateOpen) && lane != dataChannelControl {
		legacy := p.channels[dataChannelControl]
		if legacy != nil && legacy.ReadyState() == webrtc.DataChannelStateOpen {
			channel = legacy
		}
	}
	if channel == nil || channel.ReadyState() != webrtc.DataChannelStateOpen {
		if lane == dataChannelEphemeral {
			ephemeral := 0
			for _, event := range p.pending {
				if event.lane == dataChannelEphemeral {
					ephemeral++
				}
			}
			if ephemeral >= maxPendingEphemeral {
				p.pending, _ = dropOldestPendingLane(p.pending, dataChannelEphemeral)
			}
		}
		if len(p.pending) >= maxPendingDataEvents {
			var evicted bool
			p.pending, evicted = dropOldestPendingLane(p.pending, dataChannelEphemeral)
			if !evicted {
				p.channelMu.Unlock()
				go p.terminator.failPeer(p, fmt.Errorf("reliable DataChannel event queue is full"))
				return
			}
		}
		if len(p.pending) >= maxPendingDataEvents {
			p.channelMu.Unlock()
			go p.terminator.failPeer(p, fmt.Errorf("DataChannel event queue is full"))
			return
		}
		p.pending = append(p.pending, pendingDataEvent{lane: lane, raw: raw})
		p.channelMu.Unlock()
		return
	}
	if err := channel.Send(raw); err != nil {
		p.channelMu.Unlock()
		if lane == dataChannelEphemeral {
			p.terminator.report(err)
		} else {
			p.terminator.failPeer(p, err)
		}
		return
	}
	p.channelMu.Unlock()
}
