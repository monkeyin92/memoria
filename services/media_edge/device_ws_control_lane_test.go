package mediaedge

import (
	"testing"
	"time"
)

func TestDevicePriorityLaneP0PreemptsAudio(t *testing.T) {
	lane, err := newDevicePriorityLane(DefaultDeviceBackpressureConfig())
	if err != nil {
		t.Fatal(err)
	}
	defer lane.close()
	for index := 0; index < 5; index++ {
		lane.enqueueAudio([]byte{byte(index)}, 1, deviceFence{GenerationID: 1},
			uint64(index), uint64(index)*320, 320)
	}
	lane.enqueueControl(0, []byte(`{"type":"playback.flush"}`))
	item, ok := lane.pop()
	if !ok || item.kind != "control" {
		t.Fatalf("P0 control did not preempt audio: kind=%s ok=%v", item.kind, ok)
	}
	if string(item.payload) != `{"type":"playback.flush"}` {
		t.Fatalf("unexpected P0 payload: %s", item.payload)
	}
}

func TestDevicePriorityLaneBackpressureDropsOldestAudio(t *testing.T) {
	config := DefaultDeviceBackpressureConfig()
	lane, err := newDevicePriorityLane(config)
	if err != nil {
		t.Fatal(err)
	}
	defer lane.close()
	drops := 0
	lane.setDropHook(func(item deviceLaneItem) {
		if item.kind == "audio" {
			drops++
		}
	})
	base := time.Now()
	lane.now = func() time.Time { return base }
	for index := 0; index < 15; index++ {
		lane.enqueueAudio([]byte{byte(index)}, 1, deviceFence{GenerationID: 1},
			uint64(index), uint64(index)*320, 320)
	}
	pending, _ := lane.stats()
	// 200 ms ceiling: at most 10 frames of 20 ms each survive.
	if pending != 10 {
		t.Fatalf("pending = %d, want 10", pending)
	}
	if drops != 5 {
		t.Fatalf("drops = %d, want 5", drops)
	}
	// Aging past the ceiling drops the old queue and keeps the fresh frame.
	lane.now = func() time.Time { return base.Add(300 * time.Millisecond) }
	lane.enqueueAudio([]byte{0xFF}, 1, deviceFence{GenerationID: 1}, 15, 15*320, 320)
	pending, _ = lane.stats()
	if pending != 1 {
		t.Fatalf("pending = %d, want only the fresh frame", pending)
	}
	if drops != 15 {
		t.Fatalf("drops = %d, want 15", drops)
	}
}

func TestDevicePriorityLaneFIFOWithinPriority(t *testing.T) {
	lane, err := newDevicePriorityLane(DefaultDeviceBackpressureConfig())
	if err != nil {
		t.Fatal(err)
	}
	defer lane.close()
	lane.enqueueControl(1, []byte("a"))
	lane.enqueueControl(1, []byte("b"))
	first, _ := lane.pop()
	second, _ := lane.pop()
	if string(first.payload) != "a" || string(second.payload) != "b" {
		t.Fatalf("P1 order was not FIFO: %s then %s", first.payload, second.payload)
	}
}

func TestDevicePriorityLaneGenerationCompleteStaysBehindAudio(t *testing.T) {
	lane, err := newDevicePriorityLane(DefaultDeviceBackpressureConfig())
	if err != nil {
		t.Fatal(err)
	}
	defer lane.close()
	lane.enqueueAudio([]byte("audio"), 7, deviceFence{GenerationID: 7}, 1, 320, 320)
	lane.enqueueBarrier([]byte("complete"), 7)
	first, _ := lane.pop()
	second, _ := lane.pop()
	if first.kind != "audio" || second.kind != "barrier" {
		t.Fatalf("generation completion overtook audio: %s then %s", first.kind, second.kind)
	}
}

