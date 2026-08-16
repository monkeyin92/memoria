package main

import (
	"context"
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"math/big"
	mediaedge "memoria/services/media_edge"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

type directFactoryVoiceCore struct {
	mediav1.UnimplementedVoiceMediaBridgeServer
	helloReceived chan *mediav1.SessionHello
	audioReceived chan *mediav1.MediaToCore
}

func (s *directFactoryVoiceCore) Connect(
	stream grpc.BidiStreamingServer[mediav1.MediaToCore, mediav1.CoreToMedia],
) error {
	first, err := stream.Recv()
	if err != nil {
		return err
	}
	hello := first.GetHello()
	if hello == nil {
		return errors.New("direct factory did not send Voice Core hello")
	}
	s.helloReceived <- hello
	if err := stream.Send(&mediav1.CoreToMedia{Event: &mediav1.CoreToMedia_Accepted{
		Accepted: &mediav1.SessionAccepted{
			Identity:             hello.GetIdentity(),
			InteractionAuthority: mediav1.InteractionAuthority_INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE,
		},
	}}); err != nil {
		return err
	}
	for {
		message, err := stream.Recv()
		if err != nil {
			return err
		}
		select {
		case s.audioReceived <- message:
		default:
		}
	}
}

func TestBuildWebRTCConfigFailsClosedInProduction(t *testing.T) {
	t.Setenv("MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON", "")
	t.Setenv("MEDIA_EDGE_WEBRTC_PUBLIC_IPS", "")
	t.Setenv("MEDIA_EDGE_WEBRTC_UDP_PORT_MIN", "")
	t.Setenv("MEDIA_EDGE_WEBRTC_UDP_PORT_MAX", "")
	if _, err := buildWebRTCConfig(true); err == nil {
		t.Fatal("production WebRTC accepted no reachable ICE configuration")
	}
}

func TestBuildWebRTCConfigAcceptsTURNOrBoundedPublicUDP(t *testing.T) {
	t.Run("turn", func(t *testing.T) {
		t.Setenv("MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON", `[{"urls":["turn:turn.example:3478"],"username":"edge","credential":"secret"}]`)
		if _, err := buildWebRTCConfig(true); err != nil {
			t.Fatal(err)
		}
	})
	t.Run("public UDP", func(t *testing.T) {
		t.Setenv("MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON", "")
		t.Setenv("MEDIA_EDGE_WEBRTC_PUBLIC_IPS", "203.0.113.10")
		t.Setenv("MEDIA_EDGE_WEBRTC_UDP_PORT_MIN", "40000")
		t.Setenv("MEDIA_EDGE_WEBRTC_UDP_PORT_MAX", "40100")
		if _, err := buildWebRTCConfig(true); err != nil {
			t.Fatal(err)
		}
	})
}

func TestRequestedWebRTCEnabledDefaultsOnAndAllowsExplicitDeviceOnly(t *testing.T) {
	t.Setenv("MEDIA_EDGE_WEBRTC_ENABLED", "")
	enabled, err := requestedWebRTCEnabled(true, false)
	if err != nil || !enabled {
		t.Fatalf("unset WebRTC mode must preserve the enabled default: enabled=%v err=%v", enabled, err)
	}

	t.Setenv("MEDIA_EDGE_WEBRTC_ENABLED", "false")
	enabled, err = requestedWebRTCEnabled(true, true)
	if err != nil || enabled {
		t.Fatalf("production Direct Device WSS could not select device-only mode: enabled=%v err=%v", enabled, err)
	}
}

func TestRequestedWebRTCEnabledFailsClosedWithoutDirectDeviceWSS(t *testing.T) {
	t.Setenv("MEDIA_EDGE_WEBRTC_ENABLED", "false")
	if _, err := requestedWebRTCEnabled(true, false); err == nil {
		t.Fatal("production disabled WebRTC without Direct Device WSS")
	}
	t.Setenv("MEDIA_EDGE_WEBRTC_ENABLED", "not-a-bool")
	if _, err := requestedWebRTCEnabled(true, true); err == nil {
		t.Fatal("malformed WebRTC mode did not fail closed")
	}
}

func TestRequestedInteractionAuthorityNeverEnablesUnprovenGoAuthority(t *testing.T) {
	t.Setenv("MEDIA_EDGE_INTERACTION_AUTHORITY", "go_shadow")
	shadow, err := requestedInteractionAuthority()
	if err != nil || shadow.String() != "INTERACTION_AUTHORITY_GO_SHADOW" {
		t.Fatalf("go shadow was not selectable: mode=%v err=%v", shadow, err)
	}
	t.Setenv("MEDIA_EDGE_INTERACTION_AUTHORITY", "go_authoritative")
	authoritative, err := requestedInteractionAuthority()
	if err != nil || authoritative.String() != "INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE" {
		t.Fatalf("unproven Go authority did not fall back to Python: mode=%v err=%v", authoritative, err)
	}
}

// writeTestPKI writes a throwaway CA, an internal-listener server key pair
// and a client key pair signed by the CA, returning the file paths.
func writeTestPKI(t *testing.T) (caFile, serverCertFile, serverKeyFile, clientCertFile, clientKeyFile string) {
	t.Helper()
	dir := t.TempDir()
	now := time.Now()
	caKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	caTemplate := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "memoria-test-ca"},
		NotBefore:             now.Add(-time.Hour),
		NotAfter:              now.Add(24 * time.Hour),
		IsCA:                  true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTemplate, caTemplate, &caKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	caCert, err := x509.ParseCertificate(caDER)
	if err != nil {
		t.Fatal(err)
	}
	serverKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	serverTemplate := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: "media-edge-internal"},
		NotBefore:    now.Add(-time.Hour),
		NotAfter:     now.Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IPAddresses:  []net.IP{net.ParseIP("127.0.0.1")},
	}
	serverDER, err := x509.CreateCertificate(rand.Reader, serverTemplate, caCert, &serverKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	clientKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	clientTemplate := &x509.Certificate{
		SerialNumber: big.NewInt(3),
		Subject:      pkix.Name{CommonName: "edge-healthcheck-client"},
		NotBefore:    now.Add(-time.Hour),
		NotAfter:     now.Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth},
	}
	clientDER, err := x509.CreateCertificate(rand.Reader, clientTemplate, caCert, &clientKey.PublicKey, caKey)
	if err != nil {
		t.Fatal(err)
	}
	caFile = filepath.Join(dir, "ca.crt")
	writeTestPEM(t, caFile, "CERTIFICATE", caDER)
	serverCertFile = filepath.Join(dir, "server.crt")
	writeTestPEM(t, serverCertFile, "CERTIFICATE", serverDER)
	clientCertFile = filepath.Join(dir, "client.crt")
	writeTestPEM(t, clientCertFile, "CERTIFICATE", clientDER)
	serverKeyDER, err := x509.MarshalECPrivateKey(serverKey)
	if err != nil {
		t.Fatal(err)
	}
	serverKeyFile = filepath.Join(dir, "server.key")
	writeTestPEM(t, serverKeyFile, "EC PRIVATE KEY", serverKeyDER)
	clientKeyDER, err := x509.MarshalECPrivateKey(clientKey)
	if err != nil {
		t.Fatal(err)
	}
	clientKeyFile = filepath.Join(dir, "client.key")
	writeTestPEM(t, clientKeyFile, "EC PRIVATE KEY", clientKeyDER)
	return caFile, serverCertFile, serverKeyFile, clientCertFile, clientKeyFile
}

