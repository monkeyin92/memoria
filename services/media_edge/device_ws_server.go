package mediaedge

// DeviceWSServer is the hardware device WSS component: authentication,
// single-use ticket replay store, per-device connection lease, acoustic
// registry, priority lane policy and runtime factory wiring.

import (
	"fmt"
	"sync"
	"sync/atomic"
	"time"
)

type DeviceWSServer struct {
	Verifier         DeviceJWTVerifier
	Tickets          *DeviceTicketStore
	Leases           *DeviceLeaseRegistry
	SharedState      *RedisDeviceState
	Acoustic         *DeviceAcousticRegistry
	RuntimeFactory   BridgeRuntimeFactory
	MaxPendingFrames int

	// RequireConfigured fails readiness when no device verifier is
	// configured; RequireRuntime fails readiness without a Voice Core
	// runtime factory. Production sets both.
	RequireConfigured  bool
	RequireRuntime     bool
	RequireSharedState bool

	// SessionCloseReportHook receives one bounded close report per accepted
	// device session at connection teardown. Production attaches the existing
	// cmd/mediaruntime deviceCloseReporter (MEDIA_EDGE_DEVICE_CLOSE_REPORT_*)
	// with one line in main.go:
	//
	//	server.DeviceWSS.SessionCloseReportHook = func(r mediaedge.DeviceSessionCloseReport) {
	//		_ = closeReporter.report(r.Reason, r.SessionID, r.DeviceID)
	//	}
	//
	// The report token/header are the independent Descartes-aligned
	// X-Memoria-Edge-Device-Close-Token secret, never the Control-to-Edge
	// runtime-control token. The hook is invoked with the real per-connection
	// reason and must never block connection teardown.
	SessionCloseReportHook func(DeviceSessionCloseReport)

	HelloTimeout      time.Duration
	ControlRatePerSec float64
	ControlBurst      float64
	AudioRatePerSec   float64
	AudioBurst        float64
	Backpressure      DeviceBackpressureConfig

	nextConnID atomic.Uint64
	draining   atomic.Bool
	closeOnce  sync.Once
	startedAt  time.Time

	mu             sync.Mutex
	conns          map[uint64]*DeviceConnection
	connsBySession map[string]*DeviceConnection

	metrics         deviceMetricCounters
	gauges          deviceMetricGauges
	rejectedReasons *deviceRejectedReasons
}

func NewDeviceWSServer(verifier DeviceJWTVerifier) *DeviceWSServer {
	return &DeviceWSServer{
		Verifier:          verifier,
		Tickets:           NewDeviceTicketStore(),
		Leases:            NewDeviceLeaseRegistry(),
		Acoustic:          NewDeviceAcousticRegistry(),
		MaxPendingFrames:  20,
		HelloTimeout:      DeviceHelloTimeout,
		ControlRatePerSec: deviceDefaultControlRate,
		ControlBurst:      deviceDefaultControlBurst,
		AudioRatePerSec:   deviceDefaultAudioRate,
		AudioBurst:        deviceDefaultAudioBurst,
		Backpressure:      DefaultDeviceBackpressureConfig(),
		startedAt:         time.Now(),
		conns:             make(map[uint64]*DeviceConnection),
		connsBySession:    make(map[string]*DeviceConnection),
		rejectedReasons:   newDeviceRejectedReasons(),
	}
}

func (s *DeviceWSServer) monotonicMS() uint64 {
	elapsed := time.Since(s.startedAt).Milliseconds()
	if elapsed < 1 {
		return 1
	}
	return uint64(elapsed)
}

func (s *DeviceWSServer) Ready() bool {
	if s.RequireConfigured && !s.Verifier.Configured() {
		return false
	}
	if s.RequireRuntime && s.RuntimeFactory == nil {
		return false
	}
	if s.RequireSharedState && (s.SharedState == nil || !s.SharedState.Ready()) {
		return false
	}
	return true
}

func (s *DeviceWSServer) registerConn(connection *DeviceConnection) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.conns[connection.connID] = connection
	s.connsBySession[connection.sessionID] = connection
}

func (s *DeviceWSServer) unregisterConn(connection *DeviceConnection) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if current, ok := s.conns[connection.connID]; ok && current == connection {
		delete(s.conns, connection.connID)
	}
	if current, ok := s.connsBySession[connection.sessionID]; ok && current == connection {
		delete(s.connsBySession, connection.sessionID)
	}
}

func (s *DeviceWSServer) connection(connID uint64) *DeviceConnection {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.conns[connID]
}

func (s *DeviceWSServer) connectionBySession(sessionID string) *DeviceConnection {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.connsBySession[sessionID]
}

