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
	mu                        sync.Mutex
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
	uplink                    []AudioFrame
	uplinkQueuedAt            []time.Time
	downlink                  []AudioFrame
	downlinkQueuedAt          []time.Time
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
	if len(s.uplink) >= s.MaxPendingFrames {
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
	s.uplink = append(s.uplink, frame)
	s.uplinkQueuedAt = append(s.uplinkQueuedAt, time.Now())
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
	if len(s.uplink) == 0 || s.uplink[0].Sequence != sequence {
		return fmt.Errorf("uplink sequence is not pending")
	}
	s.uplink = s.uplink[1:]
	s.uplinkQueuedAt = s.uplinkQueuedAt[1:]
	return nil
}

func (s *Session) AcceptDownlink(frame AudioFrame) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.acceptDownlinkLocked(frame)
}

func (s *Session) acceptDownlinkLocked(frame AudioFrame) error {
	if s.State != SessionActive {
		s.staleFrames++
		return fmt.Errorf("session is not active")
	}
	if err := frame.Validate(s.ID, s.StreamEpoch); err != nil {
		s.staleFrames++
		return err
	}
	if s.hasDownlinkSeq && frame.Sequence <= s.lastDownlinkSeq {
		s.staleFrames++
		return fmt.Errorf("downlink sequence is stale")
	}
	if s.hasDownlinkSeq && frame.Sequence != s.lastDownlinkSeq+1 {
		s.staleFrames++
		return fmt.Errorf("downlink sequence has a gap")
	}
	if !s.hasDownlinkSeq && (frame.Sequence != 0 || frame.CaptureStartSample != 0) {
		s.staleFrames++
		return fmt.Errorf("first downlink frame must start at sequence and sample zero")
	}
	if s.hasDownlinkSeq && frame.CaptureStartSample != s.lastDownlinkSourceEnd {
		s.staleFrames++
		return fmt.Errorf("downlink sample range has a gap")
	}
	actual := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch}
	if !s.generationActive || !actual.Equal(s.Generation) {
		s.staleFrames++
		return ErrStaleDownlinkGeneration
	}
	if len(s.downlink) >= s.MaxPendingFrames {
		s.overflowFrames++
		return fmt.Errorf("downlink queue is full")
	}
	if ^uint64(0)-frame.CaptureStartSample < frame.FrameSamples {
		s.staleFrames++
		return fmt.Errorf("downlink sample range overflows")
	}
	s.lastDownlinkSeq = frame.Sequence
	s.hasDownlinkSeq = true
	frameEnd := frame.CaptureStartSample + frame.FrameSamples
	s.lastDownlinkSourceEnd = frameEnd
	if frameEnd > s.renderedSampleEnd {
		s.playoutUnderrunActive = false
	}
	if frame.Final {
		s.hasGenerationFinal = true
		s.generationFinalSampleEnd = frameEnd
	}
	s.downlink = append(s.downlink, frame)
	s.downlinkQueuedAt = append(s.downlinkQueuedAt, time.Now())
	s.downlinkFrames++
	s.mirrorLocked(LiveSessionEvent{
		Kind:        LiveEventAudioDownlink,
		StreamEpoch: s.StreamEpoch,
		Fence:       actual,
		Audio:       true,
	})
	return nil
}

// DeliverDownlink records a fenced frame before calling the external sender.
// The sender runs outside the Session mutex, so a slow encoder cannot delay a
// hard stop. CancelGeneration cancels its context before closing the gate;
// compliant terminators must not enqueue PCM once that context is done.
func (s *Session) DeliverDownlink(frame AudioFrame, sender DownlinkSender) error {
	s.mu.Lock()
	if err := s.acceptDownlinkLocked(frame); err != nil {
		s.mu.Unlock()
		return err
	}
	if sender == nil {
		s.mu.Unlock()
		return nil
	}
	deliveryCtx := s.downlinkDeliveryCtx
	s.mu.Unlock()
	if err := sender(deliveryCtx, frame); err != nil {
		if deliveryCtx.Err() != nil {
			return ErrStaleDownlinkGeneration
		}
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	actual := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch}
	if deliveryCtx.Err() != nil || !s.generationActive || !actual.Equal(s.Generation) {
		return ErrStaleDownlinkGeneration
	}
	return s.acknowledgeDownlinkLocked(frame.Sequence)
}

