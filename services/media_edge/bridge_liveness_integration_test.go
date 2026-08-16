package mediaedge

import (
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"math/big"
	"net"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/connectivity"
	"google.golang.org/grpc/credentials"
	grpc_health "google.golang.org/grpc/health"
	grpc_health_v1 "google.golang.org/grpc/health/grpc_health_v1"
)

type livenessTestPKI struct {
	caPEM         []byte
	serverCertPEM []byte
	serverKeyPEM  []byte
	clientTLS     BridgeTLSConfig
}

type livenessHealthServer struct {
	address  string
	health   *grpc_health.Server
	server   *grpc.Server
	listener net.Listener
}

func newLivenessTestPKI(t *testing.T) livenessTestPKI {
	t.Helper()
	now := time.Now()
	caPublic, caPrivate, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	caTemplate := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "memoria-liveness-test-ca"},
		NotBefore:             now.Add(-time.Minute),
		NotAfter:              now.Add(time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
	}
	caDER, err := x509.CreateCertificate(rand.Reader, caTemplate, caTemplate, caPublic, caPrivate)
	if err != nil {
		t.Fatal(err)
	}
	caCertificate, err := x509.ParseCertificate(caDER)
	if err != nil {
		t.Fatal(err)
	}
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: caDER})

	issueLeaf := func(serial int64, commonName string, usages []x509.ExtKeyUsage, dnsNames []string) ([]byte, []byte) {
		t.Helper()
		public, private, leafErr := ed25519.GenerateKey(rand.Reader)
		if leafErr != nil {
			t.Fatal(leafErr)
		}
		template := &x509.Certificate{
			SerialNumber: big.NewInt(serial),
			Subject:      pkix.Name{CommonName: commonName},
			NotBefore:    now.Add(-time.Minute),
			NotAfter:     now.Add(time.Hour),
			KeyUsage:     x509.KeyUsageDigitalSignature,
			ExtKeyUsage:  usages,
			DNSNames:     dnsNames,
		}
		der, leafErr := x509.CreateCertificate(rand.Reader, template, caCertificate, public, caPrivate)
		if leafErr != nil {
			t.Fatal(leafErr)
		}
		privateDER, leafErr := x509.MarshalPKCS8PrivateKey(private)
		if leafErr != nil {
			t.Fatal(leafErr)
		}
		return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: privateDER})
	}

	serverCertPEM, serverKeyPEM := issueLeaf(2, "voice-core.test", []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, []string{"voice-core.test"})
	clientCertPEM, clientKeyPEM := issueLeaf(3, "media-edge.test", []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}, nil)
	return livenessTestPKI{
		caPEM:         caPEM,
		serverCertPEM: serverCertPEM,
		serverKeyPEM:  serverKeyPEM,
		clientTLS: BridgeTLSConfig{
			RootCAPEM:     caPEM,
			ClientCertPEM: clientCertPEM,
			ClientKeyPEM:  clientKeyPEM,
			ServerName:    "voice-core.test",
		},
	}
}

func newLivenessHealthServer(t *testing.T, material livenessTestPKI) *livenessHealthServer {
	t.Helper()
	certificate, err := tls.X509KeyPair(material.serverCertPEM, material.serverKeyPEM)
	if err != nil {
		t.Fatal(err)
	}
	clientCAs := x509.NewCertPool()
	if !clientCAs.AppendCertsFromPEM(material.caPEM) {
		t.Fatal("test CA did not parse")
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(&tls.Config{
		MinVersion:   tls.VersionTLS13,
		Certificates: []tls.Certificate{certificate},
		ClientAuth:   tls.RequireAndVerifyClientCert,
		ClientCAs:    clientCAs,
	})))
	health := grpc_health.NewServer()
	grpc_health_v1.RegisterHealthServer(server, health)
	result := &livenessHealthServer{
		address: listener.Addr().String(), health: health, server: server, listener: listener,
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	return result
}

func TestVoiceCoreBridgeSupervisorRedialsWhenServingHealthFailsOverMTLS(t *testing.T) {
	material := newLivenessTestPKI(t)
	first := newLivenessHealthServer(t, material)
	second := newLivenessHealthServer(t, material)
	first.health.SetServingStatus(voiceCoreBridgeHealthService, grpc_health_v1.HealthCheckResponse_SERVING)
	second.health.SetServingStatus(voiceCoreBridgeHealthService, grpc_health_v1.HealthCheckResponse_SERVING)

	addresses := []string{first.address, second.address}
	dialCalls := 0
	settings := testVoiceCoreSupervisorSettings(func(
		ctx context.Context, config VoiceCoreBridgeConfig,
	) (voiceCoreBridgeTransport, error) {
		if dialCalls < len(addresses)-1 {
			config.Address = addresses[dialCalls]
		} else {
			config.Address = addresses[len(addresses)-1]
		}
		dialCalls++
		return DialVoiceCore(ctx, config)
	})
	settings.probeInterval = 5 * time.Millisecond
	settings.probeTimeout = 100 * time.Millisecond
	settings.unavailableGrace = 75 * time.Millisecond
	supervisor, err := newVoiceCoreBridgeSupervisor(
		context.Background(),
		VoiceCoreBridgeConfig{TLS: &material.clientTLS},
		time.Second,
		settings,
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = supervisor.Close() })

	first.health.SetServingStatus(voiceCoreBridgeHealthService, grpc_health_v1.HealthCheckResponse_NOT_SERVING)
	waitForSupervisorCondition(t, func() bool { return supervisor.livenessProbeFailures.Load() > 0 })
	supervisor.mu.RLock()
	initial := supervisor.bridge
	generation := supervisor.generation
	supervisor.mu.RUnlock()
	if generation != 1 {
		t.Fatalf("generation=%d before grace elapsed, want 1", generation)
	}
	if state := initial.State(); state != connectivity.Ready {
		t.Fatalf("transport state=%s, want READY while Health is NOT_SERVING", state)
	}
	if supervisor.Ready() {
		t.Fatal("ready probe accepted a READY transport with NOT_SERVING Health")
	}

	waitForSupervisorCondition(t, func() bool { return supervisorGeneration(supervisor) == 2 })
	if !supervisor.Ready() {
		t.Fatal("supervisor did not become ready after mTLS replacement")
	}
	if got := supervisor.redialAttempts.Load(); got != 1 {
		t.Fatalf("redial attempts=%d, want 1", got)
	}
	if got := supervisor.redialSuccesses.Load(); got != 1 {
		t.Fatalf("redial successes=%d, want 1", got)
	}
	if got := supervisor.livenessProbeFailures.Load(); got == 0 {
		t.Fatal("mTLS Health failure was not recorded")
	}
	// The initial synchronous probe intentionally occurs before the
	// supervisor exists and is not counted. A successful candidate probe is
	// sufficient here: generation advancement and Ready() below prove that
	// the candidate became the active replacement.
	if got := supervisor.livenessProbeSuccesses.Load(); got < 1 {
		t.Fatalf("liveness successes=%d, want successful candidate check", got)
	}

	var metrics strings.Builder
	supervisor.WriteMetrics(&metrics)
	if !strings.Contains(metrics.String(), "media_edge_voice_core_liveness_healthy 1\n") {
		t.Fatalf("liveness metric did not recover:\n%s", metrics.String())
	}
}
