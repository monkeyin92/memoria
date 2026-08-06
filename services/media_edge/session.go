package mediaedge

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"
)

var ErrStaleDownlinkGeneration = errors.New("downlink generation is stale")

const maxCancelResults = 64

type cancelResult struct {
	current   Fence
	cancelled Fence
}

type SessionState string

const (
	SessionActive   SessionState = "active"
	SessionDraining SessionState = "draining"
	SessionStopped  SessionState = "stopped"
)

type SessionStats struct {
	State                     SessionState          `json:"state"`
	StreamEpoch               uint64                `json:"stream_epoch"`
	LastUplinkSequence        uint64                `json:"last_uplink_sequence"`
	LastDownlinkSeq           uint64                `json:"last_downlink_sequence"`
	UplinkFrames              uint64                `json:"uplink_frames"`
	DownlinkFrames            uint64                `json:"downlink_frames"`
	StaleFrames               uint64                `json:"stale_frames"`
	OverflowFrames            uint64                `json:"overflow_frames"`
	ActorMailboxDepth         int                   `json:"actor_mailbox_depth"`
	ActorMailboxAgeMS         float64               `json:"actor_mailbox_age_ms"`
	ActorDroppedEvents        uint64                `json:"actor_dropped_events"`
	ActorDeadlineMisses       uint64                `json:"audio_frame_deadline_miss_total"`
	ActorAudioFrames          uint64                `json:"actor_audio_frames"`
	IngressQueueAgeMS         float64               `json:"ingress_queue_age_ms"`
	EgressQueueAgeMS          float64               `json:"egress_queue_age_ms"`
	FloorDecisionLatencyMS    float64               `json:"floor_decision_latency_ms"`
	GenerationCancelLatencyMS float64               `json:"generation_cancel_latency_ms"`
	PlayoutBufferMS           float64               `json:"playout_buffer_ms"`
	PlayoutUnderruns          uint64                `json:"playout_underrun_total"`
	SessionDurationMS         float64               `json:"session_duration_ms"`
	ShadowMismatches          uint64                `json:"shadow_decision_mismatch_total"`
	ShadowMismatchCounts      []ShadowMismatchCount `json:"shadow_decision_mismatch_counts,omitempty"`
}

type Session struct {
	mu sync.Mutex
	// lifecycleMu serializes reconnect/close transitions for this session only,
	// so a slow provider handshake cannot block unrelated sessions.
	lifecycleMu               sync.Mutex
	ID                        string
	AccountID                 string
	DeviceID                  string
	ClientType                string
	StreamEpoch               uint64
	Generation                Fence
	generationActive          bool
	floorState                ShadowFloorState
	floorEpoch                uint64
	State                     SessionState
	MaxPendingFrames          int
	uplink                    audioRing
	downlink                  audioRing
	lastUplinkSequence        uint64
	lastDownlinkSeq           uint64
	hasUplinkSequence         bool
	hasDownlinkSeq            bool
	lastCaptureEnd            uint64
	uplinkFrames              uint64
	downlinkFrames            uint64
	staleFrames               uint64
	overflowFrames            uint64
	cancelResults             map[string]cancelResult
	cancelResultOrder         []string
	lastDownlinkSourceEnd     uint64
	renderedSampleEnd         uint64
	generationFinalSampleEnd  uint64
	hasGenerationFinal        bool
	playoutUnderrunActive     bool
	playoutUnderruns          uint64
	generationCancelLatencyMS float64
	createdAt                 time.Time
	stoppedAt                 time.Time
	downlinkDeliveryCtx       context.Context
	cancelDownlinkDelivery    context.CancelFunc
	actor                     *LiveSessionActor
	retiredActorStats         LiveSessionSnapshot
}

// Epoch returns the authoritative stream epoch without exposing an unlocked
// read to HTTP or bridge goroutines.
func (s *Session) Epoch() uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.StreamEpoch
}

func (s *Session) IdentitySnapshot() (sessionID, accountID, deviceID string, streamEpoch uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ID, s.AccountID, s.DeviceID, s.StreamEpoch
}

