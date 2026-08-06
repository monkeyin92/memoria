package mediaedge

import (
	"time"
)

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
		IngressQueueAgeMS:         s.uplink.AgeMillis(now),
		EgressQueueAgeMS:          s.downlink.AgeMillis(now),
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
