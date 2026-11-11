package mediaedge

import (
	"encoding/json"
	"fmt"
	"github.com/gorilla/websocket"
	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"
)

func TestAssistantR3HTTPFixture(t *testing.T) {
	path := os.Getenv("MEMORIA_R3_EDGE_READY")
	if path == "" {
		t.Skip("cross-language fixture launched by Python integration test")
	}
	env := newDeviceTestEnv(t, nil)
	connect := func(epoch uint64) *websocket.Conn {
		token := env.token(t, func(claims *DeviceMediaClaims) {
			claims.DeviceID = "dev_test_01"
			claims.StreamEpoch = epoch
			claims.JTI = fmt.Sprintf("r3_%d", epoch)
		})
		conn, response := env.dial(t, token, "client_1")
		if conn == nil {
			t.Fatalf("dial: %v", response)
		}
		hello := deviceV2Hello()
		hello.DeviceID = "dev_test_01"
		hello.StreamEpoch = epoch
		writeDeviceJSON(t, conn, hello)
		deviceReadAccepted(t, conn)
		return conn
	}
	connection := connect(18)
	defer func() { connection.Close() }()
	server := NewServer(JWTVerifier{}, 4)
	server.DeviceWSS = env.server
	server.InternalControlToken = "r3-local-internal-token-32-characters"
	mux := http.NewServeMux()
	mux.Handle("/v1/", server.InternalHandler())
	mux.HandleFunc("/fixture/event", func(w http.ResponseWriter, r *http.Request) {
		var client mediav1.ClientEvent
		if err := json.NewDecoder(r.Body).Decode(&client); err != nil {
			http.Error(w, err.Error(), 400)
			return
		}
		env.mu.Lock()
		request := env.requests["session_1"]
		env.mu.Unlock()
		request.DeviceID = "dev_test_01"
		env.server.ForwardCoreEventForSession(request, &mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Client{Client: &client}})
	})
	mux.HandleFunc("/fixture/disconnect", func(w http.ResponseWriter, r *http.Request) {
		connection.Close()
		waitReleased(t, env, "r3 disconnect")
	})
	mux.HandleFunc("/fixture/reconnect", func(w http.ResponseWriter, r *http.Request) { connection = connect(19) })
	stop := make(chan struct{}, 1)
	mux.HandleFunc("/fixture/stop", func(w http.ResponseWriter, r *http.Request) { stop <- struct{}{} })
	httpServer := httptest.NewServer(mux)
	defer httpServer.Close()
	if err := os.WriteFile(path, []byte(httpServer.URL), 0600); err != nil {
		t.Fatal(err)
	}
	select {
	case <-stop:
	case <-time.After(90 * time.Second):
		t.Fatal("cross-language client did not finish")
	}
}
