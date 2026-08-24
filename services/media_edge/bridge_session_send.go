package mediaedge

import (
	"encoding/json"
	"fmt"
	"math"
	"time"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

func (s *VoiceCoreSession) Identity() BridgeIdentity { return s.identity }

func (s *VoiceCoreSession) InteractionAuthority() mediav1.InteractionAuthority {
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	return s.interactionAuthority
}

func (s *VoiceCoreSession) CurrentFence() Fence {
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	return s.current
}

// CurrentGeneration returns the complete authoritative reconnect snapshot.
// A cancelled fence remains current for stale-event rejection but is not an
// active playback generation.
func (s *VoiceCoreSession) CurrentGeneration() (Fence, bool) {
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	return s.current, s.currentActive
}

func (s *VoiceCoreSession) send(message *mediav1.MediaToCore) error {
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	if err := s.stream.Send(message); err != nil {
		return fmt.Errorf("send media-v1 event: %w", err)
	}
	return nil
}

func (s *VoiceCoreSession) SendAudio(frame AudioFrame) error {
	if frame.SessionID != s.identity.SessionID || frame.StreamEpoch != s.identity.StreamEpoch {
		return fmt.Errorf("audio identity or stream epoch does not match")
	}
	payload, err := frame.Payload()
	if err != nil {
		return err
	}
	if frame.FrameSamples > math.MaxUint32 || len(payload)%2 != 0 || uint64(len(payload)/2) != frame.FrameSamples {
		return fmt.Errorf("pcm payload does not match frame samples")
	}
	return s.send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Audio{
		Audio: &mediav1.AudioFrame{
			Identity:           s.identity.proto(),
			Sequence:           frame.Sequence,
			CaptureStartSample: frame.CaptureStartSample,
			FrameSamples:       uint32(frame.FrameSamples),
			Payload:            payload,
			Discontinuity:      frame.Discontinuity,
			LossConcealed:      frame.LossConcealed,
		},
	}})
}

func (s *VoiceCoreSession) SendVad(sample uint64, probability, rms, noiseFloor float32, start bool) error {
	return s.SendVadWithVoicedEnd(sample, sample, probability, rms, noiseFloor, start)
}

// SendVadWithVoicedEnd distinguishes the transport event time from the last
// voiced sample. SPEECH_END must carry this boundary so Voice Core can wait
// for late ASR text without treating VAD tail silence as missing speech.
func (s *VoiceCoreSession) SendVadWithVoicedEnd(sample, voicedEnd uint64, probability, rms, noiseFloor float32, start bool) error {
	typeValue := mediav1.VadEventType_VAD_EVENT_SPEECH_END
	if start {
		typeValue = mediav1.VadEventType_VAD_EVENT_SPEECH_START
	} else if voicedEnd > sample {
		return fmt.Errorf("vad voiced end cannot exceed event sample")
	}
	var voicedEndSample *uint64
	if !start {
		voicedEndSample = &voicedEnd
	}
	return s.send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Vad{
		Vad: &mediav1.VadEvent{
			Identity:        s.identity.proto(),
			Type:            typeValue,
			SamplePosition:  sample,
			Probability:     probability,
			Rms:             rms,
			NoiseFloor:      noiseFloor,
			VoicedEndSample: voicedEndSample,
		},
	}})
}

func (s *VoiceCoreSession) SendKeyword(keyword string, confidence float32, start, end uint64, hardStop bool) error {
	return s.SendKeywordAtFence(
		keyword, confidence, start, end, hardStop, s.CurrentFence(),
		uint64(time.Now().UnixMilli()),
	)
}

func validateKeyword(keyword string, confidence float32, start, end uint64, hardStop bool) error {
	if keyword == "" || end <= start {
		return fmt.Errorf("keyword and sample range are required")
	}
	if math.IsNaN(float64(confidence)) || math.IsInf(float64(confidence), 0) || confidence < 0 || confidence > 1 {
		return fmt.Errorf("keyword confidence must be between 0 and 1")
	}
	if hardStop && confidence < KWSHardStopMinConfidence {
		return fmt.Errorf("hard-stop keyword confidence must be at least %.1f", KWSHardStopMinConfidence)
	}
	return nil
}

// SendKeywordAtFence rejects delayed KWS evidence before it can stop a newer
// generation and stamps the edge-side detection time so Voice Core can
// measure interrupt.detect → interrupt.cancel. SendKeyword remains as the
// current-fence compatibility helper.
func (s *VoiceCoreSession) SendKeywordAtFence(keyword string, confidence float32, start, end uint64, hardStop bool, fence Fence, detectedAtMs uint64) error {
	if err := validateKeyword(keyword, confidence, start, end, hardStop); err != nil {
		return err
	}
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	s.stateMu.Lock()
	defer s.stateMu.Unlock()
	if !s.current.Equal(fence) {
		return fmt.Errorf("keyword belongs to a stale generation")
	}
	keywordEvent := &mediav1.KeywordEvent{
		Identity:    s.identity.proto(),
		Keyword:     keyword,
		Confidence:  confidence,
		StartSample: start,
		EndSample:   end,
		HardStop:    hardStop,
	}
	if detectedAtMs > 0 {
		keywordEvent.DetectedMonotonicMs = &detectedAtMs
	}
	if err := s.stream.Send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Keyword{
		Keyword: keywordEvent,
	}}); err != nil {
		return fmt.Errorf("send media-v1 event: %w", err)
	}
	return nil
}

