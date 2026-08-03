package mediaedge

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

func testFrame(sessionID string, epoch, seq uint64, generation uint64) AudioFrame {
	return AudioFrame{
		SessionID: sessionID, StreamEpoch: epoch, Sequence: seq,
		CaptureStartSample: seq * 160, FrameSamples: 160,
		GenerationID: generation, PayloadB64: base64.StdEncoding.EncodeToString(make([]byte, 320)),
	}
}

func TestGenerationGateAndReconnect(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{SessionID: "s", AccountID: "a", DeviceID: "d", StreamEpoch: 1}, 2)
	if err != nil {
		t.Fatal(err)
	}
	if err := session.AcceptDownlink(testFrame("s", 1, 0, 0)); err != nil {
		t.Fatal(err)
	}
	if err := session.AcceptDownlink(testFrame("s", 1, 1, 1)); err == nil {
		t.Fatal("stale generation accepted")
	}
	if err := session.AdvanceGeneration(Fence{SessionID: "s", TurnID: 1, GenerationID: 1}); err != nil {
		t.Fatal(err)
	}
	frame := testFrame("s", 1, 0, 1)
	frame.TurnID = 1
	if err := session.AcceptDownlink(frame); err != nil {
		t.Fatal(err)
	}
	epoch, err := session.Reconnect()
	if err != nil || epoch != 2 {
		t.Fatalf("reconnect: epoch=%d err=%v", epoch, err)
	}
	if err := session.AcceptUplink(testFrame("s", 1, 0, 0)); err == nil {
		t.Fatal("old epoch accepted")
	}
}