func writeTestPEM(t *testing.T, path, blockType string, der []byte) {
	t.Helper()
	data := pem.EncodeToMemory(&pem.Block{Type: blockType, Bytes: der})
	if err := os.WriteFile(path, data, 0o600); err != nil {
		t.Fatal(err)
	}
}

func clearInternalTLSEnv(t *testing.T) {
	t.Helper()
	for _, key := range []string{
		"MEDIA_EDGE_INTERNAL_TLS_CERT_FILE",
		"MEDIA_EDGE_INTERNAL_TLS_KEY_FILE",
		"MEDIA_EDGE_INTERNAL_TLS_CLIENT_CA_FILE",
	} {
		t.Setenv(key, "")
	}
}

func TestBuildInternalListenerTLSFailsClosedInProductionDirect(t *testing.T) {
	clearInternalTLSEnv(t)
	if _, err := buildInternalListenerTLS(true, true); err == nil {
		t.Fatal("production direct mode accepted an internal listener without TLS material")
	}
	if _, err := buildInternalListenerTLS(true, false); err != nil {
		t.Fatalf("non-direct production without TLS material should stay plaintext: %v", err)
	}
	caFile, serverCertFile, serverKeyFile, _, _ := writeTestPKI(t)
	t.Setenv("MEDIA_EDGE_INTERNAL_TLS_CERT_FILE", serverCertFile)
	t.Setenv("MEDIA_EDGE_INTERNAL_TLS_KEY_FILE", serverKeyFile)
	// Half-configured: no client CA.
	if _, err := buildInternalListenerTLS(true, true); err == nil {
		t.Fatal("half-configured internal TLS was accepted")
	}
	t.Setenv("MEDIA_EDGE_INTERNAL_TLS_CLIENT_CA_FILE", caFile)
	config, err := buildInternalListenerTLS(true, true)
	if err != nil {
		t.Fatal(err)
	}
	if config.ClientAuth != tls.RequireAndVerifyClientCert {
		t.Fatalf("internal listener must require verified client certs, got %v", config.ClientAuth)
	}
	if len(config.Certificates) != 1 || config.ClientCAs == nil {
		t.Fatal("internal listener TLS config is missing server certificate or client CA")
	}
	if config.MinVersion < tls.VersionTLS12 {
		t.Fatal("internal listener TLS must not allow pre-TLS1.2")
	}
}

