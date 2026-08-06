package mediaedge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	pionopus "github.com/pion/opus"
	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

type loopbackCore struct {
	mu        sync.Mutex
	events    chan *mediav1.CoreToMedia
	closed    chan struct{}
	closeOnce sync.Once
	current   Fence
	uplink    []AudioFrame
	vad       []mediaVADEvent
	stops     []Fence
	playback  []PlaybackProgress
	clients   []string
	authority mediav1.InteractionAuthority
}

func newLoopbackCore() *loopbackCore {
	return &loopbackCore{events: make(chan *mediav1.CoreToMedia, 64), closed: make(chan struct{})}
}

func (c *loopbackCore) SendAudio(frame AudioFrame) error {
	c.mu.Lock()
	c.uplink = append(c.uplink, frame)
	c.mu.Unlock()
	return nil
}

func (c *loopbackCore) Recv() (*mediav1.CoreToMedia, error) {
	select {
	case event := <-c.events:
		return event, nil
	case <-c.closed:
		return nil, io.EOF
	}
}

func (c *loopbackCore) Close() error {
	c.closeOnce.Do(func() { close(c.closed) })
	return nil
}

func (c *loopbackCore) CurrentFence() Fence {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.current
}

func (c *loopbackCore) InteractionAuthority() mediav1.InteractionAuthority {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.authority
}

func (c *loopbackCore) SendStop(_ string, _ string, fence Fence, _ uint64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	if !c.current.Equal(fence) {
		return fmt.Errorf("stale stop")
	}
	c.stops = append(c.stops, fence)
	return nil
}

func (c *loopbackCore) SendVadWithVoicedEnd(
	sample, voicedEnd uint64,
	probability, rms, noiseFloor float32,
	start bool,
) error {
	c.mu.Lock()
	c.vad = append(c.vad, mediaVADEvent{
		Start: start, Sample: sample, VoicedEnd: voicedEnd,
		Probability: probability, RMS: rms, NoiseFloor: noiseFloor,
	})
	c.mu.Unlock()
	return nil
}

func (c *loopbackCore) SendPlaybackProgress(progress PlaybackProgress) error {
	c.mu.Lock()
	c.playback = append(c.playback, progress)
	c.mu.Unlock()
	return nil
}

func (c *loopbackCore) SendClientEvent(_ []byte, eventType string, _ Fence, _ uint64) error {
	c.mu.Lock()
	c.clients = append(c.clients, eventType)
	c.mu.Unlock()
	return nil
}

func (c *loopbackCore) setFence(fence Fence) {
	c.mu.Lock()
	c.current = fence
	c.mu.Unlock()
}

func (c *loopbackCore) counts() (uplink, vad, stops, playback, clients int) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.uplink), len(c.vad), len(c.stops), len(c.playback), len(c.clients)
}

func (c *loopbackCore) uplinkFrames() []AudioFrame {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]AudioFrame(nil), c.uplink...)
}

func waitUntil(t *testing.T, timeout time.Duration, condition func() bool) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("condition did not become true")
}

func testMediaToken(t *testing.T, verifier JWTVerifier, request OpenSessionRequest) string {
	t.Helper()
	return signedMediaToken(t, verifier.Secret, map[string]any{
		"iss": verifier.Issuer, "aud": verifier.Audience, "sub": request.AccountID,
		"session_id": request.SessionID, "device_id": request.DeviceID,
		"client_type": request.ClientType, "stream_epoch": request.StreamEpoch,
		"exp": time.Now().Add(time.Minute).Unix(),
	})
}

type loopbackClient struct {
	pc       *webrtc.PeerConnection
	uplink   *webrtc.TrackLocalStaticSample
	channel  *webrtc.DataChannel
	open     chan struct{}
	events   chan []byte
	downlink chan []int16
}

func newLoopbackClient(t *testing.T) *loopbackClient {
	t.Helper()
	pc, err := webrtc.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		t.Fatal(err)
	}
	track, err := webrtc.NewTrackLocalStaticSample(
		webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus, ClockRate: opusSampleRate, Channels: opusRTPChannels},
		"microphone", "browser",
	)
	if err != nil {
		t.Fatal(err)
	}
	sender, err := pc.AddTrack(track)
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		for {
			if _, _, err := sender.ReadRTCP(); err != nil {
				return
			}
		}
	}()
	channel, err := pc.CreateDataChannel(webrtcEventChannelLabel, nil)
	if err != nil {
		t.Fatal(err)
	}
	client := &loopbackClient{
		pc: pc, uplink: track, channel: channel, open: make(chan struct{}),
		events: make(chan []byte, 64), downlink: make(chan []int16, 64),
	}
	var openOnce sync.Once
	channel.OnOpen(func() { openOnce.Do(func() { close(client.open) }) })
	channel.OnMessage(func(message webrtc.DataChannelMessage) {
		client.events <- append([]byte(nil), message.Data...)
	})
	pc.OnTrack(func(track *webrtc.TrackRemote, _ *webrtc.RTPReceiver) {
		go func() {
			decoder, decoderErr := pionopus.NewDecoderWithOutput(opusSampleRate, 1)
			if decoderErr != nil {
				return
			}
			for {
				packet, _, readErr := track.ReadRTP()
				if readErr != nil {
					return
				}
				decoded := make([]int16, 5_760)
				n, decodeErr := decoder.DecodeToInt16(packet.Payload, decoded)
				if decodeErr == nil && n > 0 {
					client.downlink <- append([]int16(nil), decoded[:n]...)
				}
			}
		}()
	})
	return client
}

