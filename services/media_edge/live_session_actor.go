package mediaedge

import (
	"context"
	"errors"
	"slices"
	"sort"
	"sync"
	"time"
)

var (
	ErrActorClosed      = errors.New("live session actor is closed")
	ErrActorMailboxFull = errors.New("live session actor mailbox is full")
)

const (
	defaultActorMailboxSize  = 128
	defaultActorAudioReserve = 32
	defaultFrameDeadline     = 20 * time.Millisecond
	maxShadowComparisons     = 64
	maxShadowMismatchCounts  = 64
	maxShadowSpeechSegments  = 128
	maxShadowOutputPerDomain = 4
	maxShadowOutputEvaluated = 1024
	shadowContractVersion    = "media-v1-a5"
	shadowA6AContractVersion = "media-v1-a6a"
)

type ShadowFloorState string

const (
	ShadowFloorSilence   ShadowFloorState = "silence"
	ShadowFloorUser      ShadowFloorState = "user_holds_floor"
	ShadowFloorAssistant ShadowFloorState = "assistant_holds_floor"
	ShadowFloorOverlap   ShadowFloorState = "overlap"
	ShadowFloorUncertain ShadowFloorState = "uncertain"
)

type LiveSessionEventKind string

const (
	LiveEventAudioUplink       LiveSessionEventKind = "audio_uplink"
	LiveEventAudioDownlink     LiveSessionEventKind = "audio_downlink"
	LiveEventVADStart          LiveSessionEventKind = "vad_start"
	LiveEventVADEnd            LiveSessionEventKind = "vad_end"
	LiveEventGenerationStart   LiveSessionEventKind = "generation_start"
	LiveEventGenerationCancel  LiveSessionEventKind = "generation_cancel"
	LiveEventPlaybackProgress  LiveSessionEventKind = "playback_progress"
	LiveEventAuthoritySnapshot LiveSessionEventKind = "python_authority"
	LiveEventSpeechTaskStart   LiveSessionEventKind = "speech_task_started"
	LiveEventSpeechSegment     LiveSessionEventKind = "speech_segment"
	LiveEventSpeechCommit      LiveSessionEventKind = "speech_commit"
	LiveEventContextActivate   LiveSessionEventKind = "context_activate"
	LiveEventOutputIntent      LiveSessionEventKind = "output_intent"
)

// ShadowSpeechSegment is a range-stamped, candidate-only SpeechTimeline entry.
// It is never projected into conversation history by the Go shadow actor.
type ShadowSpeechSegment struct {
	SegmentID          string `json:"segment_id"`
	CaptureStartSample uint64 `json:"capture_start_sample"`
	CaptureEndSample   uint64 `json:"capture_end_sample"`
	TaskEpoch          uint64 `json:"task_epoch"`
	Revision           uint64 `json:"revision"`
	Text               string `json:"text,omitempty"`
	TextSHA256         string `json:"text_sha256,omitempty"`
	Final              bool   `json:"final"`
}

type ShadowSpeechTimeline struct {
	CommittedSample uint64                `json:"committed_sample"`
	LatestTaskEpoch uint64                `json:"latest_task_epoch"`
	DroppedSegments uint64                `json:"dropped_segments"`
	Segments        []ShadowSpeechSegment `json:"segments,omitempty"`
}

// ShadowOutputIntentKind is the dormant arbiter's priority domain, not a wire
// replacement for media.v1.OutputIntentKind. Bridge wiring must map the proto
// kinds and control effects explicitly when A6A is connected to live traffic.
type ShadowOutputIntentKind string

const (
	ShadowOutputFastAck      ShadowOutputIntentKind = "fast_acknowledgement"
	ShadowOutputConversation ShadowOutputIntentKind = "conversation"
	ShadowOutputTool         ShadowOutputIntentKind = "tool"
	ShadowOutputDeep         ShadowOutputIntentKind = "deep"
	ShadowOutputReminder     ShadowOutputIntentKind = "reminder"
	ShadowOutputNotification ShadowOutputIntentKind = "notification"
)

type ShadowOutputFloorRequirement string

const ShadowOutputFloorAvailable ShadowOutputFloorRequirement = "available"

type ShadowOutputPlaybackRequirement string

const ShadowOutputPlaybackCurrentGeneration ShadowOutputPlaybackRequirement = "current_generation"

