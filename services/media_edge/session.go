package mediaedge

import (
	"errors"
	"fmt"
	"sync"
)

var ErrStaleDownlinkGeneration = errors.New("downlink generation is stale")

const maxCancelResults = 64

type cancelResult struct {
	current   Fence
	cancelled Fence
}

type SessionState string

const (
	SessionActive   SessionState = "active"
	SessionDraining SessionState = "draining"
	SessionStopped  SessionState = "stopped"
)

type SessionStats struct {
	State              SessionState `json:"state"`
	StreamEpoch        uint64       `json:"stream_epoch"`
	LastUplinkSequence uint64       `json:"last_uplink_sequence"`
	LastDownlinkSeq    uint64       `json:"last_downlink_sequence"`
	UplinkFrames       uint64       `json:"uplink_frames"`
	DownlinkFrames     uint64       `json:"downlink_frames"`
	StaleFrames        uint64       `json:"stale_frames"`
	OverflowFrames     uint64       `json:"overflow_frames"`
}

type Session struct {
	mu                    sync.Mutex
	ID                    string
	AccountID             string
	DeviceID              string
	ClientType            string
	StreamEpoch           uint64
	Generation            Fence
	generationActive      bool
	State                 SessionState
	MaxPendingFrames      int
	uplink                []AudioFrame
	downlink              []AudioFrame
	lastUplinkSequence    uint64
	lastDownlinkSeq       uint64
	hasUplinkSequence     bool
	hasDownlinkSeq        bool
	lastCaptureEnd        uint64
	uplinkFrames          uint64
	downlinkFrames        uint64
	staleFrames           uint64
	overflowFrames        uint64
	cancelResults         map[string]cancelResult
	cancelResultOrder     []string
	lastDownlinkSourceEnd uint64
}

// Epoch returns the authoritative stream epoch without exposing an unlocked
// read to HTTP or bridge goroutines.
func (s *Session) Epoch() uint64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.StreamEpoch
}

func (s *Session) IdentitySnapshot() (sessionID, accountID, deviceID string, streamEpoch uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ID, s.AccountID, s.DeviceID, s.StreamEpoch
}

func (s *Session) ClientTypeValue() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ClientType
}

func NewSession(request OpenSessionRequest, maxPendingFrames int) (*Session, error) {
	if err := request.Validate(); err != nil {
		return nil, err
	}
	if maxPendingFrames <= 0 {
		return nil, fmt.Errorf("max_pending_frames must be positive")
	}
	return &Session{
		ID:               request.SessionID,
		AccountID:        request.AccountID,
		DeviceID:         request.DeviceID,
		ClientType:       defaultClientType(request.ClientType),
		StreamEpoch:      request.StreamEpoch,
		Generation:       Fence{SessionID: request.SessionID},
		generationActive: true,
		State:            SessionActive,
		MaxPendingFrames: maxPendingFrames,
		cancelResults:    make(map[string]cancelResult),
	}, nil
}

func defaultClientType(value string) string {
	if value == "" {
		return "h5"
	}
	return value
}

func (s *Session) AcceptUplink(frame AudioFrame) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.State != SessionActive {
		return fmt.Errorf("session is not active")
	}
	if err := frame.Validate(s.ID, s.StreamEpoch); err != nil {
		return err
	}
	if s.hasUplinkSequence && frame.Sequence <= s.lastUplinkSequence {
		s.staleFrames++
		return fmt.Errorf("uplink sequence is stale")
	}
	if s.hasUplinkSequence && frame.Sequence != s.lastUplinkSequence+1 {
		s.staleFrames++
		return fmt.Errorf("uplink sequence has a gap")
	}
	if frame.Discontinuity {
		// A discontinuity is a transport fence, not a queue-clearing hint.  The
		// caller must reconnect and obtain a new stream_epoch before samples
		// can be accepted again.
		s.staleFrames++
		return fmt.Errorf("discontinuity requires a new stream epoch")
	}
	if frame.CaptureStartSample < s.lastCaptureEnd {
		s.staleFrames++
		return fmt.Errorf("capture sample range moved backwards")
	}
	if s.hasUplinkSequence && frame.CaptureStartSample > s.lastCaptureEnd {
		s.staleFrames++
		return fmt.Errorf("capture sample range has a gap")
	}
	if len(s.uplink) >= s.MaxPendingFrames {
		s.overflowFrames++
		return fmt.Errorf("uplink queue is full")
	}
	if ^uint64(0)-frame.CaptureStartSample < frame.FrameSamples {
		s.staleFrames++
		return fmt.Errorf("capture sample range overflows")
	}
	s.lastUplinkSequence = frame.Sequence
	s.hasUplinkSequence = true
	s.lastCaptureEnd = frame.CaptureStartSample + frame.FrameSamples
	s.uplink = append(s.uplink, frame)
	s.uplinkFrames++
	return nil
}

