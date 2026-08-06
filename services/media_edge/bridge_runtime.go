package mediaedge

// VoiceCoreMediaRuntime is the narrow session-level seam used by a media
// terminator.  It maps the edge's bounded Session state to the generated
// media-v1 stream; it does not decode RTP or run a provider.

import (
	"context"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"slices"
	"sync"
	"time"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

// CoreMediaStream is implemented by VoiceCoreSession and by deterministic
// local fakes.  A real WHIP/WebRTC adapter only needs to provide this contract.
type CoreMediaStream interface {
	SendAudio(AudioFrame) error
	Recv() (*mediav1.CoreToMedia, error)
	Close() error
}

type generationStopStream interface {
	CurrentFence() Fence
	SendStop(eventID, reason string, fence Fence, detectedAtMs uint64) error
}

type keywordStream interface {
	SendKeywordAtFence(keyword string, confidence float32, start, end uint64, hardStop bool, fence Fence, detectedAtMs uint64) error
}

type vadStream interface {
	SendVadWithVoicedEnd(sample, voicedEnd uint64, probability, rms, noiseFloor float32, start bool) error
}

type playbackProgressStream interface {
	SendPlaybackProgress(PlaybackProgress) error
}

type clientEventStream interface {
	SendClientEvent([]byte, string, Fence, uint64) error
}

type interactionAuthorityStream interface {
	InteractionAuthority() mediav1.InteractionAuthority
}

// DownlinkSender is implemented by the real media terminator. The context is
// cancelled before the Session generation gate closes, so a blocked encoder
// must abandon stale PCM without making CancelGeneration wait for it.
type DownlinkSender func(context.Context, AudioFrame) error

type stopAttempt struct {
	cancelled    Fence
	core         Fence
	reason       string
	detectedAtMs uint64
}

// VoiceCoreMediaRuntime owns one session's edge↔core forwarding loop.
type VoiceCoreMediaRuntime struct {
	session            *Session
	core               CoreMediaStream
	onEvent            func(*mediav1.CoreToMedia)
	onError            func(error)
	downlinkSender     DownlinkSender
	ctx                context.Context
	cancel             context.CancelFunc
	done               chan error
	once               sync.Once
	uplinkMu           sync.Mutex
	stopMu             sync.Mutex
	pendingStopID      string
	pendingStop        stopAttempt
	keywordMu          sync.Mutex
	sentKeywordFences  map[string]Fence
	sentKeywordOrder   []string
	lastShadowSequence uint64
	hasShadowSequence  bool
	resyncShadowSpeech bool
	resyncShadowOutput bool
	resyncShadowFloor  bool
}

func NewVoiceCoreMediaRuntime(
	ctx context.Context,
	session *Session,
	core CoreMediaStream,
	onEvent func(*mediav1.CoreToMedia),
	onError func(error),
) (*VoiceCoreMediaRuntime, error) {
	return newVoiceCoreMediaRuntime(ctx, session, core, nil, onEvent, onError)
}

func NewVoiceCoreMediaRuntimeWithDownlinkSender(
	ctx context.Context,
	session *Session,
	core CoreMediaStream,
	downlinkSender DownlinkSender,
	onEvent func(*mediav1.CoreToMedia),
	onError func(error),
) (*VoiceCoreMediaRuntime, error) {
	if downlinkSender == nil {
		return nil, fmt.Errorf("downlink sender is required")
	}
	return newVoiceCoreMediaRuntime(ctx, session, core, downlinkSender, onEvent, onError)
}

func newVoiceCoreMediaRuntime(
	ctx context.Context,
	session *Session,
	core CoreMediaStream,
	downlinkSender DownlinkSender,
	onEvent func(*mediav1.CoreToMedia),
	onError func(error),
) (*VoiceCoreMediaRuntime, error) {
	if session == nil || core == nil {
		return nil, fmt.Errorf("session and Voice Core stream are required")
	}
	streamCtx, cancel := context.WithCancel(ctx)
	return &VoiceCoreMediaRuntime{
		session:           session,
		core:              core,
		downlinkSender:    downlinkSender,
		onEvent:           onEvent,
		onError:           onError,
		ctx:               streamCtx,
		cancel:            cancel,
		done:              make(chan error, 1),
		sentKeywordFences: make(map[string]Fence),
	}, nil
}

func (r *VoiceCoreMediaRuntime) HasDownlinkSender() bool { return r.downlinkSender != nil }

func (r *VoiceCoreMediaRuntime) InteractionAuthority() mediav1.InteractionAuthority {
	provider, ok := r.core.(interactionAuthorityStream)
	if !ok {
		return mediav1.InteractionAuthority_INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE
	}
	authority, err := normalizeEffectiveInteractionAuthority(provider.InteractionAuthority())
	if err != nil {
		return mediav1.InteractionAuthority_INTERACTION_AUTHORITY_PYTHON_AUTHORITATIVE
	}
	return authority
}

func (r *VoiceCoreMediaRuntime) SendVAD(
	sample, voicedEnd uint64,
	probability, rms, noiseFloor float32,
	start bool,
) error {
	sender, ok := r.core.(vadStream)
	if !ok {
		return fmt.Errorf("voice-core stream does not support VAD events")
	}
	if err := sender.SendVadWithVoicedEnd(sample, voicedEnd, probability, rms, noiseFloor, start); err != nil {
		return err
	}
	r.session.MirrorVAD(start, sample)
	return nil
}

func (r *VoiceCoreMediaRuntime) SendPlaybackProgress(progress PlaybackProgress) error {
	sender, ok := r.core.(playbackProgressStream)
	if !ok {
		return fmt.Errorf("voice-core stream does not support playback progress")
	}
	if err := sender.SendPlaybackProgress(progress); err != nil {
		return err
	}
	r.session.MirrorPlayback(
		progress.RenderedSampleEnd,
		Fence{
			SessionID:    progress.SessionID,
			TurnID:       progress.TurnID,
			GenerationID: progress.GenerationID,
			ToolEpoch:    progress.ToolEpoch,
		},
	)
	return nil
}

// SendClientEvent forwards non-media browser events through the authenticated
// Voice Core stream. Stop and playback progress keep their dedicated methods
// because they also mutate the local generation/playout authority.
func (r *VoiceCoreMediaRuntime) SendClientEvent(
	raw []byte,
	eventType string,
	fence Fence,
	monotonicMS uint64,
) error {
	sender, ok := r.core.(clientEventStream)
	if !ok {
		return fmt.Errorf("voice-core stream does not support client events")
	}
	return r.session.withActiveGeneration(fence, func() error {
		return sender.SendClientEvent(raw, eventType, fence, monotonicMS)
	})
}

// SendKeyword applies the Session's local gate immediately around the bridge
// write so delayed hard-stop evidence cannot target a cancelled generation.
func (r *VoiceCoreMediaRuntime) SendKeyword(keyword string, confidence float32, start, end uint64, hardStop bool, fence Fence) error {
	sender, ok := r.core.(keywordStream)
	if !ok {
		return fmt.Errorf("voice-core stream does not support keyword events")
	}
	// Wall-clock detection time at the edge; Voice Core uses it as the
	// interrupt.detect anchor so the SLO covers local detection and the
	// Edge→Core network hop instead of only the Core arrival time.
	detectedAtMs := uint64(time.Now().UnixMilli())
	if err := validateKeyword(keyword, confidence, start, end, hardStop); err != nil {
		return err
	}
	if hardStop {
		eventID := fmt.Sprintf("\x00kws:%d:%d:%d", r.session.Epoch(), start, end)
		r.keywordMu.Lock()
		defer r.keywordMu.Unlock()
		if sentFence, sent := r.sentKeywordFences[eventID]; sent {
			if !sentFence.Equal(fence) {
				return fmt.Errorf("keyword event id belongs to another generation")
			}
			return nil
		}
		current, _, _, err := r.session.CancelGeneration(eventID, &fence)
		if err != nil {
			return err
		}
		if err := sender.SendKeywordAtFence(keyword, confidence, start, end, true, current, detectedAtMs); err != nil {
			return err
		}
		if len(r.sentKeywordOrder) == maxCancelResults {
			delete(r.sentKeywordFences, r.sentKeywordOrder[0])
			r.sentKeywordOrder = r.sentKeywordOrder[1:]
		}
		r.sentKeywordFences[eventID] = current
		r.sentKeywordOrder = append(r.sentKeywordOrder, eventID)
		return nil
	}
	return r.session.withActiveGeneration(fence, func() error {
		return sender.SendKeywordAtFence(keyword, confidence, start, end, false, fence, detectedAtMs)
	})
}

// CancelGeneration closes the Edge gate before asking Voice Core to cancel.
// A failed upstream send remains fail-closed and may be retried with the same
// event id and authoritative replacement fence. detectedAtMs is the edge-side
// interrupt.detect wall-clock stamp forwarded to Voice Core for SLO timing.
func (r *VoiceCoreMediaRuntime) CancelGeneration(eventID, reason string, expected *Fence, detectedAtMs uint64) (Fence, error) {
	if eventID == "" {
		return Fence{}, fmt.Errorf("stop event id is required")
	}
	stopper, ok := r.core.(generationStopStream)
	if !ok {
		return Fence{}, fmt.Errorf("voice-core stream does not support generation stop")
	}
	r.stopMu.Lock()
	defer r.stopMu.Unlock()
	if r.pendingStopID != "" {
		if r.pendingStopID != eventID {
			return Fence{}, fmt.Errorf("another generation stop is pending")
		}
		if expected != nil && !expected.Equal(r.pendingStop.core) {
			return Fence{}, fmt.Errorf("stop event id was reused for another generation")
		}
		if stopper.CurrentFence().Equal(r.pendingStop.cancelled) {
			cancelled := r.pendingStop.cancelled
			r.pendingStopID = ""
			r.pendingStop = stopAttempt{}
			return cancelled, nil
		}
		if err := stopper.SendStop(eventID, r.pendingStop.reason, r.pendingStop.core, r.pendingStop.detectedAtMs); err != nil {
			return Fence{}, err
		}
		cancelled := r.pendingStop.cancelled
		r.pendingStopID = ""
		r.pendingStop = stopAttempt{}
		return cancelled, nil
	}
	if current, cancelled, ok := r.session.cancelledGeneration(eventID); ok {
		if expected != nil && !expected.Equal(current) {
			return Fence{}, fmt.Errorf("stop event id was reused for another generation")
		}
		return cancelled, nil
	}
	core := stopper.CurrentFence()
	if expected != nil && !expected.Equal(core) {
		return Fence{}, fmt.Errorf("expected stop fence does not match Voice Core")
	}
	current, cancelled, replayed, err := r.session.CancelGeneration(eventID, &core)
	if err != nil {
		return Fence{}, err
	}
	if replayed {
		return cancelled, nil
	}
	if !current.Equal(core) {
		return Fence{}, fmt.Errorf("edge and voice-core generation fences diverged")
	}
	r.pendingStopID = eventID
	r.pendingStop = stopAttempt{cancelled: cancelled, core: core, reason: reason, detectedAtMs: detectedAtMs}
	if err := stopper.SendStop(eventID, reason, core, r.pendingStop.detectedAtMs); err != nil {
		return Fence{}, err
	}
	r.pendingStopID = ""
	r.pendingStop = stopAttempt{}
	return cancelled, nil
}

// Start launches the receive loop once.  SendUplink may be called after Start
// from the media terminator's capture loop.
func (r *VoiceCoreMediaRuntime) Start() {
	r.once.Do(func() {
		go r.receiveLoop()
	})
}

// SendUplink applies the edge sample/sequence gate, forwards the frame once,
// and retires it from the local queue only after the gRPC send succeeds.
func (r *VoiceCoreMediaRuntime) SendUplink(frame AudioFrame) error {
	r.uplinkMu.Lock()
	defer r.uplinkMu.Unlock()
	if err := r.session.AcceptUplink(frame); err != nil {
		return err
	}
	if err := r.core.SendAudio(frame); err != nil {
		// A failed send cannot be retried with the same sequence on this
		// stream. Retire it and force the caller to establish a new epoch.
		_ = r.session.AcknowledgeUplink(frame.Sequence)
		return err
	}
	return r.session.AcknowledgeUplink(frame.Sequence)
}

func (r *VoiceCoreMediaRuntime) Wait() error {
	return <-r.done
}

func (r *VoiceCoreMediaRuntime) Close() error {
	r.cancel()
	return r.core.Close()
}

func (r *VoiceCoreMediaRuntime) receiveLoop() {
	err := r.receive()
	r.done <- err
}

func (r *VoiceCoreMediaRuntime) receive() error {
	for {
		select {
		case <-r.ctx.Done():
			return nil
		default:
		}
		event, err := r.core.Recv()
		if err != nil {
			if r.ctx.Err() != nil {
				return nil
			}
			r.report(err)
			return err
		}
		if err := r.handleEvent(event); err != nil {
			r.report(err)
			return err
		}
	}
}

func (r *VoiceCoreMediaRuntime) handleEvent(event *mediav1.CoreToMedia) error {
	if event == nil {
		return fmt.Errorf("voice-core returned an empty event")
	}
	if observation := event.GetShadowObservation(); observation != nil {
		r.handleShadowObservation(observation)
		return nil
	}
	if effect := event.GetFloorEffect(); effect != nil {
		// Floor remains Python-owned through A6A. Candidate values and the
		// future Go-authoritative mode are never executable on this path.
		if effect.GetCandidateOnly() {
			return nil
		}
		if source, ok := r.core.(interactionAuthorityStream); ok &&
			source.InteractionAuthority() == mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE {
			return nil
		}
		if !r.session.IdentityMatches(effect.GetIdentity()) {
			return fmt.Errorf("voice-core floor effect identity does not match edge session")
		}
		fence, err := validateFloorEffect(effect)
		if err != nil {
			return err
		}
		floor, valid := floorStateFromProto(effect.GetFloorState())
		if !valid {
			return fmt.Errorf("voice-core floor effect state is invalid")
		}
		if err := r.session.ApplyFloorEffect(
			fence,
			floor,
			effect.GetFloorEpoch(),
			effect.GetSourceEventId(),
		); err != nil {
			return err
		}
		if r.onEvent != nil {
			r.onEvent(event)
		}
		return nil
	}
	if effect := event.GetRealtimeEffect(); effect != nil {
		// A candidate can never enter the executable path. Go authority remains
		// disabled until the A6B rollout gate is explicitly opened.
		if effect.GetCandidateOnly() {
			return nil
		}
		if source, ok := r.core.(interactionAuthorityStream); ok &&
			source.InteractionAuthority() == mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_AUTHORITATIVE {
			return nil
		}
		if !r.session.IdentityMatches(effect.GetIdentity()) {
			return fmt.Errorf("voice-core realtime effect identity does not match edge session")
		}
		sessionID, _, _, streamEpoch := r.session.IdentitySnapshot()
		if effect.GetSessionId() != sessionID || effect.GetStreamEpoch() != streamEpoch {
			return fmt.Errorf("voice-core realtime effect legacy identity does not match edge session")
		}
		fence, err := validateRealtimeEffect(effect)
		if err != nil {
			return err
		}
		if effect.GetEffectKind() == mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION {
			if err := r.session.ApplyCancelledGeneration(fence); err != nil {
				return err
			}
		} else if err := r.session.withActiveGeneration(fence, func() error { return nil }); err != nil {
			return err
		}
		if r.onEvent != nil {
			r.onEvent(event)
		}
		return nil
	}
	if generation := event.GetGeneration(); generation != nil {
		if !r.session.IdentityMatches(generation.GetIdentity()) {
			return fmt.Errorf("voice-core generation identity does not match edge session")
		}
		fence := Fence{
			SessionID:    r.sessionID(),
			TurnID:       generation.GetTurnId(),
			GenerationID: generation.GetGenerationId(),
			ToolEpoch:    generation.GetToolEpoch(),
		}
		if generation.GetAction() == mediav1.GenerationAction_GENERATION_ACTION_CANCEL {
			if err := r.session.ApplyCancelledGeneration(fence); err != nil {
				return err
			}
		} else if err := r.session.AdvanceGeneration(fence); err != nil {
			return err
		}
	}
	if audio := event.GetAudio(); audio != nil {
		if !r.session.IdentityMatches(audio.GetIdentity()) {
			return fmt.Errorf("voice-core audio identity does not match edge session")
		}
		sessionID, _, _, streamEpoch := r.session.IdentitySnapshot()
		frame := AudioFrame{
			SessionID:          sessionID,
			StreamEpoch:        streamEpoch,
			Sequence:           audio.GetSequence(),
			CaptureStartSample: audio.GetSourceStartSample(),
			FrameSamples:       uint64(audio.GetFrameSamples()),
			TurnID:             audio.GetTurnId(),
			GenerationID:       audio.GetGenerationId(),
			ToolEpoch:          audio.GetToolEpoch(),
			PayloadB64:         base64.StdEncoding.EncodeToString(audio.GetPcmS16Le()),
			Final:              audio.GetFinalFrame(),
		}
		if err := r.session.DeliverDownlink(frame, r.downlinkSender); errors.Is(err, ErrStaleDownlinkGeneration) {
			return nil
		} else if err != nil {
			return err
		}
	}
	if client := event.GetClient(); client != nil && client.GetType() == "assistant_state" {
		if !r.session.IdentityMatches(client.GetIdentity()) {
			return fmt.Errorf("voice-core client identity does not match edge session")
		}
		var envelope struct {
			Payload struct {
				Phase string `json:"phase"`
				State string `json:"state"`
			} `json:"payload"`
		}
		if err := json.Unmarshal(client.GetJsonPayload(), &envelope); err == nil {
			phase := envelope.Payload.Phase
			if phase == "" {
				phase = envelope.Payload.State
			}
			floor, decision, ok := pythonInteractionState(phase)
			if ok {
				r.session.MirrorPythonInteraction(
					floor,
					decision,
					phase,
					Fence{
						SessionID: r.sessionID(), TurnID: client.GetTurnId(),
						GenerationID: client.GetGenerationId(), ToolEpoch: client.GetToolEpoch(),
					},
				)
			}
		}
	}
	if r.onEvent != nil {
		r.onEvent(event)
	}
	return nil
}

func (r *VoiceCoreMediaRuntime) handleShadowObservation(observation *mediav1.ShadowObservation) {
	if r.InteractionAuthority() != mediav1.InteractionAuthority_INTERACTION_AUTHORITY_GO_SHADOW ||
		observation == nil || !observation.GetCandidateOnly() ||
		observation.GetContractVersion() != shadowA6AContractVersion ||
		!r.session.IdentityMatches(observation.GetIdentity()) ||
		(r.hasShadowSequence && observation.GetShadowSequence() <= r.lastShadowSequence) {
		return
	}
	shadowSequence := observation.GetShadowSequence()
	if (!r.hasShadowSequence && shadowSequence != 0) ||
		(r.hasShadowSequence && shadowSequence-r.lastShadowSequence > 1) {
		// The Python shadow lane is intentionally lossy. A sequence gap is a
		// transport discontinuity, not evidence that the Go algorithm diverged.
		// Each domain resynchronizes independently from its next complete
		// authoritative after-state so a dropped context observation cannot be
		// hidden by an unrelated Timeline update.
		r.resyncShadowSpeech = true
		r.resyncShadowOutput = true
		r.resyncShadowFloor = true
	}
	sessionID, _, _, streamEpoch := r.session.IdentitySnapshot()
	event := LiveSessionEvent{
		StreamEpoch: streamEpoch, ContractVersion: shadowA6AContractVersion,
		AuthoritativeReason: observation.GetAuthoritativeReason(),
	}
	switch observation.GetKind() {
	case mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_SPEECH_TASK_STARTED:
		expected, valid := shadowSpeechTimelineSnapshotFromObservation(
			sessionID, streamEpoch, observation,
		)
		if !valid {
			return
		}
		input := observation.GetSpeechTaskStarted()
		if input == nil || input.GetTaskEpoch() == 0 {
			return
		}
		event.Kind = LiveEventSpeechTaskStart
		event.TaskEpoch = input.GetTaskEpoch()
		event.Scenario = "speech_task_started"
		event.Authoritative = expected
		event.CompareTimeline = true
	case mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_SPEECH_SEGMENT:
		expected, valid := shadowSpeechTimelineSnapshotFromObservation(
			sessionID, streamEpoch, observation,
		)
		if !valid {
			return
		}
		input, valid := shadowSpeechSegmentFromProto(observation.GetSpeechSegment())
		if !valid {
			return
		}
		event.Kind = LiveEventSpeechSegment
		event.SpeechSegment = &input
		if observation.GetAuthoritativeAccepted() {
			event.Scenario = "speech_segment_accepted"
		} else {
			event.Scenario = "speech_segment_rejected"
		}
		event.Authoritative = expected
		event.CompareTimeline = true
	case mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_SPEECH_COMMIT:
		expected, valid := shadowSpeechTimelineSnapshotFromObservation(
			sessionID, streamEpoch, observation,
		)
		if !valid {
			return
		}
		input := observation.GetSpeechCommit()
		if input == nil || input.GetCommittedSample() == 0 {
			return
		}
		event.Kind = LiveEventSpeechCommit
		event.CommitSample = input.GetCommittedSample()
		event.Scenario = "speech_commit"
		event.Authoritative = expected
		event.CompareTimeline = true
	case mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_CONTEXT_ACTIVATED:
		input := observation.GetContextActivated()
		if input == nil || input.GetContextVersion() != observation.GetAuthoritativeContextVersion() {
			return
		}
		event.Kind = LiveEventContextActivate
		event.ContextVersion = input.GetContextVersion()
		event.Scenario = "context_activated"
		event.Authoritative = &LiveSessionSnapshot{
			SessionID: sessionID, StreamEpoch: streamEpoch,
			OutputArbiter: ShadowOutputArbiter{ContextVersion: input.GetContextVersion()},
		}
		event.CompareOutput = true
	case mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_FLOOR_DECISION:
		input := observation.GetFloorDecision()
		floor, decision, valid := shadowFloorDecisionFromProto(input)
		if !valid {
			return
		}
		fence := Fence{
			SessionID:    sessionID,
			TurnID:       input.GetTurnId(),
			GenerationID: input.GetGenerationId(),
			ToolEpoch:    input.GetToolEpoch(),
		}
		event.Kind = LiveEventAuthoritySnapshot
		event.Fence = fence
		event.Scenario = "floor_decision"
		event.Authoritative = &LiveSessionSnapshot{
			SessionID: sessionID, StreamEpoch: streamEpoch,
			Floor: floor, Generation: fence, LastDecision: decision,
		}
		event.AuthoritativeReason = observation.GetAuthoritativeReason()
		event.CompareGeneration = false
	case mediav1.ShadowObservationKind_SHADOW_OBSERVATION_KIND_OUTPUT_INTENT:
		input, inputValid := shadowOutputIntentFromProto(sessionID, observation.GetOutputIntent())
		accepted := observation.GetAuthoritativeAccepted()
		consumed := observation.GetAuthoritativeConsumed()
		if accepted && consumed {
			return
		}
		arbiter := observation.GetAuthoritativeOutputArbiter()
		observedAt := observation.GetObservedAtMs()
		expected := ShadowOutputArbiter{ContextVersion: observation.GetAuthoritativeContextVersion()}
		if arbiter != nil {
			var valid bool
			expected, valid = shadowOutputArbiterFromProto(sessionID, arbiter)
			if !valid || expected.ContextVersion != observation.GetAuthoritativeContextVersion() ||
				observedAt == 0 || observedAt > math.MaxInt64 ||
				!shadowOutputArbiterActiveAt(expected, int64(observedAt)) {
				return
			}
		} else if !inputValid || observation.GetObservedAtMs() == 0 || observation.GetObservedAtMs() > math.MaxInt64 {
			// Legacy observations have no after-state from which to compare a
			// malformed rejection. Keep the old fail-closed behavior.
			return
		}
		if observedAt == 0 || observedAt > math.MaxInt64 {
			if accepted {
				return
			}
			observedAt = uint64(time.Now().UnixMilli())
		}
		if consumed && (!inputValid || arbiter == nil || !expected.CandidatesComplete ||
			input.CreatedAtUnixMillis <= 0 || input.CreatedAtUnixMillis > int64(observedAt) ||
			input.ExpiresAtUnixMillis <= input.CreatedAtUnixMillis ||
			input.ContextVersion != observation.GetAuthoritativeContextVersion() ||
			input.FloorRequirement != ShadowOutputFloorAvailable ||
			input.PlaybackRequirement != ShadowOutputPlaybackCurrentGeneration) {
			return
		}
		event.Kind = LiveEventOutputIntent
		event.ObservedAtUnixMS = int64(observedAt)
		event.Scenario = "output_intent_accepted"
		if consumed {
			event.Scenario = "output_intent_consumed"
		} else if !accepted {
			event.Scenario = "output_intent_rejected"
		}
		if !inputValid {
			if accepted || arbiter == nil {
				return
			}
			currentFence, _ := r.session.GenerationSnapshot()
			event.Fence = currentFence
			event.OutputIntentInvalid = true
			event.AuthoritativeOutputState = true
		} else {
			event.Fence = input.Fence
			event.OutputIntent = &input
		}
		if arbiter != nil {
			event.AuthoritativeOutputState = true
			event.AuthoritativeOutputAccepted = accepted
			event.AuthoritativeOutputConsumed = consumed
		} else if accepted {
			candidate := input
			rank, ranked := shadowOutputDomainRank(&candidate)
			if !ranked {
				return
			}
			candidate.DomainRank = rank
			candidate.CandidateOnly = true
			expected.Candidate = &candidate
			event.AuthoritativeOutputAccepted = true
			event.ConsumeOutput = true
		} else {
			event.ConsumeOutput = true
		}
		event.Authoritative = &LiveSessionSnapshot{
			SessionID: sessionID, StreamEpoch: streamEpoch, OutputArbiter: expected,
		}
		event.CompareOutput = true
	default:
		return
	}
	resyncSpeech := event.CompareTimeline && r.resyncShadowSpeech
	if resyncSpeech {
		event.ResyncFromAuthoritative = true
		event.Scenario = "shadow_transport_discontinuity"
	}
	resyncOutput := event.CompareOutput && r.resyncShadowOutput
	if resyncOutput && !event.Authoritative.OutputArbiter.CandidatesComplete {
		// A winner-only snapshot cannot reconstruct the candidates that may
		// have been lost in transit. Keep the domain pending until Python sends
		// a complete bounded after-state.
		return
	}
	if resyncOutput {
		event.ResyncFromAuthoritative = true
		event.Scenario = "shadow_transport_discontinuity"
	}
	resyncFloor := event.Authoritative != nil && event.Authoritative.Floor != "" && r.resyncShadowFloor
	if resyncFloor {
		event.ResyncFromAuthoritative = true
		event.Scenario = "shadow_transport_discontinuity"
	}
	if err := r.session.ObserveShadow(event); err != nil {
		return
	}
	if resyncSpeech {
		r.resyncShadowSpeech = false
	}
	if resyncOutput {
		r.resyncShadowOutput = false
	}
	if resyncFloor {
		r.resyncShadowFloor = false
	}
	r.lastShadowSequence = observation.GetShadowSequence()
	r.hasShadowSequence = true
}

func shadowFloorDecisionFromProto(
	input *mediav1.ShadowFloorDecision,
) (ShadowFloorState, string, bool) {
	if input == nil {
		return "", "", false
	}
	floor, valid := floorStateFromProto(input.GetFloorState())
	if !valid {
		return "", "", false
	}
	decision := ""
	switch input.GetEffectKind() {
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT:
		decision = "duck_output"
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_ENQUEUE_OUTPUT_INTENT:
		decision = "enqueue_output_intent"
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_RESUME_OUTPUT:
		decision = "continue_output"
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DROP_STALE_EVENT:
		decision = "drop_stale_event"
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_PAUSE_OUTPUT:
		decision = "pause_output"
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION:
		decision = "cancel_generation"
	case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_UNSPECIFIED:
	default:
		return "", "", false
	}
	return floor, decision, true
}

func floorStateFromProto(input mediav1.FloorState) (ShadowFloorState, bool) {
	switch input {
	case mediav1.FloorState_FLOOR_STATE_SILENCE:
		return ShadowFloorSilence, true
	case mediav1.FloorState_FLOOR_STATE_USER_HOLDS_FLOOR:
		return ShadowFloorUser, true
	case mediav1.FloorState_FLOOR_STATE_ASSISTANT_HOLDS_FLOOR:
		return ShadowFloorAssistant, true
	case mediav1.FloorState_FLOOR_STATE_OVERLAP:
		return ShadowFloorOverlap, true
	case mediav1.FloorState_FLOOR_STATE_UNCERTAIN:
		return ShadowFloorUncertain, true
	default:
		return "", false
	}
}

func shadowSpeechTimelineSnapshotFromObservation(
	sessionID string,
	streamEpoch uint64,
	observation *mediav1.ShadowObservation,
) (*LiveSessionSnapshot, bool) {
	authoritative, ok := shadowSpeechTimelineStateFromProto(observation.GetAuthoritativeTimeline())
	if !ok {
		return nil, false
	}
	return &LiveSessionSnapshot{
		SessionID: sessionID, StreamEpoch: streamEpoch, SpeechTimeline: authoritative,
	}, true
}

func shadowSpeechSegmentFromProto(source *mediav1.ShadowSpeechSegment) (ShadowSpeechSegment, bool) {
	if source == nil || source.GetSegmentId() == "" || source.GetTaskEpoch() == 0 ||
		source.GetRevision() == 0 || source.GetCaptureEndSample() <= source.GetCaptureStartSample() ||
		len(source.GetTextSha256()) != 32 {
		return ShadowSpeechSegment{}, false
	}
	return ShadowSpeechSegment{
		SegmentID: source.GetSegmentId(), CaptureStartSample: source.GetCaptureStartSample(),
		CaptureEndSample: source.GetCaptureEndSample(), TaskEpoch: source.GetTaskEpoch(),
		Revision: source.GetRevision(), TextSHA256: hex.EncodeToString(source.GetTextSha256()),
		Final: source.GetFinal(),
	}, true
}

func shadowSpeechTimelineStateFromProto(source *mediav1.ShadowSpeechTimelineState) (ShadowSpeechTimeline, bool) {
	if source == nil {
		return ShadowSpeechTimeline{}, false
	}
	segments := make([]ShadowSpeechSegment, 0, len(source.GetSegments()))
	for _, input := range source.GetSegments() {
		segment, ok := shadowSpeechSegmentFromProto(input)
		if !ok {
			return ShadowSpeechTimeline{}, false
		}
		segments = append(segments, segment)
	}
	return ShadowSpeechTimeline{
		CommittedSample: source.GetCommittedSample(), LatestTaskEpoch: source.GetLatestTaskEpoch(),
		Segments: segments,
	}, true
}

func shadowOutputIntentFromProto(sessionID string, source *mediav1.ShadowOutputIntent) (ShadowOutputIntent, bool) {
	if source == nil || source.GetIntentId() == "" || source.GetCreatedAtMs() > math.MaxInt64 ||
		source.GetExpiresAtMs() > math.MaxInt64 {
		return ShadowOutputIntent{}, false
	}
	kind := ShadowOutputIntentKind("")
	switch source.GetKind() {
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT:
		kind = ShadowOutputFastAck
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_BACKCHANNEL:
		kind = ShadowOutputConversation
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_CONVERSATION_REPLY:
		kind = ShadowOutputConversation
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_DEEP_RESULT:
		kind = ShadowOutputDeep
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_TOOL_RESULT:
		kind = ShadowOutputTool
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_REMINDER:
		kind = ShadowOutputReminder
	case mediav1.OutputIntentKind_OUTPUT_INTENT_KIND_NOTIFICATION:
		kind = ShadowOutputNotification
	default:
		return ShadowOutputIntent{}, false
	}
	floor := ShadowOutputFloorRequirement("")
	if source.GetFloorRequirement() == mediav1.FloorRequirement_FLOOR_REQUIREMENT_ASSISTANT_MAY_SPEAK {
		floor = ShadowOutputFloorAvailable
	}
	return ShadowOutputIntent{
		IntentID: source.GetIntentId(),
		Fence: Fence{SessionID: sessionID, TurnID: source.GetTurnId(),
			GenerationID: source.GetGenerationId(), ToolEpoch: source.GetToolEpoch()},
		Kind: kind, Priority: source.GetPriority(), CreatedAtUnixMillis: int64(source.GetCreatedAtMs()),
		ExpiresAtUnixMillis: int64(source.GetExpiresAtMs()), ContextVersion: source.GetContextVersion(),
		FloorRequirement: floor, PlaybackRequirement: ShadowOutputPlaybackCurrentGeneration,
	}, true
}

func shadowOutputArbiterFromProto(
	sessionID string,
	source *mediav1.ShadowOutputArbiterState,
) (ShadowOutputArbiter, bool) {
	if source == nil {
		return ShadowOutputArbiter{}, false
	}
	decode := func(candidate *mediav1.ShadowOutputIntent) (ShadowOutputIntent, bool) {
		intent, valid := shadowOutputIntentFromProto(sessionID, candidate)
		if !valid {
			return ShadowOutputIntent{}, false
		}
		rank, ranked := shadowOutputDomainRank(&intent)
		if !ranked {
			return ShadowOutputIntent{}, false
		}
		intent.DomainRank = rank
		intent.CandidateOnly = true
		if intent.CreatedAtUnixMillis <= 0 || intent.ExpiresAtUnixMillis <= intent.CreatedAtUnixMillis ||
			intent.FloorRequirement != ShadowOutputFloorAvailable ||
			intent.PlaybackRequirement != ShadowOutputPlaybackCurrentGeneration {
			return ShadowOutputIntent{}, false
		}
		return intent, true
	}
	state := ShadowOutputArbiter{
		ContextVersion: source.GetContextVersion(), CandidatesComplete: source.GetActiveCandidatesComplete(),
	}
	if !state.CandidatesComplete {
		if len(source.GetActiveCandidates()) != 0 {
			return ShadowOutputArbiter{}, false
		}
		if candidate := source.GetCandidate(); candidate != nil {
			intent, valid := decode(candidate)
			if !valid {
				return ShadowOutputArbiter{}, false
			}
			state.Candidate = &intent
		}
		return state, true
	}

	inputs := source.GetActiveCandidates()
	if len(inputs) > maxShadowOutputPerDomain*4 {
		return ShadowOutputArbiter{}, false
	}
	seen := make(map[string]struct{}, len(inputs))
	domainCounts := [5]int{}
	var fence Fence
	for index, candidate := range inputs {
		intent, valid := decode(candidate)
		if !valid || intent.ContextVersion != state.ContextVersion {
			return ShadowOutputArbiter{}, false
		}
		if _, duplicate := seen[intent.IntentID]; duplicate {
			return ShadowOutputArbiter{}, false
		}
		seen[intent.IntentID] = struct{}{}
		domainCounts[intent.DomainRank]++
		if domainCounts[intent.DomainRank] > maxShadowOutputPerDomain {
			return ShadowOutputArbiter{}, false
		}
		if index == 0 {
			fence = intent.Fence
		} else if !intent.Fence.Equal(fence) {
			return ShadowOutputArbiter{}, false
		}
		state.Candidates = append(state.Candidates, intent)
	}
	canonical := append([]ShadowOutputIntent(nil), state.Candidates...)
	sortShadowOutputCandidates(canonical)
	if !slices.Equal(canonical, state.Candidates) {
		return ShadowOutputArbiter{}, false
	}
	winner := source.GetCandidate()
	if len(state.Candidates) == 0 {
		if winner != nil {
			return ShadowOutputArbiter{}, false
		}
		return state, true
	}
	decodedWinner, valid := decode(winner)
	if !valid || decodedWinner != state.Candidates[0] {
		return ShadowOutputArbiter{}, false
	}
	state.Candidate = &decodedWinner
	return state, true
}

func shadowOutputArbiterActiveAt(state ShadowOutputArbiter, observedAtUnixMillis int64) bool {
	if state.CandidatesComplete {
		for _, candidate := range state.Candidates {
			if candidate.CreatedAtUnixMillis > observedAtUnixMillis ||
				candidate.ExpiresAtUnixMillis <= observedAtUnixMillis {
				return false
			}
		}
		return true
	}
	return state.Candidate == nil ||
		(state.Candidate.CreatedAtUnixMillis <= observedAtUnixMillis &&
			state.Candidate.ExpiresAtUnixMillis > observedAtUnixMillis)
}

func pythonInteractionState(phase string) (ShadowFloorState, string, bool) {
	switch phase {
	case "connecting":
		return ShadowFloorSilence, "pause_output", true
	case "ready", "speaker_enroll", "listening":
		return ShadowFloorSilence, "continue_output", true
	case "user_speaking":
		return ShadowFloorUser, "duck_output", true
	case "backchannel":
		return ShadowFloorOverlap, "continue_output", true
	case "thinking", "thinking_silent", "tool_waiting":
		return ShadowFloorSilence, "continue_output", true
	case "speaking":
		return ShadowFloorAssistant, "enqueue_output_intent", true
	case "interrupted":
		return ShadowFloorUser, "drop_stale_event", true
	case "recovering", "closed":
		return ShadowFloorSilence, "drop_stale_event", true
	default:
		return ShadowFloorSilence, "", false
	}
}

func (r *VoiceCoreMediaRuntime) sessionID() string {
	identifier, _, _, _ := r.session.IdentitySnapshot()
	return identifier
}

func (r *VoiceCoreMediaRuntime) report(err error) {
	if r.onError != nil {
		r.onError(err)
	}
}

// IdentityMatches keeps the runtime's event check independent of generated
// protobuf details and lets other edge adapters reuse the same gate.
func (s *Session) IdentityMatches(identity *mediav1.SessionIdentity) bool {
	if identity == nil {
		return false
	}
	sessionID, accountID, deviceID, streamEpoch := s.IdentitySnapshot()
	clientType := identity.GetClientType()
	if clientType == "" {
		clientType = "h5"
	}
	return identity.GetSessionId() == sessionID && identity.GetStreamEpoch() == streamEpoch && identity.GetAccountId() == accountID && identity.GetDeviceId() == deviceID && clientType == s.ClientTypeValue()
}
