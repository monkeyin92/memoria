package mediaedge

// Priority lane for server->device messages. P0 (flush/cancel/close/error)
// always preempts P1 controls, P2 audio and P3 telemetry; audio frames are
// bounded by the 80-200 ms backpressure policy and stale generations are
// dropped instead of delaying the current answer.

import (
	"sync"
	"time"
)

type deviceLaneItem struct {
	priority     int
	kind         string // "control", "audio", or ordered "barrier"
	payload      []byte
	enqueuedAt   time.Time
	generation   uint32
	fence        deviceFence
	sequence     uint64 // device-wire sequence
	sample       uint64 // device-wire sample position
	frameSamples uint32
}

type devicePriorityLane struct {
	mu      sync.Mutex
	cond    *sync.Cond
	queues  [4][]deviceLaneItem
	closed  bool
	config  DeviceBackpressureConfig
	now     func() time.Time
	onDrop  func(item deviceLaneItem)
	pending int
	notify  chan struct{}

	// Downlink write clock: the device's per-generation sequence/sample
	// clock advances only with frames it actually receives, so queue drops
	// surface here as forward gaps that the writer flags at socket-write
	// time. The baseline is the next expected position right after an
	// authoritative generation.started / playback.flush replacement (0/0);
	// dropped tracks exactly which frames this lane removed, so an upstream
	// gap the lane did not cause is never flagged.
	writeMu           sync.Mutex
	baselineSet       bool
	baselineSeq       uint64
	baselineSample    uint64
	lastWrittenGen    uint32
	lastWrittenSeq    uint64
	lastWrittenSample uint64
	hasWrittenAudio   bool
	dropped           map[uint32]deviceDroppedRange
}

// deviceDroppedRange is a contiguous run of sequences this lane dropped
// before they reached the socket. Drops happen in enqueue order and covered
// ranges are pruned on write, so one range per generation is exact.
type deviceDroppedRange struct {
	lo uint64
	hi uint64
}

func newDevicePriorityLane(config DeviceBackpressureConfig) (*devicePriorityLane, error) {
	if err := config.validate(); err != nil {
		return nil, err
	}
	lane := &devicePriorityLane{config: config, notify: make(chan struct{}, 1)}
	lane.cond = sync.NewCond(&lane.mu)
	return lane, nil
}

func (l *devicePriorityLane) setDropHook(hook func(deviceLaneItem)) {
	l.mu.Lock()
	defer l.mu.Unlock()
	l.onDrop = hook
}

func (l *devicePriorityLane) currentTime() time.Time {
	if l.now != nil {
		return l.now()
	}
	return time.Now()
}

func (l *devicePriorityLane) enqueue(item deviceLaneItem) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.closed {
		return
	}
	if item.priority < 0 || item.priority > 3 {
		item.priority = 3
	}
	if item.enqueuedAt.IsZero() {
		item.enqueuedAt = l.currentTime()
	}
	l.queues[item.priority] = append(l.queues[item.priority], item)
	l.pending++
	l.cond.Signal()
	l.signalNotifyLocked()
}

func (l *devicePriorityLane) enqueueControl(priority int, payload []byte) {
	l.enqueue(deviceLaneItem{priority: priority, kind: "control", payload: payload})
}

// enqueueAudio applies the 80-200 ms ceiling: audio is only accepted when
// the lane holds at most TargetMaxMS of audio, and any overflow drops the
// oldest frames so playout latency stays bounded.
func (l *devicePriorityLane) enqueueAudio(
	payload []byte,
	generation uint32,
	fence deviceFence,
	sequence uint64,
	sample uint64,
	frameSamples uint32,
	_source ...uint64,
) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.closed {
		return
	}
	now := l.currentTime()
	l.dropAudioLocked(now)
	l.queues[2] = append(l.queues[2], deviceLaneItem{
		priority: 2, kind: "audio", payload: payload,
		enqueuedAt: now, generation: generation, fence: fence, sequence: sequence,
		sample: sample, frameSamples: frameSamples,
	})
	l.pending++
	l.dropAudioLocked(now)
	l.cond.Signal()
	l.signalNotifyLocked()
}