func (c *loopbackClient) connect(t *testing.T, endpoint, token string) string {
	t.Helper()
	return connectPeer(t, c.pc, c.open, endpoint, token)
}

func (c *loopbackClient) restartICE(t *testing.T, endpoint, location, token string) {
	t.Helper()
	offer, err := c.pc.CreateOffer(&webrtc.OfferOptions{ICERestart: true})
	if err != nil {
		t.Fatal(err)
	}
	gatherComplete := webrtc.GatheringCompletePromise(c.pc)
	if err := c.pc.SetLocalDescription(offer); err != nil {
		t.Fatal(err)
	}
	select {
	case <-gatherComplete:
	case <-time.After(5 * time.Second):
		t.Fatal("client ICE restart gathering timed out")
	}
	request, err := http.NewRequest(
		http.MethodPatch, endpoint+location, strings.NewReader(c.pc.LocalDescription().SDP),
	)
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("Content-Type", "application/sdp")
	request.Header.Set("If-Match", `"`+strings.TrimPrefix(location, "/whip/")+`"`)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(response.Body)
		t.Fatalf("ICE restart status=%d body=%s", response.StatusCode, body)
	}
	answer, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeAnswer, SDP: string(answer)}); err != nil {
		t.Fatal(err)
	}
	waitUntil(t, 5*time.Second, func() bool {
		return c.pc.ConnectionState() == webrtc.PeerConnectionStateConnected
	})
}

func connectPeer(
	t *testing.T,
	pc *webrtc.PeerConnection,
	open <-chan struct{},
	endpoint, token string,
) string {
	t.Helper()
	offer, err := pc.CreateOffer(nil)
	if err != nil {
		t.Fatal(err)
	}
	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err := pc.SetLocalDescription(offer); err != nil {
		t.Fatal(err)
	}
	select {
	case <-gatherComplete:
	case <-time.After(5 * time.Second):
		t.Fatal("client ICE gathering timed out")
	}
	request, err := http.NewRequest(http.MethodPost, endpoint+"/whip", strings.NewReader(pc.LocalDescription().SDP))
	if err != nil {
		t.Fatal(err)
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("Content-Type", "application/sdp")
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusCreated {
		body, _ := io.ReadAll(response.Body)
		t.Fatalf("WHIP status=%d body=%s", response.StatusCode, body)
	}
	answer, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if err := pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeAnswer, SDP: string(answer)}); err != nil {
		t.Fatal(err)
	}
	select {
	case <-open:
	case <-time.After(5 * time.Second):
		t.Fatal("DataChannel did not open")
	}
	return response.Header.Get("Location")
}

type rtpLoopbackClient struct {
	pc      *webrtc.PeerConnection
	uplink  *webrtc.TrackLocalStaticRTP
	channel *webrtc.DataChannel
	open    chan struct{}
}

func newRTPLoopbackClient(t *testing.T) *rtpLoopbackClient {
	t.Helper()
	pc, err := webrtc.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		t.Fatal(err)
	}
	track, err := webrtc.NewTrackLocalStaticRTP(
		webrtc.RTPCodecCapability{MimeType: webrtc.MimeTypeOpus, ClockRate: opusSampleRate, Channels: opusRTPChannels},
		"microphone", "browser",
	)
	if err != nil {
		t.Fatal(err)
	}
	sender, err := pc.AddTrack(track)
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		for {
			if _, _, err := sender.ReadRTCP(); err != nil {
				return
			}
		}
	}()
	channel, err := pc.CreateDataChannel(webrtcEventChannelLabel, nil)
	if err != nil {
		t.Fatal(err)
	}
	client := &rtpLoopbackClient{pc: pc, uplink: track, channel: channel, open: make(chan struct{})}
	var once sync.Once
	channel.OnOpen(func() { once.Do(func() { close(client.open) }) })
	return client
}

func setupLoopback(t *testing.T) (
	*Server,
	*WebRTCTerminator,
	*loopbackCore,
	*httptest.Server,
	OpenSessionRequest,
	string,
) {
	t.Helper()
	verifier := JWTVerifier{
		Secret: []byte("media-token-secret-that-is-long-enough"),
		Issuer: "voice-agent", Audience: "memoria-media",
	}
	request := OpenSessionRequest{
		SessionID: "session-loopback", AccountID: "account-1", DeviceID: "browser-1",
		ClientType: "h5", StreamEpoch: 1,
	}
	server := NewServer(verifier, 32)
	core := newLoopbackCore()
	terminator, err := NewWebRTCTerminator(server, verifier, WebRTCTerminatorConfig{
		OnError: func(err error) { t.Logf("terminator: %v", err) },
	})
	if err != nil {
		t.Fatal(err)
	}
	server.RequireExternalDownlinkSender = true
	server.WHIPHandler = terminator.Handler()
	server.DownlinkSenderFactory = terminator.DownlinkSender
	server.DownlinkReadyProbe = terminator.Ready
	server.SessionCloseHook = terminator.CloseSession
	server.BridgeFactory = func(open OpenSessionRequest, session *Session, sender DownlinkSender) (*VoiceCoreMediaRuntime, error) {
		return NewVoiceCoreMediaRuntimeWithDownlinkSender(
			context.Background(), session, core, sender,
			func(event *mediav1.CoreToMedia) { terminator.ForwardCoreEvent(open, event) }, nil,
		)
	}
	httpServer := httptest.NewServer(server.Handler())
	return server, terminator, core, httpServer, request, testMediaToken(t, verifier, request)
}

