package main

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/pion/webrtc/v4"

	mediaedge "memoria/services/media_edge"
	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

func loadMediaPublicKeys() (map[string]ed25519.PublicKey, error) {
	file := strings.TrimSpace(os.Getenv("MEDIA_EDGE_JWT_PUBLIC_KEY_FILE"))
	inline := strings.TrimSpace(os.Getenv("MEDIA_EDGE_JWT_PUBLIC_KEY_PEM"))
	if file != "" && inline != "" {
		return nil, errors.New("configure one MEDIA_EDGE_JWT public key source")
	}
	material := inline
	if file != "" {
		data, err := os.ReadFile(file)
		if err != nil {
			return nil, fmt.Errorf("read media edge JWT public key: %w", err)
		}
		material = string(data)
	}
	if strings.TrimSpace(material) == "" {
		return nil, nil
	}
	if strings.HasPrefix(strings.TrimSpace(material), "{") {
		return mediaedge.ParseEd25519PublicKeys([]byte(material))
	}
	block, _ := pem.Decode([]byte(material))
	if block == nil {
		decoded, err := base64.StdEncoding.DecodeString(strings.TrimSpace(material))
		if err != nil {
			decoded, err = base64.RawStdEncoding.DecodeString(strings.TrimSpace(material))
		}
		if err != nil {
			return nil, errors.New("media edge JWT public key must be PEM, JWKS, or base64")
		}
		if len(decoded) != ed25519.PublicKeySize {
			return nil, errors.New("media edge JWT public key has invalid length")
		}
		keyID := strings.TrimSpace(os.Getenv("MEDIA_EDGE_JWT_KEY_ID"))
		if keyID == "" {
			return nil, errors.New("MEDIA_EDGE_JWT_KEY_ID is required with a public key")
		}
		return map[string]ed25519.PublicKey{keyID: ed25519.PublicKey(decoded)}, nil
	}
	parsed, err := x509.ParsePKIXPublicKey(block.Bytes)
	if err != nil {
		return nil, fmt.Errorf("parse media edge JWT public key: %w", err)
	}
	key, ok := parsed.(ed25519.PublicKey)
	if !ok || len(key) != ed25519.PublicKeySize {
		return nil, errors.New("media edge JWT public key is not Ed25519")
	}
	keyID := strings.TrimSpace(os.Getenv("MEDIA_EDGE_JWT_KEY_ID"))
	if keyID == "" {
		return nil, errors.New("MEDIA_EDGE_JWT_KEY_ID is required with a public key")
	}
	return map[string]ed25519.PublicKey{keyID: key}, nil
}

func runHealthcheck() int {
	client, err := buildHealthcheckClient()
	if err != nil {
		log.Printf("media edge healthcheck misconfigured: %v", err)
		return 1
	}
	url := strings.TrimSpace(os.Getenv("MEDIA_EDGE_HEALTHCHECK_URL"))
	if url == "" {
		if strings.TrimSpace(os.Getenv("MEDIA_EDGE_INTERNAL_HTTP_ADDR")) != "" {
			url = "http://127.0.0.1:8081/readyz"
		} else {
			url = "http://127.0.0.1:8080/readyz"
		}
	}
	response, err := client.Get(url)
	if err != nil {
		log.Printf("media edge healthcheck request failed: %v", err)
		return 1
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		log.Printf("media edge healthcheck status %d", response.StatusCode)
		return 1
	}
	return 0
}

