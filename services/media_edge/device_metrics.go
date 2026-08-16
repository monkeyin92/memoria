package mediaedge

// Device WSS metrics (solution section 12.1). Counters are process-wide
// atomics; gauges keep the maximum observed value since the last scrape so
// a scrape can never observe zero while a session is degraded.

import (
	"fmt"
	"io"
	"sort"
	"sync"
	"sync/atomic"
)

type deviceMetricCounters struct {
	connectSuccess    atomic.Uint64
	reconnectTotal    atomic.Uint64
	uplinkFrames      atomic.Uint64
	uplinkGapSamples  atomic.Uint64
	downlinkFrames    atomic.Uint64
	staleGeneration   atomic.Uint64
	backpressureDrop  atomic.Uint64
	oversizeRejected  atomic.Uint64
	rateRejected      atomic.Uint64
	opusErrors        atomic.Uint64
	runtimeErrors     atomic.Uint64
	leaseRejected     atomic.Uint64
	helloRejected     atomic.Uint64
	controlRejected   atomic.Uint64
	helloV1Total      atomic.Uint64
	helloV2Total      atomic.Uint64
	authRejected      atomic.Uint64
	activeConnections atomic.Int64
}

type deviceMetricGauges struct {
	mu                 sync.Mutex
	downlinkQueueMS    int64
	playbackAckLagMS   int64
	audioModeHalf      int64
	audioModeInterrupt int64
	audioModeFull      int64
}

func (g *deviceMetricGauges) observeDownlinkQueueMS(value int64) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if value > g.downlinkQueueMS {
		g.downlinkQueueMS = value
	}
}

func (g *deviceMetricGauges) observePlaybackAckLagMS(value int64) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if value > g.playbackAckLagMS {
		g.playbackAckLagMS = value
	}
}

func (g *deviceMetricGauges) observeAudioMode(mode string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	switch mode {
	case DeviceAudioModeHalfDuplexSafe:
		g.audioModeHalf++
	case DeviceAudioModeInterruptAssist:
		g.audioModeInterrupt++
	case DeviceAudioModeFullDuplex:
		g.audioModeFull++
	}
}

func (g *deviceMetricGauges) snapshot() (queueMS, ackLagMS, half, interrupt, full int64) {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.downlinkQueueMS, g.playbackAckLagMS, g.audioModeHalf, g.audioModeInterrupt, g.audioModeFull
}

type deviceRejectedReasons struct {
	mu      sync.Mutex
	reasons map[string]uint64
}

func newDeviceRejectedReasons() *deviceRejectedReasons {
	return &deviceRejectedReasons{reasons: make(map[string]uint64)}
}

func (r *deviceRejectedReasons) add(reason string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.reasons[reason]++
}

func (r *deviceRejectedReasons) snapshot() map[string]uint64 {
	r.mu.Lock()
	defer r.mu.Unlock()
	copied := make(map[string]uint64, len(r.reasons))
	for reason, count := range r.reasons {
		copied[reason] = count
	}
	return copied
}

func (s *DeviceWSServer) writeDeviceMetrics(w io.Writer) {
	counters := &s.metrics
	queueMS, ackLagMS, half, interrupt, full := s.gauges.snapshot()
	_, _ = fmt.Fprintf(w, "device_media_connect_success_total %d\n", counters.connectSuccess.Load())
	_, _ = fmt.Fprintf(w, "device_media_reconnect_total %d\n", counters.reconnectTotal.Load())
	_, _ = fmt.Fprintf(w, "device_media_auth_rejected_total %d\n", counters.authRejected.Load())
	_, _ = fmt.Fprintf(w, "device_media_hello_rejected_total %d\n", counters.helloRejected.Load())
	_, _ = fmt.Fprintf(w, "device_media_lease_rejected_total %d\n", counters.leaseRejected.Load())
	_, _ = fmt.Fprintf(w, "device_media_replay_rejected_total %d\n", s.Tickets.ReplayCount())
	_, _ = fmt.Fprintf(w, "device_uplink_frames_total %d\n", counters.uplinkFrames.Load())
	_, _ = fmt.Fprintf(w, "device_uplink_gap_samples_total %d\n", counters.uplinkGapSamples.Load())
	_, _ = fmt.Fprintf(w, "device_downlink_frames_total %d\n", counters.downlinkFrames.Load())
	_, _ = fmt.Fprintf(w, "device_downlink_queue_ms %d\n", queueMS)
	_, _ = fmt.Fprintf(w, "stale_generation_drop_total %d\n", counters.staleGeneration.Load())
	_, _ = fmt.Fprintf(w, "device_backpressure_drop_total %d\n", counters.backpressureDrop.Load())
	_, _ = fmt.Fprintf(w, "device_message_oversize_rejected_total %d\n", counters.oversizeRejected.Load())
	_, _ = fmt.Fprintf(w, "device_message_rate_rejected_total %d\n", counters.rateRejected.Load())
	_, _ = fmt.Fprintf(w, "device_control_rejected_total %d\n", counters.controlRejected.Load())
	_, _ = fmt.Fprintf(w, "device_opus_errors_total %d\n", counters.opusErrors.Load())
	_, _ = fmt.Fprintf(w, "device_runtime_errors_total %d\n", counters.runtimeErrors.Load())
	_, _ = fmt.Fprintf(w, "device_playback_ack_lag_ms %d\n", ackLagMS)
	_, _ = fmt.Fprintf(w, "device_audio_mode_half_duplex_safe_total %d\n", half)
	_, _ = fmt.Fprintf(w, "device_audio_mode_interrupt_assist_total %d\n", interrupt)
	_, _ = fmt.Fprintf(w, "device_audio_mode_full_duplex_verified_total %d\n", full)
	_, _ = fmt.Fprintf(w, "active_device_connections %d\n", counters.activeConnections.Load())
	reasons := s.rejectedReasons.snapshot()
	if len(reasons) > 0 {
		keys := make([]string, 0, len(reasons))
		for reason := range reasons {
			keys = append(keys, reason)
		}
		sort.Strings(keys)
		for _, reason := range keys {
			_, _ = fmt.Fprintf(w, "device_media_rejected_total{reason=%s} %d\n", quoteMetricLabel(reason), reasons[reason])
		}
	}
}

func quoteMetricLabel(value string) string {
	return fmt.Sprintf("%q", value)
}
