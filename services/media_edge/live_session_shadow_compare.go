package mediaedge

import (
	"slices"
	"time"
)

func fenceBefore(left, right Fence) bool {
	if left.TurnID != right.TurnID {
		return left.TurnID < right.TurnID
	}
	if left.GenerationID != right.GenerationID {
		return left.GenerationID < right.GenerationID
	}
	return left.ToolEpoch < right.ToolEpoch
}

func (a *LiveSessionActor) compare(event LiveSessionEvent) {
	if event.Authoritative == nil {
		return
	}
	reason := ""
	if event.Authoritative.StreamEpoch != a.state.StreamEpoch {
		reason = "stream_epoch"
	} else if event.CompareGeneration &&
		(!event.Authoritative.Generation.Equal(a.state.Generation) ||
			event.Authoritative.GenerationActive != a.state.GenerationActive) {
		reason = "generation"
	} else if event.Authoritative.Floor != "" && event.Authoritative.Floor != a.state.Floor {
		reason = "floor"
	} else if event.Authoritative.LastDecision != "" && event.Authoritative.LastDecision != a.state.LastDecision {
		reason = "decision"
	} else if event.ComparePlayout && event.Authoritative.PlayoutSample != a.state.PlayoutSample {
		reason = "playout"
	} else if event.CompareTimeline && !equalShadowSpeechTimeline(
		event.Authoritative.SpeechTimeline,
		a.state.SpeechTimeline,
	) {
		reason = "speech_timeline"
	} else if event.CompareOutput && !equalShadowOutputArbiter(
		event.Authoritative.OutputArbiter,
		a.state.OutputArbiter,
	) {
		reason = "output_arbiter"
	}
	comparison := ShadowComparison{
		Sequence:             event.mailboxSequence,
		Kind:                 event.Kind,
		Scenario:             event.Scenario,
		ContractVersion:      event.ContractVersion,
		AuthoritativeReason:  event.AuthoritativeReason,
		CandidateReason:      a.state.LastDecision,
		ObservedAtUnixMillis: time.Now().UnixMilli(),
		MailboxAgeMillis:     float64(time.Since(event.enqueuedAt)) / float64(time.Millisecond),
		Mismatch:             reason != "",
		Reason:               reason,
		Authoritative:        cloneLiveSessionSnapshot(*event.Authoritative, false),
		Candidate:            cloneLiveSessionSnapshot(a.state, false),
	}
	comparison.Authoritative.ShadowMismatchCounts = nil
	comparison.Candidate.ShadowMismatchCounts = nil
	if comparison.Mismatch {
		a.state.ShadowMismatchTotal++
		a.state.ShadowMismatchCounts = mergeShadowMismatchCounts(
			a.state.ShadowMismatchCounts,
			[]ShadowMismatchCount{{
				Scenario: event.Scenario, ContractVersion: event.ContractVersion, Count: 1,
			}},
		)
	}
	if len(a.state.RecentComparisons) == maxShadowComparisons {
		a.state.RecentComparisons = a.state.RecentComparisons[1:]
	}
	a.state.RecentComparisons = append(a.state.RecentComparisons, comparison)
}

func equalShadowSpeechTimeline(left, right ShadowSpeechTimeline) bool {
	return left.CommittedSample == right.CommittedSample &&
		left.LatestTaskEpoch == right.LatestTaskEpoch &&
		slices.Equal(left.Segments, right.Segments)
}

func equalShadowOutputArbiter(left, right ShadowOutputArbiter) bool {
	if left.ContextVersion != right.ContextVersion {
		return false
	}
	if left.Candidate == nil || right.Candidate == nil {
		if left.Candidate != nil || right.Candidate != nil {
			return false
		}
	} else if *left.Candidate != *right.Candidate {
		return false
	}
	return !left.CandidatesComplete || slices.Equal(left.Candidates, right.Candidates)
}

func mergeShadowMismatchCounts(
	target []ShadowMismatchCount,
	additions []ShadowMismatchCount,
) []ShadowMismatchCount {
	for _, addition := range additions {
		found := false
		for index := range target {
			count := &target[index]
			if count.Scenario == addition.Scenario && count.ContractVersion == addition.ContractVersion {
				count.Count += addition.Count
				found = true
				break
			}
		}
		if !found {
			for index := range target {
				if target[index].Scenario == "other" && target[index].ContractVersion == "mixed" {
					target[index].Count += addition.Count
					found = true
					break
				}
			}
		}
		if !found && len(target) < maxShadowMismatchCounts-1 {
			target = append(target, addition)
		} else if !found && len(target) < maxShadowMismatchCounts {
			target = append(target, ShadowMismatchCount{
				Scenario: "other", ContractVersion: "mixed", Count: addition.Count,
			})
		} else if !found {
			target[maxShadowMismatchCounts-1].Count += addition.Count
		}
	}
	return target
}
