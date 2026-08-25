package mediaedge

// Per-connection hardware device state machine. The first frame must be a
// v2 device.hello, validated against the public contract and the server
// acoustic registry. Legacy v1 never reaches this direct endpoint. After acceptance the
// read loop forwards audio/controls to the existing Session/VoiceCoreMedia
// runtime and the write loop drains the priority lane.

import (
	"encoding/json"
	"errors"
	"log"
	"math"
	"sync"
	"sync/atomic"
	"time"

	"github.com/gorilla/websocket"
)

const (
	deviceConnHello    int32 = 0
	deviceConnAccepted int32 = 1
	deviceConnClosed   int32 = 2

	deviceIdleReadTimeout     = 90 * time.Second
	deviceWriteTimeout        = 5 * time.Second
	devicePingInterval        = 20 * time.Second
	deviceDefaultControlRate  = 40.0
	deviceDefaultControlBurst = 20.0
	deviceDefaultAudioRate    = 120.0
	deviceDefaultAudioBurst   = 300.0
)

type deviceTokenBucket struct {
	mu     sync.Mutex
	rate   float64
	burst  float64
	tokens float64
	last   time.Time
}

func newDeviceTokenBucket(rate, burst float64) *deviceTokenBucket {
	return &deviceTokenBucket{rate: rate, burst: burst, tokens: burst, last: time.Now()}
}

func (b *deviceTokenBucket) allow(now time.Time) bool {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.rate <= 0 {
		return true
	}
	elapsed := now.Sub(b.last).Seconds()
	if elapsed > 0 {
		b.tokens = math.Min(b.burst, b.tokens+elapsed*b.rate)
		b.last = now
	}
	if b.tokens >= 1 {
		b.tokens--
		return true
	}
	return false
}

type DeviceConnection struct {
	server    *DeviceWSServer
	connID    uint64
	ws        *websocket.Conn
	claims    DeviceMediaClaims
	deviceID  string
	sessionID string
	epoch     uint32

	state     atomic.Int32
	closeOnce sync.Once
	closed    chan struct{}
	writeErr  chan struct{}

	lane *devicePriorityLane

	stateMu            sync.Mutex
	runtime            *VoiceCoreMediaRuntime
	session            *Session
	currentFence       deviceFence
	sessionCloseQueued bool
	serverControlSeq   uint64

	downlinkClockMu            sync.Mutex
	downlinkClockFence         deviceFence
	downlinkSourceSequenceBase uint64
	downlinkSourceSampleBase   uint64
	downlinkRebasePending      bool

	uplinkOpus             *opusDecoder
	downlinkOpus           *opusEncoder
	downlinkRate           uint64
	audioMode              string
	helloVersion           uint64
	firmwareVersion        string
	boardProfile           string
	playbackWatermark      string
	runtimeProfileVersion  uint64
	appliedProfileVersion  uint64
	appliedSettingsVersion uint64
	profileAppliedAt       time.Time
	connectedAt            time.Time
	lease                  DeviceLease

	// closeReason is the bounded reason reported at teardown for an accepted
	// session (device_close / superseded / network / edge_shutdown). The
	// default network applies to read/write failures; explicit paths mark it
	// before closing.
	closeReason string

	// playbackActive tracks the signed playback receipts so a VAD start that
	// arrives while the device renders downlink audio is treated as a barge
	// candidate and gated on the "voice" allowlist source. Ordinary
	// listening VAD is never gated.
	playbackActive bool
	acoustic       *DeviceAcousticProfile
	ledger         *devicePlaybackLedger

	controlLimiter *deviceTokenBucket
	audioLimiter   *deviceTokenBucket

	uplinkMu       sync.Mutex
	lastUplinkSeq  uint32
	hasUplinkSeq   bool
	lastSampleEnd  uint64
	lastControlSeq uint64
	hasControlSeq  bool
}