func TestWebRTCTerminatorRealMediaAndDataChannel(t *testing.T) {
	server, terminator, core, httpServer, request, token := setupLoopback(t)
	defer httpServer.Close()
	defer func() { _ = terminator.Close() }()
	defer func() { _ = server.Close() }()
	client := newLoopbackClient(t)
	defer func() { _ = client.pc.Close() }()
	core.authority = mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW
	location := client.connect(t, httpServer.URL, token)
	if !strings.HasPrefix(location, "/whip/") {
		t.Fatalf("missing WHIP resource location: %q", location)
	}
	select {
	case raw := <-client.events:
		var event mediaDataEnvelope
		if err := json.Unmarshal(raw, &event); err != nil {
			t.Fatal(err)
		}
		var payload map[string]any
		if err := json.Unmarshal(event.Payload, &payload); err != nil {
			t.Fatal(err)
		}
		if event.Type != "session.ready" || payload["interaction_authority"] != "go_shadow" {
			t.Fatalf("missing negotiated authority in session.ready: event=%+v payload=%v", event, payload)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("session.ready was not relayed")
	}
	verifier := JWTVerifier{
		Secret: []byte("media-token-secret-that-is-long-enough"),
		Issuer: "voice-agent", Audience: "memoria-media",
	}
	forgedIdentity := request
	forgedIdentity.AccountID = "other-account"
	forgedDelete, err := http.NewRequest(http.MethodDelete, httpServer.URL+location, nil)
	if err != nil {
		t.Fatal(err)
	}
	forgedDelete.Header.Set("Authorization", "Bearer "+testMediaToken(t, verifier, forgedIdentity))
	forgedResponse, err := http.DefaultClient.Do(forgedDelete)
	if err != nil {
		t.Fatal(err)
	}
	_ = forgedResponse.Body.Close()
	if forgedResponse.StatusCode != http.StatusNotFound {
		t.Fatalf("cross-account WHIP delete status=%d", forgedResponse.StatusCode)
	}
	client.restartICE(t, httpServer.URL, location, token)

	uplinkEncoder, err := newOpusEncoder(opusSampleRate, 1)
	if err != nil {
		t.Fatal(err)
	}
	defer uplinkEncoder.close()
	uplinkPCM := make([]int16, opusFrameSamples)
	for index := range uplinkPCM {
		uplinkPCM[index] = int16(8_000 * math.Sin(2*math.Pi*440*float64(index)/opusSampleRate))
	}
	uplinkPacket := make([]byte, 4_000)
	n, err := uplinkEncoder.Encode(uplinkPCM, uplinkPacket)
	if err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if err := client.uplink.WriteSample(media.Sample{Data: uplinkPacket[:n], Duration: mediaFrameDuration}); err != nil {
			t.Fatal(err)
		}
	}
	waitUntil(t, 3*time.Second, func() bool {
		uplink, vad, _, _, _ := core.counts()
		return uplink >= 2 && vad >= 1
	})

	fence := Fence{SessionID: request.SessionID, TurnID: 1, GenerationID: 1, ToolEpoch: 2}
	core.setFence(fence)
	identity := &mediav1.SessionIdentity{
		SessionId: request.SessionID, AccountId: request.AccountID, DeviceId: request.DeviceID,
		ClientType: request.ClientType, StreamEpoch: request.StreamEpoch,
	}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: identity, TurnId: fence.TurnID, GenerationId: fence.GenerationID,
			ToolEpoch: fence.ToolEpoch, Action: mediav1.GenerationAction_GENERATION_ACTION_START,
			TaskEpoch: 3, ContextVersion: 7,
		},
	}}
	downlinkPCM := make([]byte, downlinkFrameSamples*2)
	for index := 0; index < downlinkFrameSamples; index++ {
		value := int16(8_000 * math.Sin(2*math.Pi*660*float64(index)/downlinkSampleRate))
		downlinkPCM[index*2] = byte(value)
		downlinkPCM[index*2+1] = byte(uint16(value) >> 8)
	}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Audio{
		Audio: &mediav1.AssistantAudioFrame{
			Identity: identity, TurnId: fence.TurnID, GenerationId: fence.GenerationID,
			ToolEpoch: fence.ToolEpoch, Sequence: 0, SourceStartSample: 0,
			FrameSamples: downlinkFrameSamples, PcmS16Le: downlinkPCM, FirstFrame: true, FinalFrame: true,
			TaskEpoch: 3, ContextVersion: 7,
		},
	}}
	select {
	case samples := <-client.downlink:
		if len(samples) != opusFrameSamples {
			t.Fatalf("decoded downlink samples=%d", len(samples))
		}
	case <-time.After(3 * time.Second):
		t.Fatal("downlink RTP was not received")
	}
	seenAudioMetadata := false
	deadline := time.After(3 * time.Second)
	for !seenAudioMetadata {
		select {
		case raw := <-client.events:
			var event mediaDataEnvelope
			if err := json.Unmarshal(raw, &event); err != nil {
				t.Fatal(err)
			}
			if event.Type == "assistant.audio.frame" {
				if event.TaskEpoch != 3 || event.ContextVersion != 7 {
					t.Fatalf("audio versions were not relayed: %+v", event)
				}
				seenAudioMetadata = true
			}
		case <-deadline:
			t.Fatal("assistant audio metadata was not relayed")
		}
	}
	projectionEnvelope, _ := json.Marshal(map[string]any{
		"v": 1, "protocol": "media-v1", "type": "turn.provisional.patch",
		"event_id": "projection-1", "session_id": request.SessionID,
		"stream_epoch": request.StreamEpoch, "sequence": 7,
		"turn_id": fence.TurnID, "generation_id": fence.GenerationID,
		"tool_epoch": fence.ToolEpoch, "task_epoch": 3, "context_version": 7,
		"server_monotonic_ms": 1,
		"payload":             map[string]any{"text": "你好", "revision": 1},
	})
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Transcript{
		Transcript: &mediav1.TranscriptEvent{
			Identity: identity, TurnId: fence.TurnID, GenerationId: fence.GenerationID,
			ToolEpoch: fence.ToolEpoch, Revision: 1, Text: "你好", Final: true,
			TaskEpoch: 3, ContextVersion: 7,
		},
	}}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_State{
		State: &mediav1.StateEvent{
			Identity: identity, TurnId: fence.TurnID, GenerationId: fence.GenerationID,
			ToolEpoch: fence.ToolEpoch, TaskEpoch: 3, ContextVersion: 7,
			State: mediav1.ConversationState_CONVERSATION_STATE_ASSISTANT_SPEAKING,
		},
	}}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Client{
		Client: &mediav1.ClientEvent{
			Identity: identity, Type: "turn.provisional.patch", JsonPayload: projectionEnvelope,
			TurnId: fence.TurnID, GenerationId: fence.GenerationID, ToolEpoch: fence.ToolEpoch,
			TaskEpoch: 3, ContextVersion: 7,
		},
	}}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Error{
		Error: &mediav1.CoreError{
			Identity: identity, Code: "test", Message: "diagnostic", Retryable: true,
			TurnId: fence.TurnID, GenerationId: fence.GenerationID, ToolEpoch: fence.ToolEpoch,
			TaskEpoch: 3, ContextVersion: 7,
		},
	}}
	wantedTypes := map[string]bool{
		"user.transcript.final": false, "assistant.state": false,
		"turn.provisional.patch": false, "error": false,
	}
	deadline = time.After(3 * time.Second)
	for remaining := len(wantedTypes); remaining > 0; {
		select {
		case raw := <-client.events:
			var event mediaDataEnvelope
			if err := json.Unmarshal(raw, &event); err != nil {
				t.Fatal(err)
			}
			if seen, wanted := wantedTypes[event.Type]; wanted && !seen {
				if event.TaskEpoch != 3 || event.ContextVersion != 7 {
					t.Fatalf("native event versions were not relayed: %+v", event)
				}
				wantedTypes[event.Type] = true
				remaining--
			}
		case <-deadline:
			t.Fatalf("Core events were not fully relayed: %+v", wantedTypes)
		}
	}

	sendClient := func(sequence uint64, eventType string, payload map[string]any) {
		t.Helper()
		raw, err := json.Marshal(map[string]any{
			"v": 1, "protocol": "media-v1", "type": eventType,
			"event_id": fmt.Sprintf("client-%d", sequence), "session_id": request.SessionID,
			"stream_epoch": request.StreamEpoch, "sequence": sequence,
			"turn_id": fence.TurnID, "generation_id": fence.GenerationID,
			"tool_epoch": fence.ToolEpoch, "server_monotonic_ms": 0, "payload": payload,
		})
		if err != nil {
			t.Fatal(err)
		}
		if err := client.channel.SendText(string(raw)); err != nil {
			t.Fatal(err)
		}
	}
	sendClient(0, "client.playback.progress", map[string]any{
		"received_sequence": 0, "rendered_sample_end": downlinkFrameSamples,
		"client_monotonic_ms": 100, "approximate": true,
		"turn_id": fence.TurnID, "generation_id": fence.GenerationID, "tool_epoch": fence.ToolEpoch,
	})
	sendClient(1, "client.text", map[string]any{"text": "继续"})
	waitUntil(t, time.Second, func() bool {
		_, _, _, playback, clients := core.counts()
		return playback == 1 && clients == 1
	})

	stop := map[string]any{
		"v": 1, "protocol": "media-v1", "type": "client.stop_assistant",
		"event_id": "stop-1", "session_id": request.SessionID,
		"stream_epoch": request.StreamEpoch, "sequence": 2,
		"turn_id": fence.TurnID, "generation_id": fence.GenerationID,
		"tool_epoch": fence.ToolEpoch, "server_monotonic_ms": 0,
		"payload": map[string]any{"reason": "test"},
	}
	rawStop, _ := json.Marshal(stop)
	if err := client.channel.SendText(string(rawStop)); err != nil {
		t.Fatal(err)
	}
	waitUntil(t, time.Second, func() bool {
		_, _, stops, _, _ := core.counts()
		return stops == 1
	})
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Audio{
		Audio: &mediav1.AssistantAudioFrame{
			Identity: identity, TurnId: fence.TurnID, GenerationId: fence.GenerationID,
			ToolEpoch: fence.ToolEpoch, Sequence: 1, SourceStartSample: downlinkFrameSamples,
			FrameSamples: downlinkFrameSamples, PcmS16Le: downlinkPCM, FinalFrame: true,
		},
	}}
	select {
	case <-client.downlink:
		t.Fatal("cancelled generation leaked old PCM")
	case <-time.After(100 * time.Millisecond):
	}
	cancelled := fence
	cancelled.GenerationID++
	core.setFence(cancelled)
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: identity, TurnId: cancelled.TurnID, GenerationId: cancelled.GenerationID,
			ToolEpoch: cancelled.ToolEpoch, Action: mediav1.GenerationAction_GENERATION_ACTION_CANCEL,
			Reason: "test",
		},
	}}
	seenFlush := false
	deadline = time.After(time.Second)
	for !seenFlush {
		select {
		case raw := <-client.events:
			var event mediaDataEnvelope
			_ = json.Unmarshal(raw, &event)
			seenFlush = event.Type == "playback.flush" && event.GenerationID == cancelled.GenerationID
		case <-deadline:
			t.Fatal("playback flush was not relayed")
		}
	}

	deleteRequest, err := http.NewRequest(http.MethodDelete, httpServer.URL+location, nil)
	if err != nil {
		t.Fatal(err)
	}
	deleteRequest.Header.Set("Authorization", "Bearer "+token)
	response, err := http.DefaultClient.Do(deleteRequest)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusNoContent {
		t.Fatalf("WHIP delete status=%d", response.StatusCode)
	}
}