func TestDevicePriorityLaneCancelDropsAudioAndCompletionBarrier(t *testing.T) {
	lane, err := newDevicePriorityLane(DefaultDeviceBackpressureConfig())
	if err != nil {
		t.Fatal(err)
	}
	defer lane.close()
	lane.enqueueAudio([]byte("old"), 7, deviceFence{GenerationID: 7}, 1, 320, 320)
	lane.enqueueBarrier([]byte("complete"), 7)
	lane.enqueueAudio([]byte("new"), 8, deviceFence{GenerationID: 8}, 2, 640, 320)
	lane.dropGeneration(7)
	item, ok := lane.pop()
	if !ok || item.generation != 8 || string(item.payload) != "new" {
		t.Fatalf("cancel retained stale P2 items: %+v ok=%v", item, ok)
	}
}

// TestDevicePriorityLaneFlagsSameGenerationQueueDropGap verifies the
// socket-write-time contract: frames dropped by the backpressure ceiling
// before they were written surface as a forward gap on the next frame, and
// that frame must carry the header discontinuity flag. Consecutive frames
// (before and after the gap) must never be flagged.
func TestDevicePriorityLaneFlagsSameGenerationQueueDropGap(t *testing.T) {
	config := DefaultDeviceBackpressureConfig()
	lane, err := newDevicePriorityLane(config)
	if err != nil {
		t.Fatal(err)
	}
	defer lane.close()
	base := time.Now()
	lane.now = func() time.Time { return base }
	// The authoritative generation.started precedes every audio frame.
	lane.resetWriteClock()

	// Write frames 0..4 consecutively; none of them may carry a flag.
	for sequence := uint64(0); sequence < 5; sequence++ {
		lane.enqueueAudio([]byte{byte(sequence)}, 1, deviceFence{GenerationID: 1},
			sequence, sequence*320, 320)
	}
	for sequence := uint64(0); sequence < 5; sequence++ {
		item, ok := lane.pop()
		if !ok || item.kind != "audio" || item.sequence != sequence {
			t.Fatalf("unexpected item %+v ok=%v", item, ok)
		}
		if lane.needsDiscontinuity(item.generation, item.sequence, item.sample, item.frameSamples) {
			t.Fatalf("consecutive frame %d was flagged", sequence)
		}
		lane.markAudioWritten(item.generation, item.sequence, item.sample)
	}

	// A burst past the 200 ms ceiling drops the oldest queued frames before
	// the writer can drain them: the surviving head is sequence 10, a
	// forward gap of six frames over the last written position (4).
	lane.now = func() time.Time { return base.Add(300 * time.Millisecond) }
	for sequence := uint64(5); sequence < 20; sequence++ {
		lane.enqueueAudio([]byte{byte(sequence)}, 1, deviceFence{GenerationID: 1},
			sequence, sequence*320, 320)
	}
	item, ok := lane.pop()
	if !ok || item.sequence != 10 {
		t.Fatalf("expected surviving head at sequence 10, got %+v ok=%v", item, ok)
	}
	if !lane.needsDiscontinuity(item.generation, item.sequence, item.sample, item.frameSamples) {
		t.Fatal("queue-drop forward gap was not flagged")
	}
	lane.markAudioWritten(item.generation, item.sequence, item.sample)

	// The next frame is consecutive again and must not be flagged.
	next, ok := lane.pop()
	if !ok || next.sequence != 11 {
		t.Fatalf("expected sequence 11, got %+v ok=%v", next, ok)
	}
	if lane.needsDiscontinuity(next.generation, next.sequence, next.sample, next.frameSamples) {
		t.Fatal("consecutive frame after a flagged gap was flagged")
	}
}