func newDeviceConnection(server *DeviceWSServer, ws *websocket.Conn, claims DeviceMediaClaims) (*DeviceConnection, error) {
	lane, err := newDevicePriorityLane(server.Backpressure)
	if err != nil {
		return nil, err
	}
	conn := &DeviceConnection{
		server:                server,
		connID:                server.nextConnID.Add(1),
		ws:                    ws,
		claims:                claims,
		deviceID:              claims.DeviceID,
		sessionID:             claims.SessionID,
		epoch:                 uint32(claims.StreamEpoch),
		closed:                make(chan struct{}),
		writeErr:              make(chan struct{}),
		lane:                  lane,
		controlLimiter:        newDeviceTokenBucket(server.ControlRatePerSec, server.ControlBurst),
		audioLimiter:          newDeviceTokenBucket(server.AudioRatePerSec, server.AudioBurst),
		runtimeProfileVersion: claims.RuntimeProfileVersion,
	}
	conn.state.Store(deviceConnHello)
	lane.setDropHook(func(item deviceLaneItem) {
		if item.kind == "audio" {
			server.metrics.backpressureDrop.Add(1)
		}
	})
	return conn, nil
}

// run owns the read side: hello handshake, then uplink audio and controls.
func (c *DeviceConnection) run() {
	c.server.registerConn(c)
	c.server.metrics.activeConnections.Add(1)
	defer func() {
		c.server.metrics.activeConnections.Add(-1)
		c.close()
	}()
	c.ws.SetPongHandler(func(string) error {
		_ = c.ws.SetReadDeadline(time.Now().Add(deviceIdleReadTimeout))
		return nil
	})
	go c.writeLoop()
	_ = c.ws.SetReadDeadline(time.Now().Add(c.server.HelloTimeout))
	messageType, data, err := c.ws.ReadMessage()
	if err != nil {
		c.logSocketError("hello_read", err)
		c.server.metrics.helloRejected.Add(1)
		c.server.rejectedReasons.add("hello_timeout_or_connection_failure")
		return
	}
	if messageType != websocket.TextMessage {
		c.server.metrics.helloRejected.Add(1)
		c.server.rejectedReasons.add("hello_not_text")
		c.closeWithCode(websocket.CloseProtocolError, "first frame must be device.hello")
		return
	}
	if err := c.handleHello(data); err != nil {
		c.server.metrics.helloRejected.Add(1)
		c.server.rejectedReasons.add(safeDeviceReason(err))
		c.failSession(err)
		return
	}
	// A cross-host takeover may close this socket while RuntimeFactory is
	// still completing. Never revive a closed handshake into accepted state.
	if !c.state.CompareAndSwap(deviceConnHello, deviceConnAccepted) {
		return
	}
	c.stateMu.Lock()
	c.connectedAt = time.Now().UTC()
	c.stateMu.Unlock()
	c.sendAccepted(c.audioMode, c.downlinkRate)
	c.server.metrics.connectSuccess.Add(1)
	if c.server.Leases.CheckInterval() > 0 {
		go c.leaseWatchLoop()
	}
	_ = c.ws.SetReadDeadline(time.Now().Add(deviceIdleReadTimeout))
	for {
		messageType, data, err := c.ws.ReadMessage()
		if err != nil {
			c.logSocketError("read", err)
			return
		}
		switch messageType {
		case websocket.TextMessage:
			if !c.handleControl(data) {
				log.Printf("media edge device WSS handler rejected session=%s device=%s epoch=%d kind=text", c.sessionID, c.deviceID, c.epoch)
				return
			}
		case websocket.BinaryMessage:
			if !c.handleAudio(data) {
				log.Printf("media edge device WSS handler rejected session=%s device=%s epoch=%d kind=binary", c.sessionID, c.deviceID, c.epoch)
				return
			}
		default:
			log.Printf("media edge device WSS unsupported message type session=%s device=%s epoch=%d type=%d", c.sessionID, c.deviceID, c.epoch, messageType)
			return
		}
	}
}