func TestWebRTCTerminatorMapsFencedRealtimeEffectsToPlaybackEvents(t *testing.T) {
	server, terminator, core, httpServer, request, token := setupLoopback(t)
	defer httpServer.Close()
	defer func() { _ = terminator.Close() }()
	defer func() { _ = server.Close() }()
	client := newLoopbackClient(t)
	defer func() { _ = client.pc.Close() }()
	_ = client.connect(t, httpServer.URL, token)

	fence := Fence{SessionID: request.SessionID, TurnID: 1, GenerationID: 1, ToolEpoch: 2}
	identity := &mediav1.SessionIdentity{
		SessionId: request.SessionID, AccountId: request.AccountID, DeviceId: request.DeviceID,
		ClientType: request.ClientType, StreamEpoch: request.StreamEpoch,
	}
	core.setFence(fence)
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: identity, TurnId: fence.TurnID, GenerationId: fence.GenerationID,
			ToolEpoch: fence.ToolEpoch, Action: mediav1.GenerationAction_GENERATION_ACTION_START,
		},
	}}
	effect := func(kind mediav1.RealtimeEffectKind, value Fence, sequence uint64) *mediav1.CoreToMedia {
		payload := []byte(`{"reason":"test"}`)
		if kind == mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT {
			payload = []byte(`{"reason":"test","gain":0}`)
		}
		return &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_RealtimeEffect{
			RealtimeEffect: &mediav1.RealtimeEffect{
				EffectId: fmt.Sprintf("effect-%d", sequence), SessionId: request.SessionID,
				StreamEpoch: request.StreamEpoch, Sequence: sequence, EffectKind: kind,
				SourceEventId: "voice-core-effect", TurnId: value.TurnID,
				GenerationId: value.GenerationID, ToolEpoch: value.ToolEpoch,
				Payload: payload, Identity: identity,
			},
		}}
	}
	core.events <- effect(mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT, fence, 1)
	core.events <- effect(mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_RESUME_OUTPUT, fence, 2)
	cancelled := fence
	cancelled.GenerationID++
	core.setFence(cancelled)
	core.events <- effect(mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION, cancelled, 3)

	wanted := map[string]bool{
		"playback.duck": false, "playback.restore": false, "playback.flush": false,
	}
	deadline := time.After(3 * time.Second)
	for remaining := len(wanted); remaining > 0; {
		select {
		case raw := <-client.events:
			var event mediaDataEnvelope
			if err := json.Unmarshal(raw, &event); err != nil {
				t.Fatal(err)
			}
			if seen, expected := wanted[event.Type]; expected && !seen {
				if event.TurnID != fence.TurnID || event.ToolEpoch != fence.ToolEpoch {
					t.Fatalf("effect fence was not relayed: %+v", event)
				}
				if event.Type == "playback.flush" && event.GenerationID != cancelled.GenerationID {
					t.Fatalf("cancel fence was not relayed: %+v", event)
				}
				if event.Type == "playback.duck" {
					var payload map[string]any
					if err := json.Unmarshal(event.Payload, &payload); err != nil || payload["gain"] != float64(0) {
						t.Fatalf("duck gain was not relayed: payload=%v err=%v", payload, err)
					}
				}
				wanted[event.Type] = true
				remaining--
			}
		case <-deadline:
			t.Fatalf("realtime effects were not fully relayed: %+v", wanted)
		}
	}

	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Audio{
		Audio: &mediav1.AssistantAudioFrame{
			Identity: identity, TurnId: fence.TurnID, GenerationId: fence.GenerationID,
			ToolEpoch: fence.ToolEpoch, Sequence: 0, SourceStartSample: 0,
			FrameSamples: downlinkFrameSamples, PcmS16Le: make([]byte, downlinkFrameSamples*2),
		},
	}}
	select {
	case <-client.downlink:
		t.Fatal("cancelled realtime effect leaked old PCM")
	case <-time.After(100 * time.Millisecond):
	}
}