// connectionForRequest resolves the live socket only when it still owns the
// complete Voice Core transport identity. Session ids are intentionally stable
// across reconnects, while stream_epoch is the takeover fence.
func (s *DeviceWSServer) connectionForRequest(request OpenSessionRequest) *DeviceConnection {
	s.mu.Lock()
	defer s.mu.Unlock()
	connection := s.connsBySession[request.SessionID]
	if connection == nil || connection.state.Load() != deviceConnAccepted ||
		uint64(connection.epoch) != request.StreamEpoch ||
		connection.deviceID != request.DeviceID ||
		connection.claims.Subject != request.AccountID ||
		connection.claims.SubjectID != request.SubjectID ||
		connection.claims.BindingID != request.BindingID ||
		connection.claims.BindingVersion != request.BindingVersion ||
		connection.claims.RuntimeProfileVersion != request.RuntimeProfileVersion {
		return nil
	}
	return connection
}

// DeviceRuntimeStatus is the live, server-authoritative result of the WSS
// hello/capability negotiation. It deliberately distinguishes the requested
// audio setting from the effective mode accepted by Edge.
type DeviceRuntimeStatus struct {
	DeviceID               string `json:"device_id"`
	Connected              bool   `json:"connected"`
	SessionID              string `json:"session_id,omitempty"`
	StreamEpoch            uint64 `json:"stream_epoch,omitempty"`
	ProtocolVersion        uint64 `json:"protocol_version,omitempty"`
	FirmwareVersion        string `json:"firmware_version,omitempty"`
	BoardProfile           string `json:"board_profile,omitempty"`
	RuntimeProfileVersion  uint64 `json:"runtime_profile_version,omitempty"`
	SettingsVersion        uint64 `json:"settings_version,omitempty"`
	AppliedProfileVersion  uint64 `json:"applied_profile_version,omitempty"`
	AppliedSettingsVersion uint64 `json:"applied_settings_version,omitempty"`
	ProfileAppliedAt       string `json:"profile_applied_at,omitempty"`
	AudioModeRequested     string `json:"audio_mode_requested,omitempty"`
	AudioModeEffective     string `json:"audio_mode_effective,omitempty"`
	AECProfileVersion      uint64 `json:"aec_profile_version,omitempty"`
	ConnectedAt            string `json:"connected_at,omitempty"`
}

// RuntimeStatus returns only an accepted live connection. A disconnected
// device is a successful authoritative observation with Connected=false, not
// an inferred fallback mode.
func (s *DeviceWSServer) RuntimeStatus(deviceID string) DeviceRuntimeStatus {
	status := DeviceRuntimeStatus{DeviceID: deviceID}
	s.mu.Lock()
	var connection *DeviceConnection
	for _, candidate := range s.conns {
		if candidate.deviceID == deviceID && candidate.state.Load() == deviceConnAccepted {
			connection = candidate
			break
		}
	}
	s.mu.Unlock()
	if connection == nil {
		return status
	}
	connection.stateMu.Lock()
	defer connection.stateMu.Unlock()
	if connection.state.Load() != deviceConnAccepted {
		return status
	}
	status.Connected = true
	status.SessionID = connection.sessionID
	status.StreamEpoch = uint64(connection.epoch)
	status.ProtocolVersion = connection.helloVersion
	status.FirmwareVersion = connection.firmwareVersion
	status.BoardProfile = connection.boardProfile
	status.RuntimeProfileVersion = connection.runtimeProfileVersion
	status.SettingsVersion = connection.claims.DeviceSettings.SettingsVersion
	status.AppliedProfileVersion = connection.appliedProfileVersion
	status.AppliedSettingsVersion = connection.appliedSettingsVersion
	status.AudioModeRequested = connection.claims.DeviceSettings.AudioMode
	status.AudioModeEffective = connection.audioMode
	if connection.audioMode == DeviceAudioModeFullDuplex && connection.acoustic != nil {
		status.AECProfileVersion = connection.acoustic.ProfileVersion
	}
	if !connection.connectedAt.IsZero() {
		status.ConnectedAt = connection.connectedAt.Format(time.RFC3339Nano)
	}
	if !connection.profileAppliedAt.IsZero() {
		status.ProfileAppliedAt = connection.profileAppliedAt.Format(time.RFC3339Nano)
	}
	return status
}