// logSocketError keeps the accepted device transport's close boundary visible.
// The close report intentionally remains a bounded product projection; this
// diagnostic preserves the underlying WebSocket code/error for incident work.
func (c *DeviceConnection) logSocketError(phase string, err error) {
	var closeErr *websocket.CloseError
	if errors.As(err, &closeErr) {
		log.Printf("media edge device WSS socket error session=%s device=%s epoch=%d phase=%s close_code=%d close_text=%q", c.sessionID, c.deviceID, c.epoch, phase, closeErr.Code, closeErr.Text)
		return
	}
	log.Printf("media edge device WSS socket error session=%s device=%s epoch=%d phase=%s err=%v", c.sessionID, c.deviceID, c.epoch, phase, err)
}

// leaseWatchLoop is the Pub/Sub-loss backstop for cross-host takeovers. A
// Redis error closes the socket fail-closed; continuing on process-local state
// would let an old Edge instance keep forwarding after its lease was replaced.
func (c *DeviceConnection) leaseWatchLoop() {
	interval := c.server.Leases.CheckInterval()
	if interval <= 0 {
		return
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-c.closed:
			return
		case <-ticker.C:
			owned, err := c.server.Leases.Refresh(c.lease)
			if err != nil {
				c.closeWithCode(websocket.CloseInternalServerErr, "shared lease unavailable")
				c.close()
				return
			}
			if !owned {
				c.supersede()
				return
			}
		}
	}
}

func (c *DeviceConnection) closeWithCode(code int, reason string) {
	_ = c.ws.WriteControl(websocket.CloseMessage,
		websocket.FormatCloseMessage(code, reason), time.Now().Add(deviceWriteTimeout))
}

func (c *DeviceConnection) close() {
	c.closeOnce.Do(func() {
		accepted := c.state.Load() == deviceConnAccepted
		c.state.Store(deviceConnClosed)
		c.lane.close()
		c.server.unregisterConn(c)
		c.server.Leases.Release(c.deviceID, c.connID)
		c.stateMu.Lock()
		runtime := c.runtime
		session := c.session
		reason := c.closeReason
		if reason == "" {
			reason = SessionCloseReasonNetwork
		}
		connectedAt := c.connectedAt
		c.runtime = nil
		c.session = nil
		c.stateMu.Unlock()
		if runtime != nil {
			_ = runtime.Close()
		}
		if session != nil {
			session.Stop()
		}
		if c.uplinkOpus != nil {
			c.uplinkOpus.close()
		}
		if c.downlinkOpus != nil {
			c.downlinkOpus.close()
		}
		_ = c.ws.Close()
		close(c.closed)
		if !accepted {
			// Hello-failed or pre-accept connections never owned a live
			// media session and are not reported.
			return
		}
		now := time.Now().UTC()
		report := DeviceSessionCloseReport{
			DeviceID:    c.deviceID,
			SessionID:   c.sessionID,
			StreamEpoch: uint64(c.epoch),
			AccountID:   c.claims.Subject,
			Reason:      reason,
			Connected:   true,
			ClosedAt:    now.Format(time.RFC3339Nano),
		}
		if !connectedAt.IsZero() {
			report.ConnectedAt = connectedAt.Format(time.RFC3339Nano)
		}
		c.server.reportSessionClose(report)
	})
}

// supersede closes a connection that a newer stream epoch replaced. The
// device already moved on; no error payload is sent.
func (c *DeviceConnection) supersede() {
	c.markCloseReason(SessionCloseReasonSuperseded)
	c.closeWithCode(4000, "superseded by a newer stream epoch")
	c.close()
}

// sendControl enqueues one JSON control message on its priority lane.
func (c *DeviceConnection) sendControl(priority int, payload []byte) {
	c.lane.enqueueControl(priority, payload)
}

// nextServerSequence allocates the per-connection server control sequence.
func (c *DeviceConnection) nextServerSequence() uint64 {
	c.stateMu.Lock()
	defer c.stateMu.Unlock()
	c.serverControlSeq++
	return c.serverControlSeq
}

// setCurrentFence records the authoritative generation the device has been
// told about; the write loop uses it to drop stale audio.
func (c *DeviceConnection) setCurrentFence(fence deviceFence) {
	c.stateMu.Lock()
	defer c.stateMu.Unlock()
	c.currentFence = fence
}

