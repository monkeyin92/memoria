package mediaedge

import "sort"

// Speech candidates are kept separate from output arbitration so revisions
// cannot accidentally become executable output state.
func (a *LiveSessionActor) applySpeechCommit(commitSample uint64) {
	timeline := &a.state.SpeechTimeline
	if commitSample == 0 || commitSample < timeline.CommittedSample {
		a.dropSpeechSegment("drop_stale_speech_commit")
		return
	}
	timeline.CommittedSample = commitSample
	pending := timeline.Segments[:0]
	for _, segment := range timeline.Segments {
		if segment.CaptureEndSample > commitSample {
			pending = append(pending, segment)
		}
	}
	timeline.Segments = pending
	a.state.LastDecision = "commit_speech_candidate"
}

func (a *LiveSessionActor) applySpeechSegment(candidate *ShadowSpeechSegment) {
	timeline := &a.state.SpeechTimeline
	if candidate == nil || candidate.SegmentID == "" || candidate.Revision == 0 ||
		candidate.CaptureEndSample <= candidate.CaptureStartSample {
		a.dropSpeechSegment("drop_invalid_speech_segment")
		return
	}
	if candidate.TaskEpoch < timeline.LatestTaskEpoch ||
		candidate.CaptureEndSample <= timeline.CommittedSample {
		a.dropSpeechSegment("drop_stale_speech_segment")
		return
	}
	if candidate.TaskEpoch > timeline.LatestTaskEpoch {
		timeline.LatestTaskEpoch = candidate.TaskEpoch
	}

	for index := range timeline.Segments {
		current := &timeline.Segments[index]
		if current.SegmentID != candidate.SegmentID {
			continue
		}
		if candidate.TaskEpoch < current.TaskEpoch ||
			(candidate.TaskEpoch == current.TaskEpoch && candidate.Revision < current.Revision) ||
			(candidate.TaskEpoch == current.TaskEpoch && candidate.Revision == current.Revision && current.Final && !candidate.Final) {
			a.dropSpeechSegment("drop_stale_speech_segment")
			return
		}
		*current = *candidate
		a.state.LastDecision = "record_speech_candidate"
		sortShadowSpeechSegments(timeline.Segments)
		return
	}

	if len(timeline.Segments) >= maxShadowSpeechSegments {
		a.dropSpeechSegment("drop_speech_capacity")
		return
	}
	timeline.Segments = append(timeline.Segments, *candidate)
	sortShadowSpeechSegments(timeline.Segments)
	a.state.LastDecision = "record_speech_candidate"
}

func sortShadowSpeechSegments(segments []ShadowSpeechSegment) {
	sort.Slice(segments, func(left, right int) bool {
		if segments[left].CaptureStartSample != segments[right].CaptureStartSample {
			return segments[left].CaptureStartSample < segments[right].CaptureStartSample
		}
		if segments[left].CaptureEndSample != segments[right].CaptureEndSample {
			return segments[left].CaptureEndSample < segments[right].CaptureEndSample
		}
		return segments[left].SegmentID < segments[right].SegmentID
	})
}

func (a *LiveSessionActor) dropSpeechSegment(decision string) {
	a.state.DroppedEvents++
	a.state.SpeechTimeline.DroppedSegments++
	a.state.LastDecision = decision
}
