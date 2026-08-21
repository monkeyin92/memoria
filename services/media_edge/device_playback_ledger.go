package mediaedge

// Playback ledger maps device playback receipts onto the media-v1
// PlaybackProgress facts that Voice Core needs for actual-heard accounting.
// State is kept per authoritative fence because downlink sequence/sample
// counters may restart on the next generation, while a final receipt for a
// cancelled generation can legitimately arrive after its replacement starts.

import (
	"sync"
	"time"
)

type devicePlaybackState struct {
	lastRenderedEnd uint64
	lastReceivedSeq uint64
	lastApproximate bool
	hasReceipt      bool
	terminal        bool
}

type devicePlaybackSentKey struct {
	fence    deviceFence
	sequence uint64
}

type devicePlaybackTransport struct {
	sequenceBase     uint64
	sampleBase       uint64
	maxWireSeq       uint64
	maxWireSampleEnd uint64
	hasSent          bool
	initialized      bool
	conservative     bool
}

type devicePlaybackLedger struct {
	mu           sync.Mutex
	downlinkRate uint64
	states       map[deviceFence]devicePlaybackState
	sentAt       map[devicePlaybackSentKey]time.Time
	transports   map[deviceFence]devicePlaybackTransport
	receipts     uint64
	maxAckLagMS  int64
	onAckLag     func(int64)
	now          func() time.Time
}

func newDevicePlaybackLedger(
	downlinkRate uint64,
	onAckLag func(int64),
) *devicePlaybackLedger {
	return &devicePlaybackLedger{
		downlinkRate: downlinkRate,
		onAckLag:     onAckLag,
		states:       make(map[deviceFence]devicePlaybackState),
		sentAt:       make(map[devicePlaybackSentKey]time.Time),
		transports:   make(map[deviceFence]devicePlaybackTransport),
	}
}

// recordSent anchors an outbound sequence to the edge's own monotonic clock.
// Device monotonic timestamps are never subtracted from server wall time.
func (l *devicePlaybackLedger) recordSent(
	fence deviceFence,
	wireSequence uint64,
	wireSampleEnd ...uint64,
) {
	l.mu.Lock()
	defer l.mu.Unlock()
	now := time.Now()
	if l.now != nil {
		now = l.now()
	}
	key := devicePlaybackSentKey{fence: fence, sequence: wireSequence}
	l.sentAt[key] = now
	transport := l.transports[fence]
	if !transport.initialized {
		transport.initialized = true
	}
	if !transport.hasSent || wireSequence > transport.maxWireSeq {
		transport.maxWireSeq = wireSequence
	}
	transport.hasSent = true
	if len(wireSampleEnd) > 0 && wireSampleEnd[0] > transport.maxWireSampleEnd {
		transport.maxWireSampleEnd = wireSampleEnd[0]
	}
	l.transports[fence] = transport
	// Keep the map bounded if a device stops sending receipts.
	if len(l.sentAt) > 512 {
		for key := range l.sentAt {
			if key.fence == fence && key.sequence+256 < wireSequence {
				delete(l.sentAt, key)
			}
		}
	}
}

// record validates monotonic receipt state inside one generation fence and
// converts the rendered end into the 24 kHz source domain.
func (l *devicePlaybackLedger) record(
	receipt devicePlaybackReceipt,
	monotonicMS uint64,
) (PlaybackProgress, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	state := l.states[receipt.Fence]
	if state.terminal {
		// ended/error is a terminal fence. A device cannot extend Actual Heard
		// later with progress from the same generation.
		return PlaybackProgress{}, false
	}
	if state.hasReceipt && (receipt.ReceivedSequence < state.lastReceivedSeq ||
		receipt.RenderedSampleEnd < state.lastRenderedEnd) {
		return PlaybackProgress{}, false
	}
	transport, transportOK := l.transports[receipt.Fence]
	if !transportOK || !transport.initialized || !transport.hasSent ||
		receipt.ReceivedSequence > transport.maxWireSeq ||
		(transport.maxWireSampleEnd > 0 &&
			receipt.RenderedSampleEnd > transport.maxWireSampleEnd) {
		return PlaybackProgress{}, false
	}
	renderedSourceEnd := receipt.RenderedSampleEnd
	if l.downlinkRate == 16_000 {
		if receipt.RenderedSampleEnd%2 != 0 {
			return PlaybackProgress{}, false
		}
		renderedSourceEnd = receipt.RenderedSampleEnd * 3 / 2
	}
	now := time.Now()
	if l.now != nil {
		now = l.now()
	}
	sentKey := devicePlaybackSentKey{
		fence: receipt.Fence, sequence: receipt.ReceivedSequence,
	}
	if ^uint64(0)-transport.sampleBase < renderedSourceEnd ||
		^uint64(0)-transport.sequenceBase < receipt.ReceivedSequence {
		return PlaybackProgress{}, false
	}
	renderedSourceEnd += transport.sampleBase
	sourceSequence := receipt.ReceivedSequence + transport.sequenceBase
	state.lastReceivedSeq = receipt.ReceivedSequence
	state.lastRenderedEnd = receipt.RenderedSampleEnd
	state.lastApproximate = receipt.Approximate || transport.conservative
	state.hasReceipt = true
	state.terminal = receipt.Type == "playback.ended" || receipt.Type == "playback.error"
	l.states[receipt.Fence] = state
	l.receipts++
	if sentAt, ok := l.sentAt[sentKey]; ok {
		lag := now.Sub(sentAt).Milliseconds()
		if lag < 0 {
			lag = 0
		}
		if lag > l.maxAckLagMS {
			l.maxAckLagMS = lag
		}
		if l.onAckLag != nil {
			l.onAckLag(lag)
		}
	}
	for key := range l.sentAt {
		if key.fence == receipt.Fence && key.sequence <= receipt.ReceivedSequence {
			delete(l.sentAt, key)
		}
	}
	return PlaybackProgress{
		SessionID:         "",
		StreamEpoch:       0,
		TurnID:            receipt.Fence.TurnID,
		GenerationID:      receipt.Fence.GenerationID,
		ToolEpoch:         receipt.Fence.ToolEpoch,
		SessionEpoch:      receipt.Fence.SessionEpoch,
		ReceivedSequence:  sourceSequence,
		RenderedSampleEnd: renderedSourceEnd,
		ClientMonotonicMS: monotonicMS,
		Approximate:       receipt.Approximate || transport.conservative,
	}, true
}

func (l *devicePlaybackLedger) reset(downlinkRate uint64) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.downlinkRate = downlinkRate
	l.states = make(map[deviceFence]devicePlaybackState)
	l.sentAt = make(map[devicePlaybackSentKey]time.Time)
	l.transports = make(map[deviceFence]devicePlaybackTransport)
}

func (l *devicePlaybackLedger) startTransportFence(fence deviceFence, resumed bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for key := range l.sentAt {
		if key.fence == fence {
			delete(l.sentAt, key)
		}
	}
	delete(l.states, fence)
	l.transports[fence] = devicePlaybackTransport{
		initialized:  !resumed,
		conservative: resumed,
	}
}

func (l *devicePlaybackLedger) setTransportBase(
	fence deviceFence,
	sequenceBase uint64,
	sampleBase uint64,
) {
	l.mu.Lock()
	defer l.mu.Unlock()
	transport := l.transports[fence]
	transport.sequenceBase = sequenceBase
	transport.sampleBase = sampleBase
	transport.initialized = true
	l.transports[fence] = transport
}

func (l *devicePlaybackLedger) count() uint64 {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.receipts
}