func (c *DeviceConnection) getCurrentFence() deviceFence {
	c.stateMu.Lock()
	defer c.stateMu.Unlock()
	return c.currentFence
}

// markCloseReason records the bounded close reason before teardown so the
// close report carries the real cause instead of a guessed one.
func (c *DeviceConnection) markCloseReason(reason string) {
	c.stateMu.Lock()
	defer c.stateMu.Unlock()
	c.closeReason = reason
}

// isPlaybackActive reports whether the last signed playback receipt opened
// a rendering window.
func (c *DeviceConnection) isPlaybackActive() bool {
	c.stateMu.Lock()
	defer c.stateMu.Unlock()
	return c.playbackActive
}

func (c *DeviceConnection) closeReasonValue() string {
	c.stateMu.Lock()
	defer c.stateMu.Unlock()
	if c.closeReason == "" {
		return SessionCloseReasonNetwork
	}
	return c.closeReason
}

func (c *DeviceConnection) sendSessionError(code string, retryable bool) {
	payload, err := marshalDeviceControl(deviceSessionError{
		Type: "session.error", Version: 2,
		SessionID:         c.sessionID,
		StreamEpoch:       uint64(c.epoch),
		ControlSequence:   c.nextServerSequence(),
		ServerMonotonicMS: c.server.monotonicMS(),
		Code:              code, Retryable: retryable,
	})
	if err != nil {
		return
	}
	c.sendControl(deviceControlPriority("session.error"), payload)
}

// failSession reports a session-level error (best effort) and closes.
func (c *DeviceConnection) failSession(err error) {
	code := "session_failed"
	retryable := false
	switch {
	case errors.Is(err, errDeviceBusy), errors.Is(err, errDeviceEpochStale):
		code = "device_busy"
		retryable = true
	case errors.Is(err, errDeviceRuntimeUnavailable):
		code = "runtime_unavailable"
		retryable = true
	case errors.Is(err, errDeviceInvalidHello):
		code = "invalid_hello"
	}
	c.sendSessionError(code, retryable)
	c.closeWithCode(4002, code)
	c.close()
}

var (
	errDeviceBusy               = errors.New("device already has an active connection")
	errDeviceEpochStale         = errors.New("stream epoch did not advance")
	errDeviceRuntimeUnavailable = errors.New("voice core runtime is unavailable")
	errDeviceInvalidHello       = errors.New("invalid device hello")
)

func safeDeviceReason(err error) string {
	if err == nil {
		return "unknown"
	}
	switch {
	case errors.Is(err, errDeviceBusy), errors.Is(err, errDeviceEpochStale):
		return "device_busy"
	case errors.Is(err, errDeviceRuntimeUnavailable):
		return "runtime_unavailable"
	default:
		return "invalid_hello"
	}
}