// buildHealthcheckClient returns the HTTP client used by the healthcheck
// subprocess. An https URL requires the dedicated healthcheck client identity;
// it is never shared with the Control-plane client certificate so each side
// keeps its own least-privilege identity.
func buildHealthcheckClient() (*http.Client, error) {
	url := strings.TrimSpace(os.Getenv("MEDIA_EDGE_HEALTHCHECK_URL"))
	if url != "" && !strings.HasPrefix(url, "https://") {
		return &http.Client{Timeout: 3 * time.Second}, nil
	}
	caFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_HEALTHCHECK_CA_FILE"))
	certFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_HEALTHCHECK_CLIENT_CERT_FILE"))
	keyFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_HEALTHCHECK_CLIENT_KEY_FILE"))
	if caFile == "" || certFile == "" || keyFile == "" {
		if url == "" {
			return &http.Client{Timeout: 3 * time.Second}, nil
		}
		return nil, errors.New("https healthcheck requires MEDIA_EDGE_HEALTHCHECK_CA_FILE, MEDIA_EDGE_HEALTHCHECK_CLIENT_CERT_FILE and MEDIA_EDGE_HEALTHCHECK_CLIENT_KEY_FILE")
	}
	certificate, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, fmt.Errorf("load healthcheck client identity: %w", err)
	}
	caPEM, err := os.ReadFile(caFile)
	if err != nil {
		return nil, fmt.Errorf("read healthcheck CA: %w", err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		return nil, errors.New("healthcheck CA contains no usable certificates")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.TLSClientConfig = &tls.Config{
		Certificates: []tls.Certificate{certificate},
		RootCAs:      roots,
		MinVersion:   tls.VersionTLS12,
	}
	return &http.Client{Transport: transport, Timeout: 3 * time.Second}, nil
}

// buildInternalListenerTLS loads the internal control listener's server key
// pair and the client CA used to verify Control-plane callers. In production
// direct-device mode all three files are mandatory and every client must
// present a verified certificate. Whenever any file is configured the whole
// set is required so a half-configured boundary fails closed instead of
// silently downgrading to plaintext.
func buildInternalListenerTLS(production, direct bool) (*tls.Config, error) {
	certFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_INTERNAL_TLS_CERT_FILE"))
	keyFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_INTERNAL_TLS_KEY_FILE"))
	caFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_INTERNAL_TLS_CLIENT_CA_FILE"))
	configured := certFile != "" || keyFile != "" || caFile != ""
	if !configured {
		if production && direct {
			return nil, errors.New("production direct device media requires MEDIA_EDGE_INTERNAL_TLS_CERT_FILE, MEDIA_EDGE_INTERNAL_TLS_KEY_FILE and MEDIA_EDGE_INTERNAL_TLS_CLIENT_CA_FILE")
		}
		return nil, nil
	}
	if certFile == "" || keyFile == "" || caFile == "" {
		return nil, errors.New("internal listener TLS requires MEDIA_EDGE_INTERNAL_TLS_CERT_FILE, MEDIA_EDGE_INTERNAL_TLS_KEY_FILE and MEDIA_EDGE_INTERNAL_TLS_CLIENT_CA_FILE together")
	}
	certificate, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, fmt.Errorf("load internal listener TLS key pair: %w", err)
	}
	caPEM, err := os.ReadFile(caFile)
	if err != nil {
		return nil, fmt.Errorf("read internal listener client CA: %w", err)
	}
	clientCAs := x509.NewCertPool()
	if !clientCAs.AppendCertsFromPEM(caPEM) {
		return nil, errors.New("internal listener client CA contains no usable certificates")
	}
	return &tls.Config{
		Certificates: []tls.Certificate{certificate},
		ClientCAs:    clientCAs,
		ClientAuth:   tls.RequireAndVerifyClientCert,
		MinVersion:   tls.VersionTLS12,
	}, nil
}

// buildDeviceStateRedisTLS loads the independent mTLS identity used only for
// replay and lease state. A rediss:// URL without explicit CA, client
// identity and server-name verification is not sufficient in production:
// encryption without peer authentication would let a forged Redis authority
// consume tickets or seize device leases.
func buildDeviceStateRedisTLS(production bool, redisURL string) (*tls.Config, error) {
	caFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_STATE_REDIS_CA_FILE"))
	certFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_CERT_FILE"))
	keyFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_KEY_FILE"))
	serverName := strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_STATE_REDIS_SERVER_NAME"))
	configured := caFile != "" || certFile != "" || keyFile != "" || serverName != ""
	secureURL := strings.HasPrefix(strings.ToLower(strings.TrimSpace(redisURL)), "rediss://")
	if !configured {
		if production && secureURL {
			return nil, errors.New("production device state Redis requires explicit CA, client certificate, client key and server name")
		}
		return nil, nil
	}
	if !secureURL {
		return nil, errors.New("device state Redis mTLS requires rediss://")
	}
	if caFile == "" || certFile == "" || keyFile == "" || serverName == "" {
		return nil, errors.New("device state Redis mTLS requires MEDIA_EDGE_DEVICE_STATE_REDIS_CA_FILE, MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_CERT_FILE, MEDIA_EDGE_DEVICE_STATE_REDIS_CLIENT_KEY_FILE and MEDIA_EDGE_DEVICE_STATE_REDIS_SERVER_NAME together")
	}
	certificate, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
		return nil, fmt.Errorf("load device state Redis client identity: %w", err)
	}
	caPEM, err := os.ReadFile(caFile)
	if err != nil {
		return nil, fmt.Errorf("read device state Redis CA: %w", err)
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(caPEM) {
		return nil, errors.New("device state Redis CA contains no usable certificates")
	}
	return &tls.Config{
		Certificates: []tls.Certificate{certificate},
		RootCAs:      roots,
		ServerName:   serverName,
		MinVersion:   tls.VersionTLS12,
	}, nil
}

// deviceWSSListenAddress returns the dedicated loopback-published listener
// address for the hardware device WSS endpoint. Production direct mode never
// falls back to the container-network public handler: the only public path is
// the host nginx exact route proxying to this loopback-only port.
func deviceWSSListenAddress(production, enabled bool) (string, error) {
	if !enabled {
		return "", nil
	}
	addr := strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_WSS_ADDR"))
	if production && addr == "" {
		return "", errors.New("production direct device media requires MEDIA_EDGE_DEVICE_WSS_ADDR (published loopback-only behind the host nginx exact route)")
	}
	return addr, nil
}

