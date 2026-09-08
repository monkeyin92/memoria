package mediaedge

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
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

	"github.com/gorilla/websocket"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

type deviceTestCore struct {
	mu        sync.Mutex
	events    chan *mediav1.CoreToMedia
	closed    chan struct{}
	closeOnce sync.Once
	current   Fence
	uplink    []AudioFrame
	vad       []mediaVADEvent
	stops     []Fence
	keywords  []string
	playback  []PlaybackProgress
	authority mediav1.InteractionAuthority
}

func newDeviceTestCore() *deviceTestCore {
	return &deviceTestCore{
		events:    make(chan *mediav1.CoreToMedia, 64),
		closed:    make(chan struct{}),
		authority: mediav1.InteractionAuthority_INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE,
	}
}

func (c *deviceTestCore) SendAudio(frame AudioFrame) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.uplink = append(c.uplink, frame)
	return nil
}

func (c *deviceTestCore) Recv() (*mediav1.CoreToMedia, error) {
	select {
	case event := <-c.events:
		return event, nil
	case <-c.closed:
		return nil, io.EOF
	}
}

func (c *deviceTestCore) Close() error {
	c.closeOnce.Do(func() { close(c.closed) })
	return nil
}

func (c *deviceTestCore) CurrentFence() Fence {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.current
}

func (c *deviceTestCore) InteractionAuthority() mediav1.InteractionAuthority {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.authority
}

func (c *deviceTestCore) SendStop(_ string, _ string, fence Fence, _ uint64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stops = append(c.stops, fence)
	return nil
}

func (c *deviceTestCore) SendVadWithVoicedEnd(sample, voicedEnd uint64, probability, rms, noiseFloor float32, start bool) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.vad = append(c.vad, mediaVADEvent{
		Start: start, Sample: sample, VoicedEnd: voicedEnd,
		Probability: probability, RMS: rms, NoiseFloor: noiseFloor,
	})
	return nil
}

func (c *deviceTestCore) SendKeywordAtFence(keyword string, _ float32, _, _ uint64, _ bool, _ Fence, _ uint64) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.keywords = append(c.keywords, keyword)
	return nil
}

func (c *deviceTestCore) SendPlaybackProgress(progress PlaybackProgress) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.playback = append(c.playback, progress)
	return nil
}

func (c *deviceTestCore) inject(event *mediav1.CoreToMedia) {
	c.events <- event
}

type deviceTestEnv struct {
	server     *DeviceWSServer
	httpServer *httptest.Server
	wsURL      string
	privateKey ed25519.PrivateKey

	mu       sync.Mutex
	cores    map[string]*deviceTestCore
	requests map[string]OpenSessionRequest
}

func newDeviceTestEnv(t *testing.T, configure func(*DeviceWSServer)) *deviceTestEnv {
	t.Helper()
	publicKey, privateKey, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	verifier := DeviceJWTVerifier{
		PublicKeys: map[string]ed25519.PublicKey{"test-key": publicKey},
		Issuer:     "memoria-control-api",
		Audience:   "memoria-media-edge",
		MaxTTL:     5 * time.Minute,
		ClockSkew:  5 * time.Second,
	}
	env := &deviceTestEnv{
		server:     NewDeviceWSServer(verifier),
		privateKey: privateKey,
		cores:      make(map[string]*deviceTestCore),
		requests:   make(map[string]OpenSessionRequest),
	}
	env.server.RequireRuntime = true
	env.server.RuntimeFactory = func(
		request OpenSessionRequest,
		session *Session,
		sender DownlinkSender,
	) (*VoiceCoreMediaRuntime, error) {
		core := newDeviceTestCore()
		env.mu.Lock()
		env.cores[request.SessionID] = core
		env.requests[request.SessionID] = request
		env.mu.Unlock()
		return NewVoiceCoreMediaRuntimeWithDownlinkSender(
			context.Background(), session, core, sender,
			func(event *mediav1.CoreToMedia) {
				env.server.ForwardCoreEventForSession(request, event)
			},
			nil,
		)
	}
	if configure != nil {
		configure(env.server)
	}
	mux := http.NewServeMux()
	mux.Handle(DeviceMediaEndpoint, env.server)
	mux.HandleFunc("/metrics", func(w http.ResponseWriter, _ *http.Request) {
		env.server.WriteMetrics(w)
	})
	env.httpServer = httptest.NewServer(mux)
	t.Cleanup(func() {
		env.server.Close()
		env.httpServer.Close()
	})
	env.wsURL = "ws" + strings.TrimPrefix(env.httpServer.URL, "http") + DeviceMediaEndpoint
	return env
}

func (env *deviceTestEnv) token(t *testing.T, mutate func(*DeviceMediaClaims)) string {
	t.Helper()
	claims := DeviceMediaClaims{
		Type: DeviceTokenType, SessionID: "session_1", Subject: "account_1",
		DeviceID: "dev_1", ClientID: "client_1", BindingID: "binding_1",
		BindingVersion: 3, ClientType: "device", StreamEpoch: 18,
		SubjectID: "subject_1", RuntimeProfileVersion: 27,
		DeviceSettings: DeviceSettingsClaim{
			SettingsVersion: 4, VolumeLimit: 72, ScreenBrightness: 80,
			LearningMode: "off", AudioMode: DeviceAudioModeHalfDuplexSafe,
			WakeMode: "button_or_keyword", AllowedBargeIn: []string{"button"},
		},
		JTI:    "ticket_1",
		Issuer: "memoria-control-api", Audience: "memoria-media-edge",
		Expiry:    time.Now().Add(5 * time.Minute).Unix(),
		NotBefore: time.Now().Add(-time.Minute).Unix(),
		IssuedAt:  time.Now().Unix(),
	}
	if mutate != nil {
		mutate(&claims)
	}
	return signDeviceToken(t, env.privateKey, claims, "EdDSA")
}

func (env *deviceTestEnv) dial(t *testing.T, token, clientID string) (*websocket.Conn, *http.Response) {
	t.Helper()
	header := http.Header{}
	header.Set("Authorization", "Bearer "+token)
	header.Set("Protocol-Version", "2")
	if clientID != "" {
		header.Set("X-Client-ID", clientID)
	}
	connection, response, err := websocket.DefaultDialer.Dial(env.wsURL, header)
	if err != nil {
		return nil, response
	}
	t.Cleanup(func() { _ = connection.Close() })
	return connection, response
}