func (s *Session) AcknowledgeDownlink(sequence uint64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.acknowledgeDownlinkLocked(sequence)
}

func (s *Session) acknowledgeDownlinkLocked(sequence uint64) error {
	if len(s.downlink) == 0 || s.downlink[0].Sequence != sequence {
		return fmt.Errorf("downlink sequence is not pending")
	}
	s.downlink = s.downlink[1:]
	s.downlinkQueuedAt = s.downlinkQueuedAt[1:]
	return nil
}

// PopDownlink is used by an attached media terminator after it has encoded or
// rendered the bounded PCM frame.  It never changes the generation gate.
func (s *Session) PopDownlink() (AudioFrame, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for len(s.downlink) > 0 {
		frame := s.downlink[0]
		s.downlink = s.downlink[1:]
		s.downlinkQueuedAt = s.downlinkQueuedAt[1:]
		actual := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch}
		if s.generationActive && actual.Equal(s.Generation) {
			return frame, true
		}
		s.staleFrames++
	}
	return AudioFrame{}, false
}

func (s *Session) AdvanceGeneration(fence Fence) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.State == SessionStopped {
		return fmt.Errorf("session is stopped")
	}
	if fence.SessionID != s.ID || fence.TurnID < s.Generation.TurnID ||
		(fence.TurnID == s.Generation.TurnID && fence.GenerationID < s.Generation.GenerationID) ||
		(fence.TurnID == s.Generation.TurnID && fence.GenerationID == s.Generation.GenerationID && fence.ToolEpoch < s.Generation.ToolEpoch) {
		return fmt.Errorf("generation must advance monotonically")
	}
	if fence.Equal(s.Generation) {
		if !s.generationActive {
			return fmt.Errorf("cancelled generation cannot be reactivated")
		}
		return nil
	}
	s.Generation = fence
	s.generationActive = true
	s.rotateDownlinkDeliveryLocked()
	s.hasDownlinkSeq = false
	s.lastDownlinkSeq = 0
	s.lastDownlinkSourceEnd = 0
	s.renderedSampleEnd = 0
	s.generationFinalSampleEnd = 0
	s.hasGenerationFinal = false
	s.playoutUnderrunActive = false
	s.discardStaleDownlinkLocked()
	s.mirrorLocked(LiveSessionEvent{
		Kind:              LiveEventGenerationStart,
		StreamEpoch:       s.StreamEpoch,
		Fence:             fence,
		Authoritative:     s.shadowAuthoritativeLocked(),
		CompareGeneration: true,
	})
	// The gate remains authoritative even though stale buffered frames are
	// retired eagerly to release bounded queue capacity.
	return nil
}

// CancelGeneration atomically derives and installs the next generation from
// the current authoritative fence. The session and uplink remain active.
func (s *Session) CancelGeneration(eventID string, expected *Fence) (current, cancelled Fence, replayed bool, err error) {
	started := time.Now()
	s.mu.Lock()
	defer s.mu.Unlock()
	if eventID == "" {
		return Fence{}, Fence{}, false, fmt.Errorf("stop event id is required")
	}
	if result, ok := s.cancelResults[eventID]; ok {
		if expected != nil && !expected.Equal(result.current) {
			return Fence{}, Fence{}, false, fmt.Errorf("stop event id was reused for another generation")
		}
		return result.current, result.cancelled, true, nil
	}
	if s.State != SessionActive {
		return Fence{}, Fence{}, false, fmt.Errorf("session is not active")
	}
	if !s.generationActive || (expected != nil && !expected.Equal(s.Generation)) {
		return Fence{}, Fence{}, false, fmt.Errorf("generation cancel fence is stale")
	}
	if s.Generation.GenerationID == ^uint64(0) {
		return Fence{}, Fence{}, false, fmt.Errorf("generation_id cannot advance")
	}
	current = s.Generation
	cancelled = current
	cancelled.GenerationID++
	s.cancelDownlinkDeliveryLocked()
	s.Generation = cancelled
	s.generationActive = false
	s.hasDownlinkSeq = false
	s.lastDownlinkSeq = 0
	s.lastDownlinkSourceEnd = 0
	s.renderedSampleEnd = 0
	s.generationFinalSampleEnd = 0
	s.hasGenerationFinal = false
	s.playoutUnderrunActive = false
	if len(s.cancelResultOrder) == maxCancelResults {
		delete(s.cancelResults, s.cancelResultOrder[0])
		s.cancelResultOrder = s.cancelResultOrder[1:]
	}
	s.cancelResults[eventID] = cancelResult{current: current, cancelled: cancelled}
	s.cancelResultOrder = append(s.cancelResultOrder, eventID)
	s.discardStaleDownlinkLocked()
	s.mirrorLocked(LiveSessionEvent{
		Kind:              LiveEventGenerationCancel,
		StreamEpoch:       s.StreamEpoch,
		Fence:             cancelled,
		Authoritative:     s.shadowAuthoritativeLocked(),
		CompareGeneration: true,
	})
	s.generationCancelLatencyMS = float64(time.Since(started)) / float64(time.Millisecond)
	return current, cancelled, false, nil
}