// AcknowledgeUplink removes one frame after an attached Voice Core bridge has
// accepted it.  The HTTP reference path intentionally keeps frames queued for
// inspection; a live bridge must retire them after the gRPC send succeeds so
// the bounded queue represents actual backpressure rather than duplicate
// buffering.
func (s *Session) AcknowledgeUplink(sequence uint64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.uplink) == 0 || s.uplink[0].Sequence != sequence {
		return fmt.Errorf("uplink sequence is not pending")
	}
	s.uplink = s.uplink[1:]
	return nil
}

func (s *Session) AcceptDownlink(frame AudioFrame) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.acceptDownlinkLocked(frame)
}

func (s *Session) acceptDownlinkLocked(frame AudioFrame) error {
	if s.State != SessionActive {
		s.staleFrames++
		return fmt.Errorf("session is not active")
	}
	if err := frame.Validate(s.ID, s.StreamEpoch); err != nil {
		s.staleFrames++
		return err
	}
	if s.hasDownlinkSeq && frame.Sequence <= s.lastDownlinkSeq {
		s.staleFrames++
		return fmt.Errorf("downlink sequence is stale")
	}
	if s.hasDownlinkSeq && frame.Sequence != s.lastDownlinkSeq+1 {
		s.staleFrames++
		return fmt.Errorf("downlink sequence has a gap")
	}
	if !s.hasDownlinkSeq && (frame.Sequence != 0 || frame.CaptureStartSample != 0) {
		s.staleFrames++
		return fmt.Errorf("first downlink frame must start at sequence and sample zero")
	}
	if s.hasDownlinkSeq && frame.CaptureStartSample != s.lastDownlinkSourceEnd {
		s.staleFrames++
		return fmt.Errorf("downlink sample range has a gap")
	}
	actual := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch}
	if !s.generationActive || !actual.Equal(s.Generation) {
		s.staleFrames++
		return ErrStaleDownlinkGeneration
	}
	if len(s.downlink) >= s.MaxPendingFrames {
		s.overflowFrames++
		return fmt.Errorf("downlink queue is full")
	}
	if ^uint64(0)-frame.CaptureStartSample < frame.FrameSamples {
		s.staleFrames++
		return fmt.Errorf("downlink sample range overflows")
	}
	s.lastDownlinkSeq = frame.Sequence
	s.hasDownlinkSeq = true
	s.lastDownlinkSourceEnd = frame.CaptureStartSample + frame.FrameSamples
	s.downlink = append(s.downlink, frame)
	s.downlinkFrames++
	return nil
}

// DeliverDownlink keeps the authoritative generation check, bounded queue and
// transport write inside one session lock, so a concurrent CancelGeneration
// can never admit an old-generation frame to the terminator's encoder.  The
// sender is the real media terminator's enqueue step; it must be fast and
// must not call back into Session (the gate is held across the call).  A
// failed sender leaves the frame pending; a successful sender retires it
// immediately.
func (s *Session) DeliverDownlink(frame AudioFrame, sender DownlinkSender) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.acceptDownlinkLocked(frame); err != nil {
		return err
	}
	if sender == nil {
		return nil
	}
	if err := sender(frame); err != nil {
		return err
	}
	return s.acknowledgeDownlinkLocked(frame.Sequence)
}

func (s *Session) AcknowledgeDownlink(sequence uint64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.acknowledgeDownlinkLocked(sequence)
}