func deviceV2Hello() deviceHelloV2 {
	return deviceHelloV2{
		Type: "device.hello", Version: 2,
		DeviceID: "dev_1", FirmwareVersion: "0.2.0",
		BoardProfile: "memoria-atk-dnesp32s3-v1", StreamEpoch: 18,
		Audio: deviceAudioV2{
			UplinkCodec: "opus", UplinkSampleRate: 16_000,
			DownlinkSampleRates: []uint64{16_000, 24_000},
			Channels:            1, FrameMS: 20,
		},
		Capabilities: deviceCapabilitiesV2{
			Display: true, Microphone: true, Speaker: true,
			SimultaneousCapturePlayback: true, AECMode: "fd_low_cost",
			AECReference: "software_post_gain_pre_i2s", AECReferenceVerified: true,
			LocalVAD: true, LocalStopKeyword: true, PhysicalStopButton: true,
			PlaybackWatermark: "exact", LocalDuck: true, BargeInLevel: 1,
		},
	}
}

func writeDeviceJSON(t *testing.T, connection *websocket.Conn, value any) {
	t.Helper()
	payload, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	if err := connection.WriteMessage(websocket.TextMessage, payload); err != nil {
		t.Fatal(err)
	}
}

func readDeviceMessage(connection *websocket.Conn, timeout time.Duration) (int, []byte, error) {
	_ = connection.SetReadDeadline(time.Now().Add(timeout))
	return connection.ReadMessage()
}

func deviceReadAccepted(t *testing.T, connection *websocket.Conn) deviceSessionAccepted {
	t.Helper()
	messageType, payload, err := readDeviceMessage(connection, 3*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if messageType != websocket.TextMessage {
		t.Fatalf("first device message is not text: %d", messageType)
	}
	var accepted deviceSessionAccepted
	if err := json.Unmarshal(payload, &accepted); err != nil {
		t.Fatal(err)
	}
	if accepted.Type != "session.accepted" {
		t.Fatalf("expected session.accepted, got %s", accepted.Type)
	}
	return accepted
}

func encodeUplinkOpusFrame(t *testing.T, encoder *opusEncoder, epoch uint32, sequence uint32, sampleStart uint64) []byte {
	t.Helper()
	samples := make([]int16, DeviceUplinkFrameSamples)
	for index := range samples {
		samples[index] = int16(math.Sin(2*math.Pi*440*float64(index)/16_000) * 8000)
	}
	encoded := make([]byte, 1500)
	size, err := encoder.Encode(samples, encoded)
	if err != nil {
		t.Fatal(err)
	}
	wire, err := (MemoriaAudioFrameV1{
		Version: 1, Direction: DeviceDirectionUplink,
		StreamEpoch: epoch, Sequence: sequence, SampleStart: sampleStart,
		FrameSamples: DeviceUplinkFrameSamples, GenerationID: 0,
		Payload: encoded[:size],
	}).MarshalBinary()
	if err != nil {
		t.Fatal(err)
	}
	return wire
}

func deviceCoreIdentity(sessionID string, epoch uint64) *mediav1.SessionIdentity {
	return &mediav1.SessionIdentity{
		SessionId: sessionID, AccountId: "account_1", DeviceId: "dev_1",
		ClientType: "device", StreamEpoch: epoch, SubjectId: "subject_1",
		BindingId: "binding_1", BindingVersion: 3, RuntimeProfileVersion: 27,
	}
}

func deviceGenerationEvent(sessionID string, epoch uint64, sequence uint64, turnID, generationID uint64, action mediav1.GenerationAction) *mediav1.CoreToMedia {
	return &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: deviceCoreIdentity(sessionID, epoch), Sequence: sequence,
			TurnId: turnID, GenerationId: generationID, ToolEpoch: 0,
			SessionEpoch: 1,
			Action:       action, Reason: "test",
		},
	}}
}

func deviceAssistantExpressionEvent(sessionID string, epoch, turnID, generationID uint64, expression string) *mediav1.CoreToMedia {
	payload, err := json.Marshal(map[string]any{
		"v": 1, "protocol": "media-v1", "type": "assistant_expression",
		"payload": map[string]any{"expression": expression},
	})
	if err != nil {
		panic(err)
	}
	return &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Client{
		Client: &mediav1.ClientEvent{
			Identity: deviceCoreIdentity(sessionID, epoch),
			Type:     "assistant_expression",
			JsonPayload: payload,
			TurnId: turnID, GenerationId: generationID,
		},
	}}
}

func deviceClosedStateEvent(sessionID string, epoch uint64, sequence uint64, reason string) *mediav1.CoreToMedia {
	return &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_State{
		State: &mediav1.StateEvent{
			Identity: deviceCoreIdentity(sessionID, epoch), Sequence: sequence,
			State:  mediav1.ConversationState_CONVERSATION_STATE_CLOSED,
			Reason: reason,
		},
	}}
}

func deviceAudioEvent(sessionID string, epoch uint64, sequence uint64, sourceStart uint64, samples []int16, turnID, generationID uint64) *mediav1.CoreToMedia {
	payload := int16ToLittleEndian(samples)
	return &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Audio{
		Audio: &mediav1.AssistantAudioFrame{
			Identity: deviceCoreIdentity(sessionID, epoch), Sequence: sequence,
			SourceStartSample: sourceStart, FrameSamples: uint32(len(samples)),
			PcmS16Le: payload, TurnId: turnID, GenerationId: generationID, ToolEpoch: 0,
			SessionEpoch: 1,
		},
	}}
}