// resetWriteClock installs the expected downlink write baseline. Call it when
// an authoritative generation.started or playback.flush replacement resets
// the device sequence/sample clock to 0: the first frame of the new
// generation is expected at 0/0, and a first surviving forward gap caused by
// drops before any write is still flagged against that baseline.
func (l *devicePriorityLane) resetWriteClock() {
	l.writeMu.Lock()
	defer l.writeMu.Unlock()
	l.baselineSet = true
	l.baselineSeq = 0
	l.baselineSample = 0
	l.hasWrittenAudio = false
	l.lastWrittenGen = 0
	l.lastWrittenSeq = 0
	l.lastWrittenSample = 0
	l.dropped = make(map[uint32]deviceDroppedRange)
}

// recordDrop notes one audio frame this lane removed before it reached the
// socket. Only sequences recorded here may later be covered by a
// discontinuity flag; an upstream gap the lane did not drop stays unflagged
// so the device rejects it (fail closed).
func (l *devicePriorityLane) recordDrop(generation uint32, sequence uint64) {
	l.writeMu.Lock()
	defer l.writeMu.Unlock()
	if l.dropped == nil {
		l.dropped = make(map[uint32]deviceDroppedRange)
	}
	current, ok := l.dropped[generation]
	if !ok {
		l.dropped[generation] = deviceDroppedRange{lo: sequence, hi: sequence}
		return
	}
	switch {
	case sequence == current.hi+1:
		current.hi = sequence
	case sequence+1 == current.lo:
		current.lo = sequence
	default:
		// A non-contiguous drop would make accounting inexact; forget the
		// range so no future gap of this generation can be flagged as a
		// lane drop (fail closed rather than claim a forged gap).
		delete(l.dropped, generation)
		return
	}
	l.dropped[generation] = current
}

// needsDiscontinuity reports whether the next audio frame must carry the
// header discontinuity flag before it is written: it jumps forward from the
// next expected position (one frame after the last written frame, or the
// 0/0 baseline before the first write of a generation) over a run of frames
// with sequence/sample deltas
// that are consistent multiples of frame_samples, and that exact run was
// dropped by this lane. Backward jumps, forged (inconsistent) deltas, gaps
// of a different generation and upstream gaps the lane did not drop are
// never flagged: the device must reject them.
func (l *devicePriorityLane) needsDiscontinuity(
	generation uint32,
	sequence uint64,
	sample uint64,
	frameSamples uint32,
) bool {
	l.writeMu.Lock()
	defer l.writeMu.Unlock()
	if !l.baselineSet || frameSamples == 0 {
		return false
	}
	expectedSeq := l.baselineSeq
	expectedSample := l.baselineSample
	if l.hasWrittenAudio {
		if generation != l.lastWrittenGen {
			return false
		}
		expectedSeq = l.lastWrittenSeq + 1
		if expectedSeq == 0 {
			return false
		}
		expectedSample = l.lastWrittenSample + uint64(frameSamples)
		if expectedSample < l.lastWrittenSample {
			return false
		}
	}
	if sequence <= expectedSeq || sample < expectedSample {
		return false
	}
	sequenceDelta := sequence - expectedSeq
	sampleDelta := sample - expectedSample
	if sampleDelta != sequenceDelta*uint64(frameSamples) {
		return false // forged/inconsistent delta
	}
	// The skipped run must be exactly the frames this lane dropped.
	dropped, ok := l.dropped[generation]
	return ok && dropped.lo == expectedSeq && dropped.hi == sequence-1
}

// markAudioWritten records the audio frame position the socket actually
// consumed; it must only be called after a successful write. Any dropped run
// fully covered by the write (flagged on this frame) is retired.
func (l *devicePriorityLane) markAudioWritten(generation uint32, sequence uint64, sample uint64) {
	l.writeMu.Lock()
	defer l.writeMu.Unlock()
	l.hasWrittenAudio = true
	l.lastWrittenGen = generation
	l.lastWrittenSeq = sequence
	l.lastWrittenSample = sample
	dropped, ok := l.dropped[generation]
	if !ok {
		return
	}
	if sequence >= dropped.hi {
		delete(l.dropped, generation)
	} else if sequence >= dropped.lo {
		dropped.lo = sequence + 1
		l.dropped[generation] = dropped
	}
}