func TestBuildInternalListenerTLSRejectsUnreadableMaterial(t *testing.T) {
	clearInternalTLSEnv(t)
	t.Setenv("MEDIA_EDGE_INTERNAL_TLS_CERT_FILE", "/nonexistent/server.crt")
	t.Setenv("MEDIA_EDGE_INTERNAL_TLS_KEY_FILE", "/nonexistent/server.key")
	t.Setenv("MEDIA_EDGE_INTERNAL_TLS_CLIENT_CA_FILE", "/nonexistent/ca.crt")
	if _, err := buildInternalListenerTLS(true, true); err == nil {
		t.Fatal("unreadable internal TLS material was accepted")
	}
}

func clearHealthcheckEnv(t *testing.T) {
	t.Helper()
	for _, key := range []string{
		"MEDIA_EDGE_HEALTHCHECK_URL",
		"MEDIA_EDGE_HEALTHCHECK_CA_FILE",
		"MEDIA_EDGE_HEALTHCHECK_CLIENT_CERT_FILE",
		"MEDIA_EDGE_HEALTHCHECK_CLIENT_KEY_FILE",
	} {
		t.Setenv(key, "")
	}
}

func TestBuildHealthcheckClientRequiresMTLSMaterialForHTTPS(t *testing.T) {
	clearHealthcheckEnv(t)
	if _, err := buildHealthcheckClient(); err != nil {
		t.Fatalf("plaintext default healthcheck must not require material: %v", err)
	}
	t.Setenv("MEDIA_EDGE_HEALTHCHECK_URL", "https://127.0.0.1:8081/readyz")
	if _, err := buildHealthcheckClient(); err == nil {
		t.Fatal("https healthcheck accepted missing client identity")
	}
	caFile, _, _, clientCertFile, clientKeyFile := writeTestPKI(t)
	t.Setenv("MEDIA_EDGE_HEALTHCHECK_CA_FILE", caFile)
	t.Setenv("MEDIA_EDGE_HEALTHCHECK_CLIENT_CERT_FILE", clientCertFile)
	t.Setenv("MEDIA_EDGE_HEALTHCHECK_CLIENT_KEY_FILE", clientKeyFile)
	if _, err := buildHealthcheckClient(); err != nil {
		t.Fatal(err)
	}
}

func TestRunHealthcheckMTLS(t *testing.T) {
	clearHealthcheckEnv(t)
	caFile, serverCertFile, serverKeyFile, clientCertFile, clientKeyFile := writeTestPKI(t)
	serverCert, err := tls.LoadX509KeyPair(serverCertFile, serverKeyFile)
	if err != nil {
		t.Fatal(err)
	}
	caPEM, err := os.ReadFile(caFile)
	if err != nil {
		t.Fatal(err)
	}
	clientCAs := x509.NewCertPool()
	if !clientCAs.AppendCertsFromPEM(caPEM) {
		t.Fatal("test CA did not parse")
	}
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/readyz" {
			http.NotFound(w, r)
			return
		}
		w.WriteHeader(http.StatusOK)
	}))
	server.TLS = &tls.Config{
		Certificates: []tls.Certificate{serverCert},
		ClientCAs:    clientCAs,
		ClientAuth:   tls.RequireAndVerifyClientCert,
		MinVersion:   tls.VersionTLS12,
	}
	server.StartTLS()
	defer server.Close()
	t.Setenv("MEDIA_EDGE_HEALTHCHECK_URL", server.URL+"/readyz")
	t.Setenv("MEDIA_EDGE_HEALTHCHECK_CA_FILE", caFile)
	t.Setenv("MEDIA_EDGE_HEALTHCHECK_CLIENT_CERT_FILE", clientCertFile)
	t.Setenv("MEDIA_EDGE_HEALTHCHECK_CLIENT_KEY_FILE", clientKeyFile)
	if code := runHealthcheck(); code != 0 {
		t.Fatalf("mTLS healthcheck failed with code %d", code)
	}
	t.Setenv("MEDIA_EDGE_HEALTHCHECK_CA_FILE", "")
	t.Setenv("MEDIA_EDGE_HEALTHCHECK_CLIENT_CERT_FILE", "")
	t.Setenv("MEDIA_EDGE_HEALTHCHECK_CLIENT_KEY_FILE", "")
	if code := runHealthcheck(); code == 0 {
		t.Fatal("healthcheck succeeded without its client identity")
	}
}