func TestDeviceWSSV2SessionAcceptedAndUplinkForwarded(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	accepted := deviceReadAccepted(t, connection)
	if accepted.AudioMode != DeviceAudioModeHalfDuplexSafe {
		t.Fatalf("audio mode = %s, want requested half_duplex_safe", accepted.AudioMode)
	}
	if accepted.DownlinkSampleRate != 24_000 {
		t.Fatalf("downlink rate = %d, want 24000", accepted.DownlinkSampleRate)
	}
	if accepted.InteractionAuthority != "python_authoritative" {
		t.Fatalf("interaction authority = %s", accepted.InteractionAuthority)
	}
	if accepted.SessionID != "session_1" || accepted.StreamEpoch != 18 {
		t.Fatalf("accepted identity mismatch: %+v", accepted)
	}
	if accepted.DeviceSettings.SettingsVersion != 4 ||
		accepted.DeviceSettings.VolumeLimit != 72 {
		t.Fatalf("signed device settings were not projected: %+v", accepted.DeviceSettings)
	}
	env.mu.Lock()
	request := env.requests["session_1"]
	env.mu.Unlock()
	if request.SubjectID != "subject_1" || request.BindingID != "binding_1" ||
		request.BindingVersion != 3 || request.RuntimeProfileVersion != 27 ||
		request.AudioMode != DeviceAudioModeHalfDuplexSafe {
		t.Fatalf("ticket authority fence did not reach runtime factory: %+v", request)
	}
	encoder, err := newOpusEncoder(16_000, 1)
	if err != nil {
		t.Fatal(err)
	}
	defer encoder.close()
	frame := encodeUplinkOpusFrame(t, encoder, 18, 0, 0)
	if err := connection.WriteMessage(websocket.BinaryMessage, frame); err != nil {
		t.Fatal(err)
	}
	env.mu.Lock()
	core := env.cores["session_1"]
	env.mu.Unlock()
	waitUntil(t, 3*time.Second, func() bool {
		core.mu.Lock()
		defer core.mu.Unlock()
		return len(core.uplink) == 1
	})
	core.mu.Lock()
	uplink := core.uplink[0]
	core.mu.Unlock()
	if uplink.Sequence != 0 || uplink.CaptureStartSample != 0 || uplink.FrameSamples != 320 {
		t.Fatalf("uplink frame metadata mismatch: %+v", uplink)
	}
	payload, err := uplink.Payload()
	if err != nil || len(payload) != 640 {
		t.Fatalf("uplink PCM payload mismatch: len=%d err=%v", len(payload), err)
	}
	if env.server.metrics.uplinkFrames.Load() != 1 {
		t.Fatalf("uplink frames metric = %d, want 1", env.server.metrics.uplinkFrames.Load())
	}
}

func TestDeviceWSSDirectRejectsV1Hello(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	hello := deviceHelloV1{
		Type: "device.hello", Version: 1,
		DeviceID: "dev_1", FirmwareVersion: "0.1.0",
		BoardProfile: "memoria-atk-dnesp32s3-v1", StreamEpoch: 18,
		Audio: deviceLegacyAudio{
			UplinkCodec: "opus", UplinkSampleRate: 16_000,
			DownlinkSampleRate: 24_000, Channels: 1, FrameMS: 20,
		},
		Capabilities: deviceLegacyCapabilities{
			Display: true, Microphone: true, Speaker: true,
			DeviceAEC: true, PhysicalButton: true,
		},
	}
	writeDeviceJSON(t, connection, hello)
	if _, _, err := readDeviceMessage(connection, 3*time.Second); err == nil {
		t.Fatal("direct v2 endpoint accepted a v1 hello")
	}
}

func TestDeviceWSSFullDuplexRequiresAcousticAttestation(t *testing.T) {
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.Acoustic.Add(DeviceAcousticProfile{
			BoardProfile: "memoria-atk-dnesp32s3-v1", ProfileVersion: 4,
			Verified: true, MaxBargeInLevel: 2,
		})
	})
	connection, _ := env.dial(t, env.token(t, func(claims *DeviceMediaClaims) {
		claims.DeviceSettings.AudioMode = DeviceAudioModeFullDuplex
		claims.DeviceSettings.AllowedBargeIn = []string{"button", "keyword", "voice"}
	}), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	accepted := deviceReadAccepted(t, connection)
	if accepted.AudioMode != DeviceAudioModeFullDuplex {
		t.Fatalf("audio mode = %s, want full_duplex_verified", accepted.AudioMode)
	}
	if accepted.AcousticAttestation == nil || !accepted.AcousticAttestation.Verified ||
		accepted.AcousticAttestation.ProfileVersion != 4 {
		t.Fatalf("missing acoustic attestation: %+v", accepted.AcousticAttestation)
	}
}

func TestDeviceWSSFullDuplexSettingWithoutVoicePermissionDowngrades(t *testing.T) {
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.Acoustic.Add(DeviceAcousticProfile{
			BoardProfile: "memoria-atk-dnesp32s3-v1", ProfileVersion: 4,
			Verified: true, MaxBargeInLevel: 2,
		})
	})
	connection, _ := env.dial(t, env.token(t, func(claims *DeviceMediaClaims) {
		claims.DeviceSettings.AudioMode = DeviceAudioModeFullDuplex
		claims.DeviceSettings.AllowedBargeIn = []string{"button"}
	}), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	accepted := deviceReadAccepted(t, connection)
	if accepted.AudioMode != DeviceAudioModeInterruptAssist {
		t.Fatalf("audio mode = %s, want interrupt_assist", accepted.AudioMode)
	}
	if accepted.AcousticAttestation != nil {
		t.Fatalf("downgraded mode leaked full-duplex attestation: %+v", accepted.AcousticAttestation)
	}
}

func TestDeviceWSSRequestedInterruptAssistCannotExceedDeviceCapability(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, func(claims *DeviceMediaClaims) {
		claims.DeviceSettings.AudioMode = DeviceAudioModeInterruptAssist
	}), "client_1")
	hello := deviceV2Hello()
	writeDeviceJSON(t, connection, hello)
	accepted := deviceReadAccepted(t, connection)
	if accepted.AudioMode != DeviceAudioModeInterruptAssist {
		t.Fatalf("audio mode = %s, want requested/capable interrupt_assist", accepted.AudioMode)
	}

	limitedToken := env.token(t, func(claims *DeviceMediaClaims) {
		claims.SessionID = "session_limited"
		claims.StreamEpoch = 19
		claims.JTI = "ticket_limited"
		claims.DeviceSettings.AudioMode = DeviceAudioModeInterruptAssist
	})
	limited, _ := env.dial(t, limitedToken, "client_1")
	limitedHello := deviceV2Hello()
	limitedHello.StreamEpoch = 19
	limitedHello.Capabilities.LocalVAD = false
	limitedHello.Capabilities.PhysicalStopButton = false
	limitedHello.Capabilities.LocalStopKeyword = false
	writeDeviceJSON(t, limited, limitedHello)
	accepted = deviceReadAccepted(t, limited)
	if accepted.AudioMode != DeviceAudioModeHalfDuplexSafe {
		t.Fatalf("requested interrupt exceeded device capability: %s", accepted.AudioMode)
	}
}

func TestDeviceWSSFullDuplexRefusedWhenRegistryUnverified(t *testing.T) {
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.Acoustic.Add(DeviceAcousticProfile{
			BoardProfile: "memoria-atk-dnesp32s3-v1", ProfileVersion: 4,
			Verified: false,
		})
	})
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	accepted := deviceReadAccepted(t, connection)
	if accepted.AudioMode == DeviceAudioModeFullDuplex {
		t.Fatal("full duplex opened with an unverified registry entry")
	}
	if accepted.AcousticAttestation != nil {
		t.Fatal("attestation must be null without full duplex")
	}
}