// deviceJWTIdentity pins the dedicated device issuer/audience pair. In
// production direct mode both must be explicitly configured instead of
// inheriting the H5 streamcore edge identity.
func deviceJWTIdentity(production, enabled bool) (string, string, error) {
	issuer := strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_JWT_ISSUER"))
	audience := strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_JWT_AUDIENCE"))
	if !enabled {
		return "", "", nil
	}
	if production && (issuer == "" || audience == "") {
		return "", "", errors.New("production direct device media requires dedicated MEDIA_EDGE_DEVICE_JWT_ISSUER and MEDIA_EDGE_DEVICE_JWT_AUDIENCE")
	}
	return issuer, audience, nil
}

const deviceCloseReportHeader = "X-Memoria-Edge-Device-Close-Token"

const (
	// deviceCloseReportMaxAttempts bounds the total tries for one close
	// report, including the initial attempt. Each report runs its retries in
	// a single background goroutine, so a Control outage delays nothing at
	// connection teardown and never duplicates in-flight requests.
	deviceCloseReportMaxAttempts = 3
	// deviceCloseReportRetryBase is the first backoff between attempts; each
	// following backoff doubles up to deviceCloseReportMaxBackoff.
	deviceCloseReportRetryBase  = 200 * time.Millisecond
	deviceCloseReportMaxBackoff = 800 * time.Millisecond
	// deviceCloseReportBodyDrainCap bounds how much of a non-2xx response
	// body is drained before closing so the connection can be reused without
	// buffering an unbounded server response.
	deviceCloseReportBodyDrainCap = 4 << 10
)

// deviceCloseReporter is the bounded, fire-and-forget Edge->Control device
// session close reporter. The endpoint URL and an independent >=32-character
// token must be configured together; the token travels in
// deviceCloseReportHeader and the payload only carries per-connection close
// metadata (never tickets, audio, or mTLS identity material). Container-
// internal HTTP to Control is acceptable; the Control client mTLS identity
// is deliberately not reused for this outbound call.
type deviceCloseReporter struct {
	url     string
	token   string
	client  *http.Client
	backoff func(attempt int) time.Duration // nil selects the exponential default
}

// buildDeviceSessionCloseHook returns the DeviceWSS per-connection close
// hook, or nil when the reporter is not configured. Production direct mode
// requires the reporter: every accepted device connection reports its real
// close reason through DeviceWSServer.SessionCloseReportHook (device_close /
// superseded / network / edge_shutdown), never a global empty payload.
func buildDeviceSessionCloseHook(production, enabled bool) (func(mediaedge.DeviceSessionCloseReport), error) {
	url := strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL"))
	token := strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN"))
	timeoutMS := envInt("MEDIA_EDGE_DEVICE_CLOSE_REPORT_TIMEOUT_MS", 2000)
	if production && enabled && url == "" {
		return nil, errors.New("production direct device media requires MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL for per-connection session close reports")
	}
	if url == "" && token == "" {
		return nil, nil
	}
	if url == "" || len(token) < 32 {
		return nil, errors.New("MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL and MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN (>=32 characters) must be configured together")
	}
	if production && !enabled {
		return nil, errors.New("MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL requires MEDIA_EDGE_DEVICE_WSS_ENABLED=true in production")
	}
	if timeoutMS < 100 || timeoutMS > 30000 {
		return nil, errors.New("MEDIA_EDGE_DEVICE_CLOSE_REPORT_TIMEOUT_MS must be between 100 and 30000")
	}
	reporter := &deviceCloseReporter{
		url:    url,
		token:  token,
		client: &http.Client{Timeout: time.Duration(timeoutMS) * time.Millisecond},
	}
	return func(report mediaedge.DeviceSessionCloseReport) {
		reporter.report(report)
	}, nil
}

// report posts one per-connection close report entirely inside a single
// background goroutine with bounded retries, so connection teardown is never
// blocked and a given report is never sent concurrently. Only a 2xx response
// is treated as success; anything else is retried up to
// deviceCloseReportMaxAttempts and then abandoned with failure logs.
func (r *deviceCloseReporter) report(report mediaedge.DeviceSessionCloseReport) {
	body, err := json.Marshal(report)
	if err != nil {
		log.Printf("media edge device close report marshal failed: %v", err)
		return
	}
	go r.postWithRetry(body)
}

// postWithRetry runs the bounded retry loop for one already-marshaled report.
func (r *deviceCloseReporter) postWithRetry(body []byte) {
	for attempt := 1; attempt <= deviceCloseReportMaxAttempts; attempt++ {
		if r.postOnce(body, attempt) {
			return
		}
		if attempt < deviceCloseReportMaxAttempts {
			time.Sleep(r.retryDelay(attempt))
		}
	}
}

func (r *deviceCloseReporter) retryDelay(attempt int) time.Duration {
	if r.backoff != nil {
		return r.backoff(attempt)
	}
	delay := deviceCloseReportRetryBase << (attempt - 1)
	if delay > deviceCloseReportMaxBackoff {
		return deviceCloseReportMaxBackoff
	}
	return delay
}