func TestDeviceWSSListenAddressFailsClosedInProduction(t *testing.T) {
	t.Setenv("MEDIA_EDGE_DEVICE_WSS_ADDR", "")
	if addr, err := deviceWSSListenAddress(true, false); err != nil || addr != "" {
		t.Fatalf("disabled device WSS must be inert: addr=%q err=%v", addr, err)
	}
	if _, err := deviceWSSListenAddress(true, true); err == nil {
		t.Fatal("production direct mode accepted a missing MEDIA_EDGE_DEVICE_WSS_ADDR")
	}
	t.Setenv("MEDIA_EDGE_DEVICE_WSS_ADDR", ":8082")
	addr, err := deviceWSSListenAddress(true, true)
	if err != nil || addr != ":8082" {
		t.Fatalf("dedicated listener address was not honored: addr=%q err=%v", addr, err)
	}
}

func TestDeviceJWTIdentityRequiresDedicatedValuesInProductionDirect(t *testing.T) {
	t.Setenv("MEDIA_EDGE_DEVICE_JWT_ISSUER", "")
	t.Setenv("MEDIA_EDGE_DEVICE_JWT_AUDIENCE", "")
	if issuer, audience, err := deviceJWTIdentity(false, true); err != nil || issuer != "" || audience != "" {
		t.Fatalf("development fallback must stay permissive: issuer=%q audience=%q err=%v", issuer, audience, err)
	}
	if _, _, err := deviceJWTIdentity(true, true); err == nil {
		t.Fatal("production direct mode accepted inherited device identity")
	}
	t.Setenv("MEDIA_EDGE_DEVICE_JWT_ISSUER", "memoria-control-api")
	t.Setenv("MEDIA_EDGE_DEVICE_JWT_AUDIENCE", "memoria-media-edge")
	issuer, audience, err := deviceJWTIdentity(true, true)
	if err != nil || issuer != "memoria-control-api" || audience != "memoria-media-edge" {
		t.Fatalf("dedicated device identity was not honored: issuer=%q audience=%q err=%v", issuer, audience, err)
	}
}

func TestBuildDeviceWSSRequiresSharedRedisInProduction(t *testing.T) {
	t.Setenv("MEDIA_EDGE_DEVICE_WSS_ENABLED", "true")
	t.Setenv("MEDIA_EDGE_DEVICE_ACOUSTIC_REGISTRY_FILE", "")
	t.Setenv("MEDIA_EDGE_DEVICE_STATE_REDIS_URL", "")
	publicKey, _, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	verifier := mediaedge.DeviceJWTVerifier{
		PublicKeys: map[string]ed25519.PublicKey{"test": publicKey},
		Issuer:     "memoria-control-api", Audience: "memoria-media-edge",
	}
	server := mediaedge.NewDeviceWSServer(verifier)
	if _, err := buildDeviceWSS(verifier, true, nil, server); err == nil ||
		!strings.Contains(err.Error(), "MEDIA_EDGE_DEVICE_STATE_REDIS_URL") {
		t.Fatalf("production Device WSS accepted process-local state: %v", err)
	}
	t.Setenv("MEDIA_EDGE_DEVICE_STATE_REDIS_URL", "redis://127.0.0.1:6379/4")
	if _, err := buildDeviceWSS(verifier, true, nil, server); err == nil ||
		!strings.Contains(err.Error(), "rediss://") {
		t.Fatalf("production Device WSS accepted plaintext Redis: %v", err)
	}
}