func TestDeviceWSSFullDuplexRefusedAboveRegisteredBargeLevel(t *testing.T) {
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.Acoustic.Add(DeviceAcousticProfile{
			BoardProfile: "memoria-atk-dnesp32s3-v1", ProfileVersion: 4,
			Verified: true, MaxBargeInLevel: 0,
		})
	})
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	accepted := deviceReadAccepted(t, connection)
	if accepted.AudioMode == DeviceAudioModeFullDuplex {
		t.Fatal("full duplex opened above the registered barge-in level")
	}
}

func TestDeviceWSSForwardsAssistantExpressionToScreen(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	env.mu.Lock()
	request := env.requests["session_1"]
	env.mu.Unlock()
	env.server.ForwardCoreEventForSession(
		request,
		deviceAssistantExpressionEvent("session_1", 18, 1, 2, "caring"),
	)
	_, payload, err := readDeviceMessage(connection, 3*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	var expression deviceScreenExpression
	if err := json.Unmarshal(payload, &expression); err != nil {
		t.Fatalf("payload=%s err=%v", payload, err)
	}
	if expression.Type != "screen.expression" || expression.Expression != "loving" {
		t.Fatalf("unexpected screen expression: %+v", expression)
	}
	if expression.Fence.TurnID != 1 || expression.Fence.GenerationID != 2 {
		t.Fatalf("expression fence = %+v", expression.Fence)
	}
}

func TestDeviceWSSDownlinkAudioAndGenerationControls(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	env.mu.Lock()
	core := env.cores["session_1"]
	env.mu.Unlock()
	core.inject(deviceGenerationEvent("session_1", 18, 1, 1, 1, mediav1.GenerationAction_GENERATION_ACTION_START))
	samples := make([]int16, 480)
	for index := range samples {
		samples[index] = int16(math.Sin(2*math.Pi*440*float64(index)/24_000) * 8000)
	}
	core.inject(deviceAudioEvent("session_1", 18, 0, 0, samples, 1, 1))
	core.inject(deviceAudioEvent("session_1", 18, 1, 480, samples, 1, 1))

	messageType, payload, err := readDeviceMessage(connection, 3*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if messageType != websocket.TextMessage {
		t.Fatalf("expected generation.started text, got %d", messageType)
	}
	var generation deviceGenerationControl
	if err := json.Unmarshal(payload, &generation); err != nil || generation.Type != "generation.started" {
		t.Fatalf("unexpected control: %s err=%v", payload, err)
	}
	if generation.Fence.GenerationID != 1 {
		t.Fatalf("generation fence = %+v", generation.Fence)
	}
	var frames []MemoriaAudioFrameV1
	for len(frames) < 2 {
		messageType, payload, err = readDeviceMessage(connection, 3*time.Second)
		if err != nil {
			t.Fatal(err)
		}
		if messageType != websocket.BinaryMessage {
			t.Fatalf("expected binary audio, got %d: %s", messageType, payload)
		}
		var frame MemoriaAudioFrameV1
		if err := frame.UnmarshalBinary(payload); err != nil {
			t.Fatal(err)
		}
		frames = append(frames, frame)
	}
	if frames[0].FrameSamples != 480 || frames[0].GenerationID != 1 ||
		frames[0].Direction != DeviceDirectionDownlink || frames[0].StreamEpoch != 18 {
		t.Fatalf("downlink frame mismatch: %+v", frames[0])
	}
	if frames[0].Sequence != 0 || frames[0].SampleStart != 0 {
		t.Fatalf("first downlink frame must start at sequence/sample zero: %+v", frames[0])
	}
	if frames[1].Sequence != 1 || frames[1].SampleStart != 480 {
		t.Fatalf("second downlink frame mismatch: %+v", frames[1])
	}

	core.inject(deviceGenerationEvent("session_1", 18, 2, 1, 2, mediav1.GenerationAction_GENERATION_ACTION_CANCEL))
	messageType, payload, err = readDeviceMessage(connection, 3*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if messageType != websocket.TextMessage {
		t.Fatalf("expected generation.cancelled, got %d", messageType)
	}
	if !strings.Contains(string(payload), "generation.cancelled") {
		t.Fatalf("unexpected payload: %s", payload)
	}
	core.inject(deviceAudioEvent("session_1", 18, 2, 960, samples, 1, 1))
	if _, _, err := readDeviceMessage(connection, 400*time.Millisecond); err == nil {
		t.Fatal("stale generation audio reached the device")
	}
	if env.server.metrics.downlinkFrames.Load() != 2 {
		t.Fatalf("downlink frames = %d, want 2", env.server.metrics.downlinkFrames.Load())
	}
}

func TestDeviceWSSUnknownSafeSessionKeepsBindingFenceAndEmptySubject(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, func(claims *DeviceMediaClaims) {
		claims.SubjectID = ""
	}), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	accepted := deviceReadAccepted(t, connection)
	if accepted.SessionID != "session_1" || accepted.StreamEpoch != 18 {
		t.Fatalf("unknown_safe session did not accept the signed binding: %+v", accepted)
	}
	env.mu.Lock()
	request := env.requests["session_1"]
	env.mu.Unlock()
	if request.SubjectID != "" || request.BindingID != "binding_1" ||
		request.BindingVersion != 3 || request.RuntimeProfileVersion != 27 {
		t.Fatalf("unknown_safe runtime subject or binding fence was not preserved: %+v", request)
	}

	// An empty runtime subject is an explicit fence value: core events that
	// echo the empty subject are accepted, and the identity round trip keeps
	// the empty subject on the wire.
	identity := &mediav1.SessionIdentity{
		SessionId: "session_1", AccountId: "account_1", DeviceId: "dev_1",
		ClientType: "device", StreamEpoch: 18, BindingId: "binding_1",
		BindingVersion: 3, RuntimeProfileVersion: 27,
	}
	env.mu.Lock()
	core := env.cores["session_1"]
	env.mu.Unlock()
	core.inject(&mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Generation{
		Generation: &mediav1.GenerationControl{
			Identity: identity, Sequence: 1, TurnId: 1, GenerationId: 1,
			ToolEpoch: 0, Action: mediav1.GenerationAction_GENERATION_ACTION_START,
			Reason: "test",
		},
	}})
	messageType, payload, err := readDeviceMessage(connection, 3*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if messageType != websocket.TextMessage ||
		!strings.Contains(string(payload), "generation.started") {
		t.Fatalf("empty-subject identity did not pass the runtime fence: type=%d payload=%s", messageType, payload)
	}
}

func TestDeviceWSSReconnectTakesOverAndEqualEpochRejected(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	first, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, first, deviceV2Hello())
	deviceReadAccepted(t, first)
	env.mu.Lock()
	firstRequest := env.requests["session_1"]
	env.mu.Unlock()

	secondToken := env.token(t, func(claims *DeviceMediaClaims) {
		claims.StreamEpoch = 19
		claims.JTI = "ticket_2"
	})
	second, _ := env.dial(t, secondToken, "client_1")
	hello := deviceV2Hello()
	hello.StreamEpoch = 19
	writeDeviceJSON(t, second, hello)
	accepted := deviceReadAccepted(t, second)
	if accepted.StreamEpoch != 19 {
		t.Fatalf("reconnect epoch = %d, want 19", accepted.StreamEpoch)
	}
	if _, _, err := readDeviceMessage(first, 3*time.Second); err == nil {
		t.Fatal("superseded connection was not closed")
	}
	if env.server.metrics.reconnectTotal.Load() != 1 {
		t.Fatalf("reconnect metric = %d, want 1", env.server.metrics.reconnectTotal.Load())
	}
	env.mu.Lock()
	secondRequest := env.requests["session_1"]
	env.mu.Unlock()
	// A late event and EOF from the superseded Voice Core stream share the
	// same stable Session id, but their old epoch must not mutate or close the
	// replacement socket.
	env.server.ForwardCoreEventForSession(firstRequest, deviceGenerationEvent(
		"session_1", 18, 1, 9, 9,
		mediav1.GenerationAction_GENERATION_ACTION_START,
	))
	env.server.HandleBridgeError(firstRequest, fmt.Errorf("late old-epoch bridge error"))
	env.server.ForwardCoreEventForSession(secondRequest, deviceGenerationEvent(
		"session_1", 19, 1, 1, 1,
		mediav1.GenerationAction_GENERATION_ACTION_START,
	))
	_, payload, err := readDeviceMessage(second, 3*time.Second)
	if err != nil || !strings.Contains(string(payload), `"generation_id":1`) ||
		strings.Contains(string(payload), `"generation_id":9`) {
		t.Fatalf("old epoch affected replacement connection: payload=%s err=%v", payload, err)
	}
	status := env.server.RuntimeStatus("dev_1")
	if !status.Connected || status.StreamEpoch != 19 {
		t.Fatalf("late old-epoch bridge error closed replacement: %+v", status)
	}

	equalToken := env.token(t, func(claims *DeviceMediaClaims) {
		claims.JTI = "ticket_3"
	})
	equal, _ := env.dial(t, equalToken, "client_1")
	writeDeviceJSON(t, equal, deviceV2Hello())
	if _, _, err := readDeviceMessage(equal, 3*time.Second); err == nil {
		t.Fatal("equal-epoch second connection was not closed")
	}
	if env.server.metrics.leaseRejected.Load() != 1 {
		t.Fatalf("lease rejected metric = %d, want 1", env.server.metrics.leaseRejected.Load())
	}
}

func TestDeviceWSSReplayJTIRejected(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	token := env.token(t, nil)
	first, _ := env.dial(t, token, "client_1")
	writeDeviceJSON(t, first, deviceV2Hello())
	deviceReadAccepted(t, first)
	_, response := env.dial(t, token, "client_1")
	if response == nil || response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("replayed ticket was accepted: response=%v", response)
	}
	if env.server.metrics.authRejected.Load() == 0 {
		t.Fatal("auth rejected metric not incremented")
	}
}

