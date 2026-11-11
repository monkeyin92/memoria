package mediaedge

import (
	"bufio"
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gorilla/websocket"
	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

type r5SlowConn struct {
	net.Conn
	slow *atomic.Bool
}

func (c r5SlowConn) Write(data []byte) (int, error) {
	if c.slow.Load() {
		time.Sleep(300 * time.Millisecond)
	}
	return c.Conn.Write(data)
}

type r5SlowWriter struct {
	http.ResponseWriter
	slow *atomic.Bool
}

func (w r5SlowWriter) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	c, buffer, err := w.ResponseWriter.(http.Hijacker).Hijack()
	if err != nil {
		return nil, nil, err
	}
	return r5SlowConn{c, w.slow}, buffer, nil
}

type r5Profile struct {
	Session        string `json:"session_id"`
	Actor          string `json:"actor_id"`
	Binding        string `json:"binding_id"`
	BindingVersion uint64 `json:"binding_version"`
	Subject        string `json:"active_subject_id"`
	Epoch          uint64 `json:"session_epoch"`
}

type r5Observation struct {
	runtime    *VoiceCoreMediaRuntime
	frame      AudioFrame
	audioCount int
}

func TestSoftwareR5Window(t *testing.T) {
	address := os.Getenv("MEMORIA_R5_BRIDGE")
	if address == "" {
		t.Skip("requires explicit isolated three-process runner")
	}
	directory := os.Getenv("MEMORIA_R5_OUTPUT")
	duration, err := strconv.Atoi(os.Getenv("MEMORIA_R5_DURATION"))
	if err != nil || duration < 30 {
		t.Fatal("duration must be at least 30 seconds")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	bridge, err := DialVoiceCore(ctx, VoiceCoreBridgeConfig{Address: address, AllowInsecureDevelopment: true})
	if err != nil {
		t.Fatal(err)
	}
	defer bridge.Close()
	var mu sync.Mutex
	observed := map[string]*r5Observation{}
	transcripts := make(chan *mediav1.TranscriptEvent, 1024)
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
			observation := &r5Observation{}
			wrapped := func(ctx context.Context, frame AudioFrame) error {
				mu.Lock()
				observation.frame = frame
				observation.audioCount++
				mu.Unlock()
				return sender(ctx, frame)
			}
			rt, err := NewVoiceCoreMediaRuntimeWithDownlinkSender(context.Background(), session, core, wrapped, func(event *mediav1.CoreToMedia) {
				server.ForwardCoreEventForSession(request, event)
				if v := event.GetTranscript(); v != nil {
					transcripts <- v
				}
			}, func(err error) { server.HandleBridgeError(request, err) })
			if err == nil {
				mu.Lock()
				observation.runtime = rt
				observed[request.SessionID] = observation
				mu.Unlock()
			}
			return rt, err
		}
	})
	slow := &atomic.Bool{}
	deviceServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { env.server.ServeHTTP(r5SlowWriter{w, slow}, r) }))
	defer deviceServer.Close()
	env.wsURL = "ws" + strings.TrimPrefix(deviceServer.URL, "http") + DeviceMediaEndpoint
	mustWrite := func(name string, data []byte) {
		if err := os.WriteFile(filepath.Join(directory, name), data, 0600); err != nil {
			t.Fatal(err)
		}
	}
	mustWrite("edge-pid", []byte(fmt.Sprint(os.Getpid())))
	server := NewServer(JWTVerifier{}, 4)
	server.DeviceWSS = env.server
	server.InternalControlToken = "r5-local-edge-control-token-32bytes"
	httpServer := httptest.NewServer(server.InternalHandler())
	defer httpServer.Close()
	mustWrite("edge-url", []byte(httpServer.URL))
	var control struct {
		Token   string    `json:"token"`
		Profile r5Profile `json:"profile"`
	}
	waitUntil(t, 45*time.Second, func() bool {
		data, e := os.ReadFile(filepath.Join(directory, "control-ready"))
		return e == nil && json.Unmarshal(data, &control) == nil
	})
	client := http.Client{Timeout: 5 * time.Second}
	api := os.Getenv("MEMORIA_R5_CONTROL_URL")
	waitUntil(t, 5*time.Second, func() bool {
		response, err := client.Get(api + "/v1/devices/dev_test_01/session-state")
		if err != nil {
			return false
		}
		response.Body.Close()
		return response.StatusCode == 401
	})
	requestID := 0
	request := func(method, path, token string, body any) (int, []byte) {
		requestID++
		var data []byte
		if body != nil {
			data, _ = json.Marshal(body)
		}
		req, _ := http.NewRequest(method, api+path, bytes.NewReader(data))
		req.Header.Set("X-Request-ID", fmt.Sprintf("r5-http-%d", requestID))
		req.Header.Set("X-Memoria-Internal-Token", "r5-local-policy-authority-token-32bytes")
		if token != "" {
			req.Header.Set("Authorization", "Bearer "+token)
		}
		beganRequest := time.Now()
		response, e := client.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		defer response.Body.Close()
		result, e := io.ReadAll(response.Body)
		if e != nil {
			t.Fatal(e)
		}
		row, _ := json.Marshal(map[string]any{"event": "http_request", "request_id": requestID, "method": method, "path": path, "status": response.StatusCode, "latency_ms": time.Since(beganRequest).Seconds() * 1000, "request": body})
		t.Log(string(row))
		return response.StatusCode, result
	}
	status, _ := request("GET", "/v1/devices/dev_test_01/session-state", "", nil)
	if status != 401 {
		t.Fatalf("missing Bearer status %d", status)
	}
	status, _ = request("POST", "/fixture/session-policy", "", map[string]string{"session_id": "missing-session"})
	if status != 403 {
		t.Fatalf("bad session status %d", status)
	}
	logRow := func(row map[string]any) { data, _ := json.Marshal(row); t.Log(string(data)) }
	snapshot := func(name string, epoch uint64, phase string, identityEpoch uint64) {
		var last []byte
		matched := false
		deadline := time.Now().Add(4 * time.Second)
		for time.Now().Before(deadline) {
			status, body := request("GET", "/v1/devices/dev_test_01/session-state", control.Token, nil)
			last = body
			var state struct {
				Status   string `json:"status"`
				Phase    string `json:"phase"`
				Snapshot struct {
					Session string      `json:"session_id"`
					Stream  uint64      `json:"stream_epoch"`
					Fence   deviceFence `json:"fence"`
				} `json:"snapshot"`
			}
			if json.Unmarshal(body, &state) == nil && status == 200 && state.Status == "online" && state.Phase == phase && state.Snapshot.Session == name && state.Snapshot.Stream == epoch && state.Snapshot.Fence.SessionEpoch == identityEpoch {
				matched = true
				break
			}
			time.Sleep(20 * time.Millisecond)
		}
		if !matched {
			t.Fatalf("exact Control gate name=%s epoch=%d phase=%s got=%s", name, epoch, phase, last)
		}
		logRow(map[string]any{"event": "control_positive", "session": name, "stream": epoch, "phase": phase, "response": json.RawMessage(last), "active_connections": env.connectionCount(), "active_leases": env.server.Leases.ActiveCount(), "goroutines": runtime.NumGoroutine()})
	}
	startProfile := func(name string) r5Profile {
		if name == "session_1" {
			return control.Profile
		}
		status, body := request("POST", "/fixture/start", "", map[string]string{"session_id": name})
		var p r5Profile
		if status != 200 || json.Unmarshal(body, &p) != nil || p.Epoch == 0 {
			t.Fatalf("start %d %s", status, body)
		}
		return p
	}
	open := func(p r5Profile, epoch uint64) *websocket.Conn {
		token := env.token(t, func(c *DeviceMediaClaims) {
			c.SessionID = p.Session
			c.Subject = p.Actor
			c.DeviceID = "dev_test_01"
			c.BindingID = p.Binding
			c.BindingVersion = p.BindingVersion
			c.SubjectID = p.Subject
			c.RuntimeProfileVersion = 1
			c.StreamEpoch = epoch
			c.JTI = fmt.Sprintf("r5-%s-%d", p.Session, epoch)
		})
		conn, response := env.dial(t, token, "client_1")
		if conn == nil {
			t.Fatalf("dial %v", response)
		}
		hello := deviceV2Hello()
		hello.StreamEpoch = epoch
		hello.DeviceID = "dev_test_01"
		writeDeviceJSON(t, conn, hello)
		deviceReadAccepted(t, conn)
		return conn
	}
	uplink := func(conn *websocket.Conn, epoch uint64, reconnect bool) {
		encoder, e := newOpusEncoder(16000, 1)
		if e != nil {
			t.Fatal(e)
		}
		defer encoder.close()
		sequence := uint32(0)
		sample := uint64(0)
		if reconnect {
			if e := conn.WriteMessage(2, encodeUplinkOpusFrame(t, encoder, uint32(epoch), 0, 0)); e != nil {
				t.Fatal(e)
			}
			writeDeviceJSON(t, conn, deviceListeningResume{deviceEventBase: deviceEventBase{Type: "listening.resume", Version: 2, StreamEpoch: epoch, ControlSequence: 1, DeviceMonotonicMS: 1}, WakeSample: 0})
			sequence = 1
			sample = 320
		}
		writeDeviceJSON(t, conn, deviceVADEvent{deviceEventBase: deviceEventBase{Type: "vad.start", Version: 2, StreamEpoch: epoch, ControlSequence: 2, DeviceMonotonicMS: 2}, SamplePosition: sample, Probability: 0.99, NearEndRMS: 500})
		if e := conn.WriteMessage(2, encodeUplinkOpusFrame(t, encoder, uint32(epoch), sequence, sample)); e != nil {
			t.Fatal(e)
		}
	}
	transcript := func(name string, epoch uint64) {
		deadline := time.After(8 * time.Second)
		for {
			select {
			case value := <-transcripts:
				if value.Identity.SessionId != name || value.Identity.StreamEpoch != epoch {
					continue
				}
				if value.Text != "你好" || !value.Final {
					t.Fatalf("incorrect ASR %v", value)
				}
				logRow(map[string]any{"event": "actual_transcript", "session": name, "stream": epoch, "text": value.Text})
				return
			case <-deadline:
				t.Fatalf("no real transcript %s %d", name, epoch)
			}
		}
	}
	audio := func(conn *websocket.Conn, name string, epoch uint64) {
		deadline := time.Now().Add(5 * time.Second)
		for time.Now().Before(deadline) {
			kind, payload, err := readDeviceMessage(conn, time.Second)
			if err != nil {
				t.Fatal(err)
			}
			if kind == 2 {
				var frame MemoriaAudioFrameV1
				if err := frame.UnmarshalBinary(payload); err != nil {
					t.Fatal(err)
				}
				if err := frame.Validate(uint32(epoch), DeviceDirectionDownlink); err != nil {
					t.Fatal(err)
				}
				if frame.GenerationID == 0 || len(frame.Payload) == 0 {
					t.Fatal("invalid actual Opus downlink")
				}
				logRow(map[string]any{"event": "actual_opus_downlink", "session": name, "stream": epoch, "generation": frame.GenerationID, "frame_samples": frame.FrameSamples, "bytes": len(payload)})
				return
			}
		}
		t.Fatal("no real Opus downlink")
	}
	closeSession := func(conn *websocket.Conn, epoch uint64) {
		writeDeviceJSON(t, conn, map[string]any{"type": "session.close", "version": 2, "stream_epoch": epoch, "control_sequence": 10, "device_monotonic_ms": 10, "value": map[string]string{"reason": "device_close"}})
		_ = conn.Close()
		waitReleased(t, env, "explicit session close")
	}
	healthy := func(name string, epoch uint64) (*websocket.Conn, r5Profile) {
		p := startProfile(name)
		conn := open(p, epoch)
		uplink(conn, epoch, false)
		transcript(name, epoch)
		audio(conn, name, epoch)
		snapshot(name, epoch, "speaking", p.Epoch)
		return conn, p
	}
	began := time.Now()
	mustWrite("window-start", []byte(time.Now().UTC().Format(time.RFC3339Nano)))
	faults := []bool{false, false, false}
	cycle := 0
	for time.Since(began) < time.Duration(duration)*time.Second {
		cycleStart := time.Now()
		epoch := uint64(100 + cycle*2)
		name := fmt.Sprintf("session_%d", cycle+1)
		conn, p := healthy(name, epoch)
		elapsed := time.Since(began).Seconds()
		if !faults[0] && elapsed >= float64(duration)/5 {
			_ = conn.UnderlyingConn().Close()
			waitReleased(t, env, "TCP reset")
			conn = open(p, epoch+1)
			uplink(conn, epoch+1, true)
			transcript(name, epoch+1)
			snapshot(name, epoch+1, "listening", p.Epoch)
			epoch++
			logRow(map[string]any{"event": "fault_tcp_recovered", "session": name, "stream": epoch})
			faults[0] = true
		} else if !faults[1] && elapsed >= float64(duration)*2/5 {
			mustWrite("kill-bridge", []byte(name))
			waitUntil(t, 10*time.Second, func() bool { _, err := os.Stat(filepath.Join(directory, "bridge-restarted")); return err == nil })
			deadline := time.Now().Add(5 * time.Second)
			terminal := false
			for time.Now().Before(deadline) {
				kind, payload, e := readDeviceMessage(conn, time.Second)
				if e != nil {
					break
				}
				if kind == 1 && bytes.Contains(payload, []byte("session.error")) {
					terminal = true
					logRow(map[string]any{"event": "bridge_old_terminal", "session": name, "message": json.RawMessage(payload)})
				}
			}
			_ = conn.Close()
			waitReleased(t, env, "bridge killed")
			if !terminal {
				t.Fatal("bridge death missing device session.error")
			}
			status, body := request("GET", "/v1/devices/dev_test_01/session-state", control.Token, nil)
			if status != 200 || !bytes.Contains(body, []byte(`"status":"offline"`)) {
				t.Fatalf("old connection not offline %d %s", status, body)
			}
			name = name + "_recovery"
			conn, p = healthy(name, epoch+1)
			epoch++
			logRow(map[string]any{"event": "fault_bridge_recovered", "session": name, "stream": epoch})
			faults[1] = true
		} else if !faults[2] && elapsed >= float64(duration)*3/5 {
			closeSession(conn, epoch)
			mustWrite("slow", []byte("long deterministic external TTS"))
			name = name + "_slow"
			conn, p = healthy(name, epoch+1)
			epoch++
			serverConn := env.server.connectionBySession(name)
			if serverConn == nil {
				t.Fatal("missing live slow connection")
			}
			if tcp, ok := conn.UnderlyingConn().(*net.TCPConn); ok {
				if e := tcp.SetReadBuffer(1024); e != nil {
					t.Fatal(e)
				}
			}
			if tcp, ok := serverConn.ws.UnderlyingConn().(*net.TCPConn); ok {
				if e := tcp.SetWriteBuffer(1024); e != nil {
					t.Fatal(e)
				}
			}
			slow.Store(true)
			before := env.server.metrics.backpressureDrop.Load()
			high := 0
			until := time.Now().Add(8 * time.Second)
			for time.Now().Before(until) {
				serverConn.lane.mu.Lock()
				count := len(serverConn.lane.queues[2])
				serverConn.lane.mu.Unlock()
				if count > high {
					high = count
				}
				if count > 10 {
					t.Fatalf("audio queue exceeded 200ms: %d", count)
				}
				time.Sleep(20 * time.Millisecond)
			}
			drops := env.server.metrics.backpressureDrop.Load() - before
			if drops == 0 {
				t.Fatal("slow client did not exercise actual backpressure")
			}
			mu.Lock()
			old := observed[name].frame
			rt := observed[name].runtime
			mu.Unlock()
			slow.Store(false)
			fence := serverConn.getCurrentFence()
			writeDeviceJSON(t, conn, deviceButtonStop{deviceEventBase: deviceEventBase{Type: "button.stop", Version: 2, StreamEpoch: epoch, ControlSequence: 4, DeviceMonotonicMS: 4}, ExpectedFence: fence})
			waitUntil(t, 5*time.Second, func() bool {
				current, _ := rt.session.GenerationSnapshot()
				return current.GenerationID > old.GenerationID
			})
			staleBefore := rt.session.Stats().StaleFrames
			err := rt.session.DeliverDownlink(old, func(context.Context, AudioFrame) error { t.Error("old audio reached sender"); return nil })
			if !errors.Is(err, ErrStaleDownlinkGeneration) || rt.session.Stats().StaleFrames != staleBefore+1 {
				t.Fatalf("old generation was not rejected: %v", err)
			}
			if raw, e := old.Payload(); e != nil || len(raw) == 0 || base64.StdEncoding.EncodeToString(raw) != old.PayloadB64 {
				t.Fatal("replayed audio was not actual captured PCM")
			}
			newFence, _ := rt.session.GenerationSnapshot()
			logRow(map[string]any{"event": "fault_slow_bounded_old_rejected", "session": name, "stream": epoch, "max_audio_queue": high, "dropped": drops, "old_generation": old.GenerationID, "new_generation": newFence.GenerationID})
			_ = os.Remove(filepath.Join(directory, "slow"))
			closeSession(conn, epoch)
			name = name + "_recovery"
			conn, p = healthy(name, epoch+1)
			epoch++
			faults[2] = true
		}
		closeSession(conn, epoch)
		var memory runtime.MemStats
		runtime.ReadMemStats(&memory)
		logRow(map[string]any{"event": "cycle_pass", "elapsed_s": time.Since(began).Seconds(), "session": p.Session, "stream": epoch, "latency_ms": time.Since(cycleStart).Seconds() * 1000, "connections": env.connectionCount(), "leases": env.server.Leases.ActiveCount(), "goroutines": runtime.NumGoroutine(), "heap_bytes": memory.HeapAlloc, "uplink_frames": env.server.metrics.uplinkFrames.Load(), "downlink_frames": env.server.metrics.downlinkFrames.Load(), "backpressure_drop": env.server.metrics.backpressureDrop.Load()})
		cycle++
		next := began.Add(time.Duration(cycle) * 5 * time.Second)
		if delay := time.Until(next); delay > 0 {
			time.Sleep(delay)
		}
	}
	if !faults[0] || !faults[1] || !faults[2] {
		t.Fatalf("missing faults %v", faults)
	}
	waitReleased(t, env, "final drain")
	logRow(map[string]any{"event": "window_complete", "elapsed_s": time.Since(began).Seconds(), "cycles": cycle, "connections": env.connectionCount(), "leases": env.server.Leases.ActiveCount()})
	mustWrite("window-end", []byte(time.Now().UTC().Format(time.RFC3339Nano)))
}
