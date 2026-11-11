package mediaedge

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Opt-in cross-process gate. Unlike the old window no deviceTestCore is used.
func TestSoftwareR4RealBridgePreflight(t *testing.T) {
	address := os.Getenv("MEMORIA_R4_BRIDGE")
	if address == "" {
		t.Skip("requires isolated Python bridge process")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	bridge, err := DialVoiceCore(ctx, VoiceCoreBridgeConfig{Address: address, AllowInsecureDevelopment: true})
	if err != nil {
		t.Fatal(err)
	}
	defer bridge.Close()
	transcripts := make(chan string, 8)
	env := newDeviceTestEnv(t, func(server *DeviceWSServer) {
		server.RuntimeFactory = func(request OpenSessionRequest, session *Session, sender DownlinkSender) (*VoiceCoreMediaRuntime, error) {
			handshake, done := context.WithTimeout(context.Background(), 5*time.Second)
			defer done()
			_, account, device, epoch := session.IdentitySnapshot()
			core, err := bridge.ConnectWithHandshakeContext(context.Background(), handshake,
				BridgeIdentity{SessionID: request.SessionID, AccountID: account, DeviceID: device, ClientType: "device", StreamEpoch: epoch, SubjectID: request.SubjectID, BindingID: request.BindingID, BindingVersion: request.BindingVersion, RuntimeProfileVersion: request.RuntimeProfileVersion, AudioMode: request.AudioMode},
				BridgeAudioFormat{Encoding: mediav1.AudioEncoding_AUDIO_ENCODING_PCM_S16LE, SampleRate: 16000, Channels: 1, FrameMS: 20},
				BridgeAudioFormat{Encoding: mediav1.AudioEncoding_AUDIO_ENCODING_PCM_S16LE, SampleRate: 24000, Channels: 1, FrameMS: 20})
			if err != nil {
				return nil, err
			}
			return NewVoiceCoreMediaRuntimeWithDownlinkSender(context.Background(), session, core, sender, func(event *mediav1.CoreToMedia) {
				server.ForwardCoreEventForSession(request, event)
				if client := event.GetClient(); client != nil {
					t.Logf("real client type=%s sequence=%d payload=%s", client.Type, client.Sequence, client.JsonPayload)
				}
				if value := event.GetTranscript(); value != nil {
					encoded, _ := json.Marshal(value)
					transcripts <- string(encoded)
				}
			}, func(err error) { server.HandleBridgeError(request, err) })
		}
	})
	directory := os.Getenv("MEMORIA_R4_OUTPUT")
	if err := os.WriteFile(filepath.Join(directory, "edge-pid"), []byte(fmt.Sprint(os.Getpid())), 0600); err != nil {
		t.Fatal(err)
	}
	server := NewServer(JWTVerifier{}, 4)
	server.DeviceWSS = env.server
	server.InternalControlToken = "r4-local-internal-token-00000000000000000"
	httpServer := httptest.NewServer(server.InternalHandler())
	defer httpServer.Close()
	if err := os.WriteFile(filepath.Join(directory, "edge-url"), []byte(httpServer.URL), 0600); err != nil {
		t.Fatal(err)
	}
	var control struct {
		Token string `json:"token"`
	}
	deadline := time.Now().Add(40 * time.Second)
	for {
		data, err := os.ReadFile(filepath.Join(directory, "control-ready"))
		if err == nil {
			if err = json.Unmarshal(data, &control); err != nil {
				t.Fatal(err)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("Control did not start")
		}
		time.Sleep(100 * time.Millisecond)
	}
	for {
		response, err := http.Get(os.Getenv("MEMORIA_R4_CONTROL_URL") + "/v1/devices/dev_test_01/session-state")
		if err == nil {
			response.Body.Close()
			if response.StatusCode != 401 {
				t.Fatalf("unauthenticated Control status %d", response.StatusCode)
			}
			break
		}
		if time.Now().After(deadline) {
			t.Fatal(err)
		}
		time.Sleep(50 * time.Millisecond)
	}
	for _, epoch := range []uint64{18, 19} {
		token := env.token(t, func(c *DeviceMediaClaims) {
			c.DeviceID = "dev_test_01"
			c.StreamEpoch = epoch
			c.JTI = fmt.Sprint("r4-", epoch)
		})
		conn, response := env.dial(t, token, "client_1")
		if conn == nil {
			t.Fatalf("dial %d: %v", epoch, response)
		}
		hello := deviceV2Hello()
		hello.StreamEpoch = epoch
		hello.DeviceID = "dev_test_01"
		writeDeviceJSON(t, conn, hello)
		deviceReadAccepted(t, conn)
		writeDeviceJSON(t, conn, deviceVADEvent{deviceEventBase: deviceEventBase{Type: "vad.start", Version: 2, StreamEpoch: epoch, ControlSequence: 1, DeviceMonotonicMS: 1}, SamplePosition: 0, Probability: 0.99, NearEndRMS: 500})
		// Uplink traverses Opus decoder, real gRPC, registry, ASR fixture and back.
		encoder, err := newOpusEncoder(16000, 1)
		if err != nil {
			t.Fatal(err)
		}
		if epoch > 18 {
			initial := encodeUplinkOpusFrame(t, encoder, uint32(epoch), 0, 0)
			if err := conn.WriteMessage(2, initial); err != nil {
				t.Fatal(err)
			}
			writeDeviceJSON(t, conn, deviceListeningResume{deviceEventBase: deviceEventBase{Type: "listening.resume", Version: 2, StreamEpoch: epoch, ControlSequence: 2, DeviceMonotonicMS: 2}, WakeSample: 0})
			writeDeviceJSON(t, conn, deviceVADEvent{deviceEventBase: deviceEventBase{Type: "vad.start", Version: 2, StreamEpoch: epoch, ControlSequence: 3, DeviceMonotonicMS: 3}, SamplePosition: 320, Probability: 0.99, NearEndRMS: 500})
		}
		frame := encodeUplinkOpusFrame(t, encoder, uint32(epoch), 0, 0)
		if epoch > 18 {
			frame = encodeUplinkOpusFrame(t, encoder, uint32(epoch), 1, 320)
		}
		encoder.close()
		if err := conn.WriteMessage(2, frame); err != nil {
			t.Fatal(err)
		}
		select {
		case payload := <-transcripts:
			t.Logf("actual gRPC transcript epoch=%d payload=%s", epoch, payload)
			var transcript mediav1.TranscriptEvent
			if err := json.Unmarshal([]byte(payload), &transcript); err != nil {
				t.Fatal(err)
			}
			if transcript.Text != "你好" || transcript.Identity.StreamEpoch != epoch || transcript.Identity.SessionId != "session_1" || !transcript.Final {
				t.Fatalf("wrong transcript %s", payload)
			}
			request, _ := http.NewRequest("GET", os.Getenv("MEMORIA_R4_CONTROL_URL")+"/v1/devices/dev_test_01/session-state", nil)
			request.Header.Set("Authorization", "Bearer "+control.Token)
			client := http.Client{Timeout: 5 * time.Second}
			response, err := client.Do(request)
			if err != nil {
				t.Fatal(err)
			}
			body, _ := io.ReadAll(response.Body)
			response.Body.Close()
			t.Logf("Control response epoch=%d code=%d body=%s", epoch, response.StatusCode, body)
			var state struct {
				Status string `json:"status"`
			}
			if err := json.Unmarshal(body, &state); err != nil {
				t.Fatal(err)
			}
			if response.StatusCode != 200 || state.Status != "online" {
				t.Fatalf("Control did not verify live bridge state: %s", body)
			}
		case <-time.After(10 * time.Second):
			t.Fatal("real provider transcript not observed on gRPC")
		}
		if err := conn.UnderlyingConn().Close(); err != nil {
			t.Fatal(err)
		}
		waitReleased(t, env, "real TCP loss")
		t.Logf("epoch=%d real roundtrip and drain passed", epoch)
	}
}
