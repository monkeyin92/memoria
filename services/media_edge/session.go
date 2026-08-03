package mediaedge

import (
	"fmt"
	"sync"
)

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
	mu                 sync.Mutex
	ID                 string
	AccountID          string
	DeviceID           string
	ClientType         string
	StreamEpoch        uint64
	Generation         Fence
	State              SessionState
	MaxPendingFrames   int
	uplink             []AudioFrame
	downlink           []AudioFrame
	lastUplinkSequence uint64
	lastDownlinkSeq    uint64
	hasUplinkSequence  bool
	hasDownlinkSeq     bool
	lastCaptureEnd     uint64
	uplinkFrames       uint64
	downlinkFrames     uint64
	staleFrames        uint64
	overflowFrames     uint64
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
		State:            SessionActive,
		MaxPendingFrames: maxPendingFrames,
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
	actual := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch}
	if !actual.Equal(s.Generation) {
		s.staleFrames++
		return fmt.Errorf("downlink generation is stale")
	}
	if len(s.downlink) >= s.MaxPendingFrames {
		s.overflowFrames++
		return fmt.Errorf("downlink queue is full")
	}
	s.lastDownlinkSeq = frame.Sequence
	s.hasDownlinkSeq = true
	s.downlink = append(s.downlink, frame)
	s.downlinkFrames++
	return nil
}

// PopDownlink is used by an attached media terminator after it has encoded or
// rendered the bounded PCM frame.  It never changes the generation gate.
func (s *Session) PopDownlink() (AudioFrame, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.downlink) == 0 {
		return AudioFrame{}, false
	}
	frame := s.downlink[0]
	s.downlink = s.downlink[1:]
	return frame, true
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
	s.Generation = fence
	// Cancel is a gate operation; draining the queue is intentionally not the
	// correctness mechanism.  Consumers will reject any frame with the old
	// fence even if one is already buffered.
	return nil
}

func (s *Session) Reconnect() (uint64, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.State == SessionStopped || s.State == SessionDraining {
		return 0, fmt.Errorf("session cannot reconnect while %s", s.State)
	}
	s.StreamEpoch++
	s.hasUplinkSequence = false
	s.hasDownlinkSeq = false
	s.lastCaptureEnd = 0
	s.uplink = nil
	s.downlink = nil
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
