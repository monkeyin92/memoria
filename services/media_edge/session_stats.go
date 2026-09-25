package mediaedge

import (
	"time"
)

func (s *Session) Stats() SessionStats {
	s.mu.Lock()
	defer s.mu.Unlock()
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
		IngressQueueAgeMS:         s.uplink.AgeMillis(now),
		EgressQueueAgeMS:          s.downlink.AgeMillis(now),
		GenerationCancelLatencyMS: s.generationCancelLatencyMS,
		PlayoutUnderruns:          s.playoutUnderruns,
		SessionDurationMS:         float64(durationEnd.Sub(s.createdAt)) / float64(time.Millisecond),
	}
	if s.lastDownlinkSourceEnd >= s.renderedSampleEnd {
		stats.PlayoutBufferMS = float64(s.lastDownlinkSourceEnd-s.renderedSampleEnd) * 1000 / 24_000
	}
	return stats
}

// RecordPlayback advances the rendered-sample watermark reported by the
// device/player for the current generation and counts playout underruns.
func (s *Session) RecordPlayback(sample uint64, fence Fence) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.generationActive && fence.Equal(s.Generation) && sample >= s.renderedSampleEnd {
		s.renderedSampleEnd = sample
		bufferEmpty := s.lastDownlinkSourceEnd > 0 && sample >= s.lastDownlinkSourceEnd
		generationComplete := s.hasGenerationFinal && sample >= s.generationFinalSampleEnd
		if bufferEmpty && !generationComplete && !s.playoutUnderrunActive {
			s.playoutUnderruns++
			s.playoutUnderrunActive = true
		}
	}
}