func (s *Session) cancelledGeneration(eventID string) (current, cancelled Fence, ok bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	result, ok := s.cancelResults[eventID]
	if eventID == "" || !ok {
		return Fence{}, Fence{}, false
	}
	return result.current, result.cancelled, true
}

// ApplyCancelledGeneration consumes the authoritative cancellation emitted by
// Voice Core. It is idempotent with a cancellation already initiated locally.
func (s *Session) ApplyCancelledGeneration(cancelled Fence) error {
	started := time.Now()
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.generationActive && s.Generation.Equal(cancelled) {
		return nil
	}
	if !s.generationActive || cancelled.SessionID != s.ID ||
		cancelled.TurnID != s.Generation.TurnID ||
		cancelled.ToolEpoch != s.Generation.ToolEpoch ||
		s.Generation.GenerationID == ^uint64(0) ||
		cancelled.GenerationID != s.Generation.GenerationID+1 {
		return fmt.Errorf("cancelled generation does not match current fence")
	}
	s.Generation = cancelled
	s.generationActive = false
	s.cancelDownlinkDeliveryLocked()
	s.hasDownlinkSeq = false
	s.lastDownlinkSeq = 0
	s.lastDownlinkSourceEnd = 0
	s.renderedSampleEnd = 0
	s.generationFinalSampleEnd = 0
	s.hasGenerationFinal = false
	s.playoutUnderrunActive = false
	s.discardStaleDownlinkLocked()
	s.mirrorLocked(LiveSessionEvent{
		Kind:              LiveEventGenerationCancel,
		StreamEpoch:       s.StreamEpoch,
		Fence:             cancelled,
		Authoritative:     s.shadowAuthoritativeLocked(),
		CompareGeneration: true,
	})
	s.generationCancelLatencyMS = float64(time.Since(started)) / float64(time.Millisecond)
	return nil
}

func (s *Session) GenerationSnapshot() (Fence, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.Generation, s.generationActive
}

func (s *Session) withActiveGeneration(fence Fence, action func() error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.generationActive || !s.Generation.Equal(fence) {
		return fmt.Errorf("generation fence is stale")
	}
	return action()
}

// ApplyFloorEffect installs one Python-authoritative Floor snapshot. It is
// intentionally independent of generation activity: a cancelled generation
// can still authoritatively say that the user holds the floor.
func (s *Session) ApplyFloorEffect(
	fence Fence,
	floor ShadowFloorState,
	floorEpoch uint64,
	source string,
) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.State != SessionActive || !s.Generation.Equal(fence) {
		return fmt.Errorf("floor effect fence is stale")
	}
	if floorEpoch == 0 || floorEpoch <= s.floorEpoch {
		return fmt.Errorf("floor effect epoch is stale")
	}
	s.floorState = floor
	s.floorEpoch = floorEpoch
	authoritative := s.shadowAuthoritativeLocked()
	authoritative.Generation = fence
	authoritative.Floor = floor
	authoritative.LastDecision = source
	s.mirrorLocked(LiveSessionEvent{
		Kind:              LiveEventAuthoritySnapshot,
		StreamEpoch:       s.StreamEpoch,
		Authoritative:     authoritative,
		CompareGeneration: true,
		Scenario:          "floor_effect",
		ContractVersion:   shadowA6AContractVersion,
	})
	return nil
}

