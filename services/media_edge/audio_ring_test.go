package mediaedge

import (
	"testing"
	"time"
)

func TestAudioRingWrapsWithoutGrowing(t *testing.T) {
	ring := newAudioRing(2)
	started := time.Now().Add(-time.Second)
	if !ring.Push(AudioFrame{Sequence: 1}, started) || !ring.Push(AudioFrame{Sequence: 2}, started) {
		t.Fatal("ring rejected available capacity")
	}
	if ring.Push(AudioFrame{Sequence: 3}, started) {
		t.Fatal("ring exceeded fixed capacity")
	}
	first, _ := ring.Pop()
	if first.frame.Sequence != 1 {
		t.Fatalf("first sequence=%d", first.frame.Sequence)
	}
	if !ring.Push(AudioFrame{Sequence: 3}, started) {
		t.Fatal("ring did not reuse popped slot")
	}
	second, _ := ring.Pop()
	third, _ := ring.Pop()
	if second.frame.Sequence != 2 || third.frame.Sequence != 3 || ring.Len() != 0 {
		t.Fatalf("ring order was not preserved: %d, %d", second.frame.Sequence, third.frame.Sequence)
	}
}

func TestAudioRingAgeTracksOldestEntry(t *testing.T) {
	ring := newAudioRing(2)
	now := time.Now()
	ring.Push(AudioFrame{Sequence: 1}, now.Add(-2*time.Second))
	ring.Push(AudioFrame{Sequence: 2}, now.Add(-time.Second))
	if age := ring.AgeMillis(now); age < 1_999 || age > 2_001 {
		t.Fatalf("oldest age=%f", age)
	}
	_, _ = ring.Pop()
	if age := ring.AgeMillis(now); age < 999 || age > 1_001 {
		t.Fatalf("next age=%f", age)
	}
}