func TestWebRTCTerminatorRelaysTypedFloorEffects(t *testing.T) {
	server, terminator, core, httpServer, request, token := setupLoopback(t)
	defer httpServer.Close()
	defer func() { _ = terminator.Close() }()
	defer func() { _ = server.Close() }()
	client := newLoopbackClient(t)
	defer func() { _ = client.pc.Close() }()
	_ = client.connect(t, httpServer.URL, token)

	fence := Fence{SessionID: request.SessionID, TurnID: 1, GenerationID: 1, ToolEpoch: 2}
	identity := &mediav1.SessionIdentity{
		SessionId: request.SessionID, AccountId: request.AccountID, DeviceId: request.DeviceID,
		ClientType: request.ClientType, StreamEpoch: request.StreamEpoch,
	}
	core.setFence(fence)
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: identity, TurnId: fence.TurnID, GenerationId: fence.GenerationID,
			ToolEpoch: fence.ToolEpoch, Action: mediav1.GenerationAction_GENERATION_ACTION_START,
		},
	}}
	core.events <- &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_FloorEffect{
		FloorEffect: &mediav1.FloorEffect{
			EffectId: "floor-1", Identity: identity, Sequence: 1,
			FloorState: mediav1.FloorState_FLOOR_STATE_USER_HOLDS_FLOOR,
			FloorEpoch: 1, SourceEventId: "assistant-state:user-speaking",
			TurnId: fence.TurnID, GenerationId: fence.GenerationID, ToolEpoch: fence.ToolEpoch,
			ExpiresAtMs: uint64(time.Now().Add(10 * time.Second).UnixMilli()),
		},
	}}

	deadline := time.After(3 * time.Second)
	for {
		select {
		case raw := <-client.events:
			var event mediaDataEnvelope
			if err := json.Unmarshal(raw, &event); err != nil {
				t.Fatal(err)
			}
			if event.Type != "floor.state" {
				continue
			}
			if event.TurnID != fence.TurnID || event.GenerationID != fence.GenerationID || event.ToolEpoch != fence.ToolEpoch {
				t.Fatalf("floor fence was not relayed: %+v", event)
			}
			var payload map[string]any
			if err := json.Unmarshal(event.Payload, &payload); err != nil {
				t.Fatal(err)
			}
			if payload["floor_state"] != "user_holds_floor" ||
				payload["floor_epoch"] != float64(1) ||
				payload["source_event_id"] != "assistant-state:user-speaking" {
				t.Fatalf("floor payload was not relayed: %v", payload)
			}
			return
		case <-deadline:
			t.Fatal("typed floor effect was not relayed")
		}
	}
}