func TestBuildDeviceStateRedisTLSRequiresCompleteVerifiedIdentity(t *testing.T) {
	for _, key := range []string{
		"MEDIA_EDGE_DEVICE_STATE_REDIS_CA_FILE",
		"MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_CERT_FILE",
		"MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_KEY_FILE",
		"MEDIA_EDGE_DEVICE_STATE_REDIS_SERVER_NAME",
	} {
		t.Setenv(key, "")
	}
	if _, err := buildDeviceStateRedisTLS(true, "rediss://device-state-redis:6379/4"); err == nil {
		t.Fatal("production Redis accepted no explicit trust material")
	}
	caFile, _, _, clientCertFile, clientKeyFile := writeTestPKI(t)
	t.Setenv("MEDIA_EDGE_DEVICE_STATE_REDIS_CA_FILE", caFile)
	t.Setenv("MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_CERT_FILE", clientCertFile)
	t.Setenv("MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_KEY_FILE", clientKeyFile)
	if _, err := buildDeviceStateRedisTLS(true, "rediss://device-state-redis:6379/4"); err == nil {
		t.Fatal("production Redis accepted a missing server name")
	}
	t.Setenv("MEDIA_EDGE_DEVICE_STATE_REDIS_SERVER_NAME", "device-state-redis")
	config, err := buildDeviceStateRedisTLS(true, "rediss://device-state-redis:6379/4")
	if err != nil {
		t.Fatal(err)
	}
	if config.ServerName != "device-state-redis" || len(config.Certificates) != 1 || config.RootCAs == nil {
		t.Fatal("Redis TLS config did not preserve peer verification and client identity")
	}
	if config.MinVersion < tls.VersionTLS12 {
		t.Fatal("Redis TLS config allows pre-TLS1.2")
	}
	if _, err := buildDeviceStateRedisTLS(true, "redis://device-state-redis:6379/4"); err == nil {
		t.Fatal("Redis mTLS material was accepted with a plaintext URL")
	}
}

func clearCloseReporterEnv(t *testing.T) {
	t.Helper()
	for _, key := range []string{
		"MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL",
		"MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN",
		"MEDIA_EDGE_DEVICE_CLOSE_REPORT_TIMEOUT_MS",
	} {
		t.Setenv(key, "")
	}
}

func TestBuildDeviceSessionCloseHookFailsClosedInProduction(t *testing.T) {
	clearCloseReporterEnv(t)
	if hook, err := buildDeviceSessionCloseHook(true, false); err != nil || hook != nil {
		t.Fatalf("disabled device WSS must not require a close hook: err=%v", err)
	}
	if _, err := buildDeviceSessionCloseHook(true, true); err == nil {
		t.Fatal("production direct mode accepted a missing close report URL")
	}
	t.Setenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL", "http://control-api:8000/v1/internal/device-close")
	if _, err := buildDeviceSessionCloseHook(true, true); err == nil {
		t.Fatal("close hook accepted a URL without a token")
	}
	t.Setenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN", "short-token")
	if _, err := buildDeviceSessionCloseHook(true, true); err == nil {
		t.Fatal("close hook accepted a short token")
	}
	t.Setenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN", strings.Repeat("x", 32))
	if _, err := buildDeviceSessionCloseHook(true, false); err == nil {
		t.Fatal("production accepted a close hook with device WSS disabled")
	}
	hook, err := buildDeviceSessionCloseHook(true, true)
	if err != nil || hook == nil {
		t.Fatalf("valid close hook was rejected: %v", err)
	}
	t.Setenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_TIMEOUT_MS", "50000")
	if _, err := buildDeviceSessionCloseHook(true, true); err == nil {
		t.Fatal("close hook accepted an unbounded timeout")
	}
}