// TestDevicePriorityLaneNeverFlagsBackwardForgedOrCrossGenerationGaps: only
// a same-generation forward gap with consistent multiples of frame_samples
// may be flagged; anything else must stay unflagged so the device rejects
// it as a protocol violation (fail closed).
func TestDevicePriorityLaneNeverFlagsBackwardForgedOrCrossGenerationGaps(t *testing.T) {
	lane, err := newDevicePriorityLane(DefaultDeviceBackpressureConfig())
	if err != nil {
		t.Fatal(err)
	}
	defer lane.close()
	lane.enqueueAudio([]byte("a"), 1, deviceFence{GenerationID: 1}, 5, 5*320, 320)
	item, ok := lane.pop()
	if !ok {
		t.Fatal("lane is empty")
	}
	lane.markAudioWritten(item.generation, item.sequence, item.sample)

	if lane.needsDiscontinuity(1, 3, 3*320, 320) {
		t.Fatal("backward gap was flagged")
	}
	if lane.needsDiscontinuity(1, 5, 5*320, 320) {
		t.Fatal("duplicate sequence was flagged")
	}
	if lane.needsDiscontinuity(1, 9, 5*320, 320) {
		t.Fatal("forged gap with inconsistent sample delta was flagged")
	}
	if lane.needsDiscontinuity(2, 9, 9*320, 320) {
		t.Fatal("cross-generation jump was flagged")
	}
}

// TestDevicePriorityLaneClockResetsPerGenerationStart: the first frame of a
// new generation restarts at sequence/sample zero after an authoritative
// generation.started; it must never be flagged even though its sequence is
// numerically below the previous generation's last written position.
func TestDevicePriorityLaneClockResetsPerGenerationStart(t *testing.T) {
	lane, err := newDevicePriorityLane(DefaultDeviceBackpressureConfig())
	if err != nil {
		t.Fatal(err)
	}
	defer lane.close()
	lane.enqueueAudio([]byte("a"), 1, deviceFence{GenerationID: 1}, 0, 0, 320)
	lane.enqueueAudio([]byte("b"), 1, deviceFence{GenerationID: 1}, 1, 320, 320)
	for sequence := uint64(0); sequence < 2; sequence++ {
		item, ok := lane.pop()
		if !ok {
			t.Fatal("lane is empty")
		}
		lane.markAudioWritten(item.generation, item.sequence, item.sample)
	}
	// generation.started for generation 2 resets the device clock to 0.
	lane.resetWriteClock()
	lane.enqueueAudio([]byte("c"), 2, deviceFence{GenerationID: 2}, 0, 0, 320)
	item, ok := lane.pop()
	if !ok {
		t.Fatal("lane is empty")
	}
	if lane.needsDiscontinuity(item.generation, item.sequence, item.sample, item.frameSamples) {
		t.Fatal("first frame of a new generation after a clock reset was flagged")
	}
}