func (s *Session) discardStaleDownlinkLocked() {
	kept := s.downlink[:0]
	keptQueuedAt := s.downlinkQueuedAt[:0]
	for index, frame := range s.downlink {
		actual := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch}
		if s.generationActive && actual.Equal(s.Generation) {
			kept = append(kept, frame)
			keptQueuedAt = append(keptQueuedAt, s.downlinkQueuedAt[index])
		} else {
			s.staleFrames++
		}
	}
	s.downlink = kept
	s.downlinkQueuedAt = keptQueuedAt
}

func (s *Session) cancelDownlinkDeliveryLocked() {
	if s.cancelDownlinkDelivery != nil {
		s.cancelDownlinkDelivery()
	}
}

func (s *Session) rotateDownlinkDeliveryLocked() {
	s.cancelDownlinkDeliveryLocked()
	s.downlinkDeliveryCtx, s.cancelDownlinkDelivery = context.WithCancel(context.Background())
}

func (s *Session) Reconnect() (uint64, error) {
	s.mu.Lock()
	if s.State == SessionStopped || s.State == SessionDraining {
		s.mu.Unlock()
		return 0, fmt.Errorf("session cannot reconnect while %s", s.State)
	}
	oldActor := s.actor
	if oldActor != nil {
		oldActor.Close()
		s.accumulateActorLocked(oldActor.Snapshot())
	}
	s.StreamEpoch++
	s.actor = NewLiveSessionActor(s.ID, s.StreamEpoch)
	s.rotateDownlinkDeliveryLocked()
	s.hasUplinkSequence = false
	s.hasDownlinkSeq = false
	s.lastDownlinkSeq = 0
	s.lastCaptureEnd = 0
	s.lastDownlinkSourceEnd = 0
	s.uplink = nil
	s.uplinkQueuedAt = nil
	s.downlink = nil
	s.downlinkQueuedAt = nil
	s.renderedSampleEnd = 0
	s.generationFinalSampleEnd = 0
	s.hasGenerationFinal = false
	s.playoutUnderrunActive = false
	s.floorState = ShadowFloorSilence
	s.floorEpoch = 0
	// Stop idempotency is scoped to one transport epoch. Reusing an event id
	// after reconnect must not replay a cancellation fence from the old clock.
	s.cancelResults = make(map[string]cancelResult)
	s.cancelResultOrder = nil
	epoch := s.StreamEpoch
	s.mu.Unlock()
	return epoch, nil
}

func (s *Session) Drain() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.State == SessionActive {
		s.State = SessionDraining
	}
}

func (s *Session) Stop() {
	s.mu.Lock()
	actor := s.actor
	s.actor = nil
	if actor != nil {
		actor.Close()
		s.accumulateActorLocked(actor.Snapshot())
	}
	s.State = SessionStopped
	if s.stoppedAt.IsZero() {
		s.stoppedAt = time.Now()
	}
	s.cancelDownlinkDeliveryLocked()
	s.uplink = nil
	s.uplinkQueuedAt = nil
	s.downlink = nil
	s.downlinkQueuedAt = nil
	s.mu.Unlock()
}

