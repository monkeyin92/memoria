package mediaedge

import (
	"fmt"
	"net/http"
	"runtime"
	"sort"
	"strconv"
)

func (s *Server) metrics(w http.ResponseWriter, _ *http.Request) {
	s.metricsMu.Lock()
	defer s.metricsMu.Unlock()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	if s.DeviceWSS != nil {
		s.DeviceWSS.writeDeviceMetrics(w)
	}
	_, _ = fmt.Fprintf(w, "media_edge_requests_total %d\n", s.Requests.Load())
	_, _ = fmt.Fprintf(w, "media_edge_rejected_frames_total %d\n", s.RejectedFrames.Load())
	_, _ = fmt.Fprintf(w, "media_edge_opus_fec_frames_total %d\n", s.OpusFECFrames.Load())
	_, _ = fmt.Fprintf(w, "media_edge_opus_plc_frames_total %d\n", s.OpusPLCFrames.Load())
	_, _ = fmt.Fprintf(w, "media_edge_opus_silence_concealment_frames_total %d\n", s.OpusSilenceFrames.Load())
	var memory runtime.MemStats
	runtime.ReadMemStats(&memory)
	_, _ = fmt.Fprintf(w, "media_edge_heap_alloc_bytes %d\n", memory.HeapAlloc)
	_, _ = fmt.Fprintf(w, "media_edge_mallocs_total %d\n", memory.Mallocs)
	_, _ = fmt.Fprintf(w, "media_edge_gc_pause_total_seconds %g\n", float64(memory.PauseTotalNs)/1e9)
	stats := s.Directory.Snapshots()
	deadlineMisses := s.retiredMetrics.ActorDeadlineMisses
	audioFrames := s.retiredMetrics.ActorAudioFrames
	maxMailboxAge := s.retiredMetrics.ActorMailboxAgeMS
	maxIngressQueueAge := 0.0
	maxEgressQueueAge := 0.0
	maxFloorDecisionLatency := s.retiredMetrics.FloorDecisionLatencyMS
	maxGenerationCancelLatency := s.retiredMetrics.GenerationCancelLatencyMS
	maxPlayoutBuffer := 0.0
	playoutUnderruns := s.retiredMetrics.PlayoutUnderruns
	maxSessionDuration := s.retiredMetrics.SessionDurationMS
	shadowMismatches := make(map[string]ShadowMismatchCount)
	for _, count := range s.retiredMetrics.ShadowMismatchCounts {
		shadowMismatches[count.Scenario+"\x00"+count.ContractVersion] = count
	}
	for _, session := range stats {
		deadlineMisses += session.ActorDeadlineMisses
		audioFrames += session.ActorAudioFrames
		for _, count := range session.ShadowMismatchCounts {
			key := count.Scenario + "\x00" + count.ContractVersion
			total := shadowMismatches[key]
			total.Scenario = count.Scenario
			total.ContractVersion = count.ContractVersion
			total.Count += count.Count
			shadowMismatches[key] = total
		}
		if session.ActorMailboxAgeMS > maxMailboxAge {
			maxMailboxAge = session.ActorMailboxAgeMS
		}
		if session.IngressQueueAgeMS > maxIngressQueueAge {
			maxIngressQueueAge = session.IngressQueueAgeMS
		}
		if session.EgressQueueAgeMS > maxEgressQueueAge {
			maxEgressQueueAge = session.EgressQueueAgeMS
		}
		if session.FloorDecisionLatencyMS > maxFloorDecisionLatency {
			maxFloorDecisionLatency = session.FloorDecisionLatencyMS
		}
		if session.GenerationCancelLatencyMS > maxGenerationCancelLatency {
			maxGenerationCancelLatency = session.GenerationCancelLatencyMS
		}
		if session.PlayoutBufferMS > maxPlayoutBuffer {
			maxPlayoutBuffer = session.PlayoutBufferMS
		}
		playoutUnderruns += session.PlayoutUnderruns
		if session.SessionDurationMS > maxSessionDuration {
			maxSessionDuration = session.SessionDurationMS
		}
	}
	_, _ = fmt.Fprintf(w, "active_media_sessions %d\n", len(stats))
	_, _ = fmt.Fprintf(w, "audio_frame_deadline_miss_total %d\n", deadlineMisses)
	deadlineRatio := 0.0
	if audioFrames > 0 {
		deadlineRatio = float64(deadlineMisses) / float64(audioFrames)
	}
	_, _ = fmt.Fprintf(w, "audio_frame_deadline_miss_ratio %g\n", deadlineRatio)
	_, _ = fmt.Fprintf(w, "actor_mailbox_age_ms %g\n", maxMailboxAge)
	_, _ = fmt.Fprintf(w, "ingress_queue_age_ms %g\n", maxIngressQueueAge)
	_, _ = fmt.Fprintf(w, "egress_queue_age_ms %g\n", maxEgressQueueAge)
	_, _ = fmt.Fprintf(w, "floor_decision_latency_ms %g\n", maxFloorDecisionLatency)
	_, _ = fmt.Fprintf(w, "generation_cancel_latency_ms %g\n", maxGenerationCancelLatency)
	_, _ = fmt.Fprintf(w, "playout_buffer_ms %g\n", maxPlayoutBuffer)
	_, _ = fmt.Fprintf(w, "playout_underrun_total %d\n", playoutUnderruns)
	_, _ = fmt.Fprintf(w, "session_duration_ms %g\n", maxSessionDuration)
	_, _ = fmt.Fprintln(w, "# TYPE shadow_decision_mismatch_total counter")
	keys := make([]string, 0, len(shadowMismatches))
	for key := range shadowMismatches {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		count := shadowMismatches[key]
		_, _ = fmt.Fprintf(
			w,
			"shadow_decision_mismatch_total{scenario=%s,contract_version=%s} %d\n",
			strconv.Quote(count.Scenario),
			strconv.Quote(count.ContractVersion),
			count.Count,
		)
	}
}

func (s *Server) archiveSessionMetricsLocked(stats SessionStats) {
	s.retiredMetrics.ActorDeadlineMisses += stats.ActorDeadlineMisses
	s.retiredMetrics.ActorAudioFrames += stats.ActorAudioFrames
	if stats.ActorMailboxAgeMS > s.retiredMetrics.ActorMailboxAgeMS {
		s.retiredMetrics.ActorMailboxAgeMS = stats.ActorMailboxAgeMS
	}
	if stats.FloorDecisionLatencyMS > s.retiredMetrics.FloorDecisionLatencyMS {
		s.retiredMetrics.FloorDecisionLatencyMS = stats.FloorDecisionLatencyMS
	}
	if stats.GenerationCancelLatencyMS > s.retiredMetrics.GenerationCancelLatencyMS {
		s.retiredMetrics.GenerationCancelLatencyMS = stats.GenerationCancelLatencyMS
	}
	if stats.SessionDurationMS > s.retiredMetrics.SessionDurationMS {
		s.retiredMetrics.SessionDurationMS = stats.SessionDurationMS
	}
	s.retiredMetrics.ShadowMismatches += stats.ShadowMismatches
	s.retiredMetrics.PlayoutUnderruns += stats.PlayoutUnderruns
	s.retiredMetrics.ShadowMismatchCounts = mergeShadowMismatchCounts(
		s.retiredMetrics.ShadowMismatchCounts,
		stats.ShadowMismatchCounts,
	)
}
