package mediaedge

// VoiceCoreMediaRuntime is the narrow session-level seam used by a media
// terminator.  It maps the edge's bounded Session state to the generated
// media-v1 stream; it does not decode RTP or run a provider.

import (
	"context"
	"fmt"
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

type generationSnapshotStream interface {
	CurrentGeneration() (Fence, bool)
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
			SessionEpoch: progress.SessionEpoch,
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
	expected := s.OpenRequestSnapshot()
	clientType := identity.GetClientType()
	if clientType == "" {
		clientType = "h5"
	}
	if identity.GetSessionId() != expected.SessionID || identity.GetStreamEpoch() != expected.StreamEpoch ||
		identity.GetAccountId() != expected.AccountID || identity.GetDeviceId() != expected.DeviceID ||
		clientType != expected.ClientType {
		return false
	}
	// The runtime profile authority fence participates as an explicit value.
	// An empty subject_id on the device path is the unknown_safe fence value,
	// not a wildcard: it must equal the subject echoed by the Voice Core or
	// the event is rejected.
	return identity.GetSubjectId() == expected.SubjectID && identity.GetBindingId() == expected.BindingID &&
		identity.GetBindingVersion() == expected.BindingVersion &&
		identity.GetRuntimeProfileVersion() == expected.RuntimeProfileVersion
}
