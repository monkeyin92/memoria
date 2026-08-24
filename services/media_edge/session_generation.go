package mediaedge

import (
	"context"
	"fmt"
	"time"
)

func (s *Session) AdvanceGeneration(fence Fence) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.State == SessionStopped {
		return fmt.Errorf("session is stopped")
	}
	if fence.SessionID != s.ID || fence.SessionEpoch < s.Generation.SessionEpoch ||
		(fence.SessionEpoch == s.Generation.SessionEpoch && fence.TurnID < s.Generation.TurnID) ||
		(fence.SessionEpoch == s.Generation.SessionEpoch && fence.TurnID == s.Generation.TurnID && fence.GenerationID < s.Generation.GenerationID) ||
		(fence.SessionEpoch == s.Generation.SessionEpoch && fence.TurnID == s.Generation.TurnID && fence.GenerationID == s.Generation.GenerationID && fence.ToolEpoch < s.Generation.ToolEpoch) {
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
	s.allowResumedDownlinkOrigin = false
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
	s.allowResumedDownlinkOrigin = false
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
		cancelled.SessionEpoch != s.Generation.SessionEpoch ||
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
	s.allowResumedDownlinkOrigin = false
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

// RestoreGeneration installs the Voice Core reconnect snapshot into a newly
// created transport Session. It is only legal before this Session has seen
// media. An active snapshot permits the first continued Core frame to retain
// its source sequence/sample origin; DeviceConnection rebases that source
// clock to 0/0 on the new WSS transport.
func (s *Session) RestoreGeneration(fence Fence, active bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.State != SessionActive || s.Generation.GenerationID != 0 ||
		s.hasDownlinkSeq || s.downlinkFrames != 0 || fence.SessionID != s.ID ||
		fence.TurnID == 0 || fence.GenerationID == 0 {
		return fmt.Errorf("generation reconnect snapshot is invalid")
	}
	s.Generation = fence
	s.generationActive = active
	s.hasDownlinkSeq = false
	s.allowResumedDownlinkOrigin = active
	s.lastDownlinkSeq = 0
	s.lastDownlinkSourceEnd = 0
	s.renderedSampleEnd = 0
	s.generationFinalSampleEnd = 0
	s.hasGenerationFinal = false
	s.playoutUnderrunActive = false
	if active {
		s.rotateDownlinkDeliveryLocked()
	} else {
		s.cancelDownlinkDeliveryLocked()
	}
	return nil
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
	pending := s.downlink.Len()
	for range pending {
		entry, _ := s.downlink.Pop()
		frame := entry.frame
		actual := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch, SessionEpoch: frame.SessionEpoch}
		if s.generationActive && actual.Equal(s.Generation) {
			_ = s.downlink.Push(frame, entry.queuedAt)
		} else {
			s.staleFrames++
		}
	}
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
	s.StreamEpoch++
	s.actor = NewLiveSessionActor(s.ID, s.StreamEpoch)
	s.rotateDownlinkDeliveryLocked()
	s.hasUplinkSequence = false
	s.hasDownlinkSeq = false
	s.allowResumedDownlinkOrigin = false
	s.lastDownlinkSeq = 0
	s.lastCaptureEnd = 0
	s.lastDownlinkSourceEnd = 0
	s.uplink.Clear()
	s.downlink.Clear()
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
	// Actor shutdown can wait for a goroutine and must never hold the session
	// state lock. The pointer swap above fences the old actor immediately.
	if oldActor != nil {
		oldActor.Close()
		snapshot := oldActor.Snapshot()
		s.mu.Lock()
		s.accumulateActorLocked(snapshot)
		s.mu.Unlock()
	}
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
	s.State = SessionStopped
	if s.stoppedAt.IsZero() {
		s.stoppedAt = time.Now()
	}
	s.cancelDownlinkDeliveryLocked()
	s.uplink.Clear()
	s.downlink.Clear()
	s.mu.Unlock()
	if actor != nil {
		actor.Close()
		snapshot := actor.Snapshot()
		s.mu.Lock()
		s.accumulateActorLocked(snapshot)
		s.mu.Unlock()
	}
}
