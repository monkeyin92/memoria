package mediaedge

import (
	"encoding/json"
	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
	"net/http"
	"net/http/httptest"
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

func TestAssistantSnapshotThroughAuthenticatedHTTP(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := connectAt(t, env, 18)
	defer connection.Close()
	server := NewServer(JWTVerifier{}, 4)
	server.DeviceWSS = env.server
	server.InternalControlToken = "software-window-local-control-token"
	httpServer := httptest.NewServer(server.InternalHandler())
	defer httpServer.Close()
	event := &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Client{Client: &mediav1.ClientEvent{Identity: &mediav1.SessionIdentity{SessionId: "session_1", StreamEpoch: 18}, Type: "assistant_state", Sequence: 1, GenerationId: 1, JsonPayload: []byte(`{"payload":{"phase":"speaking","state":"listening_closed"},"session_epoch":1}`)}}}
	env.mu.Lock()
	request := env.requests["session_1"]
	env.mu.Unlock()
	env.server.ForwardCoreEventForSession(request, event)
	for _, authorized := range []bool{false, true} {
		req, err := http.NewRequest(http.MethodGet, httpServer.URL+"/v1/internal/device-runtime/status?device_id=dev_1", nil)
		if err != nil {
			t.Fatal(err)
		}
		if authorized {
			req.Header.Set(internalControlTokenHeader, server.InternalControlToken)
		}
		response, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		if !authorized {
			response.Body.Close()
			if response.StatusCode != 401 {
				t.Fatal(response.StatusCode)
			}
			continue
		}
		var status DeviceRuntimeStatus
		err = json.NewDecoder(response.Body).Decode(&status)
		response.Body.Close()
		if err != nil || response.StatusCode != 200 || status.AssistantState == nil || status.AssistantState.Phase != "speaking" {
			t.Fatalf("snapshot %+v, err %v", status, err)
		}
	}
	connection.Close()
	waitReleased(t, env, "snapshot disconnect")
	if env.server.RuntimeStatus("dev_1").AssistantState != nil {
		t.Fatal("disconnected snapshot retained")
	}
	replacement, _ := connectAt(t, env, 19)
	defer replacement.Close()
	if env.server.RuntimeStatus("dev_1").AssistantState != nil {
		t.Fatal("replacement inherited old phase")
	}
}
