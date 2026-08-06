package mediaedge

import (
	"fmt"
	"time"
)

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
	if s.downlink.Len() >= s.MaxPendingFrames {
		s.overflowFrames++
		return fmt.Errorf("downlink queue is full")
	}
	if ^uint64(0)-frame.CaptureStartSample < frame.FrameSamples {
		s.staleFrames++
		return fmt.Errorf("downlink sample range overflows")
	}
	s.lastDownlinkSeq = frame.Sequence
	s.hasDownlinkSeq = true
	frameEnd := frame.CaptureStartSample + frame.FrameSamples
	s.lastDownlinkSourceEnd = frameEnd
	if frameEnd > s.renderedSampleEnd {
		s.playoutUnderrunActive = false
	}
	if frame.Final {
		s.hasGenerationFinal = true
		s.generationFinalSampleEnd = frameEnd
	}
	if !s.downlink.Push(frame, time.Now()) {
		s.overflowFrames++
		return fmt.Errorf("downlink queue is full")
	}
	s.downlinkFrames++
	s.mirrorLocked(LiveSessionEvent{
		Kind:        LiveEventAudioDownlink,
		StreamEpoch: s.StreamEpoch,
		Fence:       actual,
		Audio:       true,
	})
	return nil
}

// DeliverDownlink records a fenced frame before calling the external sender.
// The sender runs outside the Session mutex, so a slow encoder cannot delay a
// hard stop. CancelGeneration cancels its context before closing the gate;
// compliant terminators must not enqueue PCM once that context is done.
func (s *Session) DeliverDownlink(frame AudioFrame, sender DownlinkSender) error {
	s.mu.Lock()
	if err := s.acceptDownlinkLocked(frame); err != nil {
		s.mu.Unlock()
		return err
	}
	if sender == nil {
		s.mu.Unlock()
		return nil
	}
	deliveryCtx := s.downlinkDeliveryCtx
	s.mu.Unlock()
	if err := sender(deliveryCtx, frame); err != nil {
		if deliveryCtx.Err() != nil {
			return ErrStaleDownlinkGeneration
		}
		return err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	actual := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch}
	if deliveryCtx.Err() != nil || !s.generationActive || !actual.Equal(s.Generation) {
		return ErrStaleDownlinkGeneration
	}
	return s.acknowledgeDownlinkLocked(frame.Sequence)
}

func (s *Session) AcknowledgeDownlink(sequence uint64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.acknowledgeDownlinkLocked(sequence)
}

func (s *Session) acknowledgeDownlinkLocked(sequence uint64) error {
	entry, ok := s.downlink.Front()
	if !ok || entry.frame.Sequence != sequence {
		return fmt.Errorf("downlink sequence is not pending")
	}
	_, _ = s.downlink.Pop()
	return nil
}

// PopDownlink is used by an attached media terminator after it has encoded or
// rendered the bounded PCM frame.  It never changes the generation gate.
func (s *Session) PopDownlink() (AudioFrame, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for s.downlink.Len() > 0 {
		entry, _ := s.downlink.Pop()
		frame := entry.frame
		actual := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch}
		if s.generationActive && actual.Equal(s.Generation) {
			return frame, true
		}
		s.staleFrames++
	}
	return AudioFrame{}, false
}