func TestDeviceSessionCloseHookPostsPerConnectionClose(t *testing.T) {
	clearCloseReporterEnv(t)
	type receivedReport struct {
		DeviceID    string `json:"device_id"`
		SessionID   string `json:"session_id"`
		StreamEpoch uint64 `json:"stream_epoch"`
		AccountID   string `json:"account_id"`
		Reason      string `json:"reason"`
		Connected   bool   `json:"connected"`
	}
	var report receivedReport
	var receivedToken string
	received := make(chan struct{}, 1)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/internal/device-close" {
			http.NotFound(w, r)
			return
		}
		receivedToken = r.Header.Get(deviceCloseReportHeader)
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		_ = json.Unmarshal(body, &report)
		received <- struct{}{}
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()
	t.Setenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL", server.URL+"/v1/internal/device-close")
	t.Setenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN", strings.Repeat("y", 32))
	t.Setenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_TIMEOUT_MS", "2000")
	hook, err := buildDeviceSessionCloseHook(false, true)
	if err != nil {
		t.Fatal(err)
	}
	hook(mediaedge.DeviceSessionCloseReport{
		DeviceID: "dev_9", SessionID: "session-9", StreamEpoch: 3,
		AccountID: "account_1", Reason: mediaedge.SessionCloseReasonDeviceClose,
		Connected: true, ClosedAt: time.Now().UTC().Format(time.RFC3339Nano),
	})
	select {
	case <-received:
	case <-time.After(3 * time.Second):
		t.Fatal("per-connection close report was never delivered")
	}
	if receivedToken != strings.Repeat("y", 32) {
		t.Fatal("close report did not carry the independent authenticated header")
	}
	if report.DeviceID != "dev_9" || report.SessionID != "session-9" ||
		report.StreamEpoch != 3 || report.AccountID != "account_1" ||
		report.Reason != mediaedge.SessionCloseReasonDeviceClose || !report.Connected {
		t.Fatalf("close report did not carry per-connection metadata: %+v", report)
	}
}

func TestDeviceSessionCloseHookDoesNotBlockTeardown(t *testing.T) {
	clearCloseReporterEnv(t)
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		time.Sleep(2 * time.Second)
		w.WriteHeader(http.StatusOK)
	}))
	defer server.Close()
	t.Setenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL", server.URL)
	t.Setenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN", strings.Repeat("z", 32))
	t.Setenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_TIMEOUT_MS", "100")
	hook, err := buildDeviceSessionCloseHook(false, true)
	if err != nil {
		t.Fatal(err)
	}
	started := time.Now()
	hook(mediaedge.DeviceSessionCloseReport{
		DeviceID: "dev_1", SessionID: "session-1", StreamEpoch: 1,
		AccountID: "account_1", Reason: mediaedge.SessionCloseReasonNetwork,
		Connected: true, ClosedAt: time.Now().UTC().Format(time.RFC3339Nano),
	})
	if elapsed := time.Since(started); elapsed > 1500*time.Millisecond {
		t.Fatalf("close hook blocked connection teardown: elapsed=%v", elapsed)
	}
	// The bounded retries continue in the background goroutine without
	// blocking teardown: every attempt times out (100ms client timeout
	// against a 2s handler) and the loop still runs all three attempts.
	waitForCondition(t, 3*time.Second, func() bool { return calls.Load() == deviceCloseReportMaxAttempts },
		"bounded background retries did not complete")
	if got := calls.Load(); got != deviceCloseReportMaxAttempts {
		t.Fatalf("close reporter overran its attempt bound: calls=%d", got)
	}
}

// roundTripperFunc adapts a function into an http.RoundTripper so tests can
// inject deterministic network failures and count attempts without a server.
type roundTripperFunc func(*http.Request) (*http.Response, error)

func (f roundTripperFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return f(request)
}

type deviceCloseReportReceived struct {
	DeviceID    string `json:"device_id"`
	SessionID   string `json:"session_id"`
	StreamEpoch uint64 `json:"stream_epoch"`
	AccountID   string `json:"account_id"`
	Reason      string `json:"reason"`
	Connected   bool   `json:"connected"`
}

func waitForCondition(t *testing.T, timeout time.Duration, condition func() bool, failure string) {
	t.Helper()
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if condition() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal(failure)
}

func TestDeviceSessionCloseReporterBoundedRetriesOnNetworkError(t *testing.T) {
	var attempts atomic.Int32
	reporter := &deviceCloseReporter{
		url:   "http://control.invalid/v1/internal/device-close",
		token: strings.Repeat("n", 32),
		client: &http.Client{Transport: roundTripperFunc(func(*http.Request) (*http.Response, error) {
			attempts.Add(1)
			return nil, errors.New("connection refused")
		})},
		backoff: func(int) time.Duration { return 0 },
	}
	reporter.report(mediaedge.DeviceSessionCloseReport{
		DeviceID: "dev_1", SessionID: "session-1", StreamEpoch: 1,
		AccountID: "account_1", Reason: mediaedge.SessionCloseReasonNetwork,
		Connected: true, ClosedAt: time.Now().UTC().Format(time.RFC3339Nano),
	})
	waitForCondition(t, 3*time.Second, func() bool { return attempts.Load() == deviceCloseReportMaxAttempts },
		"network-error report did not reach its bounded attempt count")
	time.Sleep(50 * time.Millisecond)
	if got := attempts.Load(); got != deviceCloseReportMaxAttempts {
		t.Fatalf("network-error report kept retrying past its bound: calls=%d", got)
	}
}