// InvalidateRuntimeProfile projects a Control-plane version change to the
// currently connected device. The firmware applies it only at the requested
// safe boundary; stale/replayed versions are idempotently ignored here.
func (s *DeviceWSServer) InvalidateRuntimeProfile(
	deviceID string,
	profileVersion uint64,
	applyAt string,
) (bool, error) {
	if profileVersion == 0 {
		return false, fmt.Errorf("profile_version must be positive")
	}
	switch applyAt {
	case "next_safe_point", "next_session", "immediate_fail_closed":
	default:
		return false, fmt.Errorf("apply_at is invalid")
	}
	s.mu.Lock()
	var connection *DeviceConnection
	for _, candidate := range s.conns {
		if candidate.deviceID == deviceID {
			connection = candidate
			break
		}
	}
	s.mu.Unlock()
	if connection == nil || connection.state.Load() != deviceConnAccepted {
		return false, nil
	}
	connection.stateMu.Lock()
	current := connection.runtimeProfileVersion
	if profileVersion <= current {
		connection.stateMu.Unlock()
		return true, nil
	}
	connection.runtimeProfileVersion = profileVersion
	connection.stateMu.Unlock()
	payload, err := marshalDeviceControl(deviceRuntimeProfileInvalidated{
		Type: "runtime_profile.invalidated", Version: 2,
		SessionID: connection.sessionID, StreamEpoch: uint64(connection.epoch),
		ControlSequence:   connection.nextServerSequence(),
		ServerMonotonicMS: s.monotonicMS(), ProfileVersion: profileVersion,
		ApplyAt: applyAt,
	})
	if err != nil {
		return false, err
	}
	connection.sendControl(deviceControlPriority("runtime_profile.invalidated"), payload)
	return true, nil
}

// CloseSession closes the live device connection for one session id. It is
// the Control-to-Edge socket-close seam used by the owner DELETE pipeline so
// a closed authority never leaves a live device socket behind. The close
// reason is bounded; the connection teardown then reports the close through
// SessionCloseReportHook with that reason. Returns false when no accepted
// connection owns the session (idempotent).
func (s *DeviceWSServer) CloseSession(sessionID string, reason string) bool {
	if !validSessionCloseReason(reason) {
		return false
	}
	connection := s.connectionBySession(sessionID)
	if connection == nil || connection.state.Load() != deviceConnAccepted {
		return false
	}
	connection.markCloseReason(reason)
	connection.closeWithCode(4001, "closed by control plane")
	connection.close()
	return true
}

// Close releases every device connection and its Voice Core runtime.
func (s *DeviceWSServer) Close() {
	s.closeOnce.Do(func() {
		s.draining.Store(true)
		s.Leases.BeginShutdown()
		s.mu.Lock()
		connections := make([]*DeviceConnection, 0, len(s.conns))
		for _, connection := range s.conns {
			connections = append(connections, connection)
		}
		s.mu.Unlock()
		for _, connection := range connections {
			// Per-connection reason: every accepted session reports its own
			// edge_shutdown close instead of one empty global report.
			connection.markCloseReason(SessionCloseReasonEdgeShutdown)
			connection.closeWithCode(1001, "media edge is shutting down")
			connection.close()
		}
		if s.SharedState != nil {
			_ = s.SharedState.Close()
		}
	})
}

// DeviceSessionCloseReport is the bounded Edge-to-Control close report for
// one accepted device session. The reason is restricted to the allowlist
// below so Control can persist it as the projection close_reason.
type DeviceSessionCloseReport struct {
	DeviceID    string `json:"device_id"`
	SessionID   string `json:"session_id"`
	StreamEpoch uint64 `json:"stream_epoch"`
	AccountID   string `json:"account_id"`
	Reason      string `json:"reason"`
	Connected   bool   `json:"connected"`
	ConnectedAt string `json:"connected_at,omitempty"`
	ClosedAt    string `json:"closed_at"`
}

const (
	SessionCloseReasonDeviceClose        = "device_close"
	SessionCloseReasonSuperseded         = "superseded"
	SessionCloseReasonNetwork            = "network"
	SessionCloseReasonEdgeShutdown       = "edge_shutdown"
	SessionCloseReasonProfileInvalidated = "runtime_profile_invalidated"
)

// validSessionCloseReason reports whether reason is in the bounded allowlist.
func validSessionCloseReason(reason string) bool {
	switch reason {
	case SessionCloseReasonDeviceClose,
		SessionCloseReasonSuperseded,
		SessionCloseReasonNetwork,
		SessionCloseReasonEdgeShutdown,
		SessionCloseReasonProfileInvalidated:
		return true
	default:
		return false
	}
}

// reportSessionClose delivers one close report for an accepted session to
// the configured hook. The hook is invoked synchronously so tests can assert
// ordering; production reporters must not block teardown.
func (s *DeviceWSServer) reportSessionClose(report DeviceSessionCloseReport) {
	if s.SessionCloseReportHook == nil {
		return
	}
	s.SessionCloseReportHook(report)
}

// WriteMetrics emits the device WSS metrics block.
func (s *DeviceWSServer) WriteMetrics(w interface{ Write([]byte) (int, error) }) {
	s.writeDeviceMetrics(w)
}