func TestDeviceWSSExpiredTokenRejected(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	token := env.token(t, func(claims *DeviceMediaClaims) {
		claims.Expiry = time.Now().Add(-time.Minute).Unix()
	})
	_, response := env.dial(t, token, "client_1")
	if response == nil || response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("expired token was accepted: %v", response)
	}
}

func TestDeviceWSSCrossDeviceHelloRejected(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	hello := deviceV2Hello()
	hello.DeviceID = "dev_other"
	writeDeviceJSON(t, connection, hello)
	if _, _, err := readDeviceMessage(connection, 3*time.Second); err == nil {
		t.Fatal("cross-device hello was accepted")
	}
}

func TestDeviceWSSClientMismatchRejected(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	_, response := env.dial(t, env.token(t, nil), "client_other")
	if response == nil || response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("client mismatch was accepted: %v", response)
	}
}

func TestDeviceWSSOldGatewayTicketRejected(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	token := env.token(t, func(claims *DeviceMediaClaims) {
		claims.Type = "memoria_device_gateway"
	})
	_, response := env.dial(t, token, "client_1")
	if response == nil || response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("legacy gateway ticket was accepted: %v", response)
	}
}

func TestDeviceWSSRateLimitClosesConnection(t *testing.T) {
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.ControlRatePerSec = 5
		server.ControlBurst = 3
	})
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	sequence := uint64(1)
	for index := 0; index < 10; index++ {
		writeDeviceJSON(t, connection, deviceStateEvent{
			deviceEventBase: deviceEventBase{
				Type: "device.telemetry", Version: 2, StreamEpoch: 18,
				ControlSequence: sequence, DeviceMonotonicMS: uint64(index + 1),
			},
			Value: map[string]any{"rssi": -60},
		})
		sequence++
	}
	if _, _, err := readDeviceMessage(connection, 3*time.Second); err == nil {
		t.Fatal("rate-limited connection stayed open")
	}
	if env.server.metrics.rateRejected.Load() == 0 {
		t.Fatal("rate rejected metric not incremented")
	}
}

func TestDeviceWSSOversizeControlClosesConnection(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	oversize := strings.Repeat("x", DeviceMaxControlBytes+1)
	if err := connection.WriteMessage(websocket.TextMessage, []byte(oversize)); err != nil {
		t.Fatal(err)
	}
	if _, _, err := readDeviceMessage(connection, 3*time.Second); err == nil {
		t.Fatal("oversize control stayed open")
	}
	if env.server.metrics.oversizeRejected.Load() == 0 {
		t.Fatal("oversize rejected metric not incremented")
	}
}

