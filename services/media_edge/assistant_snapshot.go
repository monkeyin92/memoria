package mediaedge

import (
	"encoding/json"
	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
	"time"
)

type AssistantSnapshot struct {
	Phase       string      `json:"phase"`
	State       string      `json:"state"`
	Sequence    uint64      `json:"sequence"`
	SessionID   string      `json:"session_id"`
	StreamEpoch uint64      `json:"stream_epoch"`
	Fence       deviceFence `json:"fence"`
	ObservedAt  time.Time   `json:"observed_at"`
}

func (c *DeviceConnection) observeAssistant(client *mediav1.ClientEvent) {
	identity := client.GetIdentity()
	if identity.GetSessionId() != c.sessionID || identity.GetStreamEpoch() != uint64(c.epoch) || client.GetSequence() == 0 {
		return
	}
	var envelope struct {
		Payload struct {
			Phase string `json:"phase"`
			State string `json:"state"`
		} `json:"payload"`
		SessionEpoch uint64 `json:"session_epoch"`
	}
	if json.Unmarshal(client.GetJsonPayload(), &envelope) != nil || envelope.SessionEpoch == 0 {
		return
	}
	switch envelope.Payload.Phase {
	case "connecting", "listening", "user_speaking", "backchannel", "thinking_silent", "speaking", "interrupted", "tool_waiting", "recovering", "closed":
	default:
		return
	}
	c.stateMu.Lock()
	defer c.stateMu.Unlock()
	if c.assistantSnapshot != nil && (client.GetSequence() <= c.assistantSnapshot.Sequence ||
		envelope.SessionEpoch < c.assistantSnapshot.Fence.SessionEpoch ||
		client.GetGenerationId() < c.assistantSnapshot.Fence.GenerationID) {
		return
	}
	if envelope.SessionEpoch < c.currentFence.SessionEpoch || client.GetGenerationId() < c.currentFence.GenerationID {
		return
	}
	c.assistantSnapshot = &AssistantSnapshot{Phase: envelope.Payload.Phase, State: envelope.Payload.State, Sequence: client.GetSequence(), SessionID: c.sessionID, StreamEpoch: uint64(c.epoch), Fence: deviceFence{TurnID: client.GetTurnId(), GenerationID: client.GetGenerationId(), ToolEpoch: client.GetToolEpoch(), SessionEpoch: envelope.SessionEpoch}, ObservedAt: time.Now().UTC()}
}