// ShadowOutputIntent is observable candidate state only. The actor has no
// sender, tool, or persistence dependency through which it could execute it.
type ShadowOutputIntent struct {
	IntentID            string                          `json:"intent_id"`
	Fence               Fence                           `json:"fence"`
	Kind                ShadowOutputIntentKind          `json:"kind"`
	Priority            uint32                          `json:"priority"`
	DomainRank          uint8                           `json:"domain_rank"`
	CreatedAtUnixMillis int64                           `json:"created_at_unix_ms"`
	ExpiresAtUnixMillis int64                           `json:"expires_at_unix_ms"`
	ContextVersion      uint64                          `json:"context_version"`
	FloorRequirement    ShadowOutputFloorRequirement    `json:"floor_requirement"`
	PlaybackRequirement ShadowOutputPlaybackRequirement `json:"playback_requirement"`
	CandidateOnly       bool                            `json:"candidate_only"`
}

type ShadowOutputArbiter struct {
	ContextVersion     uint64               `json:"context_version"`
	DroppedIntents     uint64               `json:"dropped_intents"`
	Candidate          *ShadowOutputIntent  `json:"candidate,omitempty"`
	Candidates         []ShadowOutputIntent `json:"active_candidates,omitempty"`
	CandidatesComplete bool                 `json:"active_candidates_complete"`
}

type LiveSessionEvent struct {
	Kind                        LiveSessionEventKind
	StreamEpoch                 uint64
	Fence                       Fence
	SamplePosition              uint64
	Audio                       bool
	Authoritative               *LiveSessionSnapshot
	CompareGeneration           bool
	ComparePlayout              bool
	CompareTimeline             bool
	CompareOutput               bool
	ConsumeOutput               bool
	Scenario                    string
	ContractVersion             string
	AuthoritativeReason         string
	SpeechSegment               *ShadowSpeechSegment
	TaskEpoch                   uint64
	CommitSample                uint64
	ContextVersion              uint64
	OutputIntent                *ShadowOutputIntent
	AuthoritativeOutputState    bool
	AuthoritativeOutputAccepted bool
	AuthoritativeOutputConsumed bool
	OutputIntentInvalid         bool
	ObservedAtUnixMS            int64
	ResyncFromAuthoritative     bool
	enqueuedAt                  time.Time
	mailboxSequence             uint64
}

type ShadowComparison struct {
	Sequence             uint64               `json:"sequence"`
	Kind                 LiveSessionEventKind `json:"kind"`
	Scenario             string               `json:"scenario,omitempty"`
	ContractVersion      string               `json:"contract_version"`
	AuthoritativeReason  string               `json:"authoritative_reason,omitempty"`
	CandidateReason      string               `json:"candidate_reason,omitempty"`
	ObservedAtUnixMillis int64                `json:"observed_at_unix_ms"`
	MailboxAgeMillis     float64              `json:"mailbox_age_ms"`
	Mismatch             bool                 `json:"mismatch"`
	Reason               string               `json:"reason,omitempty"`
	Authoritative        LiveSessionSnapshot  `json:"authoritative"`
	Candidate            LiveSessionSnapshot  `json:"candidate"`
}

type ShadowMismatchCount struct {
	Scenario        string `json:"scenario"`
	ContractVersion string `json:"contract_version"`
	Count           uint64 `json:"count"`
}

type LiveSessionSnapshot struct {
	SessionID                  string                `json:"session_id"`
	StreamEpoch                uint64                `json:"stream_epoch"`
	Floor                      ShadowFloorState      `json:"floor"`
	Generation                 Fence                 `json:"generation"`
	GenerationActive           bool                  `json:"generation_active"`
	PlayoutSample              uint64                `json:"playout_sample"`
	LastDecision               string                `json:"last_decision,omitempty"`
	LastMailboxSequence        uint64                `json:"last_mailbox_sequence"`
	ProcessedEvents            uint64                `json:"processed_events"`
	ProcessedAudioFrames       uint64                `json:"processed_audio_frames"`
	DroppedEvents              uint64                `json:"dropped_events"`
	FrameDeadlineMisses        uint64                `json:"frame_deadline_misses"`
	MaxMailboxAgeMillis        float64               `json:"max_mailbox_age_ms"`
	FloorDecisionLatencyMillis float64               `json:"floor_decision_latency_ms"`
	ShadowMismatchTotal        uint64                `json:"shadow_mismatch_total"`
	ShadowMismatchCounts       []ShadowMismatchCount `json:"shadow_mismatch_counts,omitempty"`
	MailboxDepth               int                   `json:"mailbox_depth"`
	RecentComparisons          []ShadowComparison    `json:"recent_comparisons,omitempty"`
	SpeechTimeline             ShadowSpeechTimeline  `json:"speech_timeline"`
	OutputArbiter              ShadowOutputArbiter   `json:"output_arbiter"`
}