func TestDeviceWSSVADKeywordButtonPlaybackMapped(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, func(claims *DeviceMediaClaims) {
		claims.DeviceSettings.AllowedBargeIn = []string{"button", "keyword"}
	}), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	env.mu.Lock()
	core := env.cores["session_1"]
	env.mu.Unlock()
	core.mu.Lock()
	core.current = Fence{SessionID: "session_1", TurnID: 1, GenerationID: 1, SessionEpoch: 1}
	core.mu.Unlock()
	core.inject(deviceGenerationEvent(
		"session_1", 18, 1, 1, 1,
		mediav1.GenerationAction_GENERATION_ACTION_START,
	))
	if _, payload, err := readDeviceMessage(connection, 3*time.Second); err != nil ||
		!strings.Contains(string(payload), "generation.started") {
		t.Fatalf("generation 1 did not become active: payload=%s err=%v", payload, err)
	}

	writeDeviceJSON(t, connection, deviceVADEvent{
		deviceEventBase: deviceEventBase{
			Type: "vad.start", Version: 2, StreamEpoch: 18,
			ControlSequence: 1, DeviceMonotonicMS: 100,
		},
		SamplePosition: 3200, Probability: 0.9, NearEndRMS: 500,
	})
	writeDeviceJSON(t, connection, deviceVADEvent{
		deviceEventBase: deviceEventBase{
			Type: "vad.end", Version: 2, StreamEpoch: 18,
			ControlSequence: 2, DeviceMonotonicMS: 400,
		},
		SamplePosition: 9600, VoicedEndSample: 9200, Probability: 0.4, NearEndRMS: 80,
	})
	writeDeviceJSON(t, connection, deviceKeywordEvent{
		deviceEventBase: deviceEventBase{
			Type: "keyword.detected", Version: 2, StreamEpoch: 18,
			ControlSequence: 3, DeviceMonotonicMS: 500,
		},
		KeywordID: "stop", Confidence: 0.92, HardStop: false,
		Evidence: deviceInterruptionEvidence{
			DetectedSample: 9800, Source: "local_kws", DurationMS: 240, AECMode: "fd_low_cost",
			AECVerified: false, VADProbability: 0.9, NearEndRMS: 600,
			FarEndRMS: 1200, SpeakerClass: "owner",
		},
		ExpectedFence: deviceFence{TurnID: 1, GenerationID: 1, SessionEpoch: 1},
	})
	serverConn := env.server.connectionBySession("session_1")
	if serverConn == nil || serverConn.ledger == nil {
		t.Fatal("server playback ledger is unavailable")
	}
	serverConn.ledger.recordSent(deviceFence{TurnID: 1, GenerationID: 1, SessionEpoch: 1}, 0, 320)
	writeDeviceJSON(t, connection, devicePlaybackReceipt{
		deviceEventBase: deviceEventBase{
			Type: "playback.started", Version: 2, StreamEpoch: 18,
			ControlSequence: 4, DeviceMonotonicMS: 600,
		},
		Fence:            deviceFence{TurnID: 1, GenerationID: 1, SessionEpoch: 1},
		ReceivedSequence: 0, RenderedSampleEnd: 320, Approximate: false,
	})
	writeDeviceJSON(t, connection, deviceButtonStop{
		deviceEventBase: deviceEventBase{
			Type: "button.stop", Version: 2, StreamEpoch: 18,
			ControlSequence: 5, DeviceMonotonicMS: 700,
		},
		ExpectedFence:       deviceFence{TurnID: 1, GenerationID: 1, SessionEpoch: 1},
		LocalFlushSampleEnd: 10_000,
	})

	waitUntil(t, 3*time.Second, func() bool {
		core.mu.Lock()
		defer core.mu.Unlock()
		return len(core.vad) == 2 && len(core.keywords) == 1 &&
			len(core.stops) == 1 && len(core.playback) == 1
	})
	core.mu.Lock()
	defer core.mu.Unlock()
	if !core.vad[0].Start || core.vad[0].Sample != 3200 {
		t.Fatalf("vad.start mismatch: %+v", core.vad[0])
	}
	if core.vad[1].Start || core.vad[1].Sample != 9600 || core.vad[1].VoicedEnd != 9200 {
		t.Fatalf("vad.end mismatch: %+v", core.vad[1])
	}
	if core.keywords[0] != "stop" {
		t.Fatalf("keyword = %s", core.keywords[0])
	}
	// The target board keeps its native 24 kHz device clock, so the receipt
	// watermark remains in the same sample domain as the media-v1 bridge.
	if len(core.playback) != 1 || core.playback[0].RenderedSampleEnd != 320 {
		t.Fatalf("playback progress mismatch: %+v", core.playback)
	}
	if len(core.stops) != 1 {
		t.Fatalf("button stop not forwarded: %+v", core.stops)
	}
}

func TestDeviceWSSUplinkSampleGapCloses(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	encoder, err := newOpusEncoder(16_000, 1)
	if err != nil {
		t.Fatal(err)
	}
	defer encoder.close()
	first := encodeUplinkOpusFrame(t, encoder, 18, 0, 0)
	if err := connection.WriteMessage(websocket.BinaryMessage, first); err != nil {
		t.Fatal(err)
	}
	gapped := encodeUplinkOpusFrame(t, encoder, 18, 1, 5000)
	if err := connection.WriteMessage(websocket.BinaryMessage, gapped); err != nil {
		t.Fatal(err)
	}
	if _, _, err := readDeviceMessage(connection, 3*time.Second); err == nil {
		t.Fatal("sample-gap connection stayed open")
	}
	if env.server.metrics.uplinkGapSamples.Load() != 4680 {
		t.Fatalf("gap samples = %d, want 4680", env.server.metrics.uplinkGapSamples.Load())
	}
}

func TestDeviceWSSUplinkInitialClockMustStartAtZero(t *testing.T) {
	tests := []struct {
		name        string
		sequence    uint32
		sampleStart uint64
	}{
		{name: "nonzero sequence", sequence: 1, sampleStart: 0},
		{name: "nonzero sample", sequence: 0, sampleStart: 320},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			env := newDeviceTestEnv(t, nil)
			connection, _ := env.dial(t, env.token(t, nil), "client_1")
			writeDeviceJSON(t, connection, deviceV2Hello())
			deviceReadAccepted(t, connection)
			encoder, err := newOpusEncoder(16_000, 1)
			if err != nil {
				t.Fatal(err)
			}
			defer encoder.close()
			frame := encodeUplinkOpusFrame(
				t,
				encoder,
				18,
				tc.sequence,
				tc.sampleStart,
			)
			if err := connection.WriteMessage(websocket.BinaryMessage, frame); err != nil {
				t.Fatal(err)
			}
			if _, _, err := readDeviceMessage(connection, 3*time.Second); err == nil {
				t.Fatal("nonzero initial uplink clock stayed connected")
			}
		})
	}
}