func TestDeviceSessionCloseReporterRetriesNon2xxThenSucceeds(t *testing.T) {
	var calls atomic.Int32
	var delivered atomic.Bool
	var received deviceCloseReportReceived
	var receivedToken string
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(r.Body)
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		_ = json.Unmarshal(body, &received)
		receivedToken = r.Header.Get(deviceCloseReportHeader)
		if calls.Add(1) == 1 {
			// First attempt is rejected with an error body that the client
			// must drain and close before retrying.
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusInternalServerError)
			_, _ = w.Write([]byte(strings.Repeat("y", 2048)))
			return
		}
		delivered.Store(true)
		w.WriteHeader(http.StatusAccepted)
	}))
	defer server.Close()
	reporter := &deviceCloseReporter{
		url:     server.URL + "/v1/internal/device-close",
		token:   strings.Repeat("t", 32),
		client:  &http.Client{Timeout: 2 * time.Second},
		backoff: func(int) time.Duration { return 0 },
	}
	reporter.report(mediaedge.DeviceSessionCloseReport{
		DeviceID: "dev_2", SessionID: "session-2", StreamEpoch: 2,
		AccountID: "account_2", Reason: mediaedge.SessionCloseReasonSuperseded,
		Connected: true, ClosedAt: time.Now().UTC().Format(time.RFC3339Nano),
	})
	waitForCondition(t, 3*time.Second, delivered.Load, "report never succeeded after a transient 500")
	if got := calls.Load(); got != 2 {
		t.Fatalf("expected exactly one retry after a 500: calls=%d", got)
	}
	if receivedToken != strings.Repeat("t", 32) {
		t.Fatal("retried report lost its independent authenticated header")
	}
	if received.DeviceID != "dev_2" || received.SessionID != "session-2" ||
		received.StreamEpoch != 2 || received.AccountID != "account_2" ||
		received.Reason != mediaedge.SessionCloseReasonSuperseded || !received.Connected {
		t.Fatalf("retried report carried wrong metadata: %+v", received)
	}
}

func TestDeviceSessionCloseReporterGivesUpAfterBoundedNon2xxAttempts(t *testing.T) {
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()
	reporter := &deviceCloseReporter{
		url:     server.URL + "/v1/internal/device-close",
		token:   strings.Repeat("p", 32),
		client:  &http.Client{Timeout: 2 * time.Second},
		backoff: func(int) time.Duration { return 0 },
	}
	reporter.report(mediaedge.DeviceSessionCloseReport{
		DeviceID: "dev_3", SessionID: "session-3", StreamEpoch: 3,
		AccountID: "account_3", Reason: mediaedge.SessionCloseReasonDeviceClose,
		Connected: true, ClosedAt: time.Now().UTC().Format(time.RFC3339Nano),
	})
	waitForCondition(t, 3*time.Second, func() bool { return calls.Load() == deviceCloseReportMaxAttempts },
		"permanent-failure report did not reach its bounded attempt count")
	time.Sleep(50 * time.Millisecond)
	if got := calls.Load(); got != deviceCloseReportMaxAttempts {
		t.Fatalf("permanent-failure report kept retrying past its bound: calls=%d", got)
	}
}

func TestDeviceSessionCloseReporterNeverFiresConcurrentRequests(t *testing.T) {
	var mu sync.Mutex
	var calls atomic.Int32
	var windows [][2]time.Time
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		windows = append(windows, [2]time.Time{time.Now(), time.Time{}})
		index := len(windows) - 1
		mu.Unlock()
		time.Sleep(40 * time.Millisecond)
		mu.Lock()
		windows[index][1] = time.Now()
		mu.Unlock()
		calls.Add(1)
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer server.Close()
	reporter := &deviceCloseReporter{
		url:     server.URL + "/v1/internal/device-close",
		token:   strings.Repeat("s", 32),
		client:  &http.Client{Timeout: 2 * time.Second},
		backoff: func(int) time.Duration { return 0 },
	}
	reporter.report(mediaedge.DeviceSessionCloseReport{
		DeviceID: "dev_4", SessionID: "session-4", StreamEpoch: 4,
		AccountID: "account_4", Reason: mediaedge.SessionCloseReasonEdgeShutdown,
		Connected: true, ClosedAt: time.Now().UTC().Format(time.RFC3339Nano),
	})
	waitForCondition(t, 3*time.Second, func() bool { return calls.Load() == deviceCloseReportMaxAttempts },
		"sequential-report test did not observe all attempts")
	mu.Lock()
	defer mu.Unlock()
	if len(windows) != deviceCloseReportMaxAttempts {
		t.Fatalf("unexpected request count: %d", len(windows))
	}
	for i := 1; i < len(windows); i++ {
		if windows[i][0].Before(windows[i-1][1]) {
			t.Fatalf("attempt %d overlapped attempt %d: %v vs %v", i, i-1, windows[i][0], windows[i-1][1])
		}
	}
}