// handleHello validates the first frame, resolves the audio mode, builds the
// Session + Voice Core runtime and replies with session.accepted.
func (c *DeviceConnection) handleHello(data []byte) error {
	var envelope struct {
		Type    string          `json:"type"`
		Version json.RawMessage `json:"version"`
	}
	if err := json.Unmarshal(data, &envelope); err != nil || envelope.Type != "device.hello" {
		return errDeviceInvalidHello
	}
	var version uint64
	if err := json.Unmarshal(envelope.Version, &version); err != nil {
		return errDeviceInvalidHello
	}
	audioMode := DeviceAudioModeHalfDuplexSafe
	downlinkRate := uint64(24_000)
	firmwareVersion := ""
	boardProfile := ""
	playbackWatermark := "none"
	var acoustic *DeviceAcousticProfile
	if version == 2 {
		var hello deviceHelloV2
		if err := jsonUnmarshalStrict(data, &hello); err != nil {
			return errDeviceInvalidHello
		}
		if err := hello.validate(c.deviceID, uint64(c.epoch)); err != nil {
			return errDeviceInvalidHello
		}
		firmwareVersion = hello.FirmwareVersion
		boardProfile = hello.BoardProfile
		playbackWatermark = hello.Capabilities.PlaybackWatermark
		mode, err := c.server.Acoustic.ResolveAudioModeV2(hello)
		if err != nil {
			return errDeviceInvalidHello
		}
		audioMode = applyBargeInPolicy(
			intersectRequestedAudioMode(c.claims.DeviceSettings.AudioMode, mode),
			c.claims.DeviceSettings.AllowedBargeIn,
		)
		downlinkRate = ResolveDownlinkRate(version, hello.Audio)
		if profile, ok := c.server.Acoustic.Lookup(hello.BoardProfile); ok {
			acoustic = &profile
		}
		c.server.metrics.helloV2Total.Add(1)
	} else {
		return errDeviceInvalidHello
	}
	c.helloVersion = version
	c.audioMode = audioMode
	c.downlinkRate = downlinkRate
	c.firmwareVersion = firmwareVersion
	c.boardProfile = boardProfile
	c.playbackWatermark = playbackWatermark
	c.acoustic = acoustic
	c.server.gauges.observeAudioMode(audioMode)
	c.ledger = newDevicePlaybackLedger(downlinkRate, func(lag int64) {
		c.server.gauges.observePlaybackAckLagMS(lag)
	})

	downlinkOpus, err := newOpusEncoder(int(downlinkRate), 1)
	if err != nil {
		return errDeviceRuntimeUnavailable
	}
	uplinkOpus, err := newOpusDecoder(16_000, 1)
	if err != nil {
		downlinkOpus.close()
		return errDeviceRuntimeUnavailable
	}
	c.downlinkOpus = downlinkOpus
	c.uplinkOpus = uplinkOpus

	request := OpenSessionRequest{
		SessionID: c.sessionID, AccountID: c.claims.Subject, DeviceID: c.deviceID,
		ClientType: "device", StreamEpoch: uint64(c.epoch),
		SubjectID: c.claims.SubjectID, BindingID: c.claims.BindingID,
		BindingVersion:        c.claims.BindingVersion,
		RuntimeProfileVersion: c.claims.RuntimeProfileVersion,
	}
	session, err := NewSession(request, c.server.MaxPendingFrames)
	if err != nil {
		return errDeviceInvalidHello
	}
	c.stateMu.Lock()
	c.session = session
	c.stateMu.Unlock()

	lease := DeviceLease{
		DeviceID: c.deviceID, SessionID: c.sessionID,
		StreamEpoch: uint64(c.epoch), ConnID: c.connID,
		OwnerID: c.server.Leases.OwnerID(),
	}
	old, replaced, err := c.server.Leases.Install(lease)
	if err != nil {
		session.Stop()
		c.server.metrics.leaseRejected.Add(1)
		if errors.Is(err, ErrDeviceSharedStateUnavailable) {
			return errDeviceRuntimeUnavailable
		}
		return errDeviceEpochStale
	}
	if replaced {
		c.server.metrics.reconnectTotal.Add(1)
		// conn_id is process-local and can collide across Edge hosts. Only
		// resolve it in this server when the shared owner_id also matches;
		// remote owners are retired by Redis Pub/Sub plus lease refresh.
		if old.OwnerID == c.server.Leases.OwnerID() {
			if oldConnection := c.server.connection(old.ConnID); oldConnection != nil {
				go oldConnection.supersede()
			}
		}
	}
	c.lease = lease
	if c.state.Load() == deviceConnClosed {
		c.server.Leases.Release(c.deviceID, c.connID)
		session.Stop()
		return errDeviceEpochStale
	}

	var runtime *VoiceCoreMediaRuntime
	if c.server.RuntimeFactory != nil {
		runtime, err = c.server.RuntimeFactory(request, session, c.downlinkSender)
		if err != nil {
			c.server.Leases.Release(c.deviceID, c.connID)
			session.Stop()
			c.server.metrics.runtimeErrors.Add(1)
			return errDeviceRuntimeUnavailable
		}
		c.stateMu.Lock()
		if c.state.Load() == deviceConnClosed {
			c.stateMu.Unlock()
			_ = runtime.Close()
			c.server.Leases.Release(c.deviceID, c.connID)
			session.Stop()
			return errDeviceEpochStale
		}
		c.runtime = runtime
		c.stateMu.Unlock()
		if currentProvider, ok := runtime.core.(generationSnapshotStream); ok {
			currentFence, active := currentProvider.CurrentGeneration()
			if currentFence.GenerationID > 0 {
				if err := session.RestoreGeneration(currentFence, active); err != nil {
					c.server.Leases.Release(c.deviceID, c.connID)
					session.Stop()
					_ = runtime.Close()
					return errDeviceRuntimeUnavailable
				}
				c.setCurrentFence(fenceToDevice(currentFence))
				if active {
					c.beginDownlinkClock(fenceToDevice(currentFence), true)
				}
			}
		} else if currentProvider, ok := runtime.core.(generationStopStream); ok {
			// A CurrentFence-only provider cannot prove whether its fence is
			// active or cancelled. Preserve the fence for stale-event rejection,
			// but fail closed instead of reviving playback on reconnect.
			currentFence := currentProvider.CurrentFence()
			if currentFence.GenerationID > 0 {
				if err := session.RestoreGeneration(currentFence, false); err != nil {
					c.server.Leases.Release(c.deviceID, c.connID)
					session.Stop()
					_ = runtime.Close()
					return errDeviceRuntimeUnavailable
				}
				c.setCurrentFence(fenceToDevice(currentFence))
			}
		}
		runtime.Start()
	} else if c.server.RequireRuntime {
		c.server.Leases.Release(c.deviceID, c.connID)
		session.Stop()
		c.server.metrics.runtimeErrors.Add(1)
		return errDeviceRuntimeUnavailable
	}

	return nil
}