func TestDeviceWSSFirstFrameMustBeHello(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	if err := connection.WriteMessage(websocket.BinaryMessage, []byte{0x01, 0x02}); err != nil {
		t.Fatal(err)
	}
	if _, _, err := readDeviceMessage(connection, 3*time.Second); err == nil {
		t.Fatal("non-hello first frame stayed open")
	}
}

func TestDeviceWSSRequiresV2ProtocolHeaderBeforeConsumingTicket(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	token := env.token(t, nil)
	header := http.Header{}
	header.Set("Authorization", "Bearer "+token)
	header.Set("X-Client-ID", "client_1")
	_, response, err := websocket.DefaultDialer.Dial(env.wsURL, header)
	if err == nil || response == nil || response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("missing v2 header was accepted: response=%v err=%v", response, err)
	}
	// Header rejection happens before JTI consumption; the same one-time token
	// remains usable by a correctly negotiated v2 connection.
	connection, response := env.dial(t, token, "client_1")
	if connection == nil || (response != nil && response.StatusCode >= 400) {
		t.Fatalf("header rejection consumed the ticket: response=%v", response)
	}
}

func TestDeviceWSSRejectsLegacyClientIDHeaderBeforeConsumingTicket(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	token := env.token(t, nil)
	header := http.Header{}
	header.Set("Authorization", "Bearer "+token)
	header.Set("Protocol-Version", "2")
	header.Set("Client-Id", "client_1")
	_, response, err := websocket.DefaultDialer.Dial(env.wsURL, header)
	if err == nil || response == nil || response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("legacy client header was accepted: response=%v err=%v", response, err)
	}
	// Header rejection happens before JTI consumption; a correctly bound v2
	// request can still use the same one-time token.
	connection, response := env.dial(t, token, "client_1")
	if connection == nil || (response != nil && response.StatusCode >= 400) {
		t.Fatalf("legacy header rejection consumed the ticket: response=%v", response)
	}
}

func TestDeviceWSSBadTokenNoLeak(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	header := http.Header{}
	header.Set("Authorization", "Bearer not-a-jwt")
	header.Set("X-Client-ID", "client_1")
	header.Set("Protocol-Version", "2")
	_, response, err := websocket.DefaultDialer.Dial(env.wsURL, header)
	if err == nil {
		t.Fatal("bad token was accepted")
	}
	if response == nil || response.StatusCode != http.StatusUnauthorized {
		t.Fatalf("bad token response = %v", response)
	}
	if strings.Contains(response.Header.Get("WWW-Authenticate"), "not-a-jwt") {
		t.Fatal("token material leaked in a header")
	}
}

func TestDeviceWSSControlPriorityIntegration(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	env.mu.Lock()
	core := env.cores["session_1"]
	env.mu.Unlock()
	core.inject(deviceGenerationEvent("session_1", 18, 1, 1, 1, mediav1.GenerationAction_GENERATION_ACTION_START))
	samples := make([]int16, 480)
	for sequence := uint64(0); sequence < 12; sequence++ {
		core.inject(deviceAudioEvent("session_1", 18, sequence, sequence*480, samples, 1, 1))
	}
	core.inject(&mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_RealtimeEffect{
		RealtimeEffect: &mediav1.RealtimeEffect{
			Identity: deviceCoreIdentity("session_1", 18), Sequence: 2,
			SessionId: "session_1", StreamEpoch: 18,
			EffectId: "flush-1", EffectKind: mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION,
			SourceEventId: "test-cancel", Payload: []byte(`{"reason":"test"}`),
			TurnId: 1, GenerationId: 2, ToolEpoch: 0, SessionEpoch: 1,
		},
	}})
	deadline := time.Now().Add(3 * time.Second)
	_ = connection.SetReadDeadline(deadline)
	for {
		messageType, payload, err := connection.ReadMessage()
		if err != nil {
			t.Fatal(err)
		}
		if messageType != websocket.TextMessage {
			continue
		}
		if strings.Contains(string(payload), "playback.flush") {
			return
		}
	}
}

func TestDeviceWSSMetricsExposeDeviceBlock(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	encoder, err := newOpusEncoder(16_000, 1)
	if err != nil {
		t.Fatal(err)
	}
	defer encoder.close()
	frame := encodeUplinkOpusFrame(t, encoder, 18, 0, 0)
	if err := connection.WriteMessage(websocket.BinaryMessage, frame); err != nil {
		t.Fatal(err)
	}
	waitUntil(t, 3*time.Second, func() bool {
		return env.server.metrics.uplinkFrames.Load() == 1
	})
	request, _ := http.NewRequest(http.MethodGet, env.httpServer.URL+"/metrics", nil)
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	body, err := io.ReadAll(response.Body)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(body), "device_media_connect_success_total 1") {
		t.Fatalf("metrics block missing connect success:\n%s", body)
	}
	if !strings.Contains(string(body), "device_uplink_frames_total 1") {
		t.Fatalf("metrics block missing uplink frames:\n%s", body)
	}
}

func TestDeviceWSSRejectsV1BeforeCreatingRuntime(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	hello := deviceHelloV1{
		Type: "device.hello", Version: 1,
		DeviceID: "dev_1", FirmwareVersion: "0.1.0",
		BoardProfile: "memoria-atk-dnesp32s3-v1", StreamEpoch: 18,
		Audio: deviceLegacyAudio{
			UplinkCodec: "opus", UplinkSampleRate: 16_000,
			DownlinkSampleRate: 24_000, Channels: 1, FrameMS: 20,
		},
		Capabilities: deviceLegacyCapabilities{
			Display: true, Microphone: true, Speaker: true,
			DeviceAEC: true, PhysicalButton: true,
		},
	}
	writeDeviceJSON(t, connection, hello)
	if _, _, err := readDeviceMessage(connection, 3*time.Second); err == nil {
		t.Fatal("direct v2 endpoint accepted a v1 hello")
	}
	env.mu.Lock()
	_, created := env.cores["session_1"]
	env.mu.Unlock()
	if created {
		t.Fatal("v1 hello created a Voice Core runtime")
	}
}

func TestDeviceWSSUplinkRateLimitsBinaryFrames(t *testing.T) {
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.AudioRatePerSec = 10
		server.AudioBurst = 5
	})
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	encoder, err := newOpusEncoder(16_000, 1)
	if err != nil {
		t.Fatal(err)
	}
	defer encoder.close()
	for sequence := uint32(0); sequence < 20; sequence++ {
		frame := encodeUplinkOpusFrame(t, encoder, 18, sequence, uint64(sequence)*320)
		if err := connection.WriteMessage(websocket.BinaryMessage, frame); err != nil {
			// The server is expected to close as soon as the limiter trips; a
			// client-side broken pipe on a later burst write is valid evidence,
			// not a flaky test failure.
			break
		}
	}
	if _, _, err := readDeviceMessage(connection, 3*time.Second); err == nil {
		t.Fatal("audio rate-limited connection stayed open")
	}
	if env.server.metrics.rateRejected.Load() == 0 {
		t.Fatal("audio rate rejected metric not incremented")
	}
}

