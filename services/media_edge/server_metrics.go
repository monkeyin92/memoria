package mediaedge

import (
	"fmt"
	"net/http"
	"runtime"
)

func (s *Server) metrics(w http.ResponseWriter, _ *http.Request) {
	s.metricsMu.Lock()
	defer s.metricsMu.Unlock()
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	if s.BridgeMetricsWriter != nil {
		s.BridgeMetricsWriter(w)
	}
	if s.DeviceWSS != nil {
		s.DeviceWSS.writeDeviceMetrics(w)
	}
	_, _ = fmt.Fprintf(w, "media_edge_requests_total %d\n", s.Requests.Load())
	_, _ = fmt.Fprintf(w, "media_edge_rejected_frames_total %d\n", s.RejectedFrames.Load())
	var memory runtime.MemStats
	runtime.ReadMemStats(&memory)
	_, _ = fmt.Fprintf(w, "media_edge_heap_alloc_bytes %d\n", memory.HeapAlloc)
	_, _ = fmt.Fprintf(w, "media_edge_mallocs_total %d\n", memory.Mallocs)
	_, _ = fmt.Fprintf(w, "media_edge_gc_pause_total_seconds %g\n", float64(memory.PauseTotalNs)/1e9)
	stats := s.Directory.Snapshots()
	maxIngressQueueAge := 0.0
	maxEgressQueueAge := 0.0
	maxGenerationCancelLatency := s.retiredMetrics.GenerationCancelLatencyMS
	maxPlayoutBuffer := 0.0
	playoutUnderruns := s.retiredMetrics.PlayoutUnderruns
	maxSessionDuration := s.retiredMetrics.SessionDurationMS
	for _, session := range stats {
		if session.IngressQueueAgeMS > maxIngressQueueAge {
			maxIngressQueueAge = session.IngressQueueAgeMS
		}
		if session.EgressQueueAgeMS > maxEgressQueueAge {
			maxEgressQueueAge = session.EgressQueueAgeMS
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
	_, _ = fmt.Fprintf(w, "ingress_queue_age_ms %g\n", maxIngressQueueAge)
	_, _ = fmt.Fprintf(w, "egress_queue_age_ms %g\n", maxEgressQueueAge)
	_, _ = fmt.Fprintf(w, "generation_cancel_latency_ms %g\n", maxGenerationCancelLatency)
	_, _ = fmt.Fprintf(w, "playout_buffer_ms %g\n", maxPlayoutBuffer)
	_, _ = fmt.Fprintf(w, "playout_underrun_total %d\n", playoutUnderruns)
	_, _ = fmt.Fprintf(w, "session_duration_ms %g\n", maxSessionDuration)
}

func (s *Server) archiveSessionMetricsLocked(stats SessionStats) {
	if stats.GenerationCancelLatencyMS > s.retiredMetrics.GenerationCancelLatencyMS {
		s.retiredMetrics.GenerationCancelLatencyMS = stats.GenerationCancelLatencyMS
	}
	if stats.SessionDurationMS > s.retiredMetrics.SessionDurationMS {
		s.retiredMetrics.SessionDurationMS = stats.SessionDurationMS
	}
	s.retiredMetrics.PlayoutUnderruns += stats.PlayoutUnderruns
}