// postOnce performs a single close-report POST and reports whether Control
// accepted it. Network failures and non-2xx responses are logged without the
// token or any payload body; non-2xx bodies are drained up to
// deviceCloseReportBodyDrainCap and closed.
func (r *deviceCloseReporter) postOnce(body []byte, attempt int) bool {
	request, err := http.NewRequest(http.MethodPost, r.url, bytes.NewReader(body))
	if err != nil {
		log.Printf("media edge device close report request build failed: %v", err)
		return false
	}
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set(deviceCloseReportHeader, r.token)
	response, err := r.client.Do(request)
	if err != nil {
		log.Printf("media edge device close report attempt %d/%d failed: %v", attempt, deviceCloseReportMaxAttempts, err)
		return false
	}
	defer func() {
		_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, deviceCloseReportBodyDrainCap))
		_ = response.Body.Close()
	}()
	if response.StatusCode >= 200 && response.StatusCode < 300 {
		return true
	}
	log.Printf("media edge device close report attempt %d/%d rejected status=%d", attempt, deviceCloseReportMaxAttempts, response.StatusCode)
	return false
}

func envInt(name string, fallback int) int {
	value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(name)))
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}

func envBool(name string) bool {
	value, err := strconv.ParseBool(strings.TrimSpace(os.Getenv(name)))
	return err == nil && value
}

// requestedWebRTCEnabled keeps the existing production WebRTC behavior as
// the default, while allowing an explicitly device-only Edge process to avoid
// depending on TURN/WHIP configuration it cannot serve. Production may only
// disable WebRTC when the Direct Device WSS listener is requested; malformed
// values fail closed instead of silently selecting either media path.
func requestedWebRTCEnabled(production bool, deviceWSSRequested bool) (bool, error) {
	raw := strings.TrimSpace(os.Getenv("MEDIA_EDGE_WEBRTC_ENABLED"))
	if raw == "" {
		return true, nil
	}
	enabled, err := strconv.ParseBool(raw)
	if err != nil {
		return false, errors.New("invalid MEDIA_EDGE_WEBRTC_ENABLED")
	}
	if production && !enabled && !deviceWSSRequested {
		return false, errors.New("production may disable WebRTC only when Direct Device WSS is enabled")
	}
	return enabled, nil
}

func envString(name, fallback string) string {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	return value
}

func envDuration(name string, fallback time.Duration) time.Duration {
	value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(name)))
	if err != nil || value <= 0 {
		return fallback
	}
	return time.Duration(value) * time.Millisecond
}

func requestedInteractionAuthority() (mediav1.InteractionAuthority, error) {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("MEDIA_EDGE_INTERACTION_AUTHORITY"))) {
	case "", "python", "python_authoritative":
		return mediav1.InteractionAuthority_INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE, nil
	case "go_shadow":
		return mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW, nil
	case "go_authoritative":
		// A6 is intentionally unavailable until parity/SLO/rollback evidence is
		// wired as a separate promotion gate.
		return mediav1.InteractionAuthority_INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE, nil
	default:
		return mediav1.InteractionAuthority_INTERACTION_AUTHORITY_UNSPECIFIED,
			fmt.Errorf("invalid MEDIA_EDGE_INTERACTION_AUTHORITY")
	}
}

func buildWebRTCConfig(production bool) (mediaedge.WebRTCTerminatorConfig, error) {
	var iceServers []webrtc.ICEServer
	if raw := strings.TrimSpace(os.Getenv("MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON")); raw != "" {
		if err := json.Unmarshal([]byte(raw), &iceServers); err != nil {
			return mediaedge.WebRTCTerminatorConfig{}, fmt.Errorf("invalid MEDIA_EDGE_WEBRTC_ICE_SERVERS_JSON: %w", err)
		}
	}
	var publicIPs []string
	for _, value := range strings.Split(os.Getenv("MEDIA_EDGE_WEBRTC_PUBLIC_IPS"), ",") {
		if value = strings.TrimSpace(value); value != "" {
			publicIPs = append(publicIPs, value)
		}
	}
	portMin := envInt("MEDIA_EDGE_WEBRTC_UDP_PORT_MIN", 0)
	portMax := envInt("MEDIA_EDGE_WEBRTC_UDP_PORT_MAX", 0)
	if (portMin == 0) != (portMax == 0) || portMin > portMax || portMax > 65535 {
		return mediaedge.WebRTCTerminatorConfig{}, errors.New("WebRTC UDP port range must provide valid min and max values")
	}
	hasTURN := false
	for _, server := range iceServers {
		for _, url := range server.URLs {
			hasTURN = hasTURN || strings.HasPrefix(url, "turn:") || strings.HasPrefix(url, "turns:")
		}
	}
	if production && !hasTURN && (len(publicIPs) == 0 || portMin == 0) {
		return mediaedge.WebRTCTerminatorConfig{}, errors.New("production WebRTC requires TURN or public IP plus a bounded UDP port range")
	}
	settingEngine := webrtc.SettingEngine{}
	if portMin > 0 {
		if err := settingEngine.SetEphemeralUDPPortRange(uint16(portMin), uint16(portMax)); err != nil {
			return mediaedge.WebRTCTerminatorConfig{}, err
		}
	}
	if len(publicIPs) > 0 {
		if err := settingEngine.SetICEAddressRewriteRules(webrtc.ICEAddressRewriteRule{
			External:        publicIPs,
			AsCandidateType: webrtc.ICECandidateTypeHost,
			Mode:            webrtc.ICEAddressRewriteReplace,
		}); err != nil {
			return mediaedge.WebRTCTerminatorConfig{}, fmt.Errorf("configure public ICE address rewrite: %w", err)
		}
	}
	configuration := webrtc.Configuration{ICEServers: iceServers}
	if production && hasTURN {
		configuration.ICETransportPolicy = webrtc.ICETransportPolicyRelay
	}
	return mediaedge.WebRTCTerminatorConfig{
		API:                   webrtc.NewAPI(webrtc.WithSettingEngine(settingEngine)),
		Configuration:         configuration,
		RequireRelayCandidate: production && hasTURN,
		OnError:               func(err error) { log.Printf("media edge WebRTC error: %v", err) },
	}, nil
}

