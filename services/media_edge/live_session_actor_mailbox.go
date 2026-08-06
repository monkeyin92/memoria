package mediaedge

import (
	"sync/atomic"
	"time"
)

// Mailbox admission and scheduling are isolated from the candidate-state
// reducer so priority policy can evolve without touching shadow semantics.

func (a *LiveSessionActor) TrySubmit(event LiveSessionEvent) error {
	a.submitMu.Lock()
	defer a.submitMu.Unlock()
	if a.closed {
		return ErrActorClosed
	}
	isAudio := event.Kind == LiveEventAudioUplink || event.Kind == LiveEventAudioDownlink
	event.Audio = isAudio
	if event.Scenario == "" {
		event.Scenario = string(event.Kind)
	}
	if event.ContractVersion == "" {
		if requiresExactStreamEpoch(event.Kind) {
			event.ContractVersion = shadowA6AContractVersion
		} else {
			event.ContractVersion = shadowContractVersion
		}
	}
	if event.Authoritative != nil {
		authoritative := cloneLiveSessionSnapshot(*event.Authoritative, false)
		event.Authoritative = &authoritative
	}
	if event.SpeechSegment != nil {
		segment := *event.SpeechSegment
		event.SpeechSegment = &segment
	}
	if event.OutputIntent != nil {
		intent := *event.OutputIntent
		event.OutputIntent = &intent
	}
	critical := !isAudio && isCriticalEvent(event.Kind)
	if !isAudio && !critical && (a.mailboxDepth() >= a.mailboxSize-a.audioReserve ||
		a.bulkPending.Load() >= int64(cap(a.bulk))) {
		a.recordDrop(false)
		return ErrActorMailboxFull
	}
	var target chan LiveSessionEvent
	if isAudio {
		target = a.audio
	} else if critical {
		target = a.critical
	} else {
		target = a.bulk
	}
	if !a.reserveLane(isAudio, critical, cap(target)) {
		a.recordDrop(isAudio)
		return ErrActorMailboxFull
	}
	event.enqueuedAt = time.Now()
	a.nextSequence++
	event.mailboxSequence = a.nextSequence
	select {
	case target <- event:
		return nil
	default:
		a.releaseLane(isAudio, critical)
		a.recordDrop(isAudio)
		return ErrActorMailboxFull
	}
}

func (a *LiveSessionActor) Close() {
	a.submitMu.Lock()
	if a.closed {
		a.submitMu.Unlock()
		<-a.done
		return
	}
	a.closed = true
	a.cancel()
	a.submitMu.Unlock()
	<-a.done
}

func (a *LiveSessionActor) Snapshot() LiveSessionSnapshot {
	a.stateMu.Lock()
	defer a.stateMu.Unlock()
	a.refreshOutputCandidate(time.Now().UnixMilli())
	snapshot := cloneLiveSessionSnapshot(a.state, true)
	snapshot.MailboxDepth = a.mailboxDepth()
	return snapshot
}

func (a *LiveSessionActor) mailboxDepth() int {
	return int(a.pending.Load())
}

func (a *LiveSessionActor) reserveLane(audio, critical bool, capacity int) bool {
	var lane *atomic.Int64
	if audio {
		lane = &a.audioPending
	} else if critical {
		lane = &a.criticalPending
	} else {
		lane = &a.bulkPending
	}
	if lane.Load() >= int64(capacity) {
		return false
	}
	for {
		pending := a.pending.Load()
		if pending >= int64(a.mailboxSize) {
			return false
		}
		if !a.pending.CompareAndSwap(pending, pending+1) {
			continue
		}
		lane.Add(1)
		return true
	}
}

func (a *LiveSessionActor) releaseLane(audio, critical bool) {
	if audio {
		a.audioPending.Add(-1)
	} else if critical {
		a.criticalPending.Add(-1)
	} else {
		a.bulkPending.Add(-1)
	}
	a.pending.Add(-1)
}

func (a *LiveSessionActor) releaseEvent(event LiveSessionEvent) {
	a.releaseLane(event.Audio, !event.Audio && isCriticalEvent(event.Kind))
}

func isCriticalEvent(kind LiveSessionEventKind) bool {
	switch kind {
	case LiveEventGenerationStart,
		LiveEventGenerationCancel,
		LiveEventVADStart,
		LiveEventVADEnd,
		LiveEventPlaybackProgress:
		return true
	default:
		return false
	}
}

func (a *LiveSessionActor) run() {
	defer func() {
		for {
			event, ok := a.nextEvent(true)
			if !ok {
				close(a.done)
				return
			}
			a.recordDrained(event)
			a.releaseEvent(event)
		}
	}()
	for {
		event, ok := a.nextEvent(false)
		if !ok {
			return
		}
		if a.processDelay > 0 {
			select {
			case <-a.ctx.Done():
				a.recordDrained(event)
				a.releaseEvent(event)
				return
			case <-time.After(a.processDelay):
			}
		}
		a.apply(event)
		a.releaseEvent(event)
	}
}

// nextEvent drains critical controls before audio and gives bulk work a turn
// only when the higher-priority lanes are empty. `drain` is used during
// shutdown to account for every queued event without waiting on the context.
func (a *LiveSessionActor) nextEvent(drain bool) (LiveSessionEvent, bool) {
	if drain {
		for _, lane := range []<-chan LiveSessionEvent{a.critical, a.audio, a.bulk} {
			select {
			case event := <-lane:
				return event, true
			default:
			}
		}
		return LiveSessionEvent{}, false
	}
	for _, lane := range []<-chan LiveSessionEvent{a.critical, a.audio, a.bulk} {
		select {
		case event := <-lane:
			return event, true
		default:
		}
	}
	select {
	case <-a.ctx.Done():
		return LiveSessionEvent{}, false
	case event := <-a.critical:
		return event, true
	case event := <-a.audio:
		return event, true
	case event := <-a.bulk:
		return event, true
	}
}

func (a *LiveSessionActor) recordDrop(audio bool) {
	a.stateMu.Lock()
	defer a.stateMu.Unlock()
	a.state.DroppedEvents++
	if audio {
		a.state.ProcessedAudioFrames++
		a.state.FrameDeadlineMisses++
	}
}

func (a *LiveSessionActor) recordDrained(event LiveSessionEvent) {
	a.stateMu.Lock()
	defer a.stateMu.Unlock()
	a.state.DroppedEvents++
	if event.Audio {
		a.state.ProcessedAudioFrames++
		if time.Since(event.enqueuedAt) > a.deadline {
			a.state.FrameDeadlineMisses++
		}
	}
}