func (c *DeviceConnection) sendAccepted(audioMode string, downlinkRate uint64) {
	var currentPtr *deviceFence
	currentActive := false
	c.stateMu.Lock()
	if c.session != nil {
		fence, active := c.session.GenerationSnapshot()
		current := fenceToDevice(fence)
		if !current.isZero() {
			currentPtr = &current
			currentActive = active
		}
	}
	c.stateMu.Unlock()
	profileVersion := c.claims.RuntimeProfileVersion
	if profileVersion == 0 {
		profileVersion = 1
	}
	settings := c.claims.DeviceSettings
	settings.AudioMode = audioMode
	accepted := deviceSessionAccepted{
		Type:                    "session.accepted",
		Version:                 2,
		SessionID:               c.sessionID,
		StreamEpoch:             uint64(c.epoch),
		InteractionAuthority:    DeviceInteractionAuthority,
		AudioMode:               audioMode,
		DownlinkSampleRate:      downlinkRate,
		RuntimeProfileVersion:   profileVersion,
		DeviceSettings:          settings,
		CurrentFence:            currentPtr,
		CurrentGenerationActive: currentActive,
	}
	if audioMode == DeviceAudioModeFullDuplex && c.acoustic != nil {
		accepted.AcousticAttestation = &deviceAcousticAttestation{
			Verified: true, BoardProfile: c.acoustic.BoardProfile,
			ProfileVersion: c.acoustic.ProfileVersion,
		}
	}
	payload, err := marshalDeviceControl(accepted)
	if err != nil {
		return
	}
	c.sendControl(deviceControlPriority("session.accepted"), payload)
}

func intersectRequestedAudioMode(requested, capability string) string {
	rank := map[string]int{
		DeviceAudioModeHalfDuplexSafe:  0,
		DeviceAudioModeInterruptAssist: 1,
		DeviceAudioModeFullDuplex:      2,
	}
	requestedRank, requestedOK := rank[requested]
	capabilityRank, capabilityOK := rank[capability]
	if !requestedOK || !capabilityOK {
		return DeviceAudioModeHalfDuplexSafe
	}
	if requestedRank < capabilityRank {
		return requested
	}
	return capability
}

func (c *DeviceConnection) runtimeRef() (*VoiceCoreMediaRuntime, *Session) {
	c.stateMu.Lock()
	defer c.stateMu.Unlock()
	return c.runtime, c.session
}