func buildVoiceCoreBridge() (*mediaedge.VoiceCoreBridge, error) {
	address := strings.TrimSpace(os.Getenv("MEDIA_EDGE_VOICE_CORE_ADDR"))
	production := strings.EqualFold(strings.TrimSpace(os.Getenv("ENVIRONMENT")), "production")
	required := production || envBool("MEDIA_EDGE_VOICE_CORE_REQUIRED")
	if address == "" {
		if required {
			return nil, errors.New("MEDIA_EDGE_VOICE_CORE_ADDR is required for this edge process")
		}
		return nil, nil
	}
	allowInsecure := !production && envBool("MEDIA_EDGE_VOICE_CORE_ALLOW_INSECURE_DEVELOPMENT")
	var tlsConfig *mediaedge.BridgeTLSConfig
	caFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_VOICE_CORE_CA_FILE"))
	certFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_VOICE_CORE_CLIENT_CERT_FILE"))
	keyFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_VOICE_CORE_CLIENT_KEY_FILE"))
	if caFile != "" || certFile != "" || keyFile != "" {
		loaded, err := mediaedge.LoadBridgeTLSConfig(
			caFile,
			certFile,
			keyFile,
			strings.TrimSpace(os.Getenv("MEDIA_EDGE_VOICE_CORE_SERVER_NAME")),
		)
		if err != nil {
			return nil, err
		}
		tlsConfig = &loaded
	}
	connectContext, cancel := context.WithTimeout(
		context.Background(), envDuration("MEDIA_EDGE_VOICE_CORE_CONNECT_TIMEOUT_MS", 5*time.Second),
	)
	defer cancel()
	interactionAuthority, err := requestedInteractionAuthority()
	if err != nil {
		return nil, err
	}
	return mediaedge.DialVoiceCore(connectContext, mediaedge.VoiceCoreBridgeConfig{
		Address: address, TLS: tlsConfig, AllowInsecureDevelopment: allowInsecure,
		InteractionAuthority: interactionAuthority,
	})
}

