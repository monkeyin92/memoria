package mediaedge

// VoiceCoreMediaRuntime is the narrow session-level seam used by a media
// terminator.  It maps the edge's bounded Session state to the generated
// media-v1 stream; it does not decode RTP or run a provider.

import (
	"context"
	"encoding/base64"
	"fmt"
	"sync"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

// CoreMediaStream is implemented by VoiceCoreSession and by deterministic
// local fakes.  A real WHIP/WebRTC adapter only needs to provide this contract.
type CoreMediaStream interface {
	SendAudio(AudioFrame) error
	Recv() (*mediav1.CoreToMedia, error)
	Close() error
}

// VoiceCoreMediaRuntime owns one session's edge↔core forwarding loop.
type VoiceCoreMediaRuntime struct {
	session              *Session
	core                 CoreMediaStream
	onEvent              func(*mediav1.CoreToMedia)
	onError              func(error)
	ctx                  context.Context
	cancel               context.CancelFunc
	done                 chan error
	once                 sync.Once
	mu                   sync.Mutex
	nextDownlinkSequence uint64
}

func NewVoiceCoreMediaRuntime(
	ctx context.Context,
	session *Session,
	core CoreMediaStream,
	onEvent func(*mediav1.CoreToMedia),
	onError func(error),
) (*VoiceCoreMediaRuntime, error) {
	if session == nil || core == nil {
		return nil, fmt.Errorf("session and Voice Core stream are required")
	}
	streamCtx, cancel := context.WithCancel(ctx)
	return &VoiceCoreMediaRuntime{
		session: session,
		core:    core,
		onEvent: onEvent,
		onError: onError,
		ctx:     streamCtx,
		cancel:  cancel,
		done:    make(chan error, 1),
	}, nil
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
		if err := r.session.AdvanceGeneration(fence); err != nil {
			return err
		}
	}
	if audio := event.GetAudio(); audio != nil {
		if !r.session.IdentityMatches(audio.GetIdentity()) {
			return fmt.Errorf("Voice Core audio identity does not match edge session")
		}
		r.mu.Lock()
		sequence := r.nextDownlinkSequence
		r.nextDownlinkSequence++
		r.mu.Unlock()
		sessionID, _, _, streamEpoch := r.session.IdentitySnapshot()
		frame := AudioFrame{
			SessionID:          sessionID,
			StreamEpoch:        streamEpoch,
			Sequence:           sequence,
			CaptureStartSample: audio.GetSourceStartSample(),
			FrameSamples:       uint64(audio.GetFrameSamples()),
			TurnID:             audio.GetTurnId(),
			GenerationID:       audio.GetGenerationId(),
			ToolEpoch:          audio.GetToolEpoch(),
			PayloadB64:         base64.StdEncoding.EncodeToString(audio.GetPcmS16Le()),
		}
		if err := r.session.AcceptDownlink(frame); err != nil {
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
	return identity.GetSessionId() == sessionID && identity.GetStreamEpoch() == streamEpoch && identity.GetAccountId() == accountID && identity.GetDeviceId() == deviceID
}
