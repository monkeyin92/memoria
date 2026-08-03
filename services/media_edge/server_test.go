package mediaedge

import (
	"context"
	"encoding/base64"
	"encoding/json"
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
		GenerationID: generation, PayloadB64: base64.StdEncoding.EncodeToString([]byte("pcm")),
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
	frame := testFrame("s", 1, 1, 1)
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
