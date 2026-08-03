package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	mediaedge "memoria/services/media_edge"
	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

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
	defer response.Body.Close()
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
	return mediaedge.DialVoiceCore(connectContext, mediaedge.VoiceCoreBridgeConfig{
		Address: address, TLS: tlsConfig, AllowInsecureDevelopment: allowInsecure,
	})
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "healthcheck" {
		os.Exit(runHealthcheck())
	}
	verifier := mediaedge.JWTVerifier{
		Secret:   []byte(strings.TrimSpace(os.Getenv("MEDIA_EDGE_JWT_SECRET"))),
		Issuer:   strings.TrimSpace(os.Getenv("MEDIA_EDGE_JWT_ISSUER")),
		Audience: strings.TrimSpace(os.Getenv("MEDIA_EDGE_JWT_AUDIENCE")),
	}
	production := strings.EqualFold(strings.TrimSpace(os.Getenv("ENVIRONMENT")), "production")
	if production && (len(verifier.Secret) < 32 || verifier.Issuer == "" || verifier.Audience == "") {
		log.Fatal("production media edge requires JWT secret, issuer and audience")
	}
	server := mediaedge.NewServer(verifier, envInt("MEDIA_EDGE_MAX_PENDING_FRAMES", 100))
	server.AllowInsecureDevelopment = !production && envBool("MEDIA_EDGE_ALLOW_INSECURE_DEVELOPMENT")
	// This binary currently exposes only the development HTTP queue. It stays
	// fail-closed in production until a real WHIP/WebRTC/RTP terminator is
	// installed and the operator explicitly confirms it with
	// MEDIA_EDGE_EXTERNAL_DOWNLINK_SENDER_READY=true. The flag is the
	// documented hand-over seam for that terminator, not a way to bypass the
	// gate: session creation additionally requires every Voice Core bridge to
	// carry an actual DownlinkSender (see Server.sessions), so a bare flag
	// without an installed sender still rejects media sessions.
	server.RequireExternalDownlinkSender = production
	externalDownlinkSenderReady := envBool("MEDIA_EDGE_EXTERNAL_DOWNLINK_SENDER_READY")
	server.ExternalDownlinkSenderReady = func() bool { return externalDownlinkSenderReady }
	if production && !externalDownlinkSenderReady {
		log.Print("production media edge has no installed downlink sender; readiness stays fail-closed until a real terminator is attached and MEDIA_EDGE_EXTERNAL_DOWNLINK_SENDER_READY=true")
	}
	voiceCore, err := buildVoiceCoreBridge()
	if err != nil {
		log.Fatal(err)
	}
	if voiceCore != nil {
		server.ReadyProbe = voiceCore.Ready
		server.BridgeFactory = func(request mediaedge.OpenSessionRequest, session *mediaedge.Session) (*mediaedge.VoiceCoreMediaRuntime, error) {
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
			return mediaedge.NewVoiceCoreMediaRuntime(
				context.Background(), session, core, nil,
				func(bridgeErr error) {
					log.Printf("media edge Voice Core stream failed session=%s err=%v", request.SessionID, bridgeErr)
				},
			)
		}
		log.Printf("media edge Voice Core bridge enabled address=%s", strings.TrimSpace(os.Getenv("MEDIA_EDGE_VOICE_CORE_ADDR")))
	} else {
		log.Printf("media edge running provider-neutral HTTP reference; Voice Core bridge is not configured")
	}
	addr := strings.TrimSpace(os.Getenv("MEDIA_EDGE_HTTP_ADDR"))
	if addr == "" {
		addr = ":8080"
	}
	log.Printf("memoria media edge listening on %s", addr)
	httpServer := &http.Server{Addr: addr, Handler: server.Handler(), ReadHeaderTimeout: 5 * time.Second}
	stopContext, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	errors := make(chan error, 1)
	go func() { errors <- httpServer.ListenAndServe() }()
	select {
	case err := <-errors:
		if err != nil && err != http.ErrServerClosed {
			log.Fatal(err)
		}
	case <-stopContext.Done():
		server.Draining.Store(true)
		_ = server.Close()
		_ = voiceCore.Close()
		shutdownContext, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		_ = httpServer.Shutdown(shutdownContext)
		cancel()
	}
}