// buildDeviceWSS wires the hardware device endpoint. Production accepts only
// EdDSA/JWKS tokens with typ=memoria_device_media; HS256 requires an explicit
// development flag and a non-production environment. The acoustic registry
// controls whether any board may open full_duplex_verified.
func buildDeviceWSS(
	verifier mediaedge.DeviceJWTVerifier,
	production bool,
	voiceCore *mediaedge.VoiceCoreBridge,
	deviceServer *mediaedge.DeviceWSServer,
) (bool, error) {
	if !envBool("MEDIA_EDGE_DEVICE_WSS_ENABLED") {
		return false, nil
	}
	registryFile := strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_ACOUSTIC_REGISTRY_FILE"))
	registry, err := mediaedge.LoadDeviceAcousticRegistry(registryFile)
	if err != nil {
		return false, err
	}
	deviceServer.Verifier = verifier
	deviceServer.Acoustic = registry
	deviceServer.RequireConfigured = production
	deviceServer.RequireRuntime = production || envBool("MEDIA_EDGE_DEVICE_REQUIRED")
	if voiceCore != nil {
		deviceServer.RuntimeFactory = func(
			request mediaedge.OpenSessionRequest,
			session *mediaedge.Session,
			sender mediaedge.DownlinkSender,
		) (*mediaedge.VoiceCoreMediaRuntime, error) {
			handshakeCtx, cancel := context.WithTimeout(
				context.Background(),
				envDuration("MEDIA_EDGE_VOICE_CORE_CONNECT_TIMEOUT_MS", 5*time.Second),
			)
			defer cancel()
			_, accountID, deviceID, streamEpoch := session.IdentitySnapshot()
			core, err := voiceCore.ConnectWithHandshakeContext(
				context.Background(),
				handshakeCtx,
				mediaedge.BridgeIdentity{
					SessionID: request.SessionID, AccountID: accountID, DeviceID: deviceID,
					ClientType: "device", StreamEpoch: streamEpoch,
					SubjectID: request.SubjectID, BindingID: request.BindingID,
					BindingVersion:        request.BindingVersion,
					RuntimeProfileVersion: request.RuntimeProfileVersion,
				},
				mediaedge.BridgeAudioFormat{Encoding: mediav1.AudioEncoding_AUDIO_ENCODING_PCM_S16LE, SampleRate: 16_000, Channels: 1, FrameMS: 20},
				mediaedge.BridgeAudioFormat{Encoding: mediav1.AudioEncoding_AUDIO_ENCODING_PCM_S16LE, SampleRate: 24_000, Channels: 1, FrameMS: 20},
			)
			if err != nil {
				return nil, err
			}
			onError := func(bridgeErr error) {
				log.Printf("media edge device Voice Core stream failed session=%s err=%v", request.SessionID, bridgeErr)
				deviceServer.HandleBridgeError(request, bridgeErr)
			}
			return mediaedge.NewVoiceCoreMediaRuntimeWithDownlinkSender(
				context.Background(), session, core, sender,
				func(event *mediav1.CoreToMedia) {
					deviceServer.ForwardCoreEventForSession(request, event)
				},
				onError,
			)
		}
	}
	if production && !deviceServer.Verifier.Configured() {
		return false, errors.New("production device WSS requires EdDSA/JWKS public keys")
	}
	deviceServer.RequireSharedState = production
	redisURL := strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_STATE_REDIS_URL"))
	if redisURL == "" {
		if production {
			return false, errors.New("production device WSS requires MEDIA_EDGE_DEVICE_STATE_REDIS_URL")
		}
	} else {
		if production && !strings.HasPrefix(strings.ToLower(redisURL), "rediss://") {
			return false, errors.New("production device shared state Redis URL must use rediss://")
		}
		redisTLS, tlsErr := buildDeviceStateRedisTLS(production, redisURL)
		if tlsErr != nil {
			return false, tlsErr
		}
		ownerID := strings.TrimSpace(os.Getenv("MEDIA_EDGE_INSTANCE_ID"))
		if ownerID == "" {
			hostname, hostnameErr := os.Hostname()
			if hostnameErr != nil || strings.TrimSpace(hostname) == "" {
				return false, errors.New("device shared state requires MEDIA_EDGE_INSTANCE_ID or a hostname")
			}
			ownerID = fmt.Sprintf("%s-%d", hostname, os.Getpid())
		}
		sharedState, sharedErr := mediaedge.NewRedisDeviceState(mediaedge.RedisDeviceStateConfig{
			URL:                redisURL,
			TLSConfig:          redisTLS,
			KeyPrefix:          strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_STATE_KEY_PREFIX")),
			OwnerID:            ownerID,
			CommandTimeout:     envDuration("MEDIA_EDGE_DEVICE_STATE_TIMEOUT_MS", 500*time.Millisecond),
			LeaseTTL:           envDuration("MEDIA_EDGE_DEVICE_LEASE_TTL_MS", 30*time.Second),
			LeaseCheckInterval: envDuration("MEDIA_EDGE_DEVICE_LEASE_CHECK_INTERVAL_MS", 5*time.Second),
		})
		if sharedErr != nil {
			return false, sharedErr
		}
		sharedState.Install(deviceServer)
	}
	log.Printf("media edge device WSS enabled endpoint=%s", mediaedge.DeviceMediaEndpoint)
	return true, nil
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		os.Exit(runHealthcheck())
	}
	publicKeys, err := loadMediaPublicKeys()
	if err != nil {
		log.Fatal(err)
	}
	verifier := mediaedge.JWTVerifier{
		Secret:     []byte(strings.TrimSpace(os.Getenv("MEDIA_EDGE_JWT_SECRET"))),
		PublicKeys: publicKeys,
		KeyID:      strings.TrimSpace(os.Getenv("MEDIA_EDGE_JWT_KEY_ID")),
		Issuer:     strings.TrimSpace(os.Getenv("MEDIA_EDGE_JWT_ISSUER")),
		Audience:   strings.TrimSpace(os.Getenv("MEDIA_EDGE_JWT_AUDIENCE")),
		MaxTTL:     time.Duration(envInt("MEDIA_EDGE_JWT_MAX_TTL_S", 300)) * time.Second,
		ClockSkew:  time.Duration(envInt("MEDIA_EDGE_JWT_CLOCK_SKEW_S", 30)) * time.Second,
	}
	production := strings.EqualFold(strings.TrimSpace(os.Getenv("ENVIRONMENT")), "production")
	if production && ((len(publicKeys) == 0 && len(verifier.Secret) < 32) ||
		verifier.Issuer == "" || verifier.Audience == "") {
		log.Fatal("production media edge requires JWT secret, issuer and audience")
	}
	server := mediaedge.NewServer(verifier, envInt("MEDIA_EDGE_MAX_PENDING_FRAMES", 20))
	server.InternalControlToken = strings.TrimSpace(os.Getenv("MEDIA_EDGE_INTERNAL_CONTROL_TOKEN"))
	server.AllowInsecureDevelopment = !production && envBool("MEDIA_EDGE_ALLOW_INSECURE_DEVELOPMENT")
	deviceWSSRequested := envBool("MEDIA_EDGE_DEVICE_WSS_ENABLED")
	webRTCEnabled, err := requestedWebRTCEnabled(production, deviceWSSRequested)
	if err != nil {
		log.Fatal(err)
	}
	var terminator *mediaedge.WebRTCTerminator
	if webRTCEnabled {
		webrtcConfig, configErr := buildWebRTCConfig(production)
		if configErr != nil {
			log.Fatal(configErr)
		}
		terminator, err = mediaedge.NewWebRTCTerminator(server, verifier, webrtcConfig)
		if err != nil {
			log.Fatal(err)
		}
		server.WHIPHandler = terminator.Handler()
		server.DownlinkSenderFactory = terminator.DownlinkSender
		server.DownlinkReadyProbe = terminator.Ready
		server.SessionCloseHook = terminator.CloseSession
		server.RequireExternalDownlinkSender = production
	} else {
		log.Printf("media edge WebRTC disabled; serving Direct Device WSS only")
	}
	voiceCore, err := buildVoiceCoreBridge()
	if err != nil {
		log.Fatal(err)
	}
	if voiceCore != nil {
		server.ReadyProbe = voiceCore.Ready
		if terminator != nil {
			server.BridgeFactory = func(request mediaedge.OpenSessionRequest, session *mediaedge.Session, sender mediaedge.DownlinkSender) (*mediaedge.VoiceCoreMediaRuntime, error) {
				handshakeCtx, cancel := context.WithTimeout(context.Background(), envDuration("MEDIA_EDGE_VOICE_CORE_CONNECT_TIMEOUT_MS", 5*time.Second))
				defer cancel()
				clientType := request.ClientType
				if clientType == "" {
					clientType = session.ClientTypeValue()
				}
				_, accountID, deviceID, streamEpoch := session.IdentitySnapshot()
				core, err := voiceCore.ConnectWithHandshakeContext(
					context.Background(),
					handshakeCtx,
					mediaedge.BridgeIdentity{
						SessionID: request.SessionID, AccountID: accountID, DeviceID: deviceID,
						ClientType: clientType, StreamEpoch: streamEpoch,
						SubjectID: request.SubjectID, BindingID: request.BindingID,
						BindingVersion:        request.BindingVersion,
						RuntimeProfileVersion: request.RuntimeProfileVersion,
					},
					mediaedge.BridgeAudioFormat{Encoding: mediav1.AudioEncoding_AUDIO_ENCODING_PCM_S16LE, SampleRate: 16_000, Channels: 1, FrameMS: 20},
					mediaedge.BridgeAudioFormat{Encoding: mediav1.AudioEncoding_AUDIO_ENCODING_PCM_S16LE, SampleRate: 24_000, Channels: 1, FrameMS: 20},
				)
				if err != nil {
					return nil, err
				}
				onError := func(bridgeErr error) {
					log.Printf("media edge Voice Core stream failed session=%s err=%v", request.SessionID, bridgeErr)
					terminator.HandleBridgeError(request, bridgeErr)
				}
				if sender != nil {
					return mediaedge.NewVoiceCoreMediaRuntimeWithDownlinkSender(
						context.Background(), session, core, sender,
						func(event *mediav1.CoreToMedia) { terminator.ForwardCoreEvent(request, event) }, onError,
					)
				}
				return mediaedge.NewVoiceCoreMediaRuntime(context.Background(), session, core, nil, onError)
			}
		}
		log.Printf("media edge Voice Core bridge enabled address=%s", strings.TrimSpace(os.Getenv("MEDIA_EDGE_VOICE_CORE_ADDR")))
	} else {
		log.Printf("media edge running provider-neutral HTTP reference; Voice Core bridge is not configured")
	}
	deviceVerifier := mediaedge.DeviceJWTVerifier{
		PublicKeys:        publicKeys,
		Secret:            []byte(strings.TrimSpace(os.Getenv("MEDIA_EDGE_JWT_SECRET"))),
		MaxTTL:            time.Duration(envInt("MEDIA_EDGE_JWT_MAX_TTL_S", 300)) * time.Second,
		ClockSkew:         time.Duration(envInt("MEDIA_EDGE_JWT_CLOCK_SKEW_S", 30)) * time.Second,
		AllowHS256DevOnly: !production && envBool("MEDIA_EDGE_DEVICE_ALLOW_HS256_DEVELOPMENT"),
	}
	deviceIssuer, deviceAudience, err := deviceJWTIdentity(production, deviceWSSRequested)
	if err != nil {
		log.Fatal(err)
	}
	deviceVerifier.Issuer = deviceIssuer
	deviceVerifier.Audience = deviceAudience
	deviceWSS := mediaedge.NewDeviceWSServer(deviceVerifier)
	deviceWSSEnabled, err := buildDeviceWSS(
		deviceVerifier,
		production,
		voiceCore,
		deviceWSS,
	)
	if err != nil {
		log.Fatal(err)
	}
	if deviceWSSEnabled {
		if production && len(server.InternalControlToken) < 32 {
			log.Fatal("production device WSS requires MEDIA_EDGE_INTERNAL_CONTROL_TOKEN with at least 32 characters")
		}
		server.DeviceWSS = deviceWSS
	}
	internalTLS, err := buildInternalListenerTLS(production, deviceWSSEnabled)
	if err != nil {
		log.Fatal(err)
	}
	if internalTLS != nil {
		if !strings.HasPrefix(strings.TrimSpace(os.Getenv("MEDIA_EDGE_HEALTHCHECK_URL")), "https://") {
			log.Fatal("internal listener TLS requires MEDIA_EDGE_HEALTHCHECK_URL=https:// with MEDIA_EDGE_HEALTHCHECK_CA_FILE, MEDIA_EDGE_HEALTHCHECK_CLIENT_CERT_FILE and MEDIA_EDGE_HEALTHCHECK_CLIENT_KEY_FILE")
		}
		if _, err := buildHealthcheckClient(); err != nil {
			log.Fatal(err)
		}
	}
	deviceWSSAddr, err := deviceWSSListenAddress(production, deviceWSSEnabled)
	if err != nil {
		log.Fatal(err)
	}
	sessionCloseHook, err := buildDeviceSessionCloseHook(production, deviceWSSEnabled)
	if err != nil {
		log.Fatal(err)
	}
	if sessionCloseHook != nil {
		// Per-connection close reporting: every accepted device session posts
		// its real close reason through this hook (device_close / superseded /
		// network / edge_shutdown). No global empty payload is ever sent.
		server.DeviceWSS.SessionCloseReportHook = sessionCloseHook
		log.Printf("media edge device session close reporter configured url=%s", strings.TrimSpace(os.Getenv("MEDIA_EDGE_DEVICE_CLOSE_REPORT_URL")))
	}
	addr := strings.TrimSpace(os.Getenv("MEDIA_EDGE_HTTP_ADDR"))
	if addr == "" {
		addr = ":8080"
	}
	internalAddr := strings.TrimSpace(os.Getenv("MEDIA_EDGE_INTERNAL_HTTP_ADDR"))
	log.Printf("memoria media edge listening on %s", addr)
	if internalAddr != "" {
		log.Printf("memoria media edge internal listener on %s", internalAddr)
	}
	publicHandler := server.Handler()
	if internalAddr != "" {
		publicHandler = server.PublicHandler()
	}
	httpServer := &http.Server{Addr: addr, Handler: publicHandler, ReadHeaderTimeout: 5 * time.Second}
	internalServer := &http.Server{Addr: internalAddr, Handler: server.InternalHandler(), ReadHeaderTimeout: 5 * time.Second}
	if internalTLS != nil {
		internalServer.TLSConfig = internalTLS
	}
	stopContext, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	errors := make(chan error, 1)
	go func() { errors <- httpServer.ListenAndServe() }()
	if internalAddr != "" {
		go func() {
			if internalTLS != nil {
				errors <- internalServer.ListenAndServeTLS("", "")
			} else {
				errors <- internalServer.ListenAndServe()
			}
		}()
	}
	var deviceServer *http.Server
	if deviceWSSAddr != "" {
		deviceMux := http.NewServeMux()
		deviceMux.Handle(mediaedge.DeviceMediaEndpoint, deviceWSS)
		deviceServer = &http.Server{Addr: deviceWSSAddr, Handler: deviceMux, ReadHeaderTimeout: 5 * time.Second}
		log.Printf("media edge direct device WSS listener on %s endpoint=%s", deviceWSSAddr, mediaedge.DeviceMediaEndpoint)
		go func() { errors <- deviceServer.ListenAndServe() }()
	}
	select {
	case err := <-errors:
		if err != nil && err != http.ErrServerClosed {
			log.Fatal(err)
		}
	case <-stopContext.Done():
		server.Draining.Store(true)
		if terminator != nil {
			_ = terminator.Close()
		}
		_ = server.Close()
		if voiceCore != nil {
			_ = voiceCore.Close()
		}
		shutdownContext, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_ = httpServer.Shutdown(shutdownContext)
		if internalAddr != "" {
			_ = internalServer.Shutdown(shutdownContext)
		}
		if deviceServer != nil {
			_ = deviceServer.Shutdown(shutdownContext)
		}
		cancel()
	}
}
