package mediaedge

import (
	"encoding/json"
	"os"
	"runtime"
	"testing"
	"time"
)

func TestSoftwareSoakWindow(t *testing.T) {
	if os.Getenv("MEMORIA_SOFTWARE_SOAK") != "900" {
		t.Skip("explicit 900 second software window only")
	}
	env := newDeviceTestEnv(t, nil)
	start := time.Now()
	for epoch := uint64(100); time.Since(start) < 900*time.Second; epoch++ {
		began := time.Now()
		connection, core := connectAt(t, env, epoch)
		if epoch%3 == 0 {
			_ = connection.UnderlyingConn().Close()
		} else {
			_ = connection.Close()
		}
		waitCoreClosed(t, core, "software window")
		waitReleased(t, env, "software window")
		var memory runtime.MemStats
		runtime.ReadMemStats(&memory)
		row, err := json.Marshal(map[string]any{"elapsed_s": time.Since(start).Seconds(), "cycle_latency_ms": time.Since(began).Seconds() * 1000, "heap_bytes": memory.HeapAlloc, "goroutines": runtime.NumGoroutine(), "connections": env.connectionCount(), "leases": env.server.Leases.ActiveCount()})
		if err != nil {
			t.Fatal(err)
		}
		t.Log(string(row))
		time.Sleep(5 * time.Second)
	}
	waitReleased(t, env, "final drain")
}
