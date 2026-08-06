package mediaedge

import "time"

type queuedAudioFrame struct {
	frame    AudioFrame
	queuedAt time.Time
}

// audioRing keeps each session queue at a fixed, directly calculable size.
type audioRing struct {
	entries []queuedAudioFrame
	head    int
	size    int
}

func newAudioRing(capacity int) audioRing {
	if capacity <= 0 {
		panic("audio ring capacity must be positive")
	}
	return audioRing{entries: make([]queuedAudioFrame, capacity)}
}

func (r *audioRing) Len() int { return r.size }

func (r *audioRing) Push(frame AudioFrame, queuedAt time.Time) bool {
	if r.size == len(r.entries) {
		return false
	}
	index := (r.head + r.size) % len(r.entries)
	r.entries[index] = queuedAudioFrame{frame: frame, queuedAt: queuedAt}
	r.size++
	return true
}

func (r *audioRing) Front() (queuedAudioFrame, bool) {
	if r.size == 0 {
		return queuedAudioFrame{}, false
	}
	return r.entries[r.head], true
}

func (r *audioRing) Pop() (queuedAudioFrame, bool) {
	entry, ok := r.Front()
	if !ok {
		return queuedAudioFrame{}, false
	}
	r.entries[r.head] = queuedAudioFrame{}
	r.head = (r.head + 1) % len(r.entries)
	r.size--
	if r.size == 0 {
		r.head = 0
	}
	return entry, true
}

func (r *audioRing) Clear() {
	for r.size > 0 {
		_, _ = r.Pop()
	}
}

func (r *audioRing) AgeMillis(now time.Time) float64 {
	entry, ok := r.Front()
	if !ok {
		return 0
	}
	return float64(now.Sub(entry.queuedAt)) / float64(time.Millisecond)
}