// enqueueBarrier appends a same-generation control after all audio already
// accepted into P2. It therefore cannot overtake the tail of that generation.
func (l *devicePriorityLane) enqueueBarrier(payload []byte, generation uint32) {
	l.enqueue(deviceLaneItem{
		priority: 2, kind: "barrier", payload: payload, generation: generation,
	})
}

func (l *devicePriorityLane) signalNotifyLocked() {
	select {
	case l.notify <- struct{}{}:
	default:
	}
}

func (l *devicePriorityLane) dropAudioLocked(now time.Time) {
	queue := l.queues[2]
	for {
		firstAudio := -1
		audioCount := 0
		for index, item := range queue {
			if item.kind != "audio" {
				continue
			}
			audioCount++
			if firstAudio < 0 {
				firstAudio = index
			}
		}
		if firstAudio < 0 {
			break
		}
		head := queue[firstAudio]
		headAgeMS := frameAgeMS(head.enqueuedAt, now)
		over := int64(audioCount) * 20
		if headAgeMS <= l.config.TargetMaxMS && over <= l.config.TargetMaxMS {
			break
		}
		if l.onDrop != nil {
			l.onDrop(head)
		}
		l.recordDrop(head.generation, head.sequence)
		queue = append(queue[:firstAudio], queue[firstAudio+1:]...)
		l.pending--
	}
	l.queues[2] = queue
}

// dropGeneration removes queued audio and completion barriers invalidated by
// an authoritative cancel before they reach the socket writer.
func (l *devicePriorityLane) dropGeneration(generation uint32) {
	l.mu.Lock()
	defer l.mu.Unlock()
	queue := l.queues[2]
	kept := queue[:0]
	for _, item := range queue {
		if item.generation == generation && (item.kind == "audio" || item.kind == "barrier") {
			if item.kind == "audio" && l.onDrop != nil {
				l.onDrop(item)
			}
			if item.kind == "audio" {
				l.recordDrop(item.generation, item.sequence)
			}
			l.pending--
			continue
		}
		kept = append(kept, item)
	}
	l.queues[2] = kept
}

// pop returns the highest-priority item, blocking until one is available or
// the lane is closed.
func (l *devicePriorityLane) pop() (deviceLaneItem, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	for l.pending == 0 && !l.closed {
		l.cond.Wait()
	}
	if l.pending == 0 {
		return deviceLaneItem{}, false
	}
	for priority := 0; priority < 4; priority++ {
		if len(l.queues[priority]) > 0 {
			item := l.queues[priority][0]
			l.queues[priority] = l.queues[priority][1:]
			l.pending--
			return item, true
		}
	}
	return deviceLaneItem{}, false
}

// tryPop is the non-blocking form used by the WebSocket writer so the same
// goroutine can service ping deadlines while the lane is idle.
func (l *devicePriorityLane) tryPop() (deviceLaneItem, bool) {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.pending == 0 {
		return deviceLaneItem{}, false
	}
	for priority := 0; priority < 4; priority++ {
		if len(l.queues[priority]) == 0 {
			continue
		}
		item := l.queues[priority][0]
		l.queues[priority] = l.queues[priority][1:]
		l.pending--
		if l.pending > 0 {
			l.signalNotifyLocked()
		}
		return item, true
	}
	return deviceLaneItem{}, false
}

func (l *devicePriorityLane) isClosed() bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.closed
}

func (l *devicePriorityLane) close() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.closed {
		return
	}
	l.closed = true
	l.pending = 0
	l.cond.Broadcast()
	l.signalNotifyLocked()
}

// stats returns the pending depth and the age of the oldest audio frame for
// metrics.
func (l *devicePriorityLane) stats() (pending int, oldestAudioAgeMS int64) {
	l.mu.Lock()
	defer l.mu.Unlock()
	oldest := int64(0)
	for _, item := range l.queues[2] {
		if item.kind != "audio" {
			continue
		}
		if age := frameAgeMS(item.enqueuedAt, l.currentTime()); age > oldest {
			oldest = age
		}
	}
	return l.pending, oldest
}
