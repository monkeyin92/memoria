package mediaedge

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
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
	critical     chan LiveSessionEvent
	audio        chan LiveSessionEvent
	bulk         chan LiveSessionEvent
	mailboxSize  int
	audioReserve int
	deadline     time.Duration
	processDelay time.Duration

	submitMu        sync.Mutex
	closed          bool
	nextSequence    uint64
	pending         atomic.Int64
	criticalPending atomic.Int64
	audioPending    atomic.Int64
	bulkPending     atomic.Int64

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
	controlCapacity := mailboxSize - audioReserve
	a := &LiveSessionActor{
		ctx:                      ctx,
		cancel:                   cancel,
		done:                     make(chan struct{}),
		critical:                 make(chan LiveSessionEvent, controlCapacity),
		audio:                    make(chan LiveSessionEvent, mailboxSize),
		bulk:                     make(chan LiveSessionEvent, controlCapacity),
		mailboxSize:              mailboxSize,
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