func TestBuildDeviceWSSDirectFactoryCancelsOnlyHandshakeContext(t *testing.T) {
	t.Setenv("MEDIA_EDGE_DEVICE_WSS_ENABLED", "true")
	t.Setenv("MEDIA_EDGE_DEVICE_REQUIRED", "true")
	t.Setenv("MEDIA_EDGE_DEVICE_STATE_REDIS_URL", "")
	t.Setenv("MEDIA_EDGE_DEVICE_ACOUSTIC_REGISTRY_FILE", "")

	service := &directFactoryVoiceCore{
		helloReceived: make(chan *mediav1.SessionHello, 1),
		audioReceived: make(chan *mediav1.MediaToCore, 1),
	}
	listener := bufconn.Listen(1024 * 1024)
	grpcServer := grpc.NewServer()
	mediav1.RegisterVoiceMediaBridgeServer(grpcServer, service)
	go func() { _ = grpcServer.Serve(listener) }()
	conn, err := grpc.NewClient(
		"passthrough:///direct-factory",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		grpcServer.Stop()
		_ = listener.Close()
		t.Fatal(err)
	}
	bridge := mediaedge.NewVoiceCoreBridge(conn)
	t.Cleanup(func() {
		_ = bridge.Close()
		grpcServer.Stop()
		_ = listener.Close()
	})

	verifier := mediaedge.DeviceJWTVerifier{}
	deviceServer := mediaedge.NewDeviceWSServer(verifier)
	if enabled, err := buildDeviceWSS(verifier, false, bridge, deviceServer); err != nil || !enabled {
		t.Fatalf("Direct Device WSS factory was not enabled: enabled=%v err=%v", enabled, err)
	}
	if deviceServer.RuntimeFactory == nil {
		t.Fatal("Direct Device WSS did not install a Voice Core runtime factory")
	}

	request := mediaedge.OpenSessionRequest{
		SessionID: "direct-session", AccountID: "account-1", DeviceID: "device-1",
		ClientType: "device", StreamEpoch: 18, SubjectID: "subject-1",
		BindingID: "binding-1", BindingVersion: 3, RuntimeProfileVersion: 27,
	}
	session, err := mediaedge.NewSession(request, 20)
	if err != nil {
		t.Fatal(err)
	}
	runtime, err := deviceServer.RuntimeFactory(request, session, func(context.Context, mediaedge.AudioFrame) error {
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = runtime.Close() })

	select {
	case hello := <-service.helloReceived:
		if hello.GetDownlinkFormat().GetSampleRate() != 24_000 {
			t.Fatalf("Direct factory Voice Core downlink rate = %d, want 24000", hello.GetDownlinkFormat().GetSampleRate())
		}
	case <-time.After(time.Second):
		t.Fatal("Direct factory Voice Core handshake was not observed")
	}

	if err := runtime.SendUplink(mediaedge.AudioFrame{
		SessionID: "direct-session", StreamEpoch: 18, Sequence: 0,
		CaptureStartSample: 0, FrameSamples: 320,
		PayloadB64: base64.StdEncoding.EncodeToString(make([]byte, 640)),
	}); err != nil {
		t.Fatalf("accepted Direct runtime could not send audio after factory return: %v", err)
	}
	select {
	case message := <-service.audioReceived:
		if message.GetAudio() == nil || message.GetAudio().GetFrameSamples() != 320 {
			t.Fatalf("Direct runtime forwarded unexpected audio after handshake cancellation: %+v", message)
		}
	case <-time.After(time.Second):
		t.Fatal("Direct runtime stream was cancelled with the handshake context")
	}
}