func (s *Session) acknowledgeDownlinkLocked(sequence uint64) error {
	if len(s.downlink) == 0 || s.downlink[0].Sequence != sequence {
		return fmt.Errorf("downlink sequence is not pending")
	}
	s.downlink = s.downlink[1:]
	return nil
}

// PopDownlink is used by an attached media terminator after it has encoded or
// rendered the bounded PCM frame.  It never changes the generation gate.
func (s *Session) PopDownlink() (AudioFrame, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for len(s.downlink) > 0 {
		frame := s.downlink[0]
		s.downlink = s.downlink[1:]
		actual := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch}
		if s.generationActive && actual.Equal(s.Generation) {
			return frame, true
		}
		s.staleFrames++
	}
	return AudioFrame{}, false
}

func (s *Session) AdvanceGeneration(fence Fence) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.State == SessionStopped {
		return fmt.Errorf("session is stopped")
	}
	if fence.SessionID != s.ID || fence.TurnID < s.Generation.TurnID ||
		(fence.TurnID == s.Generation.TurnID && fence.GenerationID < s.Generation.GenerationID) ||
		(fence.TurnID == s.Generation.TurnID && fence.GenerationID == s.Generation.GenerationID && fence.ToolEpoch < s.Generation.ToolEpoch) {
		return fmt.Errorf("generation must advance monotonically")
	}
	if fence.Equal(s.Generation) {
		if !s.generationActive {
			return fmt.Errorf("cancelled generation cannot be reactivated")
		}
		return nil
	}
	s.Generation = fence
	s.generationActive = true
	s.hasDownlinkSeq = false
	s.lastDownlinkSeq = 0
	s.lastDownlinkSourceEnd = 0
	s.discardStaleDownlinkLocked()
	// The gate remains authoritative even though stale buffered frames are
	// retired eagerly to release bounded queue capacity.
	return nil
}

// CancelGeneration atomically derives and installs the next generation from
// the current authoritative fence. The session and uplink remain active.
func (s *Session) CancelGeneration(eventID string, expected *Fence) (current, cancelled Fence, replayed bool, err error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if eventID == "" {
		return Fence{}, Fence{}, false, fmt.Errorf("stop event id is required")
	}
	if result, ok := s.cancelResults[eventID]; ok {
		if expected != nil && !expected.Equal(result.current) {
			return Fence{}, Fence{}, false, fmt.Errorf("stop event id was reused for another generation")
		}
		return result.current, result.cancelled, true, nil
	}
	if s.State != SessionActive {
		return Fence{}, Fence{}, false, fmt.Errorf("session is not active")
	}
	if !s.generationActive || (expected != nil && !expected.Equal(s.Generation)) {
		return Fence{}, Fence{}, false, fmt.Errorf("generation cancel fence is stale")
	}
	if s.Generation.GenerationID == ^uint64(0) {
		return Fence{}, Fence{}, false, fmt.Errorf("generation_id cannot advance")
	}
	current = s.Generation
	cancelled = current
	cancelled.GenerationID++
	s.Generation = cancelled
	s.generationActive = false
	s.hasDownlinkSeq = false
	s.lastDownlinkSeq = 0
	s.lastDownlinkSourceEnd = 0
	if len(s.cancelResultOrder) == maxCancelResults {
		delete(s.cancelResults, s.cancelResultOrder[0])
		s.cancelResultOrder = s.cancelResultOrder[1:]
	}
	s.cancelResults[eventID] = cancelResult{current: current, cancelled: cancelled}
	s.cancelResultOrder = append(s.cancelResultOrder, eventID)
	s.discardStaleDownlinkLocked()
	return current, cancelled, false, nil
}

func (s *Session) cancelledGeneration(eventID string) (current, cancelled Fence, ok bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	result, ok := s.cancelResults[eventID]
	if eventID == "" || !ok {
		return Fence{}, Fence{}, false
	}
	return result.current, result.cancelled, true
}

