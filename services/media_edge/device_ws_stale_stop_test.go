package mediaedge

import (
	"strings"
	"testing"
	"time"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

func TestDeviceWSSStaleStopsKeepServingCurrentGeneration(t *testing.T) {
	for _, kind := range []string{"button", "keyword"} {
		for _, invalid := range []string{"", "epoch", "fence_epoch", "shape", "unknown", "sequence"} {
			t.Run(kind+"/"+invalid, func(t *testing.T) {
				env := newDeviceTestEnv(t, func(s *DeviceWSServer) { s.SessionCloseReportHook = func(DeviceSessionCloseReport) {} })
				conn, _ := env.dial(t, env.token(t, func(c *DeviceMediaClaims) { c.DeviceSettings.AllowedBargeIn = []string{"button", "keyword"} }), "client_1")
				writeDeviceJSON(t, conn, deviceV2Hello())
				deviceReadAccepted(t, conn)
				env.mu.Lock()
				core := env.cores["session_1"]
				env.mu.Unlock()
				core.mu.Lock()
				core.current = Fence{SessionID: "session_1", TurnID: 1, GenerationID: 2, SessionEpoch: 1}
				core.mu.Unlock()
				core.inject(deviceGenerationEvent("session_1", 18, 1, 1, 2, mediav1.GenerationAction_GENERATION_ACTION_START))
				if _, payload, err := readDeviceMessage(conn, 3*time.Second); err != nil || !strings.Contains(string(payload), "generation.started") {
					t.Fatalf("start: %s %v", payload, err)
				}
				stop := loadKeywordStopFixture(t)
				if kind == "button" {
					stop = map[string]any{"type": "button.stop", "version": 2, "stream_epoch": 18, "control_sequence": 1, "device_monotonic_ms": 2000, "expected_fence": stop["expected_fence"]}
				}
				switch invalid {
				case "epoch":
					stop["stream_epoch"] = 17
				case "fence_epoch":
					stop["expected_fence"].(map[string]any)["session_epoch"] = 2
				case "shape":
					stop["expected_fence"].(map[string]any)["generation_id"] = 0
				case "unknown":
					stop["unexpected"] = true
				case "sequence":
					stop["control_sequence"] = 1 << 32
				}
				writeRawDeviceJSON(t, conn, stop)
				if invalid != "" {
					waitUntil(t, 3*time.Second, func() bool { return env.server.metrics.controlRejected.Load() > 0 })
					core.mu.Lock()
					defer core.mu.Unlock()
					if len(core.stops)+len(core.keywords) != 0 {
						t.Fatal("invalid stop forwarded")
					}
					return
				}
				waitUntil(t, 3*time.Second, func() bool {
					return env.server.metrics.staleGeneration.Load() == 1 || env.server.metrics.controlRejected.Load() > 0
				})
				if env.server.metrics.controlRejected.Load() != 0 {
					t.Fatal("stale stop rejected")
				}
				writeDeviceJSON(t, conn, deviceButtonStop{deviceEventBase: deviceEventBase{Type: "button.stop", Version: 2, StreamEpoch: 18, ControlSequence: 2, DeviceMonotonicMS: 3000}, ExpectedFence: deviceFence{TurnID: 1, GenerationID: 2, SessionEpoch: 1}})
				waitUntil(t, 3*time.Second, func() bool { core.mu.Lock(); defer core.mu.Unlock(); return len(core.stops) == 1 })
				core.mu.Lock()
				defer core.mu.Unlock()
				if core.stops[0].GenerationID != 2 || len(core.keywords) != 0 {
					t.Fatalf("wrong generation stopped: %+v", core.stops)
				}
			})
		}
	}
}
