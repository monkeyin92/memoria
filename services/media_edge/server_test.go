package mediaedge

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
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

func TestPublicAndInternalHandlersSeparateOperationalRoutes(t *testing.T) {
	server := NewServer(JWTVerifier{}, 4)
	public := httptest.NewRecorder()
	server.PublicHandler().ServeHTTP(public, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if public.Code != http.StatusNotFound {
		t.Fatalf("public metrics route status=%d, want 404", public.Code)
	}
	internal := httptest.NewRecorder()
	server.InternalHandler().ServeHTTP(internal, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	if internal.Code != http.StatusOK {
		t.Fatalf("internal metrics route status=%d, want 200", internal.Code)
	}
	publicReady := httptest.NewRecorder()
	server.PublicHandler().ServeHTTP(publicReady, httptest.NewRequest(http.MethodGet, "/readyz", nil))
	if publicReady.Code != http.StatusNotFound {
		t.Fatalf("public readiness route status=%d, want 404", publicReady.Code)
	}
}

func TestMetricsExposeActorShadowAndDeadlineSignals(t *testing.T) {
	server := NewServer(JWTVerifier{}, 4)
	session, err := NewSession(OpenSessionRequest{
		SessionID: "metrics-session", AccountID: "a", DeviceID: "d", StreamEpoch: 1,
	}, 4)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Stop()
	if err := server.Directory.Put(session); err != nil {
		t.Fatal(err)
	}
	session.MirrorPythonInteraction(
		ShadowFloorAssistant,
		"enqueue_output_intent",
		"speaking",
		Fence{SessionID: "metrics-session"},
	)
	frame := testFrame("metrics-session", 1, 0, 0)
	if err := session.AcceptDownlink(frame); err != nil {
		t.Fatal(err)
	}
	session.MirrorPlayback(frame.FrameSamples, Fence{SessionID: "metrics-session"})
	if err := session.AcknowledgeDownlink(frame.Sequence); err != nil {
		t.Fatal(err)
	}
	waitForActorEvents(t, session.actor, 3)
	recorder := httptest.NewRecorder()
	server.metrics(recorder, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	body := recorder.Body.String()
	for _, metric := range []string{
		"active_media_sessions 1",
		"audio_frame_deadline_miss_total 0",
		"audio_frame_deadline_miss_ratio 0",
		"actor_mailbox_age_ms 0",
		"ingress_queue_age_ms 0",
		"egress_queue_age_ms 0",
		"floor_decision_latency_ms ",
		"generation_cancel_latency_ms 0",
		"playout_buffer_ms 0",
		"playout_underrun_total 1",
		"session_duration_ms ",
		"# TYPE shadow_decision_mismatch_total counter",
		`shadow_decision_mismatch_total{scenario="speaking",contract_version="media-v1-a5"} 1`,
	} {
		if !strings.Contains(body, metric) {
			t.Fatalf("metrics omitted %q: %s", metric, body)
		}
	}
}

func TestSessionShadowSnapshotHasAuthenticatedReadOnlyEndpoint(t *testing.T) {
	t.Setenv("ENVIRONMENT", "development")
	server := NewServer(JWTVerifier{}, 4)
	server.AllowInsecureDevelopment = true
	session, err := NewSession(OpenSessionRequest{
		SessionID: "shadow-session", AccountID: "a", DeviceID: "d", StreamEpoch: 1,
	}, 4)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Stop()
	if err := server.Directory.Put(session); err != nil {
		t.Fatal(err)
	}
	session.MirrorPythonInteraction(
		ShadowFloorAssistant,
		"enqueue_output_intent",
		"speaking",
		Fence{SessionID: "shadow-session"},
	)
	waitForActorEvents(t, session.actor, 1)

	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(http.MethodGet, "/v1/media/sessions/shadow-session/shadow", nil)
	server.Handler().ServeHTTP(recorder, request)

	if recorder.Code != http.StatusOK {
		t.Fatalf("shadow endpoint status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	var snapshot LiveSessionSnapshot
	if err := json.Unmarshal(recorder.Body.Bytes(), &snapshot); err != nil {
		t.Fatal(err)
	}
	if len(snapshot.RecentComparisons) != 1 || len(snapshot.ShadowMismatchCounts) != 1 {
		t.Fatalf("shadow evidence unavailable: %+v", snapshot)
	}
	if snapshot.ShadowMismatchCounts[0].ContractVersion != shadowContractVersion {
		t.Fatalf("shadow contract version missing: %+v", snapshot.ShadowMismatchCounts)
	}

	writeRecorder := httptest.NewRecorder()
	writeRequest := httptest.NewRequest(http.MethodPost, request.URL.String(), nil)
	server.Handler().ServeHTTP(writeRecorder, writeRequest)
	if writeRecorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("shadow endpoint accepted a write: %d", writeRecorder.Code)
	}
}

func TestShadowMismatchCounterSurvivesReconnectAndClose(t *testing.T) {
	server := NewServer(JWTVerifier{}, 4)
	session, err := NewSession(OpenSessionRequest{
		SessionID: "counter-session", AccountID: "a", DeviceID: "d", StreamEpoch: 1,
	}, 4)
	if err != nil {
		t.Fatal(err)
	}
	if err := server.Directory.Put(session); err != nil {
		t.Fatal(err)
	}
	session.MirrorPythonInteraction(
		ShadowFloorAssistant,
		"enqueue_output_intent",
		"speaking",
		Fence{SessionID: "counter-session"},
	)
	waitForActorEvents(t, session.actor, 1)
	assertMismatchCount := func(wantActive int) {
		t.Helper()
		recorder := httptest.NewRecorder()
		server.metrics(recorder, httptest.NewRequest(http.MethodGet, "/metrics", nil))
		body := recorder.Body.String()
		if !strings.Contains(body, fmt.Sprintf("active_media_sessions %d", wantActive)) ||
			!strings.Contains(
				body,
				`shadow_decision_mismatch_total{scenario="speaking",contract_version="media-v1-a5"} 1`,
			) {
			t.Fatalf("shadow counter regressed: %s", body)
		}
	}

	assertMismatchCount(1)
	if _, err := session.Reconnect(); err != nil {
		t.Fatal(err)
	}
	assertMismatchCount(1)
	if !server.CloseSession("counter-session") {
		t.Fatal("session close failed")
	}
	assertMismatchCount(0)
}

func TestShadowMismatchCounterSurvivesServerShutdown(t *testing.T) {
	server := NewServer(JWTVerifier{}, 4)
	session, err := NewSession(OpenSessionRequest{
		SessionID: "shutdown-counter", AccountID: "a", DeviceID: "d", StreamEpoch: 1,
	}, 4)
	if err != nil {
		t.Fatal(err)
	}
	if err := server.Directory.Put(session); err != nil {
		t.Fatal(err)
	}
	session.MirrorPythonInteraction(
		ShadowFloorAssistant,
		"enqueue_output_intent",
		"speaking",
		Fence{SessionID: "shutdown-counter"},
	)
	waitForActorEvents(t, session.actor, 1)
	if err := server.Close(); err != nil {
		t.Fatal(err)
	}

	recorder := httptest.NewRecorder()
	server.metrics(recorder, httptest.NewRequest(http.MethodGet, "/metrics", nil))
	body := recorder.Body.String()
	if !strings.Contains(body, "active_media_sessions 0") || !strings.Contains(
		body,
		`shadow_decision_mismatch_total{scenario="speaking",contract_version="media-v1-a5"} 1`,
	) {
		t.Fatalf("shutdown dropped shadow counters: %s", body)
	}
}

func TestServerShutdownSerializesConcurrentSessionCreation(t *testing.T) {
	t.Setenv("ENVIRONMENT", "development")
	server := NewServer(JWTVerifier{}, 4)
	server.AllowInsecureDevelopment = true
	started := make(chan struct{})
	release := make(chan struct{})
	core := newFakeCoreStream()
	var actor *LiveSessionActor
	server.BridgeFactory = func(
		_ OpenSessionRequest,
		session *Session,
		_ DownlinkSender,
	) (*VoiceCoreMediaRuntime, error) {
		actor = session.actor
		close(started)
		<-release
		return NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	}
	request := httptest.NewRequest(
		http.MethodPost,
		"/v1/media/sessions",
		strings.NewReader(`{"session_id":"closing-create","account_id":"a","device_id":"d","stream_epoch":1}`),
	)
	request.Header.Set("Content-Type", "application/json")
	recorder := httptest.NewRecorder()
	requestDone := make(chan struct{})
	go func() {
		server.Handler().ServeHTTP(recorder, request)
		close(requestDone)
	}()
	<-started
	closeDone := make(chan error, 1)
	go func() { closeDone <- server.Close() }()
	deadline := time.Now().Add(time.Second)
	for !server.Draining.Load() && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	close(release)
	<-requestDone
	if err := <-closeDone; err != nil {
		t.Fatal(err)
	}
	if recorder.Code != http.StatusCreated {
		t.Fatalf("in-flight create status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if len(server.Directory.Snapshots()) != 0 {
		t.Fatal("session survived server shutdown")
	}
	if actor == nil {
		t.Fatal("session actor was not created")
	}
	if err := actor.TrySubmit(LiveSessionEvent{Kind: LiveEventVADStart}); !errors.Is(err, ErrActorClosed) {
		t.Fatalf("shutdown left the in-flight actor running: %v", err)
	}
}

func TestReconnectUsesPerSessionLifecycleWithoutLeakingAReplacement(t *testing.T) {
	t.Setenv("ENVIRONMENT", "development")
	server := NewServer(JWTVerifier{}, 4)
	server.AllowInsecureDevelopment = true
	defer func() { _ = server.Close() }()

	slow, err := NewSession(OpenSessionRequest{
		SessionID: "slow-reconnect", AccountID: "account", DeviceID: "device", StreamEpoch: 1,
	}, 4)
	if err != nil {
		t.Fatal(err)
	}
	other, err := NewSession(OpenSessionRequest{
		SessionID: "other-session", AccountID: "account", DeviceID: "device", StreamEpoch: 1,
	}, 4)
	if err != nil {
		t.Fatal(err)
	}
	if err := server.Directory.Put(slow); err != nil {
		t.Fatal(err)
	}
	if err := server.Directory.Put(other); err != nil {
		t.Fatal(err)
	}

	bridgeStarted := make(chan struct{})
	releaseBridge := make(chan struct{})
	server.BridgeFactory = func(
		request OpenSessionRequest,
		session *Session,
		_ DownlinkSender,
	) (*VoiceCoreMediaRuntime, error) {
		if request.SessionID == slow.ID && request.StreamEpoch == 2 {
			close(bridgeStarted)
			<-releaseBridge
		}
		return NewVoiceCoreMediaRuntime(
			context.Background(), session, newFakeCoreStream(), nil, nil,
		)
	}

	reconnectRecorder := httptest.NewRecorder()
	reconnectDone := make(chan struct{})
	go func() {
		request := httptest.NewRequest(
			http.MethodPost,
			"/v1/media/sessions/slow-reconnect/reconnect",
			nil,
		)
		server.Handler().ServeHTTP(reconnectRecorder, request)
		close(reconnectDone)
	}()
	select {
	case <-bridgeStarted:
	case <-time.After(time.Second):
		t.Fatal("reconnect did not reach the slow bridge")
	}

	otherClosed := make(chan bool, 1)
	go func() { otherClosed <- server.CloseSession(other.ID) }()
	select {
	case closed := <-otherClosed:
		if !closed {
			t.Fatal("unrelated session could not close during reconnect")
		}
	case <-time.After(200 * time.Millisecond):
		t.Fatal("slow reconnect held the global lifecycle lock")
	}

	slowClosed := make(chan bool, 1)
	go func() { slowClosed <- server.CloseSession(slow.ID) }()
	select {
	case <-slowClosed:
		t.Fatal("same-session close bypassed reconnect lifecycle")
	case <-time.After(50 * time.Millisecond):
	}
	close(releaseBridge)

	select {
	case <-reconnectDone:
	case <-time.After(time.Second):
		t.Fatal("reconnect did not finish after bridge release")
	}
	if reconnectRecorder.Code != http.StatusOK {
		t.Fatalf("reconnect status=%d body=%s", reconnectRecorder.Code, reconnectRecorder.Body.String())
	}
	select {
	case closed := <-slowClosed:
		if !closed {
			t.Fatal("same-session close failed after reconnect")
		}
	case <-time.After(time.Second):
		t.Fatal("same-session close did not finish")
	}
	if _, ok := server.Directory.Get(slow.ID); ok {
		t.Fatal("closed reconnect session remained in the directory")
	}
	if runtime := server.bridgeFor(slow.ID); runtime != nil {
		t.Fatal("closed reconnect session retained its replacement bridge")
	}
}

func TestFailedHTTPSessionCreationStopsActor(t *testing.T) {
	t.Setenv("ENVIRONMENT", "development")
	server := NewServer(JWTVerifier{}, 4)
	server.AllowInsecureDevelopment = true
	var actor *LiveSessionActor
	server.BridgeFactory = func(
		_ OpenSessionRequest,
		session *Session,
		_ DownlinkSender,
	) (*VoiceCoreMediaRuntime, error) {
		actor = session.actor
		return nil, errors.New("bridge failed")
	}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest(
		http.MethodPost,
		"/v1/media/sessions",
		strings.NewReader(`{"session_id":"failed-create","account_id":"a","device_id":"d","stream_epoch":1}`),
	)
	server.Handler().ServeHTTP(recorder, request)

	if recorder.Code != http.StatusBadGateway {
		t.Fatalf("failed create status=%d body=%s", recorder.Code, recorder.Body.String())
	}
	if actor == nil {
		t.Fatal("session actor was not created")
	}
	if err := actor.TrySubmit(LiveSessionEvent{Kind: LiveEventVADStart}); !errors.Is(err, ErrActorClosed) {
		t.Fatalf("failed create leaked an actor: %v", err)
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
	firstAfterReconnect := testFrame("s", 2, 0, 1)
	firstAfterReconnect.TurnID = 1
	if err := session.AcceptDownlink(firstAfterReconnect); err != nil {
		t.Fatalf("first downlink after reconnect: %v", err)
	}
}

func TestSessionCapacityMetricsUseLiveQueueAndPlayoutState(t *testing.T) {
	session, err := NewSession(
		OpenSessionRequest{SessionID: "capacity", AccountID: "a", DeviceID: "d", StreamEpoch: 1},
		2,
	)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Stop()
	if err := session.AcceptUplink(testFrame("capacity", 1, 0, 0)); err != nil {
		t.Fatal(err)
	}
	if err := session.AcceptDownlink(testFrame("capacity", 1, 0, 0)); err != nil {
		t.Fatal(err)
	}
	time.Sleep(time.Millisecond)
	session.MirrorPlayback(0, Fence{SessionID: "capacity"})
	session.MirrorVAD(true, 1)
	waitForActorEvents(t, session.actor, 3)
	stats := session.Stats()
	if stats.IngressQueueAgeMS <= 0 || stats.EgressQueueAgeMS <= 0 {
		t.Fatalf("queue ages were not measured: %+v", stats)
	}
	if stats.PlayoutBufferMS <= 0 || stats.FloorDecisionLatencyMS <= 0 || stats.SessionDurationMS <= 0 {
		t.Fatalf("capacity state was not measured: %+v", stats)
	}
	if err := session.AcknowledgeUplink(0); err != nil {
		t.Fatal(err)
	}
	if err := session.AcknowledgeDownlink(0); err != nil {
		t.Fatal(err)
	}
	if _, _, _, err := session.CancelGeneration(
		"capacity-stop",
		&Fence{SessionID: "capacity"},
	); err != nil {
		t.Fatal(err)
	}
	stats = session.Stats()
	if stats.IngressQueueAgeMS != 0 || stats.EgressQueueAgeMS != 0 {
		t.Fatalf("empty queues retained age: %+v", stats)
	}
	if stats.GenerationCancelLatencyMS <= 0 {
		t.Fatalf("generation cancel latency was not measured: %+v", stats)
	}
}

func TestHTTPReferenceEdge(t *testing.T) {
	server := NewServer(JWTVerifier{}, 4)
	server.AllowInsecureDevelopment = true
	defer func() { _ = server.Close() }()
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
	defer func() { _ = server.Close() }()
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
	server.BridgeFactory = func(_ OpenSessionRequest, session *Session, _ DownlinkSender) (*VoiceCoreMediaRuntime, error) {
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

func TestProductionBridgeInjectsTheActualDownlinkSender(t *testing.T) {
	server := NewServer(JWTVerifier{}, 4)
	server.AllowInsecureDevelopment = true
	server.RequireExternalDownlinkSender = true
	sent := make(chan AudioFrame, 1)
	server.DownlinkSenderFactory = func(_ OpenSessionRequest, _ *Session) (DownlinkSender, error) {
		return func(ctx context.Context, frame AudioFrame) error {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case sent <- frame:
				return nil
			}
		}, nil
	}
	server.DownlinkReadyProbe = func() bool { return true }
	server.BridgeFactory = func(_ OpenSessionRequest, session *Session, sender DownlinkSender) (*VoiceCoreMediaRuntime, error) {
		return NewVoiceCoreMediaRuntimeWithDownlinkSender(
			context.Background(), session, newFakeCoreStream(), sender, nil, nil,
		)
	}
	defer func() { _ = server.Close() }()
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()

	ready, err := http.Get(ts.URL + "/readyz")
	if err != nil || ready.StatusCode != http.StatusOK {
		t.Fatalf("ready status=%v err=%v", ready.StatusCode, err)
	}
	_ = ready.Body.Close()
	created, err := http.Post(
		ts.URL+"/v1/media/sessions", "application/json",
		strings.NewReader(`{"session_id":"sender","account_id":"a","device_id":"d","stream_epoch":1}`),
	)
	if err != nil || created.StatusCode != http.StatusCreated {
		t.Fatalf("create status=%v err=%v", created.StatusCode, err)
	}
	_ = created.Body.Close()
}

func TestProductionBridgeFailsClosedWhenDownlinkTransportIsUnhealthy(t *testing.T) {
	server := NewServer(JWTVerifier{}, 4)
	server.AllowInsecureDevelopment = true
	server.RequireExternalDownlinkSender = true
	server.DownlinkSenderFactory = func(_ OpenSessionRequest, _ *Session) (DownlinkSender, error) {
		return func(context.Context, AudioFrame) error { return nil }, nil
	}
	server.DownlinkReadyProbe = func() bool { return false }
	server.BridgeFactory = func(_ OpenSessionRequest, session *Session, sender DownlinkSender) (*VoiceCoreMediaRuntime, error) {
		return NewVoiceCoreMediaRuntimeWithDownlinkSender(
			context.Background(), session, newFakeCoreStream(), sender, nil, nil,
		)
	}
	defer func() { _ = server.Close() }()
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()

	ready, err := http.Get(ts.URL + "/readyz")
	if err != nil || ready.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("ready status=%v err=%v", ready.StatusCode, err)
	}
	_ = ready.Body.Close()
	created, err := http.Post(
		ts.URL+"/v1/media/sessions", "application/json",
		strings.NewReader(`{"session_id":"unhealthy","account_id":"a","device_id":"d","stream_epoch":1}`),
	)
	if err != nil || created.StatusCode != http.StatusServiceUnavailable {
		t.Fatalf("create status=%v err=%v", created.StatusCode, err)
	}
	_ = created.Body.Close()
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
	server.BridgeFactory = func(_ OpenSessionRequest, session *Session, _ DownlinkSender) (*VoiceCoreMediaRuntime, error) {
		return NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	}
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()
	defer func() { _ = server.Close() }()
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
	defer func() { _ = server.Close() }()
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

func TestHTTPAuthorizationAcceptsConfiguredEdDSAWithoutHMACFallback(t *testing.T) {
	t.Setenv("ENVIRONMENT", "production")
	public, private, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	identity := MediaTokenIdentity{
		SessionID: "eddsa-session", AccountID: "account-1", DeviceID: "h5",
		ClientType: "h5", StreamEpoch: 1,
	}
	token := signedEdDSAToken(t, private, "media-2026-08", map[string]any{
		"iss": "voice-agent", "aud": "memoria-media", "sub": identity.AccountID,
		"session_id": identity.SessionID, "device_id": identity.DeviceID,
		"client_type": identity.ClientType, "stream_epoch": identity.StreamEpoch,
		"iat": now.Unix(), "exp": now.Add(time.Minute).Unix(),
	})
	server := NewServer(JWTVerifier{
		PublicKeys: map[string]ed25519.PublicKey{"media-2026-08": public},
		Issuer:     "voice-agent", Audience: "memoria-media",
	}, 4)
	request := httptest.NewRequest(http.MethodPost, "/whip", nil)
	request.Header.Set("Authorization", "Bearer "+token)

	if err := server.authorizeIdentity(request, identity); err != nil {
		t.Fatalf("configured EdDSA verifier was rejected: %v", err)
	}
}

func TestHTTPServerForwardsFramesThroughVoiceCoreRuntime(t *testing.T) {
	server := NewServer(JWTVerifier{}, 2)
	server.AllowInsecureDevelopment = true
	core := newFakeCoreStream()
	server.BridgeFactory = func(_ OpenSessionRequest, session *Session, _ DownlinkSender) (*VoiceCoreMediaRuntime, error) {
		return NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, nil)
	}
	ts := httptest.NewServer(server.Handler())
	defer ts.Close()
	defer func() { _ = server.Close() }()

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

func TestOpenWebRTCSessionKeepsOldEpochWhenReplacementBridgeFails(t *testing.T) {
	server := NewServer(JWTVerifier{}, 2)
	server.RequireExternalDownlinkSender = true
	server.DownlinkReadyProbe = func() bool { return true }
	server.DownlinkSenderFactory = func(OpenSessionRequest, *Session) (DownlinkSender, error) {
		return func(context.Context, AudioFrame) error { return nil }, nil
	}
	core := newFakeCoreStream()
	var failedActor *LiveSessionActor
	server.BridgeFactory = func(request OpenSessionRequest, session *Session, sender DownlinkSender) (*VoiceCoreMediaRuntime, error) {
		if request.StreamEpoch == 2 {
			failedActor = session.actor
			return nil, errors.New("replacement bridge failed")
		}
		return NewVoiceCoreMediaRuntimeWithDownlinkSender(context.Background(), session, core, sender, nil, nil)
	}
	defer func() { _ = server.Close() }()
	first := OpenSessionRequest{SessionID: "s", AccountID: "a", DeviceID: "d", ClientType: "h5", StreamEpoch: 1}
	if _, err := server.OpenWebRTCSession(first); err != nil {
		t.Fatal(err)
	}
	second := first
	second.StreamEpoch = 2
	if _, err := server.OpenWebRTCSession(second); err == nil {
		t.Fatal("replacement bridge failure was accepted")
	}
	current, ok := server.Directory.Get(first.SessionID)
	if !ok || current.Epoch() != first.StreamEpoch {
		t.Fatal("failed replacement removed the old epoch")
	}
	if failedActor == nil {
		t.Fatal("replacement actor was not created")
	}
	if err := failedActor.TrySubmit(LiveSessionEvent{Kind: LiveEventVADStart}); !errors.Is(err, ErrActorClosed) {
		t.Fatalf("failed replacement leaked an actor: %v", err)
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
