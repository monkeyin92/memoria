package mediaedge

// Server->device direction: Voice Core 24 kHz PCM is converted to the
// negotiated device rate, encoded to Opus and wrapped in MemoriaAudioFrameV1
// before entering the priority lane. Generation controls and playback
// effects are projected onto the device-media-v2 control messages; audio
// belonging to a superseded generation is dropped at arrival or at write
// time, never delayed.

import (
	"context"
	"errors"
	"log"
	"math"
	"time"

	"github.com/gorilla/websocket"

	mediav1 "memoria/services/media_edge/gen/memoria/media/v1"
)

// resample24kTo16k linearly converts one 480-sample frame into 320 samples
// (2/3 ratio). Linear interpolation is deterministic and adequate for voice;
// the device never sees a partial frame.
func resample24kTo16k(input []int16) []int16 {
	if len(input) != DeviceDownlinkFrameSamples24k {
		return nil
	}
	output := make([]int16, DeviceDownlinkFrameSamples16k)
	for index := range output {
		position := float64(index) * 1.5
		left := int(position)
		frac := position - float64(left)
		right := left + 1
		if right >= len(input) {
			right = len(input) - 1
		}
		a := float64(input[left])
		b := float64(input[right])
		output[index] = int16(a + (b-a)*frac)
	}
	return output
}

// beginDownlinkClock resets the device-facing clock for one authoritative
// generation. A reconnect resumes Core's source clock, but the new WSS wire
// epoch starts at 0/0 and retains a reverse mapping for playback receipts.
func (c *DeviceConnection) beginDownlinkClock(fence deviceFence, resumed bool) {
	c.downlinkClockMu.Lock()
	c.downlinkClockFence = fence
	c.downlinkSourceSequenceBase = 0
	c.downlinkSourceSampleBase = 0
	c.downlinkRebasePending = resumed
	c.downlinkClockMu.Unlock()
	c.lane.resetWriteClock()
	if c.ledger != nil {
		c.ledger.startTransportFence(fence, resumed)
	}
}

func (c *DeviceConnection) projectDownlinkClock(
	fence deviceFence,
	sourceSequence uint64,
	sourceSampleStart uint64,
) (uint64, uint64, bool) {
	c.downlinkClockMu.Lock()
	if c.downlinkClockFence != fence {
		c.downlinkClockFence = fence
		c.downlinkSourceSequenceBase = 0
		c.downlinkSourceSampleBase = 0
		c.downlinkRebasePending = false
	}
	rebased := false
	if c.downlinkRebasePending {
		c.downlinkSourceSequenceBase = sourceSequence
		c.downlinkSourceSampleBase = sourceSampleStart
		c.downlinkRebasePending = false
		rebased = true
	}
	if sourceSequence < c.downlinkSourceSequenceBase ||
		sourceSampleStart < c.downlinkSourceSampleBase {
		c.downlinkClockMu.Unlock()
		return 0, 0, false
	}
	sequenceBase := c.downlinkSourceSequenceBase
	sampleBase := c.downlinkSourceSampleBase
	wireSequence := sourceSequence - sequenceBase
	wireSample := sourceSampleStart - sampleBase
	c.downlinkClockMu.Unlock()
	if rebased && c.ledger != nil {
		c.ledger.setTransportBase(fence, sequenceBase, sampleBase)
	}
	return wireSequence, wireSample, true
}

