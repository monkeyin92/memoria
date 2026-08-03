package mediaedge

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
	"strings"
)

const (
	ProtocolVersion = 1
	MaxPayloadBytes = 256 * 1024
)

// Fence is the only authority a downlink frame may use.  A sequence number
// orders frames within an epoch; it never replaces the generation gate.
type Fence struct {
	SessionID    string `json:"session_id"`
	TurnID       uint64 `json:"turn_id"`
	GenerationID uint64 `json:"generation_id"`
	ToolEpoch    uint64 `json:"tool_epoch"`
}

func (f Fence) Validate() error {
	if strings.TrimSpace(f.SessionID) == "" || len(f.SessionID) > 128 {
		return fmt.Errorf("session_id is required")
	}
	return nil
}

func (f Fence) Equal(other Fence) bool {
	return f.SessionID == other.SessionID && f.TurnID == other.TurnID &&
		f.GenerationID == other.GenerationID && f.ToolEpoch == other.ToolEpoch
}

// AudioFrame carries a bounded encoded PCM/Opus payload and absolute capture
// range.  The HTTP reference edge uses base64; a native WebRTC adapter can
// map RTP/Opus packets into this same shape without changing Voice Core.
type AudioFrame struct {
	SessionID          string `json:"session_id"`
	StreamEpoch        uint64 `json:"stream_epoch"`
	Sequence           uint64 `json:"sequence"`
	CaptureStartSample uint64 `json:"capture_start_sample"`
	FrameSamples       uint64 `json:"frame_samples"`
	TurnID             uint64 `json:"turn_id"`
	GenerationID       uint64 `json:"generation_id"`
	ToolEpoch          uint64 `json:"tool_epoch"`
	PayloadB64         string `json:"payload_b64"`
	Discontinuity      bool   `json:"discontinuity,omitempty"`
}

func (f AudioFrame) Payload() ([]byte, error) {
	if f.PayloadB64 == "" {
		return nil, fmt.Errorf("payload_b64 is required")
	}
	payload, err := base64.StdEncoding.DecodeString(f.PayloadB64)
	if err != nil {
		return nil, fmt.Errorf("invalid payload_b64: %w", err)
	}
	if len(payload) == 0 || len(payload) > MaxPayloadBytes {
		return nil, fmt.Errorf("payload exceeds bound")
	}
	return payload, nil
}

func (f AudioFrame) Validate(expectedSession string, expectedEpoch uint64) error {
	if f.SessionID != expectedSession || strings.TrimSpace(f.SessionID) == "" {
		return fmt.Errorf("frame session does not match")
	}
	if f.StreamEpoch != expectedEpoch || f.FrameSamples == 0 {
		return fmt.Errorf("frame epoch or sample range is invalid")
	}
	if _, err := f.Payload(); err != nil {
		return err
	}
	return nil
}

type OpenSessionRequest struct {
	SessionID   string `json:"session_id"`
	AccountID   string `json:"account_id"`
	DeviceID    string `json:"device_id"`
	ClientType  string `json:"client_type,omitempty"`
	StreamEpoch uint64 `json:"stream_epoch"`
}

func (r OpenSessionRequest) Validate() error {
	if strings.TrimSpace(r.SessionID) == "" || len(r.SessionID) > 128 {
		return fmt.Errorf("session_id is required")
	}
	if strings.TrimSpace(r.AccountID) == "" || strings.TrimSpace(r.DeviceID) == "" {
		return fmt.Errorf("account_id and device_id are required")
	}
	if r.ClientType != "" && r.ClientType != "h5" && r.ClientType != "device" {
		return fmt.Errorf("client_type must be h5 or device")
	}
	if r.StreamEpoch == 0 {
		return fmt.Errorf("stream_epoch must be positive")
	}
	return nil
}

type SessionResponse struct {
	SessionID    string `json:"session_id"`
	MediaRuntime string `json:"media_runtime"`
	StreamEpoch  uint64 `json:"stream_epoch"`
	Protocol     string `json:"protocol"`
}

func writeJSON(w interface{ Write([]byte) (int, error) }, value any) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	_, err = w.Write(data)
	return err
}
