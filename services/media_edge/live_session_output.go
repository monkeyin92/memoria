package mediaedge

import (
	"sort"
	"time"
)

// The output reducer is candidate-only state. Keeping it separate from the
// mailbox loop makes the authority boundary obvious: it never owns a sender.
func (a *LiveSessionActor) applyOutputIntent(input *ShadowOutputIntent, nowUnixMillis int64) {
	rank, ranked := shadowOutputDomainRank(input)
	output := &a.state.OutputArbiter
	a.refreshOutputCandidate(nowUnixMillis)
	if !ranked || input.IntentID == "" {
		a.dropOutputIntent("drop_invalid_output_intent")
		return
	}
	if !a.recordOutputEvaluation(input.IntentID) {
		a.dropOutputIntent("drop_stale_output_intent")
		return
	}
	if input.CreatedAtUnixMillis <= 0 ||
		input.CreatedAtUnixMillis > nowUnixMillis ||
		input.ExpiresAtUnixMillis <= nowUnixMillis ||
		input.CreatedAtUnixMillis > input.ExpiresAtUnixMillis ||
		!input.Fence.Equal(a.state.Generation) || !a.state.GenerationActive ||
		input.ContextVersion != output.ContextVersion ||
		input.FloorRequirement != ShadowOutputFloorAvailable ||
		input.PlaybackRequirement != ShadowOutputPlaybackCurrentGeneration ||
		a.state.Floor == ShadowFloorUser || a.state.Floor == ShadowFloorOverlap {
		a.dropOutputIntent("drop_invalid_output_intent")
		return
	}

	candidate := *input
	candidate.DomainRank = rank
	candidate.CandidateOnly = true
	a.outputCandidates[candidate.IntentID] = candidate
	a.trimOutputCandidates(rank)
	a.refreshOutputCandidate(nowUnixMillis)
	if _, retained := a.outputCandidates[candidate.IntentID]; !retained {
		a.dropOutputIntent("drop_superseded_output_intent")
		return
	}
	a.state.LastDecision = "record_output_candidate"
}

func (a *LiveSessionActor) refreshOutputCandidate(nowUnixMillis int64) {
	for intentID, candidate := range a.outputCandidates {
		if candidate.ExpiresAtUnixMillis <= nowUnixMillis {
			delete(a.outputCandidates, intentID)
		}
	}
	candidates := make([]ShadowOutputIntent, 0, len(a.outputCandidates))
	for _, candidate := range a.outputCandidates {
		candidates = append(candidates, candidate)
	}
	sortShadowOutputCandidates(candidates)
	a.state.OutputArbiter.Candidates = candidates
	a.state.OutputArbiter.CandidatesComplete = a.outputCandidatesComplete
	a.state.OutputArbiter.Candidate = nil
	if len(candidates) > 0 {
		winner := candidates[0]
		a.state.OutputArbiter.Candidate = &winner
	}
}

func (a *LiveSessionActor) consumeOutputIntent(input *ShadowOutputIntent, nowUnixMillis int64) {
	if input != nil {
		delete(a.outputCandidates, input.IntentID)
	}
	if nowUnixMillis <= 0 {
		nowUnixMillis = time.Now().UnixMilli()
	}
	a.refreshOutputCandidate(nowUnixMillis)
	a.state.LastDecision = "consume_output_intent"
}

func (a *LiveSessionActor) replaceOutputCandidates(output ShadowOutputArbiter) {
	a.state.OutputArbiter = output
	a.outputCandidates = make(map[string]ShadowOutputIntent)
	a.outputCandidatesComplete = output.CandidatesComplete
	a.outputEvaluated = make(map[string]struct{})
	a.outputEvaluatedOrder = nil
	candidates := output.Candidates
	if !output.CandidatesComplete && output.Candidate != nil {
		candidates = []ShadowOutputIntent{*output.Candidate}
	}
	for _, candidate := range candidates {
		a.outputCandidates[candidate.IntentID] = candidate
		a.outputEvaluated[candidate.IntentID] = struct{}{}
		a.outputEvaluatedOrder = append(a.outputEvaluatedOrder, candidate.IntentID)
	}
}

func (a *LiveSessionActor) recordOutputEvaluation(intentID string) bool {
	if _, exists := a.outputEvaluated[intentID]; exists {
		return false
	}
	if len(a.outputEvaluatedOrder) == maxShadowOutputEvaluated {
		delete(a.outputEvaluated, a.outputEvaluatedOrder[0])
		a.outputEvaluatedOrder = a.outputEvaluatedOrder[1:]
	}
	a.outputEvaluated[intentID] = struct{}{}
	a.outputEvaluatedOrder = append(a.outputEvaluatedOrder, intentID)
	return true
}

func (a *LiveSessionActor) trimOutputCandidates(rank uint8) {
	candidates := make([]ShadowOutputIntent, 0, maxShadowOutputPerDomain+1)
	for _, candidate := range a.outputCandidates {
		if candidate.DomainRank == rank {
			candidates = append(candidates, candidate)
		}
	}
	sortShadowOutputCandidates(candidates)
	if len(candidates) <= maxShadowOutputPerDomain {
		return
	}
	for _, candidate := range candidates[maxShadowOutputPerDomain:] {
		delete(a.outputCandidates, candidate.IntentID)
	}
}

func shadowOutputDomainRank(intent *ShadowOutputIntent) (uint8, bool) {
	if intent == nil {
		return 0, false
	}
	switch intent.Kind {
	case ShadowOutputFastAck:
		return 4, true
	case ShadowOutputConversation:
		return 3, true
	case ShadowOutputTool, ShadowOutputDeep:
		return 2, true
	case ShadowOutputReminder, ShadowOutputNotification:
		return 1, true
	default:
		return 0, false
	}
}

func newerOutputIntent(candidate, current ShadowOutputIntent) bool {
	return newerOutputWatermark(
		shadowOutputWatermark{candidate.Priority, candidate.CreatedAtUnixMillis, candidate.IntentID},
		shadowOutputWatermark{current.Priority, current.CreatedAtUnixMillis, current.IntentID},
	)
}

func newerOutputWatermark(candidate, current shadowOutputWatermark) bool {
	if candidate.priority != current.priority {
		return candidate.priority > current.priority
	}
	if candidate.created != current.created {
		return candidate.created > current.created
	}
	return candidate.intentID > current.intentID
}

func sortShadowOutputCandidates(candidates []ShadowOutputIntent) {
	sort.Slice(candidates, func(left, right int) bool {
		if candidates[left].DomainRank != candidates[right].DomainRank {
			return candidates[left].DomainRank > candidates[right].DomainRank
		}
		return newerOutputIntent(candidates[left], candidates[right])
	})
}

func (a *LiveSessionActor) clearOutputCandidate() {
	a.state.OutputArbiter.Candidate = nil
	a.state.OutputArbiter.Candidates = nil
	a.state.OutputArbiter.CandidatesComplete = true
	a.outputCandidates = make(map[string]ShadowOutputIntent)
	a.outputCandidatesComplete = true
	a.outputEvaluated = make(map[string]struct{})
	a.outputEvaluatedOrder = nil
}

func (a *LiveSessionActor) dropOutputIntent(decision string) {
	a.state.DroppedEvents++
	a.state.OutputArbiter.DroppedIntents++
	a.state.LastDecision = decision
}