func TestHTTPReferenceEdge(t *testing.T) {
	server := NewServer(JWTVerifier{}, 4)
	server.AllowInsecureDevelopment = true
	defer server.Close()
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()
	body := `{"session_id":"s","account_id":"a","device_id":"d","stream_epoch":1}`
	response, err := http.Post(ts.URL+"/v1/media/sessions", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusCreated {
		t.Fatalf("status=%d", response.StatusCode)
	}
	response, err = http.Get(ts.URL + "/v1/media/sessions/s")
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK {
		t.Fatalf("status=%d", response.StatusCode)
	}
}

func TestHTTPDeleteClosesSessionAndReleasesDirectoryEntry(t *testing.T) {
	server := NewServer(JWTVerifier{}, 4)
	server.AllowInsecureDevelopment = true
	defer server.Close()
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()
	created, err := http.Post(
		ts.URL+"/v1/media/sessions",
		"application/json",
		strings.NewReader(`{"session_id":"delete-me","account_id":"a","device_id":"d","stream_epoch":1}`),
	)
	if err != nil || created.StatusCode != http.StatusCreated {
		t.Fatalf("create status=%v err=%v", created.StatusCode, err)
	}
	request, err := http.NewRequest(http.MethodDelete, ts.URL+"/v1/media/sessions/delete-me", nil)
	if err != nil {
		t.Fatal(err)
	}
	closed, err := http.DefaultClient.Do(request)
	if err != nil || closed.StatusCode != http.StatusOK {
		t.Fatalf("close status=%v err=%v", closed.StatusCode, err)
	}
	if _, ok := server.Directory.Get("delete-me"); ok {
		t.Fatal("closed session remained in directory")
	}
}

func TestHTTPReferenceEdgeFailsClosedWithoutExplicitDevelopmentOptIn(t *testing.T) {
	server := NewServer(JWTVerifier{}, 4)
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()
	body := `{"session_id":"s","account_id":"a","device_id":"d","stream_epoch":1}`
	response, err := http.Post(ts.URL+"/v1/media/sessions", "application/json", strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status=%d", response.StatusCode)
	}
}

func TestProductionBridgeFailsClosedWithoutExternalDownlinkSender(t *testing.T) {
	server := NewServer(JWTVerifier{}, 4)
	server.AllowInsecureDevelopment = true
	server.RequireExternalDownlinkSender = true
	server.BridgeFactory = func(_ OpenSessionRequest, session *Session) (*VoiceCoreMediaRuntime, error) {
		return NewVoiceCoreMediaRuntime(context.Background(), session, newFakeCoreStream(), nil, nil)
	}
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()

	ready, err := http.Get(ts.URL + "/readyz")
	if err != nil {
		t.Fatal(err)
	}
	if ready.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("ready status=%d, want %d", ready.StatusCode, http.StatusServiceUnavailable)
	}
	_ = ready.Body.Close()
	server.ExternalDownlinkSenderReady = func() bool { return true }
	created, err := http.Post(
		ts.URL+"/v1/media/sessions", "application/json",
		strings.NewReader(`{"session_id":"s","account_id":"a","device_id":"d","stream_epoch":1}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	if created.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("create status=%d, want %d", created.StatusCode, http.StatusServiceUnavailable)
	}
}

func TestSessionCancelGenerationKeepsSessionUsableAndFencesQueuedAudio(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{SessionID: "s", AccountID: "a", DeviceID: "d", StreamEpoch: 1}, 2)
	if err != nil {
		t.Fatal(err)
	}
	current := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(current); err != nil {
		t.Fatal(err)
	}
	old := testFrame("s", 1, 0, 1)
	old.TurnID = 1
	if err := session.AcceptDownlink(old); err != nil {
		t.Fatal(err)
	}
	cancelled := Fence{SessionID: "s", TurnID: 1, GenerationID: 2}
	_, actual, _, err := session.CancelGeneration("stop-1", &current)
	if err != nil {
		t.Fatal(err)
	}
	if !actual.Equal(cancelled) {
		t.Fatalf("cancelled=%+v, want %+v", actual, cancelled)
	}
	if stats := session.Stats(); stats.State != SessionActive {
		t.Fatalf("state=%s, want active", stats.State)
	}
	if _, ok := session.PopDownlink(); ok {
		t.Fatal("queued audio from cancelled generation escaped the gate")
	}
	stale := testFrame("s", 1, 1, 1)
	stale.TurnID = 1
	if err := session.AcceptDownlink(stale); err == nil {
		t.Fatal("cancelled generation accepted old audio")
	}
	if err := session.AcceptUplink(testFrame("s", 1, 0, 0)); err != nil {
		t.Fatalf("session did not continue after generation cancel: %v", err)
	}
	next := Fence{SessionID: "s", TurnID: 2, GenerationID: 0}
	if err := session.AdvanceGeneration(next); err != nil {
		t.Fatal(err)
	}
	fresh := testFrame("s", 1, 0, 0)
	fresh.TurnID = 2
	if err := session.AcceptDownlink(fresh); err != nil {
		t.Fatalf("new generation audio rejected: %v", err)
	}
}

func TestSessionCancelIdempotencyIsScopedToReconnectEpoch(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{SessionID: "s", AccountID: "a", DeviceID: "d", StreamEpoch: 1}, 2)
	if err != nil {
		t.Fatal(err)
	}
	current := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(current); err != nil {
		t.Fatal(err)
	}
	_, cancelled, _, err := session.CancelGeneration("reused-stop", &current)
	if err != nil {
		t.Fatal(err)
	}
	if !cancelled.Equal(Fence{SessionID: "s", TurnID: 1, GenerationID: 2}) {
		t.Fatalf("cancelled=%+v", cancelled)
	}
	if _, err := session.Reconnect(); err != nil {
		t.Fatal(err)
	}
	next := Fence{SessionID: "s", TurnID: 2, GenerationID: 0}
	if err := session.AdvanceGeneration(next); err != nil {
		t.Fatal(err)
	}
	_, replay, wasReplay, err := session.CancelGeneration("reused-stop", &next)
	if err != nil || wasReplay {
		t.Fatalf("reused stop replayed across epoch: fence=%+v replay=%v err=%v", replay, wasReplay, err)
	}
	if !replay.Equal(Fence{SessionID: "s", TurnID: 2, GenerationID: 1}) {
		t.Fatalf("cancelled=%+v", replay)
	}
}

func TestSessionCancelResultLedgerKeepsLast64Stops(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{SessionID: "s", AccountID: "a", DeviceID: "d", StreamEpoch: 1}, 2)
	if err != nil {
		t.Fatal(err)
	}
	current := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(current); err != nil {
		t.Fatal(err)
	}
	firstCurrent := current
	var firstCancelled Fence
	for index := 0; index < 64; index++ {
		if index > 0 {
			current.GenerationID++
			if err := session.AdvanceGeneration(current); err != nil {
				t.Fatal(err)
			}
		}
		_, cancelled, _, err := session.CancelGeneration(fmt.Sprintf("stop-%d", index), &current)
		if err != nil {
			t.Fatal(err)
		}
		if index == 0 {
			firstCancelled = cancelled
		}
		current = cancelled
	}
	_, replayed, wasReplay, err := session.CancelGeneration("stop-0", &firstCurrent)
	if err != nil || !wasReplay || !replayed.Equal(firstCancelled) {
		t.Fatalf("oldest retained replay=%+v replayed=%v err=%v", replayed, wasReplay, err)
	}
	current.GenerationID++
	if err := session.AdvanceGeneration(current); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := session.CancelGeneration("stop-64", &current); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := session.CancelGeneration("stop-0", &firstCurrent); err == nil {
		t.Fatal("evicted 65th-old stop result was still retained")
	}
}

func TestHTTPStopCancelsGenerationWithoutStoppingSession(t *testing.T) {
	server := NewServer(JWTVerifier{}, 2)
	server.AllowInsecureDevelopment = true
	core := newFakeCoreStream()
	server.BridgeFactory = func(_ OpenSessionRequest, session *Session) (*VoiceCoreMediaRuntime, error) {
		return NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	}
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()
	defer server.Close()
	created, err := http.Post(
		ts.URL+"/v1/media/sessions", "application/json",
		strings.NewReader(`{"session_id":"s","account_id":"a","device_id":"d","stream_epoch":1}`),
	)
	if err != nil || created.StatusCode != http.StatusCreated {
		t.Fatalf("create status=%v err=%v", created, err)
	}
	_ = created.Body.Close()
	current := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	core.setCurrentFence(current)
	session, ok := server.Directory.Get("s")
	if !ok {
		t.Fatal("session not found")
	}
	if err := session.AdvanceGeneration(current); err != nil {
		t.Fatal(err)
	}
	request, err := http.NewRequest(
		http.MethodPost, ts.URL+"/v1/media/sessions/s/stop",
		strings.NewReader(`{"session_id":"s","stream_epoch":1,"reason":"user_button"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Idempotency-Key", "stop-1")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusOK {
		t.Fatalf("stop status=%d", response.StatusCode)
	}
	var stopped struct {
		StreamEpoch  uint64 `json:"stream_epoch"`
		TurnID       uint64 `json:"turn_id"`
		GenerationID uint64 `json:"generation_id"`
		ToolEpoch    uint64 `json:"tool_epoch"`
	}
	if err := json.NewDecoder(response.Body).Decode(&stopped); err != nil {
		t.Fatal(err)
	}
	_ = response.Body.Close()
	if stopped.StreamEpoch != 1 || stopped.TurnID != 1 || stopped.GenerationID != 2 || stopped.ToolEpoch != 0 {
		t.Fatalf("unexpected cancelled fence: %+v", stopped)
	}
	retry, err := http.NewRequest(
		http.MethodPost, ts.URL+"/v1/media/sessions/s/stop",
		strings.NewReader(`{"session_id":"s","stream_epoch":1,"reason":"user_button"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	retry.Header.Set("Content-Type", "application/json")
	retry.Header.Set("Idempotency-Key", "stop-1")
	repeated, err := http.DefaultClient.Do(retry)
	if err != nil {
		t.Fatal(err)
	}
	if repeated.StatusCode != http.StatusOK {
		t.Fatalf("repeated stop status=%d", repeated.StatusCode)
	}
	_ = repeated.Body.Close()
	if core.stopCount() != 1 {
		t.Fatalf("stop count=%d, want 1", core.stopCount())
	}
	if stats := session.Stats(); stats.State != SessionActive {
		t.Fatalf("state=%s, want active", stats.State)
	}
	uplink := `{"session_id":"s","stream_epoch":1,"sequence":0,"capture_start_sample":0,"frame_samples":1,"payload_b64":"AAE="}`
	continued, err := http.Post(ts.URL+"/v1/media/sessions/s/uplink", "application/json", strings.NewReader(uplink))
	if err != nil {
		t.Fatal(err)
	}
	if continued.StatusCode != http.StatusAccepted {
		t.Fatalf("continued uplink status=%d", continued.StatusCode)
	}
}

func TestStopGenerationRequestRequiresCompleteExpectedFence(t *testing.T) {
	generation := uint64(1)
	if _, err := (StopGenerationRequest{GenerationID: &generation}).ExpectedFence("s"); err == nil {
		t.Fatal("partial expected fence was accepted")
	}
	turn, tool := uint64(2), uint64(3)
	expected, err := (StopGenerationRequest{
		TurnID: &turn, GenerationID: &generation, ToolEpoch: &tool,
	}).ExpectedFence("s")
	if err != nil {
		t.Fatal(err)
	}
	if !expected.Equal(Fence{SessionID: "s", TurnID: 2, GenerationID: 1, ToolEpoch: 3}) {
		t.Fatalf("unexpected expected fence: %+v", expected)
	}
}

func TestHTTPReferenceEdgeBindsJWTIdentityToSessionBody(t *testing.T) {
	secret := []byte("media-token-secret-that-is-long-enough")
	server := NewServer(JWTVerifier{Secret: secret, Issuer: "voice-agent", Audience: "memoria-media"}, 4)
	defer server.Close()
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()
	token := signedMediaToken(t, secret, map[string]any{
		"iss":          "voice-agent",
		"aud":          "memoria-media",
		"sub":          "account-1",
		"session_id":   "s",
		"device_id":    "device-1",
		"client_type":  "h5",
		"stream_epoch": 1,
		"exp":          time.Now().Add(time.Minute).Unix(),
	})
	request, err := http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/media/sessions",
		strings.NewReader(`{"session_id":"s","account_id":"other","device_id":"device-1","client_type":"h5","stream_epoch":1}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer "+token)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("status=%d, want %d", response.StatusCode, http.StatusUnauthorized)
	}
	_ = response.Body.Close()

	request, err = http.NewRequest(
		http.MethodPost,
		ts.URL+"/v1/media/sessions",
		strings.NewReader(`{"session_id":"s","account_id":"account-1","device_id":"device-1","client_type":"h5","stream_epoch":1}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer "+token)
	response, err = http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusCreated {
		t.Fatalf("valid identity status=%d, want %d", response.StatusCode, http.StatusCreated)
	}
	_ = response.Body.Close()
}

func TestHTTPServerForwardsFramesThroughVoiceCoreRuntime(t *testing.T) {
	server := NewServer(JWTVerifier{}, 2)
	server.AllowInsecureDevelopment = true
	core := newFakeCoreStream()
	server.BridgeFactory = func(_ OpenSessionRequest, session *Session) (*VoiceCoreMediaRuntime, error) {
		return NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	}
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()
	defer server.Close()

	create, err := http.Post(
		ts.URL+"/v1/media/sessions",
		"application/json",
		strings.NewReader(`{"session_id":"s","account_id":"a","device_id":"d","stream_epoch":1}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	if create.StatusCode != http.StatusCreated {
		t.Fatalf("create status=%d", create.StatusCode)
	}
	uplink := `{"session_id":"s","stream_epoch":1,"sequence":0,"capture_start_sample":0,"frame_samples":1,"payload_b64":"AAE="}`
	response, err := http.Post(ts.URL+"/v1/media/sessions/s/uplink", "application/json", strings.NewReader(uplink))
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != http.StatusAccepted || core.sentCount() != 1 {
		t.Fatalf("uplink status=%d sent=%d", response.StatusCode, core.sentCount())
	}
	identity := runtimeIdentity()
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{Identity: identity, TurnId: 1, GenerationId: 1, Sequence: 1},
	}}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Audio{
		Audio: &mediav1.AssistantAudioFrame{Identity: identity, TurnId: 1, GenerationId: 1, Sequence: 0, FrameSamples: 1, PcmS16Le: []byte{0, 1}},
	}}
	deadline := time.Now().Add(time.Second)
	var downlinkResponse *http.Response
	for time.Now().Before(deadline) {
		downlinkResponse, err = http.Get(ts.URL + "/v1/media/sessions/s/downlink")
		if err != nil {
			t.Fatal(err)
		}
		if downlinkResponse.StatusCode != http.StatusNoContent {
			break
		}
		_ = downlinkResponse.Body.Close()
		time.Sleep(time.Millisecond)
	}
	if downlinkResponse == nil || downlinkResponse.StatusCode != http.StatusOK {
		t.Fatalf("downlink status=%v", downlinkResponse)
	}
	var frame AudioFrame
	if err := json.NewDecoder(downlinkResponse.Body).Decode(&frame); err != nil {
		t.Fatal(err)
	}
	_ = downlinkResponse.Body.Close()
	if frame.GenerationID != 1 || frame.Sequence != 0 {
		t.Fatalf("unexpected downlink frame: %+v", frame)
	}
}

func TestSessionRejectsDiscontinuityAndBoundsQueues(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{SessionID: "s", AccountID: "a", DeviceID: "d", StreamEpoch: 1}, 1)
	if err != nil {
		t.Fatal(err)
	}
	first := testFrame("s", 1, 0, 0)
	if err := session.AcceptUplink(first); err != nil {
		t.Fatal(err)
	}
	if err := session.AcceptUplink(testFrame("s", 1, 1, 0)); err == nil {
		t.Fatal("queue overflow accepted")
	}
	discontinuity := testFrame("s", 1, 1, 0)
	discontinuity.Discontinuity = true
	if err := session.AcceptUplink(discontinuity); err == nil {
		t.Fatal("discontinuity accepted without reconnect")
	}
}

func TestSessionRejectsCaptureGap(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{SessionID: "s", AccountID: "a", DeviceID: "d", StreamEpoch: 1}, 4)
	if err != nil {
		t.Fatal(err)
	}
	if err := session.AcceptUplink(testFrame("s", 1, 0, 0)); err != nil {
		t.Fatal(err)
	}
	gapped := testFrame("s", 1, 1, 0)
	gapped.CaptureStartSample += 160
	if err := session.AcceptUplink(gapped); err == nil {
		t.Fatal("capture gap accepted")
	}
}

