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

// AudioFrame carries a bounded 16-bit PCM payload and absolute capture range.
// The HTTP reference edge uses base64; a native WebRTC adapter decodes its
// RTP/Opus input before handing PCM to this Voice Core seam.
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
	LossConcealed      bool   `json:"loss_concealed,omitempty"`
	Final              bool   `json:"final,omitempty"`
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
	if ^uint64(0)-f.CaptureStartSample < f.FrameSamples {
		return fmt.Errorf("frame sample range overflows")
	}
	payload, err := f.Payload()
	if err != nil {
		return err
	}
	if len(payload)%2 != 0 || uint64(len(payload)/2) != f.FrameSamples {
		return fmt.Errorf("pcm payload length does not match frame samples")
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

// StopGenerationRequest optionally carries the complete generation expected
// by the caller. With no fence fields, Edge/Core current state is authoritative.
type StopGenerationRequest struct {
	SessionID    string  `json:"session_id,omitempty"`
	StreamEpoch  uint64  `json:"stream_epoch,omitempty"`
	TurnID       *uint64 `json:"turn_id,omitempty"`
	GenerationID *uint64 `json:"generation_id,omitempty"`
	ToolEpoch    *uint64 `json:"tool_epoch,omitempty"`
	Reason       string  `json:"reason,omitempty"`
}

func (r StopGenerationRequest) ExpectedFence(sessionID string) (*Fence, error) {
	if r.SessionID != "" && r.SessionID != sessionID {
		return nil, fmt.Errorf("stop session does not match")
	}
	provided := 0
	if r.TurnID != nil {
		provided++
	}
	if r.GenerationID != nil {
		provided++
	}
	if r.ToolEpoch != nil {
		provided++
	}
	if provided == 0 {
		return nil, nil
	}
	if provided != 3 {
		return nil, fmt.Errorf("expected stop fence must include turn_id, generation_id and tool_epoch")
	}
	return &Fence{
		SessionID: sessionID, TurnID: *r.TurnID,
		GenerationID: *r.GenerationID, ToolEpoch: *r.ToolEpoch,
	}, nil
}

func writeJSON(w interface{ Write([]byte) (int, error) }, value any) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	_, err = w.Write(data)
	return err
}