// TestDevicePriorityLaneFlagsInitialDropsBeforeFirstWrite: the backpressure
// ceiling can drop frames of a fresh generation before the writer has
// written anything. The first surviving frame must still be flagged: its
// forward gap is measured against the 0/0 baseline installed by
// generation.started, and the skipped run was exactly this lane's drop.
func TestDevicePriorityLaneFlagsInitialDropsBeforeFirstWrite(t *testing.T) {
	config := DefaultDeviceBackpressureConfig()
	lane, err := newDevicePriorityLane(config)
	if err != nil {
		t.Fatal(err)
	}
	defer lane.close()
	base := time.Now()
	lane.now = func() time.Time { return base }

	// Without an authoritative baseline there is nothing to measure against:
	// nothing may be flagged.
	for sequence := uint64(0); sequence < 15; sequence++ {
		lane.enqueueAudio([]byte{byte(sequence)}, 1, deviceFence{GenerationID: 1},
			sequence, sequence*320, 320)
	}
	item, ok := lane.pop()
	if !ok || item.sequence != 5 {
		t.Fatalf("expected surviving head at sequence 5, got %+v ok=%v", item, ok)
	}
	if lane.needsDiscontinuity(item.generation, item.sequence, item.sample, item.frameSamples) {
		t.Fatal("frame was flagged without an authoritative generation baseline")
	}
	lane.close()

	// Now the authoritative generation.started baseline: the same drop run
	// (0..4) is measured against the next-expected 0/0 and must be flagged.
	// Use a fresh lane so frames left behind by the no-baseline case cannot
	// contaminate this assertion.
	lane, err = newDevicePriorityLane(config)
	if err != nil {
		t.Fatal(err)
	}
	defer lane.close()
	lane.now = func() time.Time { return base }
	lane.resetWriteClock()
	for sequence := uint64(0); sequence < 15; sequence++ {
		lane.enqueueAudio([]byte{byte(sequence)}, 1, deviceFence{GenerationID: 1},
			sequence, sequence*320, 320)
	}
	item, ok = lane.pop()
	if !ok || item.sequence != 5 {
		t.Fatalf("expected surviving head at sequence 5, got %+v ok=%v", item, ok)
	}
	if !lane.needsDiscontinuity(item.generation, item.sequence, item.sample, item.frameSamples) {
		t.Fatal("first surviving frame after pre-write drops was not flagged")
	}
	lane.markAudioWritten(item.generation, item.sequence, item.sample)
	// The next frame is consecutive and must not be flagged.
	next, ok := lane.pop()
	if !ok || next.sequence != 6 {
		t.Fatalf("expected sequence 6, got %+v ok=%v", next, ok)
	}
	if lane.needsDiscontinuity(next.generation, next.sequence, next.sample, next.frameSamples) {
		t.Fatal("consecutive frame after the pre-write gap was flagged")
	}
}

// TestDevicePriorityLaneDoesNotFlagUpstreamGapWithoutLaneDrop: a sequence
// jump the lane did not cause (forged upstream skip) must stay unflagged
// even when the sample delta is a consistent multiple of frame_samples. The
// device then rejects the unflagged gap instead of accepting a gap the edge
// cannot account for.
func TestDevicePriorityLaneDoesNotFlagUpstreamGapWithoutLaneDrop(t *testing.T) {
	lane, err := newDevicePriorityLane(DefaultDeviceBackpressureConfig())
	if err != nil {
		t.Fatal(err)
	}
	defer lane.close()
	lane.resetWriteClock()
	for sequence := uint64(0); sequence < 3; sequence++ {
		lane.enqueueAudio([]byte{byte(sequence)}, 1, deviceFence{GenerationID: 1},
			sequence, sequence*320, 320)
	}
	for sequence := uint64(0); sequence < 3; sequence++ {
		item, ok := lane.pop()
		if !ok || item.sequence != sequence {
			t.Fatalf("unexpected item %+v ok=%v", item, ok)
		}
		if lane.needsDiscontinuity(item.generation, item.sequence, item.sample, item.frameSamples) {
			t.Fatalf("consecutive frame %d was flagged", sequence)
		}
		lane.markAudioWritten(item.generation, item.sequence, item.sample)
	}
	// The upstream skipped sequences 3 and 4: the lane never saw them and
	// never dropped them, so the jump to 5 must not be flagged.
	lane.enqueueAudio([]byte("forged"), 1, deviceFence{GenerationID: 1}, 5, 5*320, 320)
	item, ok := lane.pop()
	if !ok || item.sequence != 5 {
		t.Fatalf("unexpected item %+v ok=%v", item, ok)
	}
	if lane.needsDiscontinuity(item.generation, item.sequence, item.sample, item.frameSamples) {
		t.Fatal("upstream gap not covered by a lane drop was flagged")
	}
}

func TestDeviceTokenBucket(t *testing.T) {
	bucket := newDeviceTokenBucket(10, 3)
	now := time.Now()
	allowed := 0
	for index := 0; index < 10; index++ {
		if bucket.allow(now) {
			allowed++
		}
	}
	if allowed != 3 {
		t.Fatalf("burst allowed %d, want 3", allowed)
	}
	now = now.Add(time.Second)
	if !bucket.allow(now) {
		t.Fatal("refill did not allow a token")
	}
}
