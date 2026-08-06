package main

import (
	"context"
	"crypto/ed25519"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
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
	url := strings.TrimSpace(os.Getenv("MEDIA_EDGE_HEALTHCHECK_URL"))
	if url == "" {
		url = "http://127.0.0.1:8080/readyz"
	}
	client := &http.Client{Timeout: 3 * time.Second}
	response, err := client.Get(url)
	if err != nil {
		return 1
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return 1
	}
	return 0
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
	server.AllowInsecureDevelopment = !production && envBool("MEDIA_EDGE_ALLOW_INSECURE_DEVELOPMENT")
	webrtcConfig, err := buildWebRTCConfig(production)
	if err != nil {
		log.Fatal(err)
	}
	terminator, err := mediaedge.NewWebRTCTerminator(server, verifier, webrtcConfig)
	if err != nil {
		log.Fatal(err)
	}
	server.WHIPHandler = terminator.Handler()
	server.DownlinkSenderFactory = terminator.DownlinkSender
	server.DownlinkReadyProbe = terminator.Ready
	server.SessionCloseHook = terminator.CloseSession
	server.RequireExternalDownlinkSender = production
	voiceCore, err := buildVoiceCoreBridge()
	if err != nil {
		log.Fatal(err)
	}
	if voiceCore != nil {
		server.ReadyProbe = voiceCore.Ready
		server.BridgeFactory = func(request mediaedge.OpenSessionRequest, session *mediaedge.Session, sender mediaedge.DownlinkSender) (*mediaedge.VoiceCoreMediaRuntime, error) {
			connectCtx, cancel := context.WithTimeout(context.Background(), envDuration("MEDIA_EDGE_VOICE_CORE_CONNECT_TIMEOUT_MS", 5*time.Second))
			defer cancel()
			clientType := request.ClientType
			if clientType == "" {
				clientType = session.ClientTypeValue()
			}
			_, accountID, deviceID, streamEpoch := session.IdentitySnapshot()
			core, err := voiceCore.Connect(
				connectCtx,
				mediaedge.BridgeIdentity{
					SessionID: request.SessionID, AccountID: accountID, DeviceID: deviceID,
					ClientType: clientType, StreamEpoch: streamEpoch,
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
		log.Printf("media edge Voice Core bridge enabled address=%s", strings.TrimSpace(os.Getenv("MEDIA_EDGE_VOICE_CORE_ADDR")))
	} else {
		log.Printf("media edge running provider-neutral HTTP reference; Voice Core bridge is not configured")
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
	stopContext, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	errors := make(chan error, 1)
	go func() { errors <- httpServer.ListenAndServe() }()
	if internalAddr != "" {
		go func() { errors <- internalServer.ListenAndServe() }()
	}
	select {
	case err := <-errors:
		if err != nil && err != http.ErrServerClosed {
			log.Fatal(err)
		}
	case <-stopContext.Done():
		server.Draining.Store(true)
		_ = terminator.Close()
		_ = server.Close()
		if voiceCore != nil {
			_ = voiceCore.Close()
		}
		shutdownContext, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_ = httpServer.Shutdown(shutdownContext)
		if internalAddr != "" {
			_ = internalServer.Shutdown(shutdownContext)
		}
		cancel()
	}
}