func TestWebRTCTerminatorRejectsMissingOrForgedIdentity(t *testing.T) {
	server, terminator, _, httpServer, _, _ := setupLoopback(t)
	defer httpServer.Close()
	defer func() { _ = terminator.Close() }()
	defer func() { _ = server.Close() }()
	for name, authorization := range map[string]string{
		"missing": "",
		"forged":  "Bearer invalid.token.value",
	} {
		t.Run(name, func(t *testing.T) {
			request, err := http.NewRequest(http.MethodPost, httpServer.URL+"/whip", strings.NewReader("v=0\r\n"))
			if err != nil {
				t.Fatal(err)
			}
			request.Header.Set("Content-Type", "application/sdp")
			if authorization != "" {
				request.Header.Set("Authorization", authorization)
			}
			response, err := http.DefaultClient.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = response.Body.Close() }()
			if response.StatusCode != http.StatusUnauthorized {
				t.Fatalf("status=%d", response.StatusCode)
			}
		})
	}
}

func TestWebRTCTerminatorReadinessRequiresConfiguredCandidateType(t *testing.T) {
	server := NewServer(JWTVerifier{}, 1)
	terminator, err := NewWebRTCTerminator(server, JWTVerifier{}, WebRTCTerminatorConfig{
		RequireRelayCandidate: true,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = terminator.Close() }()
	if terminator.Ready() {
		t.Fatal("relay-only readiness accepted a host-only candidate")
	}
}

func TestWebRTCTerminatorMarksRTPLossAndDropsOutOfOrder(t *testing.T) {
	server, terminator, core, httpServer, _, token := setupLoopback(t)
	defer httpServer.Close()
	defer func() { _ = terminator.Close() }()
	defer func() { _ = server.Close() }()
	client := newRTPLoopbackClient(t)
	defer func() { _ = client.pc.Close() }()
	_ = connectPeer(t, client.pc, client.open, httpServer.URL, token)
	encoder, err := newOpusEncoder(opusSampleRate, 1)
	if err != nil {
		t.Fatal(err)
	}
	defer encoder.close()
	pcm := make([]int16, opusFrameSamples)
	for index := range pcm {
		pcm[index] = int16(8_000 * math.Sin(2*math.Pi*440*float64(index)/opusSampleRate))
	}
	payload := make([]byte, 4_000)
	n, err := encoder.Encode(pcm, payload)
	if err != nil {
		t.Fatal(err)
	}
	write := func(sequence uint16, timestamp uint32) {
		t.Helper()
		if err := client.uplink.WriteRTP(&rtp.Packet{
			Header:  rtp.Header{Version: 2, SequenceNumber: sequence, Timestamp: timestamp, SSRC: 1},
			Payload: payload[:n],
		}); err != nil {
			t.Fatal(err)
		}
	}
	write(100, 0)
	write(102, 1_920)
	write(101, 960)
	waitUntil(t, 3*time.Second, func() bool {
		uplink, _, _, _, _ := core.counts()
		return uplink >= 3
	})
	frames := core.uplinkFrames()
	if len(frames) != 3 {
		t.Fatalf("uplink frames=%d, want 3", len(frames))
	}
	for index, frame := range frames {
		if frame.Sequence != uint64(index) || frame.CaptureStartSample != uint64(index*uplinkFrameSamples) {
			t.Fatalf("frame %d clock=%+v", index, frame)
		}
		if frame.LossConcealed != (index == 1) {
			t.Fatalf("frame %d loss_concealed=%v", index, frame.LossConcealed)
		}
	}
	if server.OpusFECFrames.Load()+server.OpusPLCFrames.Load() == 0 {
		t.Fatal("lost RTP packet was not decoded with Opus PLC/FEC")
	}
}

func TestWebRTCTerminatorReconnectReplacesOnlyWithNewerEpoch(t *testing.T) {
	server, terminator, _, httpServer, firstRequest, firstToken := setupLoopback(t)
	defer httpServer.Close()
	defer func() { _ = terminator.Close() }()
	defer func() { _ = server.Close() }()
	var coresMu sync.Mutex
	var cores []*loopbackCore
	server.BridgeFactory = func(open OpenSessionRequest, session *Session, sender DownlinkSender) (*VoiceCoreMediaRuntime, error) {
		core := newLoopbackCore()
		coresMu.Lock()
		cores = append(cores, core)
		coresMu.Unlock()
		return NewVoiceCoreMediaRuntimeWithDownlinkSender(
			context.Background(), session, core, sender,
			func(event *mediav1.CoreToMedia) { terminator.ForwardCoreEvent(open, event) }, nil,
		)
	}
	first := newLoopbackClient(t)
	defer func() { _ = first.pc.Close() }()
	firstLocation := first.connect(t, httpServer.URL, firstToken)
	secondRequest := firstRequest
	secondRequest.StreamEpoch++
	verifier := JWTVerifier{
		Secret: []byte("media-token-secret-that-is-long-enough"),
		Issuer: "voice-agent", Audience: "memoria-media",
	}
	second := newLoopbackClient(t)
	defer func() { _ = second.pc.Close() }()
	secondLocation := second.connect(t, httpServer.URL, testMediaToken(t, verifier, secondRequest))
	if secondLocation == firstLocation {
		t.Fatal("reconnect reused the old WHIP resource")
	}
	waitUntil(t, 3*time.Second, func() bool {
		session, ok := server.Directory.Get(firstRequest.SessionID)
		return ok && session.Epoch() == secondRequest.StreamEpoch
	})
	waitUntil(t, 3*time.Second, func() bool {
		coresMu.Lock()
		defer coresMu.Unlock()
		return len(cores) == 2
	})
	coresMu.Lock()
	coreCount := len(cores)
	coresMu.Unlock()
	if coreCount != 2 {
		t.Fatalf("Voice Core streams=%d, want 2", coreCount)
	}
	deleteOld, err := http.NewRequest(http.MethodDelete, httpServer.URL+firstLocation, nil)
	if err != nil {
		t.Fatal(err)
	}
	deleteOld.Header.Set("Authorization", "Bearer "+firstToken)
	response, err := http.DefaultClient.Do(deleteOld)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusNotFound {
		t.Fatalf("old WHIP resource status=%d", response.StatusCode)
	}
	if current, ok := server.Directory.Get(firstRequest.SessionID); !ok || current.Epoch() != secondRequest.StreamEpoch {
		t.Fatal("deleting the old resource affected the replacement session")
	}
	if server.CloseWebRTCSession(firstRequest) {
		t.Fatal("old epoch close deleted the replacement session")
	}
	terminator.HandleBridgeError(firstRequest, fmt.Errorf("late old-epoch bridge error"))
	if current, ok := server.Directory.Get(firstRequest.SessionID); !ok || current.Epoch() != secondRequest.StreamEpoch {
		t.Fatal("old-epoch bridge error closed the replacement session")
	}
}

func TestWebRTCPeerFailurePreventsActivationBeforeCleanupGetsLock(t *testing.T) {
	for _, test := range []struct {
		name    string
		trigger func(*webRTCPeer)
	}{
		{"connection state", func(peer *webRTCPeer) {
			peer.onConnectionStateChange(webrtc.PeerConnectionStateFailed)
		}},
		{"runtime error", func(peer *webRTCPeer) {
			peer.terminator.failPeer(peer, fmt.Errorf("pending runtime failed"))
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := NewServer(JWTVerifier{}, 1)
			terminator := &WebRTCTerminator{
				server: server, pending: make(map[string]*webRTCPeer),
				active: make(map[string]*webRTCPeer), resource: make(map[string]*webRTCPeer),
			}
			pc, err := webrtc.NewPeerConnection(webrtc.Configuration{})
			if err != nil {
				t.Fatal(err)
			}
			request := OpenSessionRequest{
				SessionID: "session-race", AccountID: "account-1", DeviceID: "browser-1",
				ClientType: "h5", StreamEpoch: 2,
			}
			peer := &webRTCPeer{
				terminator: terminator, request: request, resourceID: "pending-resource", pc: pc,
				runtimeReady: make(chan struct{}),
			}
			terminator.pending[peerKey(request)] = peer
			terminator.resource[peer.resourceID] = peer

			terminator.mu.Lock()
			triggerReturned := make(chan struct{})
			go func() {
				test.trigger(peer)
				close(triggerReturned)
			}()
			deadline := time.Now().Add(time.Second)
			for !peer.closed.Load() && time.Now().Before(deadline) {
				time.Sleep(time.Millisecond)
			}
			if !peer.closed.Load() {
				terminator.mu.Unlock()
				t.Fatal("failure was not published before cleanup acquired the terminator lock")
			}
			if err := terminator.canActivateLocked(peer); err == nil {
				terminator.mu.Unlock()
				t.Fatal("failed peer remained eligible to replace the active epoch")
			}
			terminator.mu.Unlock()
			select {
			case <-triggerReturned:
			case <-time.After(time.Second):
				t.Fatal("failure cleanup did not return")
			}
			waitUntil(t, time.Second, func() bool {
				return pc.ConnectionState() == webrtc.PeerConnectionStateClosed
			})
		})
	}
}

func TestCanActivateRejectsClosedNativePeerBeforeCallbackRuns(t *testing.T) {
	pc, err := webrtc.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		t.Fatal(err)
	}
	if err := pc.Close(); err != nil {
		t.Fatal(err)
	}
	request := OpenSessionRequest{
		SessionID: "session-native-closed", AccountID: "account-1", DeviceID: "browser-1",
		ClientType: "h5", StreamEpoch: 2,
	}
	terminator := &WebRTCTerminator{
		pending: make(map[string]*webRTCPeer), active: make(map[string]*webRTCPeer),
		resource: make(map[string]*webRTCPeer),
	}
	peer := &webRTCPeer{request: request, resourceID: "pending-resource", pc: pc}
	terminator.pending[peerKey(request)] = peer
	terminator.resource[peer.resourceID] = peer
	if peer.closed.Load() {
		t.Fatal("test precondition failed: wrapper was already marked closed")
	}
	if err := terminator.canActivateLocked(peer); err == nil {
		t.Fatal("native closed peer was accepted before its callback updated wrapper state")
	}
}