type LiveSessionActor struct {
	ctx          context.Context
	cancel       context.CancelFunc
	done         chan struct{}
	mailbox      chan LiveSessionEvent
	audioReserve int
	deadline     time.Duration
	processDelay time.Duration

	submitMu     sync.Mutex
	closed       bool
	nextSequence uint64

	stateMu sync.RWMutex
	state   LiveSessionSnapshot

	outputCandidates         map[string]ShadowOutputIntent
	outputCandidatesComplete bool
	outputEvaluated          map[string]struct{}
	outputEvaluatedOrder     []string
}

type shadowOutputWatermark struct {
	priority uint32
	created  int64
	intentID string
}

func NewLiveSessionActor(sessionID string, streamEpoch uint64) *LiveSessionActor {
	return newLiveSessionActor(
		sessionID,
		streamEpoch,
		defaultActorMailboxSize,
		defaultActorAudioReserve,
		defaultFrameDeadline,
		0,
	)
}

func newLiveSessionActor(
	sessionID string,
	streamEpoch uint64,
	mailboxSize int,
	audioReserve int,
	deadline time.Duration,
	processDelay time.Duration,
) *LiveSessionActor {
	if mailboxSize <= 0 || audioReserve < 0 || audioReserve >= mailboxSize || deadline <= 0 || processDelay < 0 {
		panic("invalid live session actor configuration")
	}
	ctx, cancel := context.WithCancel(context.Background())
	a := &LiveSessionActor{
		ctx:                      ctx,
		cancel:                   cancel,
		done:                     make(chan struct{}),
		mailbox:                  make(chan LiveSessionEvent, mailboxSize),
		audioReserve:             audioReserve,
		deadline:                 deadline,
		processDelay:             processDelay,
		outputCandidates:         make(map[string]ShadowOutputIntent),
		outputCandidatesComplete: true,
		outputEvaluated:          make(map[string]struct{}),
		state: LiveSessionSnapshot{
			SessionID:   sessionID,
			StreamEpoch: streamEpoch,
			Floor:       ShadowFloorSilence,
			Generation:  Fence{SessionID: sessionID},
		},
	}
	go a.run()
	return a
}

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
	if !isAudio && len(a.mailbox) >= cap(a.mailbox)-a.audioReserve {
		a.recordDrop(false)
		return ErrActorMailboxFull
	}
	event.enqueuedAt = time.Now()
	a.nextSequence++
	event.mailboxSequence = a.nextSequence
	select {
	case a.mailbox <- event:
		return nil
	default:
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
	snapshot.MailboxDepth = len(a.mailbox)
	return snapshot
}

func cloneLiveSessionSnapshot(source LiveSessionSnapshot, includeComparisons bool) LiveSessionSnapshot {
	clone := source
	clone.ShadowMismatchCounts = append([]ShadowMismatchCount(nil), source.ShadowMismatchCounts...)
	clone.SpeechTimeline.Segments = append(
		[]ShadowSpeechSegment(nil), source.SpeechTimeline.Segments...,
	)
	if source.OutputArbiter.Candidate != nil {
		candidate := *source.OutputArbiter.Candidate
		clone.OutputArbiter.Candidate = &candidate
	}
	clone.OutputArbiter.Candidates = append(
		[]ShadowOutputIntent(nil), source.OutputArbiter.Candidates...,
	)
	clone.RecentComparisons = nil
	if includeComparisons {
		clone.RecentComparisons = make([]ShadowComparison, len(source.RecentComparisons))
		for index, comparison := range source.RecentComparisons {
			clone.RecentComparisons[index] = comparison
			clone.RecentComparisons[index].Authoritative = cloneLiveSessionSnapshot(
				comparison.Authoritative, false,
			)
			clone.RecentComparisons[index].Candidate = cloneLiveSessionSnapshot(
				comparison.Candidate, false,
			)
		}
	}
	return clone
}