func (s *VoiceCoreSession) SendPlaybackProgress(progress PlaybackProgress) error {
	if progress.SessionID != s.identity.SessionID || progress.StreamEpoch != s.identity.StreamEpoch {
		return fmt.Errorf("playback identity or stream epoch does not match")
	}
	s.stateMu.Lock()
	current := s.current
	s.stateMu.Unlock()
	if !current.Equal(Fence{SessionID: s.identity.SessionID, TurnID: progress.TurnID, GenerationID: progress.GenerationID, ToolEpoch: progress.ToolEpoch, SessionEpoch: progress.SessionEpoch}) {
		return fmt.Errorf("playback progress belongs to a stale generation")
	}
	return s.send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Playback{
		Playback: &mediav1.PlaybackProgress{
			Identity:          s.identity.proto(),
			GenerationId:      progress.GenerationID,
			ReceivedSequence:  progress.ReceivedSequence,
			RenderedSampleEnd: progress.RenderedSampleEnd,
			ClientMonotonicMs: progress.ClientMonotonicMS,
			Approximate:       progress.Approximate,
			TurnId:            progress.TurnID,
			ToolEpoch:         progress.ToolEpoch,
			SessionEpoch:      progress.SessionEpoch,
			EventType:         progress.EventType,
		},
	}})
}

// SendClientEvent re-sequences a validated browser envelope onto the single
// Media Edge -> Voice Core client-event stream. Browser sequence numbers are
// transport-local and cannot be mixed with Edge-generated stop events.
func (s *VoiceCoreSession) SendClientEvent(
	raw []byte,
	eventType string,
	fence Fence,
	monotonicMS uint64,
) error {
	if len(raw) == 0 || eventType == "" || fence.SessionID != s.identity.SessionID {
		return fmt.Errorf("client event and matching fence are required")
	}
	var envelope map[string]any
	if err := json.Unmarshal(raw, &envelope); err != nil {
		return fmt.Errorf("decode client envelope: %w", err)
	}
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	s.stateMu.Lock()
	if !s.current.Equal(fence) {
		s.stateMu.Unlock()
		return fmt.Errorf("client event belongs to a stale generation")
	}
	sequence := s.nextClientSequence
	s.nextClientSequence++
	s.stateMu.Unlock()
	envelope["sequence"] = sequence
	encoded, err := json.Marshal(envelope)
	if err != nil {
		return fmt.Errorf("encode client envelope: %w", err)
	}
	if err := s.stream.Send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Device{
		Device: &mediav1.DeviceEvent{
			Identity: s.identity.proto(), EventType: eventType,
			JsonPayload: encoded, MonotonicMs: monotonicMS,
		},
	}}); err != nil {
		return fmt.Errorf("send media-v1 event: %w", err)
	}
	return nil
}

func (s *VoiceCoreSession) SendStop(eventID, reason string, fence Fence, detectedAtMs uint64) error {
	if eventID == "" || fence.SessionID != s.identity.SessionID {
		return fmt.Errorf("stop event id and matching fence are required")
	}
	s.stateMu.Lock()
	current := s.current
	s.stateMu.Unlock()
	if !current.Equal(fence) {
		return fmt.Errorf("stop fence is stale")
	}
	s.sendMu.Lock()
	defer s.sendMu.Unlock()
	s.stateMu.Lock()
	clientSequence := s.nextClientSequence
	s.nextClientSequence++
	s.stateMu.Unlock()
	envelope := map[string]any{
		"v": 1, "protocol": "media-v1", "type": "client.stop_assistant", "event_id": eventID,
		"session_id": s.identity.SessionID, "stream_epoch": s.identity.StreamEpoch,
		"sequence": clientSequence, "session_epoch": fence.SessionEpoch,
		"turn_id": fence.TurnID, "generation_id": fence.GenerationID,
		"tool_epoch": fence.ToolEpoch, "server_monotonic_ms": 0,
		"payload": map[string]any{"idempotency_key": eventID, "reason": reason},
	}
	raw, err := json.Marshal(envelope)
	if err != nil {
		return fmt.Errorf("encode stop envelope: %w", err)
	}
	deviceEvent := &mediav1.DeviceEvent{
		Identity:    s.identity.proto(),
		EventType:   "client.stop_assistant",
		JsonPayload: raw,
		MonotonicMs: detectedAtMs,
	}
	if err := s.stream.Send(&mediav1.MediaToCore{Event: &mediav1.MediaToCore_Device{
		Device: deviceEvent,
	}}); err != nil {
		return fmt.Errorf("send media-v1 event: %w", err)
	}
	return nil
}

// Recv returns the next core event after applying identity, event sequence and
// complete generation checks.  A stale event is an error, not a silent queue
// clear, so the media terminator can reconnect with a fresh stream epoch.

type PlaybackProgress struct {
	SessionID         string
	StreamEpoch       uint64
	GenerationID      uint64
	ReceivedSequence  uint64
	RenderedSampleEnd uint64
	ClientMonotonicMS uint64
	Approximate       bool
	TurnID            uint64
	ToolEpoch         uint64
	SessionEpoch      uint64
	EventType         mediav1.PlaybackEventType
}