func (s *Session) Stats() SessionStats {
	s.mu.Lock()
	now := time.Now()
	durationEnd := now
	if !s.stoppedAt.IsZero() {
		durationEnd = s.stoppedAt
	}
	stats := SessionStats{
		State: s.State, StreamEpoch: s.StreamEpoch,
		LastUplinkSequence: s.lastUplinkSequence, LastDownlinkSeq: s.lastDownlinkSeq,
		UplinkFrames: s.uplinkFrames, DownlinkFrames: s.downlinkFrames,
		StaleFrames: s.staleFrames, OverflowFrames: s.overflowFrames,
		ActorMailboxAgeMS:         s.retiredActorStats.MaxMailboxAgeMillis,
		ActorDroppedEvents:        s.retiredActorStats.DroppedEvents,
		ActorDeadlineMisses:       s.retiredActorStats.FrameDeadlineMisses,
		ActorAudioFrames:          s.retiredActorStats.ProcessedAudioFrames,
		IngressQueueAgeMS:         queueAgeMillis(s.uplinkQueuedAt, now),
		EgressQueueAgeMS:          queueAgeMillis(s.downlinkQueuedAt, now),
		FloorDecisionLatencyMS:    s.retiredActorStats.FloorDecisionLatencyMillis,
		GenerationCancelLatencyMS: s.generationCancelLatencyMS,
		PlayoutUnderruns:          s.playoutUnderruns,
		SessionDurationMS:         float64(durationEnd.Sub(s.createdAt)) / float64(time.Millisecond),
		ShadowMismatches:          s.retiredActorStats.ShadowMismatchTotal,
		ShadowMismatchCounts: append(
			[]ShadowMismatchCount(nil),
			s.retiredActorStats.ShadowMismatchCounts...,
		),
	}
	actor := s.actor
	if actor != nil {
		shadow := actor.Snapshot()
		stats.ActorMailboxDepth = shadow.MailboxDepth
		if shadow.MaxMailboxAgeMillis > stats.ActorMailboxAgeMS {
			stats.ActorMailboxAgeMS = shadow.MaxMailboxAgeMillis
		}
		if shadow.FloorDecisionLatencyMillis > stats.FloorDecisionLatencyMS {
			stats.FloorDecisionLatencyMS = shadow.FloorDecisionLatencyMillis
		}
		stats.ActorDroppedEvents += shadow.DroppedEvents
		stats.ActorDeadlineMisses += shadow.FrameDeadlineMisses
		stats.ActorAudioFrames += shadow.ProcessedAudioFrames
		stats.ShadowMismatches += shadow.ShadowMismatchTotal
		stats.ShadowMismatchCounts = mergeShadowMismatchCounts(
			stats.ShadowMismatchCounts,
			shadow.ShadowMismatchCounts,
		)
	}
	if s.lastDownlinkSourceEnd >= s.renderedSampleEnd {
		stats.PlayoutBufferMS = float64(s.lastDownlinkSourceEnd-s.renderedSampleEnd) * 1000 / 24_000
	}
	s.mu.Unlock()
	return stats
}

func (s *Session) accumulateActorLocked(snapshot LiveSessionSnapshot) {
	s.retiredActorStats.DroppedEvents += snapshot.DroppedEvents
	s.retiredActorStats.FrameDeadlineMisses += snapshot.FrameDeadlineMisses
	s.retiredActorStats.ProcessedAudioFrames += snapshot.ProcessedAudioFrames
	s.retiredActorStats.ShadowMismatchTotal += snapshot.ShadowMismatchTotal
	if snapshot.MaxMailboxAgeMillis > s.retiredActorStats.MaxMailboxAgeMillis {
		s.retiredActorStats.MaxMailboxAgeMillis = snapshot.MaxMailboxAgeMillis
	}
	if snapshot.FloorDecisionLatencyMillis > s.retiredActorStats.FloorDecisionLatencyMillis {
		s.retiredActorStats.FloorDecisionLatencyMillis = snapshot.FloorDecisionLatencyMillis
	}
	s.retiredActorStats.ShadowMismatchCounts = mergeShadowMismatchCounts(
		s.retiredActorStats.ShadowMismatchCounts,
		snapshot.ShadowMismatchCounts,
	)
}

func (s *Session) ShadowSnapshot() (LiveSessionSnapshot, bool) {
	s.mu.Lock()
	actor := s.actor
	s.mu.Unlock()
	if actor == nil {
		return LiveSessionSnapshot{}, false
	}
	return actor.Snapshot(), true
}

func (s *Session) MirrorVAD(start bool, sample uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	kind := LiveEventVADEnd
	if start {
		kind = LiveEventVADStart
	}
	s.mirrorLocked(LiveSessionEvent{
		Kind:           kind,
		StreamEpoch:    s.StreamEpoch,
		SamplePosition: sample,
	})
}

func (s *Session) MirrorPlayback(sample uint64, fence Fence) {
	s.mu.Lock()
	defer s.mu.Unlock()
	authoritative := s.shadowAuthoritativeLocked()
	authoritative.PlayoutSample = sample
	if s.generationActive && fence.Equal(s.Generation) && sample >= s.renderedSampleEnd {
		s.renderedSampleEnd = sample
		bufferEmpty := s.lastDownlinkSourceEnd > 0 && sample >= s.lastDownlinkSourceEnd
		generationComplete := s.hasGenerationFinal && sample >= s.generationFinalSampleEnd
		if bufferEmpty && !generationComplete && !s.playoutUnderrunActive {
			s.playoutUnderruns++
			s.playoutUnderrunActive = true
		}
	}
	s.mirrorLocked(LiveSessionEvent{
		Kind:              LiveEventPlaybackProgress,
		StreamEpoch:       s.StreamEpoch,
		Fence:             fence,
		SamplePosition:    sample,
		Authoritative:     authoritative,
		CompareGeneration: true,
		ComparePlayout:    true,
	})
}

