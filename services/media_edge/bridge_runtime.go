package mediaedge

// VoiceCoreMediaRuntime is the narrow session-level seam used by a media
// terminator.  It maps the edge's bounded Session state to the generated
// media-v1 stream; it does not decode RTP or run a provider.

import (
	"context"
	"encoding/base64"
	"errors"
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

type keywordStream interface {
	SendKeywordAtFence(keyword string, confidence float32, start, end uint64, hardStop bool, fence Fence, detectedAtMs uint64) error
}

// DownlinkSender is implemented by the real media terminator. Returning nil
// means the frame was accepted for transport; returning an error retains the
// frame in the bounded Session queue as backpressure evidence.
type DownlinkSender func(AudioFrame) error

type stopAttempt struct {
	cancelled    Fence
	core         Fence
	reason       string
	detectedAtMs uint64
}

// VoiceCoreMediaRuntime owns one session's edge↔core forwarding loop.
type VoiceCoreMediaRuntime struct {
	session           *Session
	core              CoreMediaStream
	onEvent           func(*mediav1.CoreToMedia)
	onError           func(error)
	downlinkSender    DownlinkSender
	ctx               context.Context
	cancel            context.CancelFunc
	done              chan error
	once              sync.Once
	uplinkMu          sync.Mutex
	stopMu            sync.Mutex
	pendingStopID     string
	pendingStop       stopAttempt
	keywordMu         sync.Mutex
	sentKeywordFences map[string]Fence
	sentKeywordOrder  []string
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

// SendKeyword applies the Session's local gate immediately around the bridge
// write so delayed hard-stop evidence cannot target a cancelled generation.
func (r *VoiceCoreMediaRuntime) SendKeyword(keyword string, confidence float32, start, end uint64, hardStop bool, fence Fence) error {
	sender, ok := r.core.(keywordStream)
	if !ok {
		return fmt.Errorf("Voice Core stream does not support keyword events")
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
		return Fence{}, fmt.Errorf("Voice Core stream does not support generation stop")
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
		return Fence{}, fmt.Errorf("Edge and Voice Core generation fences diverged")
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
		return fmt.Errorf("Voice Core returned an empty event")
	}
	if generation := event.GetGeneration(); generation != nil {
		if !r.session.IdentityMatches(generation.GetIdentity()) {
			return fmt.Errorf("Voice Core generation identity does not match edge session")
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
			return fmt.Errorf("Voice Core audio identity does not match edge session")
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
		}
		if err := r.session.DeliverDownlink(frame, r.downlinkSender); errors.Is(err, ErrStaleDownlinkGeneration) {
			return nil
		} else if err != nil {
			return err
		}
	}
	if r.onEvent != nil {
		r.onEvent(event)
	}
	return nil
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
