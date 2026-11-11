package mediaedge

import (
	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
	"testing"
)

func TestAssistantSnapshotRejectsOldStreamSequenceAndGeneration(t *testing.T) {
	c := &DeviceConnection{sessionID: "session", epoch: 3, currentFence: deviceFence{GenerationID: 4}}
	event := &mediav1.ClientEvent{Identity: &mediav1.SessionIdentity{SessionId: "session", StreamEpoch: 3}, Type: "assistant_state", Sequence: 2, GenerationId: 4, JsonPayload: []byte(`{"payload":{"phase":"thinking_silent","state":"listening_closed"},"session_epoch":1}`)}
	c.observeAssistant(event)
	if c.assistantSnapshot == nil || c.assistantSnapshot.Phase != "thinking_silent" {
		t.Fatal("missing Python phase")
	}
	event.Sequence = 1
	c.observeAssistant(event)
	if c.assistantSnapshot.Sequence != 2 {
		t.Fatal("old sequence replaced state")
	}
	event.Sequence = 3
	event.GenerationId = 3
	c.observeAssistant(event)
	if c.assistantSnapshot.Sequence != 2 {
		t.Fatal("old generation replaced state")
	}
	event.GenerationId = 4
	event.Identity.StreamEpoch = 2
	c.observeAssistant(event)
	if c.assistantSnapshot.Sequence != 2 {
		t.Fatal("old stream replaced state")
	}
}