func TestDecodeClientEnvelopeAcceptsExtensionsAndRejectsWrongEpoch(t *testing.T) {
	request := OpenSessionRequest{SessionID: "s", AccountID: "a", DeviceID: "d", ClientType: "h5", StreamEpoch: 2}
	base := `{"v":1,"protocol":"media-v1","type":"client.text","event_id":"e","session_id":"s","stream_epoch":2,"sequence":0,"turn_id":0,"generation_id":0,"tool_epoch":0,"client_monotonic_ms":12,"payload":{}}`
	if _, err := decodeClientEnvelope([]byte(base), request); err != nil {
		t.Fatal(err)
	}
	versioned := strings.Replace(
		base,
		`"client_monotonic_ms":12,`,
		`"client_monotonic_ms":12,"task_epoch":3,"context_version":7,`,
		1,
	)
	decoded, err := decodeClientEnvelope([]byte(versioned), request)
	if err != nil || decoded.TaskEpoch != 3 || decoded.ContextVersion != 7 {
		t.Fatalf("versioned envelope was not preserved: envelope=%+v err=%v", decoded, err)
	}
	if _, err := decodeClientEnvelope([]byte(strings.Replace(base, `"payload":{}`, `"future_extension":true,"payload":{}`, 1)), request); err != nil {
		t.Fatalf("future extension should remain forward-compatible: %v", err)
	}
	legacy := strings.Replace(base, `"client_monotonic_ms":12`, `"server_monotonic_ms":0`, 1)
	if _, err := decodeClientEnvelope([]byte(legacy), request); err != nil {
		t.Fatalf("legacy zero server clock should remain compatible: %v", err)
	}
	for _, raw := range []string{
		strings.Replace(base, `"stream_epoch":2`, `"stream_epoch":1`, 1),
		strings.Replace(base, `"client_monotonic_ms":12`, `"server_monotonic_ms":1`, 1),
		strings.Replace(base, `"payload":{}`, `"server_monotonic_ms":0,"payload":{}`, 1),
	} {
		if _, err := decodeClientEnvelope([]byte(raw), request); err == nil {
			t.Fatal("invalid envelope accepted")
		}
	}
}