func TestDeviceWSSUplinkFramePayloadDecodedToCore(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	env.mu.Lock()
	core := env.cores["session_1"]
	env.mu.Unlock()
	encoder, err := newOpusEncoder(16_000, 1)
	if err != nil {
		t.Fatal(err)
	}
	defer encoder.close()
	frame := encodeUplinkOpusFrame(t, encoder, 18, 0, 0)
	if err := connection.WriteMessage(websocket.BinaryMessage, frame); err != nil {
		t.Fatal(err)
	}
	waitUntil(t, 3*time.Second, func() bool {
		core.mu.Lock()
		defer core.mu.Unlock()
		return len(core.uplink) == 1
	})
	core.mu.Lock()
	payload, err := core.uplink[0].Payload()
	core.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	decoded := make([]int16, len(payload)/2)
	for index := range decoded {
		decoded[index] = int16(payload[index*2]) | int16(payload[index*2+1])<<8
	}
	if len(decoded) != 320 {
		t.Fatalf("decoded PCM samples = %d, want 320", len(decoded))
	}
	energy := 0.0
	for _, sample := range decoded {
		energy += float64(sample) * float64(sample)
	}
	if energy < 1000 {
		t.Fatalf("decoded PCM is silent: energy=%f", energy)
	}
}

func TestDeviceWSSSessionCloseCleansUpLease(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	if env.server.Leases.ActiveCount() != 1 {
		t.Fatalf("active leases = %d, want 1", env.server.Leases.ActiveCount())
	}
	writeDeviceJSON(t, connection, deviceSessionClose{
		deviceEventBase: deviceEventBase{
			Type: "session.close", Version: 2, StreamEpoch: 18,
			ControlSequence: 1, DeviceMonotonicMS: 1,
		},
		Value: struct {
			Reason string `json:"reason"`
		}{Reason: "device_close"},
	})
	waitUntil(t, 3*time.Second, func() bool {
		return env.server.Leases.ActiveCount() == 0
	})
	reconnectToken := env.token(t, func(claims *DeviceMediaClaims) {
		claims.StreamEpoch = 19
		claims.JTI = "ticket_2"
	})
	reconnect, _ := env.dial(t, reconnectToken, "client_1")
	hello := deviceV2Hello()
	hello.StreamEpoch = 19
	writeDeviceJSON(t, reconnect, hello)
	accepted := deviceReadAccepted(t, reconnect)
	if accepted.StreamEpoch != 19 {
		t.Fatalf("reconnect after close epoch = %d", accepted.StreamEpoch)
	}
}

func TestDeviceRuntimeInvalidationDeliveredAndReplayedIdempotently(t *testing.T) {
	env := newDeviceTestEnv(t, nil)
	connection, _ := env.dial(t, env.token(t, nil), "client_1")
	writeDeviceJSON(t, connection, deviceV2Hello())
	deviceReadAccepted(t, connection)
	status := env.server.RuntimeStatus("dev_1")
	if !status.Connected || status.SessionID != "session_1" || status.StreamEpoch != 18 ||
		status.ProtocolVersion != 2 || status.FirmwareVersion != "0.2.0" ||
		status.BoardProfile != "memoria-atk-dnesp32s3-v1" ||
		status.RuntimeProfileVersion != 27 || status.SettingsVersion != 4 ||
		status.AudioModeRequested != DeviceAudioModeHalfDuplexSafe ||
		status.AudioModeEffective != DeviceAudioModeHalfDuplexSafe || status.ConnectedAt == "" {
		t.Fatalf("unexpected live runtime status: %+v", status)
	}
	writeDeviceJSON(t, connection, deviceRuntimeProfileApplied{
		deviceEventBase: deviceEventBase{
			Type: "runtime_profile.applied", Version: 2, StreamEpoch: 18,
			ControlSequence: 1, DeviceMonotonicMS: 100,
		},
		ProfileVersion: 27, SettingsVersion: 4,
	})
	waitUntil(t, 3*time.Second, func() bool {
		applied := env.server.RuntimeStatus("dev_1")
		return applied.AppliedProfileVersion == 27 && applied.AppliedSettingsVersion == 4 &&
			applied.ProfileAppliedAt != ""
	})

	delivered, err := env.server.InvalidateRuntimeProfile(
		"dev_1", 28, "next_safe_point",
	)
	if err != nil || !delivered {
		t.Fatalf("runtime invalidation was not delivered: delivered=%v err=%v", delivered, err)
	}
	messageType, payload, err := readDeviceMessage(connection, 3*time.Second)
	if err != nil || messageType != websocket.TextMessage {
		t.Fatalf("runtime invalidation frame missing: type=%d err=%v", messageType, err)
	}
	var event deviceRuntimeProfileInvalidated
	if err := json.Unmarshal(payload, &event); err != nil {
		t.Fatal(err)
	}
	if event.Type != "runtime_profile.invalidated" || event.ProfileVersion != 28 ||
		event.ApplyAt != "next_safe_point" {
		t.Fatalf("invalid runtime event: %+v", event)
	}
	writeDeviceJSON(t, connection, deviceRuntimeProfileApplied{
		deviceEventBase: deviceEventBase{
			Type: "runtime_profile.applied", Version: 2, StreamEpoch: 18,
			ControlSequence: 2, DeviceMonotonicMS: 200,
		},
		ProfileVersion: 28, SettingsVersion: 4,
	})
	waitUntil(t, 3*time.Second, func() bool {
		applied := env.server.RuntimeStatus("dev_1")
		return applied.Connected && applied.AppliedProfileVersion == 28 &&
			applied.AppliedSettingsVersion == 4
	})
	// Same/older versions are accepted as idempotent replays and emit no
	// second control frame.
	if delivered, err = env.server.InvalidateRuntimeProfile(
		"dev_1", 28, "next_safe_point",
	); err != nil || !delivered {
		t.Fatalf("runtime invalidation replay failed: delivered=%v err=%v", delivered, err)
	}
	_ = connection.SetReadDeadline(time.Now().Add(100 * time.Millisecond))
	if _, _, err := connection.ReadMessage(); err == nil {
		t.Fatal("runtime invalidation replay emitted a duplicate frame")
	}
}