func (s *Session) ObserveShadow(event LiveSessionEvent) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	event.StreamEpoch = s.StreamEpoch
	if s.actor == nil {
		return ErrActorClosed
	}
	return s.actor.TrySubmit(event)
}

func queueAgeMillis(enqueued []time.Time, now time.Time) float64 {
	if len(enqueued) == 0 {
		return 0
	}
	return float64(now.Sub(enqueued[0])) / float64(time.Millisecond)
}

func (s *Session) MirrorPythonInteraction(
	floor ShadowFloorState,
	decision string,
	scenario string,
	fence Fence,
) {
	s.mu.Lock()
	defer s.mu.Unlock()
	authoritative := s.shadowAuthoritativeLocked()
	authoritative.Generation = fence
	authoritative.Floor = floor
	authoritative.LastDecision = decision
	s.mirrorLocked(LiveSessionEvent{
		Kind:              LiveEventAuthoritySnapshot,
		StreamEpoch:       s.StreamEpoch,
		Authoritative:     authoritative,
		CompareGeneration: true,
		Scenario:          scenario,
		ContractVersion:   shadowContractVersion,
	})
}

func (s *Session) mirrorLocked(event LiveSessionEvent) {
	if s.actor != nil {
		_ = s.actor.TrySubmit(event)
	}
}

func (s *Session) shadowAuthoritativeLocked() *LiveSessionSnapshot {
	return &LiveSessionSnapshot{
		SessionID:        s.ID,
		StreamEpoch:      s.StreamEpoch,
		Generation:       s.Generation,
		GenerationActive: s.generationActive,
	}
}

type Directory struct {
	mu       sync.RWMutex
	sessions map[string]*Session
}

func NewDirectory() *Directory { return &Directory{sessions: make(map[string]*Session)} }

func (d *Directory) Put(session *Session) error {
	if session == nil || session.ID == "" {
		return fmt.Errorf("session is required")
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if _, exists := d.sessions[session.ID]; exists {
		return fmt.Errorf("session already exists")
	}
	d.sessions[session.ID] = session
	return nil
}

// ReplaceNewer atomically installs a new transport epoch and returns the old
// session for cleanup. Equal/older epochs are rejected so a retried WHIP offer
// cannot displace an already-connected peer.
func (d *Directory) ReplaceNewer(session *Session) (*Session, error) {
	if session == nil || session.ID == "" {
		return nil, fmt.Errorf("session is required")
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	old := d.sessions[session.ID]
	if old != nil && session.StreamEpoch <= old.Epoch() {
		return nil, fmt.Errorf("stream epoch must advance")
	}
	d.sessions[session.ID] = session
	return old, nil
}

func (d *Directory) Get(id string) (*Session, bool) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	session, ok := d.sessions[id]
	return session, ok
}

func (d *Directory) Snapshots() []SessionStats {
	d.mu.RLock()
	sessions := make([]*Session, 0, len(d.sessions))
	for _, session := range d.sessions {
		sessions = append(sessions, session)
	}
	d.mu.RUnlock()
	stats := make([]SessionStats, 0, len(sessions))
	for _, session := range sessions {
		stats = append(stats, session.Stats())
	}
	return stats
}

func (d *Directory) Delete(id string) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	if _, ok := d.sessions[id]; !ok {
		return false
	}
	delete(d.sessions, id)
	return true
}

// CloseAll releases every edge-owned session during process shutdown. The
// control-plane TTL still uses CloseSession for individual expiry; this sweep
// prevents a graceful server stop from retaining the directory map.
func (d *Directory) CloseAll() {
	for _, session := range d.TakeAll() {
		session.Stop()
	}
}

func (d *Directory) TakeAll() []*Session {
	d.mu.Lock()
	sessions := make([]*Session, 0, len(d.sessions))
	for id, session := range d.sessions {
		sessions = append(sessions, session)
		delete(d.sessions, id)
	}
	d.mu.Unlock()
	return sessions
}