// downlinkSender is the DownlinkSender the Voice Core runtime calls for every
// accepted 24 kHz PCM frame. It runs on the runtime receive loop and must not
// block on the network: Opus encode and lane enqueue are both bounded.
func (c *DeviceConnection) downlinkSender(ctx context.Context, frame AudioFrame) error {
	if err := ctx.Err(); err != nil {
		return ErrStaleDownlinkGeneration
	}
	if c.state.Load() != deviceConnAccepted {
		return ErrStaleDownlinkGeneration
	}
	pcm, err := frame.Payload()
	if err != nil {
		return err
	}
	samples := make([]int16, len(pcm)/2)
	for index := range samples {
		samples[index] = int16(pcm[index*2]) | int16(pcm[index*2+1])<<8
	}
	if c.downlinkRate == 16_000 {
		samples = resample24kTo16k(samples)
		if samples == nil {
			return errors.New("device downlink resample failed")
		}
	}
	fence := deviceFence{
		TurnID: frame.TurnID, GenerationID: frame.GenerationID,
		ToolEpoch:    frame.ToolEpoch,
		SessionEpoch: frame.SessionEpoch,
	}
	wireSequence, wireSourceSampleStart, ok := c.projectDownlinkClock(
		fence, frame.Sequence, frame.CaptureStartSample,
	)
	if !ok || wireSequence > math.MaxUint32 {
		return errors.New("device downlink transport clock moved backwards or overflowed")
	}
	deviceSampleStart := wireSourceSampleStart
	if c.downlinkRate == 16_000 {
		if wireSourceSampleStart%3 != 0 {
			return errors.New("device downlink source sample is not aligned to 24k-to-16k clock")
		}
		deviceSampleStart = wireSourceSampleStart * 2 / 3
	}
	encoded := make([]byte, 1500)
	encodedSize, err := c.downlinkOpus.Encode(samples, encoded)
	if err != nil {
		c.server.metrics.opusErrors.Add(1)
		return err
	}
	wire, err := (MemoriaAudioFrameV1{
		Version:      DeviceFrameVersion,
		Direction:    DeviceDirectionDownlink,
		StreamEpoch:  c.epoch,
		Sequence:     uint32(wireSequence),
		SampleStart:  deviceSampleStart,
		FrameSamples: uint32(len(samples)),
		GenerationID: uint32(frame.GenerationID),
		Payload:      encoded[:encodedSize],
	}).MarshalBinary()
	if err != nil {
		return err
	}
	// The wire sequence/sample clock is per-generation: it starts at 0 on
	// every authoritative generation.started / playback.flush replacement
	// (the runtime Session gate already restarts the source clock), and the
	// device mirrors that reset. Queue drops below therefore surface as a
	// forward gap that the writer flags at socket-write time.
	c.lane.enqueueAudio(
		wire, uint32(frame.GenerationID), fence, wireSequence,
		deviceSampleStart, uint32(len(samples)),
	)
	c.server.metrics.downlinkFrames.Add(1)
	return nil
}

// writeLoop drains the priority lane and owns the single WebSocket writer.
// P0/P1 controls always preempt audio; audio for a stale generation is
// dropped, and the oldest audio is dropped when the 80-200 ms ceiling is
// exceeded instead of growing playout delay.
func (c *DeviceConnection) writeLoop() {
	defer close(c.writeErr)
	pingTicker := time.NewTicker(devicePingInterval)
	defer pingTicker.Stop()
	for {
		select {
		case <-c.closed:
			return
		case <-pingTicker.C:
			if err := c.ws.WriteControl(
				websocket.PingMessage,
				nil,
				time.Now().Add(deviceWriteTimeout),
			); err != nil {
				c.logSocketError("ping_write", err)
				return
			}
		case <-c.lane.notify:
			item, ok := c.lane.tryPop()
			if !ok {
				if c.lane.isClosed() {
					return
				}
				continue
			}
			if !c.writeLaneItem(item) {
				return
			}
		}
	}
}

func (c *DeviceConnection) writeLaneItem(item deviceLaneItem) bool {
	messageType := websocket.TextMessage
	if item.kind == "audio" || item.kind == "barrier" {
		if item.generation == 0 ||
			uint64(item.generation) != c.getCurrentFence().GenerationID {
			c.server.metrics.staleGeneration.Add(1)
			return true
		}
	}
	if item.kind == "audio" {
		// A same-generation forward gap can only come from frames the lane
		// dropped (backpressure) before they reached the socket. Mark it on
		// the header now: this is the moment the device will observe the gap.
		if c.lane.needsDiscontinuity(item.generation, item.sequence, item.sample, item.frameSamples) {
			setDeviceFrameDiscontinuity(item.payload)
		}
		_, oldestAge := c.lane.stats()
		c.server.gauges.observeDownlinkQueueMS(oldestAge)
		messageType = websocket.BinaryMessage
	}
	_ = c.ws.SetWriteDeadline(time.Now().Add(deviceWriteTimeout))
	if err := c.ws.WriteMessage(messageType, item.payload); err != nil {
		log.Printf("media edge device WSS socket error session=%s device=%s epoch=%d phase=message_write kind=%s generation=%d sequence=%d err=%v", c.sessionID, c.deviceID, c.epoch, item.kind, item.generation, item.sequence, err)
		return false
	}
	if item.kind == "audio" {
		c.lane.markAudioWritten(item.generation, item.sequence, item.sample)
		if c.ledger != nil {
			c.ledger.recordSent(
				item.fence,
				item.sequence,
				item.sample+uint64(item.frameSamples),
			)
		}
	}
	return true
}