func TestSessionRejectsDownlinkSampleGap(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{SessionID: "s", AccountID: "a", DeviceID: "d", StreamEpoch: 1}, 4)
	if err != nil {
		t.Fatal(err)
	}
	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	first := testFrame("s", 1, 0, 0)
	first.TurnID, first.GenerationID = 1, 1
	if err := session.AcceptDownlink(first); err != nil {
		t.Fatal(err)
	}
	gapped := testFrame("s", 1, 1, 1)
	gapped.TurnID, gapped.GenerationID = 1, 1
	gapped.CaptureStartSample = first.CaptureStartSample + first.FrameSamples + 1
	if err := session.AcceptDownlink(gapped); err == nil {
		t.Fatal("downlink sample gap accepted")
	}
}

func TestSessionRejectsMalformedPCMPayload(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{SessionID: "s", AccountID: "a", DeviceID: "d", StreamEpoch: 1}, 4)
	if err != nil {
		t.Fatal(err)
	}
	frame := testFrame("s", 1, 0, 0)
	frame.PayloadB64 = base64.StdEncoding.EncodeToString([]byte{0, 1})
	if err := session.AcceptUplink(frame); err == nil {
		t.Fatal("malformed PCM payload accepted")
	}
}

func TestSessionRequiresZeroOriginForNewGenerationDownlink(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{SessionID: "s", AccountID: "a", DeviceID: "d", StreamEpoch: 1}, 4)
	if err != nil {
		t.Fatal(err)
	}
	fence := Fence{SessionID: "s", TurnID: 1, GenerationID: 1}
	if err := session.AdvanceGeneration(fence); err != nil {
		t.Fatal(err)
	}
	frame := testFrame("s", 1, 1, 1)
	frame.TurnID, frame.GenerationID = 1, 1
	if err := session.AcceptDownlink(frame); err == nil {
		t.Fatal("non-zero first downlink origin accepted")
	}
}

func TestSessionRejectsSequenceGap(t *testing.T) {
	session, err := NewSession(OpenSessionRequest{SessionID: "s", AccountID: "a", DeviceID: "d", StreamEpoch: 1}, 4)
	if err != nil {
		t.Fatal(err)
	}
	if err := session.AcceptUplink(testFrame("s", 1, 0, 0)); err != nil {
		t.Fatal(err)
	}
	if err := session.AcceptUplink(testFrame("s", 1, 2, 0)); err == nil {
		t.Fatal("sequence gap accepted")
	}
}