func (a *LiveSessionActor) run() {
	defer func() {
		for {
			select {
			case event := <-a.mailbox:
				a.recordDrained(event)
			default:
				close(a.done)
				return
			}
		}
	}()
	for {
		select {
		case <-a.ctx.Done():
			return
		case event := <-a.mailbox:
			if a.processDelay > 0 {
				select {
				case <-a.ctx.Done():
					a.recordDrained(event)
					return
				case <-time.After(a.processDelay):
				}
			}
			a.apply(event)
		}
	}
}

func (a *LiveSessionActor) apply(event LiveSessionEvent) {
	age := time.Since(event.enqueuedAt)
	a.stateMu.Lock()
	defer a.stateMu.Unlock()
	a.state.ProcessedEvents++
	if event.Audio {
		a.state.ProcessedAudioFrames++
	}
	a.state.LastMailboxSequence = event.mailboxSequence
	if millis := float64(age) / float64(time.Millisecond); millis > a.state.MaxMailboxAgeMillis {
		a.state.MaxMailboxAgeMillis = millis
	}
	if (event.Kind == LiveEventVADStart || event.Kind == LiveEventVADEnd) &&
		float64(age)/float64(time.Millisecond) > a.state.FloorDecisionLatencyMillis {
		a.state.FloorDecisionLatencyMillis = float64(age) / float64(time.Millisecond)
	}
	if (requiresExactStreamEpoch(event.Kind) && event.StreamEpoch != a.state.StreamEpoch) ||
		(!requiresExactStreamEpoch(event.Kind) && event.StreamEpoch != 0 && event.StreamEpoch != a.state.StreamEpoch) ||
		((event.Kind == LiveEventGenerationStart || event.Kind == LiveEventGenerationCancel) &&
			(event.Fence.SessionID != a.state.SessionID ||
				fenceBefore(event.Fence, a.state.Generation) ||
				(event.Kind == LiveEventGenerationStart &&
					event.Fence.Equal(a.state.Generation) && !a.state.GenerationActive))) {
		a.state.DroppedEvents++
		if event.Kind == LiveEventSpeechSegment || event.Kind == LiveEventSpeechCommit {
			a.state.SpeechTimeline.DroppedSegments++
		}
		if event.Kind == LiveEventOutputIntent {
			a.state.OutputArbiter.DroppedIntents++
		}
		a.state.LastDecision = "drop_stale_event"
		a.compare(event)
		return
	}
	if event.StreamEpoch != 0 {
		a.state.StreamEpoch = event.StreamEpoch
	}
	if age > a.deadline && event.Audio {
		a.state.FrameDeadlineMisses++
		a.state.DroppedEvents++
		a.state.LastDecision = "drop_stale_event"
		a.compare(event)
		return
	}
	if event.ResyncFromAuthoritative && event.Authoritative != nil {
		if event.CompareTimeline {
			dropped := a.state.SpeechTimeline.DroppedSegments
			a.state.SpeechTimeline = event.Authoritative.SpeechTimeline
			a.state.SpeechTimeline.Segments = append(
				[]ShadowSpeechSegment(nil), event.Authoritative.SpeechTimeline.Segments...,
			)
			a.state.SpeechTimeline.DroppedSegments = dropped
		}
		if event.CompareOutput {
			dropped := a.state.OutputArbiter.DroppedIntents
			a.replaceOutputCandidates(event.Authoritative.OutputArbiter)
			a.refreshOutputCandidate(event.ObservedAtUnixMS)
			a.state.OutputArbiter.DroppedIntents = dropped
			if event.Kind == LiveEventOutputIntent &&
				!event.AuthoritativeOutputAccepted && !event.AuthoritativeOutputConsumed {
				if event.OutputIntentInvalid {
					a.state.LastDecision = "drop_invalid_output_intent"
				} else {
					a.state.LastDecision = "drop_authoritative_output"
				}
			}
		}
		if event.Kind == LiveEventAuthoritySnapshot && event.Authoritative.Floor != "" {
			a.state.Floor = event.Authoritative.Floor
			a.state.LastDecision = event.Authoritative.LastDecision
		} else if event.Kind != LiveEventOutputIntent ||
			event.AuthoritativeOutputAccepted || event.AuthoritativeOutputConsumed {
			a.state.LastDecision = "resync_shadow_transport"
		}
		a.compare(event)
		return
	}
	switch event.Kind {
	case LiveEventVADStart:
		a.clearOutputCandidate()
		if a.state.GenerationActive {
			a.state.Floor = ShadowFloorOverlap
		} else {
			a.state.Floor = ShadowFloorUser
		}
		a.state.OutputArbiter.Candidate = nil
		a.state.LastDecision = "duck_output"
	case LiveEventVADEnd:
		if a.state.GenerationActive {
			a.state.Floor = ShadowFloorAssistant
		} else {
			a.state.Floor = ShadowFloorSilence
		}
		a.state.LastDecision = "continue_output"
	case LiveEventGenerationStart:
		if !event.Fence.Equal(a.state.Generation) {
			a.state.PlayoutSample = 0
			a.clearOutputCandidate()
		}
		a.state.Generation = event.Fence
		a.state.GenerationActive = true
		if a.state.Floor == ShadowFloorUser {
			a.state.Floor = ShadowFloorOverlap
		} else {
			a.state.Floor = ShadowFloorAssistant
		}
		a.state.LastDecision = "enqueue_output_intent"
	case LiveEventGenerationCancel:
		a.state.Generation = event.Fence
		a.state.GenerationActive = false
		a.clearOutputCandidate()
		if a.state.Floor == ShadowFloorOverlap {
			a.state.Floor = ShadowFloorUser
		} else {
			a.state.Floor = ShadowFloorSilence
		}
		a.state.LastDecision = "drop_stale_event"
	case LiveEventPlaybackProgress:
		if a.state.GenerationActive &&
			event.Fence.Equal(a.state.Generation) &&
			event.SamplePosition >= a.state.PlayoutSample {
			a.state.PlayoutSample = event.SamplePosition
		} else {
			a.state.LastDecision = "drop_stale_event"
		}
	case LiveEventAudioDownlink:
		if !a.state.GenerationActive ||
			!event.Fence.Equal(a.state.Generation) ||
			a.state.Floor == ShadowFloorUser ||
			a.state.Floor == ShadowFloorOverlap {
			a.state.LastDecision = "drop_stale_event"
		} else {
			a.state.LastDecision = "enqueue_output_intent"
		}
	case LiveEventAudioUplink:
		// Frame accounting only; VAD owns the shadow floor decision.
	case LiveEventAuthoritySnapshot:
		// Python authority is comparison input only in go_shadow.
	case LiveEventSpeechTaskStart:
		a.applySpeechTaskStart(event.TaskEpoch)
	case LiveEventSpeechSegment:
		a.applySpeechSegment(event.SpeechSegment)
	case LiveEventSpeechCommit:
		a.applySpeechCommit(event.CommitSample)
	case LiveEventContextActivate:
		a.applyContextVersion(event.ContextVersion)
	case LiveEventOutputIntent:
		now := event.ObservedAtUnixMS
		if now <= 0 {
			now = time.Now().UnixMilli()
		}
		if event.AuthoritativeOutputConsumed {
			a.consumeOutputIntent(event.OutputIntent, now)
		} else if event.OutputIntentInvalid {
			a.refreshOutputCandidate(now)
			a.dropOutputIntent("drop_invalid_output_intent")
		} else {
			a.applyOutputIntent(event.OutputIntent, now)
		}
	}
	a.compare(event)
	if event.ConsumeOutput {
		a.consumeOutputIntent(event.OutputIntent, event.ObservedAtUnixMS)
	}
}