// ForwardCoreEvent projects one CoreToMedia event onto the device control
// lane. Audio is delivered through downlinkSender, not through this method.
func (c *DeviceConnection) ForwardCoreEvent(event *mediav1.CoreToMedia) {
	if event == nil || c.state.Load() != deviceConnAccepted {
		return
	}
	now := c.server.monotonicMS()
	switch {
	case event.GetGeneration() != nil:
		generation := event.GetGeneration()
		fence := deviceFence{
			TurnID:       generation.GetTurnId(),
			GenerationID: generation.GetGenerationId(),
			ToolEpoch:    generation.GetToolEpoch(),
			SessionEpoch: generation.GetSessionEpoch(),
		}
		messageType := "generation.started"
		switch generation.GetAction() {
		case mediav1.GenerationAction_GENERATION_ACTION_PAUSE:
			messageType = "generation.pause"
		case mediav1.GenerationAction_GENERATION_ACTION_RESUME:
			messageType = "generation.resume"
		case mediav1.GenerationAction_GENERATION_ACTION_COMPLETE:
			messageType = "generation.completed"
		case mediav1.GenerationAction_GENERATION_ACTION_CANCEL:
			messageType = "generation.cancelled"
		}
		if messageType == "generation.cancelled" {
			c.lane.dropGeneration(uint32(fence.GenerationID))
			c.setCurrentFence(deviceFence{})
		} else {
			c.setCurrentFence(fence)
			if messageType == "generation.started" {
				// Authoritative start: the device downlink sequence/sample
				// clock resets to 0 for the new generation.
				c.beginDownlinkClock(fence, false)
			}
		}
		payload, err := marshalDeviceControl(deviceGenerationControl{
			Type: messageType, Version: 2,
			SessionID: c.sessionID, StreamEpoch: uint64(c.epoch),
			ControlSequence:   c.nextServerSequence(),
			ServerMonotonicMS: now,
			Fence:             fence, Reason: generation.GetReason(),
		})
		if err != nil {
			return
		}
		if messageType == "generation.completed" {
			c.lane.enqueueBarrier(payload, uint32(fence.GenerationID))
		} else {
			c.sendControl(deviceControlPriority(messageType), payload)
		}
	case event.GetRealtimeEffect() != nil:
		effect := event.GetRealtimeEffect()
		fence := deviceFence{
			TurnID:       effect.GetTurnId(),
			GenerationID: effect.GetGenerationId(),
			ToolEpoch:    effect.GetToolEpoch(),
		}
		switch effect.GetEffectKind() {
		case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_CANCEL_GENERATION:
			// The executable Core effect already carries the authoritative
			// replacement fence. Never derive a device-side N+1 mapping.
			c.setCurrentFence(fence)
			// playback.flush replacement: the device downlink clock resets
			// to 0 for the replacement generation.
			c.beginDownlinkClock(fence, false)
			payload, err := marshalDeviceControl(devicePlaybackControl{
				Type: "playback.flush", Version: 2,
				SessionID: c.sessionID, StreamEpoch: uint64(c.epoch),
				ControlSequence:   c.nextServerSequence(),
				ServerMonotonicMS: now,
				Fence:             fence, ReplacementGeneration: fence.GenerationID,
				DuckDB: 0,
			})
			if err != nil {
				return
			}
			c.sendControl(0, payload)
		case mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_DUCK_OUTPUT,
			mediav1.RealtimeEffectKind_REALTIME_EFFECT_KIND_PAUSE_OUTPUT:
			duckDB := 0.0
			if gain, ok := realtimeEffectGain(effect); ok {
				duckDB = -20 * math.Log10(math.Max(gain, 1e-3))
				if duckDB > 60 {
					duckDB = 60
				}
				if duckDB < 0 {
					duckDB = 0
				}
			}
			payload, err := marshalDeviceControl(devicePlaybackControl{
				Type: "playback.duck", Version: 2,
				SessionID: c.sessionID, StreamEpoch: uint64(c.epoch),
				ControlSequence:   c.nextServerSequence(),
				ServerMonotonicMS: now,
				Fence:             fence, ReplacementGeneration: fence.GenerationID,
				DuckDB: duckDB,
			})
			if err != nil {
				return
			}
			c.sendControl(deviceControlPriority("playback.duck"), payload)
		}
	case event.GetState() != nil:
		state := event.GetState()
		if state.GetState() != mediav1.ConversationState_CONVERSATION_STATE_CLOSED {
			return
		}
		// Voice Core is the interaction authority. A terminal CLOSED state
		// revokes the device generation before the P0 close is queued, so
		// concurrent/late PCM cannot slip behind the standby transition.
		c.stateMu.Lock()
		if c.sessionCloseQueued {
			c.stateMu.Unlock()
			return
		}
		c.sessionCloseQueued = true
		c.currentFence = deviceFence{}
		c.serverControlSeq++
		controlSequence := c.serverControlSeq
		c.stateMu.Unlock()
		reason := state.GetReason()
		if reason == "" {
			reason = "conversation_closed"
		}
		payload, err := marshalDeviceControl(deviceServerSessionClose{
			Type: "session.close", Version: 2,
			SessionID: c.sessionID, StreamEpoch: uint64(c.epoch),
			ControlSequence:   controlSequence,
			ServerMonotonicMS: now,
			Reason:            reason,
		})
		if err != nil {
			c.stateMu.Lock()
			c.sessionCloseQueued = false
			c.stateMu.Unlock()
			return
		}
		log.Printf("media edge projected conversation close session=%s device=%s epoch=%d reason=%s control_sequence=%d", c.sessionID, c.deviceID, c.epoch, reason, controlSequence)
		c.sendControl(deviceControlPriority("session.close"), payload)
	case event.GetError() != nil:
		coreError := event.GetError()
		payload, err := marshalDeviceControl(deviceSessionError{
			Type: "session.error", Version: 2,
			SessionID: c.sessionID, StreamEpoch: uint64(c.epoch),
			ControlSequence:   c.nextServerSequence(),
			ServerMonotonicMS: now,
			Code:              coreError.GetCode(), Retryable: coreError.GetRetryable(),
		})
		if err != nil {
			return
		}
		c.sendControl(deviceControlPriority("session.error"), payload)
	}
}

// ForwardCoreEventForSession routes one core event to the exact device
// transport that opened the Voice Core stream. Session ids survive reconnects,
// so the stream epoch and authority facts are part of the lookup fence: a late
// event from a superseded Core stream must never reach the replacement socket.
func (s *DeviceWSServer) ForwardCoreEventForSession(request OpenSessionRequest, event *mediav1.CoreToMedia) {
	connection := s.connectionForRequest(request)
	if connection == nil {
		return
	}
	connection.ForwardCoreEvent(event)
}

// HandleBridgeError closes only the exact device transport whose Voice Core
// stream failed. A late EOF/error from an old epoch is harmless after a
// reconnect and must not close the new connection that shares the Session id.
func (s *DeviceWSServer) HandleBridgeError(request OpenSessionRequest, bridgeErr error) {
	connection := s.connectionForRequest(request)
	if connection == nil {
		return
	}
	log.Printf("media edge device Voice Core stream closing session=%s device=%s epoch=%d err=%v", connection.sessionID, connection.deviceID, connection.epoch, bridgeErr)
	connection.sendSessionError("voice_core_unavailable", true)
	connection.closeWithCode(1011, "voice core stream failed")
	connection.close()
}