// ApplyCancelledGeneration consumes the authoritative cancellation emitted by
// Voice Core. It is idempotent with a cancellation already initiated locally.
func (s *Session) ApplyCancelledGeneration(cancelled Fence) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.generationActive && s.Generation.Equal(cancelled) {
		return nil
	}
	if !s.generationActive || cancelled.SessionID != s.ID ||
		cancelled.TurnID != s.Generation.TurnID ||
		cancelled.ToolEpoch != s.Generation.ToolEpoch ||
		s.Generation.GenerationID == ^uint64(0) ||
		cancelled.GenerationID != s.Generation.GenerationID+1 {
		return fmt.Errorf("cancelled generation does not match current fence")
	}
	s.Generation = cancelled
	s.generationActive = false
	s.hasDownlinkSeq = false
	s.lastDownlinkSeq = 0
	s.lastDownlinkSourceEnd = 0
	s.discardStaleDownlinkLocked()
	return nil
}

func (s *Session) GenerationSnapshot() (Fence, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.Generation, s.generationActive
}

func (s *Session) withActiveGeneration(fence Fence, action func() error) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.generationActive || !s.Generation.Equal(fence) {
		return fmt.Errorf("generation fence is stale")
	}
	return action()
}

func (s *Session) discardStaleDownlinkLocked() {
	kept := s.downlink[:0]
	for _, frame := range s.downlink {
		actual := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch}
		if s.generationActive && actual.Equal(s.Generation) {
			kept = append(kept, frame)
		} else {
			s.staleFrames++
		}
	}
	s.downlink = kept
}

func (s *Session) Reconnect() (uint64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.State == SessionStopped || s.State == SessionDraining {
		return 0, fmt.Errorf("session cannot reconnect while %s", s.State)
	}
	s.StreamEpoch++
	s.hasUplinkSequence = false
	s.lastCaptureEnd = 0
	s.uplink = nil
	s.downlink = nil
	// Stop idempotency is scoped to one transport epoch. Reusing an event id
	// after reconnect must not replay a cancellation fence from the old clock.
	s.cancelResults = make(map[string]cancelResult)
	s.cancelResultOrder = nil
	return s.StreamEpoch, nil
}

func (s *Session) Drain() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.State == SessionActive {
		s.State = SessionDraining
	}
}

func (s *Session) Stop() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.State = SessionStopped
	s.uplink = nil
	s.downlink = nil
}

func (s *Session) Stats() SessionStats {
	s.mu.Lock()
	defer s.mu.Unlock()
	return SessionStats{
		State: s.State, StreamEpoch: s.StreamEpoch,
		LastUplinkSequence: s.lastUplinkSequence, LastDownlinkSeq: s.lastDownlinkSeq,
		UplinkFrames: s.uplinkFrames, DownlinkFrames: s.downlinkFrames,
		StaleFrames: s.staleFrames, OverflowFrames: s.overflowFrames,
	}
}

type Directory struct {
	mu       sync.RWMutex
	sessions map[string]*Session
}

func NewDirectory() *Directory { return &Directory{sessions: make(map[string]*Session)} }

func (d *Directory) Put(session *Session) error {
	if session == nil || session.ID == "" {
		return fmt.Errorf("session is required")
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if _, exists := d.sessions[session.ID]; exists {
		return fmt.Errorf("session already exists")
	}
	d.sessions[session.ID] = session
	return nil
}

func (d *Directory) Get(id string) (*Session, bool) {
	d.mu.RLock()
	defer d.mu.RUnlock()
	session, ok := d.sessions[id]
	return session, ok
}

func (d *Directory) Delete(id string) bool {
	d.mu.Lock()
	defer d.mu.Unlock()
	if _, ok := d.sessions[id]; !ok {
		return false
	}
	delete(d.sessions, id)
	return true
}

// CloseAll releases every edge-owned session during process shutdown. The
// control-plane TTL still uses CloseSession for individual expiry; this sweep
// prevents a graceful server stop from retaining the directory map.
func (d *Directory) CloseAll() {
	d.mu.Lock()
	sessions := make([]*Session, 0, len(d.sessions))
	for id, session := range d.sessions {
		sessions = append(sessions, session)
		delete(d.sessions, id)
	}
	d.mu.Unlock()
	for _, session := range sessions {
		session.Stop()
	}
}