func requiresExactStreamEpoch(kind LiveSessionEventKind) bool {
	switch kind {
	case LiveEventSpeechTaskStart, LiveEventSpeechSegment, LiveEventSpeechCommit, LiveEventContextActivate, LiveEventOutputIntent:
		return true
	default:
		return false
	}
}

func (a *LiveSessionActor) applySpeechTaskStart(taskEpoch uint64) {
	timeline := &a.state.SpeechTimeline
	if taskEpoch == 0 || taskEpoch < timeline.LatestTaskEpoch {
		a.dropSpeechSegment("drop_stale_speech_task")
		return
	}
	timeline.LatestTaskEpoch = taskEpoch
	a.state.LastDecision = "record_speech_task"
}

func (a *LiveSessionActor) applyContextVersion(contextVersion uint64) {
	output := &a.state.OutputArbiter
	if contextVersion < output.ContextVersion {
		a.state.DroppedEvents++
		a.state.LastDecision = "drop_stale_context"
		return
	}
	if contextVersion > output.ContextVersion {
		output.ContextVersion = contextVersion
		a.clearOutputCandidate()
	}
	a.state.LastDecision = "record_context_candidate"
}

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
		input.ExpiresAtUnixMillis <= nowUnixMillis || input.CreatedAtUnixMillis > input.ExpiresAtUnixMillis ||
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