func TestDataChannelLanesKeepControlIndependentFromEphemeralTraffic(t *testing.T) {
	tests := map[string]dataChannelLane{
		"playback.flush":          dataChannelControl,
		"floor.state":             dataChannelControl,
		"turn.committed":          dataChannelConversation,
		"assistant.audio.frame":   dataChannelConversation,
		"user.transcript.partial": dataChannelEphemeral,
		"turn.provisional.patch":  dataChannelEphemeral,
	}
	for eventType, expected := range tests {
		if actual := dataChannelLaneForEvent(eventType); actual != expected {
			t.Fatalf("event %q lane=%d want=%d", eventType, actual, expected)
		}
	}

	pending := make([]pendingDataEvent, 0, maxPendingDataEvents)
	for range maxPendingEphemeral {
		pending = append(pending, pendingDataEvent{lane: dataChannelEphemeral})
	}
	pending = append(pending, pendingDataEvent{lane: dataChannelControl})
	trimmed, dropped := dropOldestPendingLane(pending, dataChannelEphemeral)
	if !dropped || len(trimmed) != len(pending)-1 {
		t.Fatalf("ephemeral event was not evicted: dropped=%v len=%d", dropped, len(trimmed))
	}
	if trimmed[len(trimmed)-1].lane != dataChannelControl {
		t.Fatal("control event was displaced by ephemeral eviction")
	}
}

func TestOpusEncoderRejectsUseAfterClose(t *testing.T) {
	encoder, err := newOpusEncoder(opusSampleRate, 1)
	if err != nil {
		t.Fatal(err)
	}
	encoder.close()
	if _, err := encoder.Encode(make([]int16, opusFrameSamples), make([]byte, 4_000)); err == nil {
		t.Fatal("closed encoder accepted PCM")
	}
	if err := encoder.Reset(); err == nil {
		t.Fatal("closed encoder reset succeeded")
	}
}