func (s *Session) ClientTypeValue() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ClientType
}

func NewSession(request OpenSessionRequest, maxPendingFrames int) (*Session, error) {
	if err := request.Validate(); err != nil {
		return nil, err
	}
	if maxPendingFrames <= 0 {
		return nil, fmt.Errorf("max_pending_frames must be positive")
	}
	deliveryCtx, cancelDelivery := context.WithCancel(context.Background())
	session := &Session{
		ID:                     request.SessionID,
		AccountID:              request.AccountID,
		DeviceID:               request.DeviceID,
		ClientType:             defaultClientType(request.ClientType),
		StreamEpoch:            request.StreamEpoch,
		Generation:             Fence{SessionID: request.SessionID},
		generationActive:       true,
		floorState:             ShadowFloorSilence,
		State:                  SessionActive,
		MaxPendingFrames:       maxPendingFrames,
		uplink:                 newAudioRing(maxPendingFrames),
		downlink:               newAudioRing(maxPendingFrames),
		cancelResults:          make(map[string]cancelResult),
		downlinkDeliveryCtx:    deliveryCtx,
		cancelDownlinkDelivery: cancelDelivery,
		createdAt:              time.Now(),
	}
	session.actor = NewLiveSessionActor(session.ID, session.StreamEpoch)
	return session, nil
}

func defaultClientType(value string) string {
	if value == "" {
		return "h5"
	}
	return value
}

func (s *Session) AcceptUplink(frame AudioFrame) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.State != SessionActive {
		return fmt.Errorf("session is not active")
	}
	if err := frame.Validate(s.ID, s.StreamEpoch); err != nil {
		return err
	}
	if s.hasUplinkSequence && frame.Sequence <= s.lastUplinkSequence {
		s.staleFrames++
		return fmt.Errorf("uplink sequence is stale")
	}
	if s.hasUplinkSequence && frame.Sequence != s.lastUplinkSequence+1 {
		s.staleFrames++
		return fmt.Errorf("uplink sequence has a gap")
	}
	if frame.Discontinuity {
		// A discontinuity is a transport fence, not a queue-clearing hint.  The
		// caller must reconnect and obtain a new stream_epoch before samples
		// can be accepted again.
		s.staleFrames++
		return fmt.Errorf("discontinuity requires a new stream epoch")
	}
	if frame.CaptureStartSample < s.lastCaptureEnd {
		s.staleFrames++
		return fmt.Errorf("capture sample range moved backwards")
	}
	if s.hasUplinkSequence && frame.CaptureStartSample > s.lastCaptureEnd {
		s.staleFrames++
		return fmt.Errorf("capture sample range has a gap")
	}
	if s.uplink.Len() >= s.MaxPendingFrames {
		s.overflowFrames++
		return fmt.Errorf("uplink queue is full")
	}
	if ^uint64(0)-frame.CaptureStartSample < frame.FrameSamples {
		s.staleFrames++
		return fmt.Errorf("capture sample range overflows")
	}
	s.lastUplinkSequence = frame.Sequence
	s.hasUplinkSequence = true
	s.lastCaptureEnd = frame.CaptureStartSample + frame.FrameSamples
	if !s.uplink.Push(frame, time.Now()) {
		s.overflowFrames++
		return fmt.Errorf("uplink queue is full")
	}
	s.uplinkFrames++
	s.mirrorLocked(LiveSessionEvent{
		Kind:        LiveEventAudioUplink,
		StreamEpoch: s.StreamEpoch,
		Audio:       true,
	})
	return nil
}

// AcknowledgeUplink removes one frame after an attached Voice Core bridge has
// accepted it.  The HTTP reference path intentionally keeps frames queued for
// inspection; a live bridge must retire them after the gRPC send succeeds so
// the bounded queue represents actual backpressure rather than duplicate
// buffering.
func (s *Session) AcknowledgeUplink(sequence uint64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	entry, ok := s.uplink.Front()
	if !ok || entry.frame.Sequence != sequence {
		return fmt.Errorf("uplink sequence is not pending")
	}
	_, _ = s.uplink.Pop()
	return nil
}
