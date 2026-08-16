package mediaedge

// Device->server direction: MemoriaAudioFrameV1 binary frames are decoded
// from Opus into 16 kHz PCM and forwarded through the existing fenced
// Session/VoiceCoreMediaRuntime; VAD, keyword, button-stop and playback
// receipts are mapped onto the media-v1 control facts.

import (
	"encoding/base64"
	"fmt"
	"time"

	"github.com/gorilla/websocket"
)

// handleControl processes one device text frame. It returns false when the
// connection must be closed.
func (c *DeviceConnection) handleControl(data []byte) bool {
	if len(data) > DeviceMaxControlBytes {
		c.server.metrics.oversizeRejected.Add(1)
		c.closeWithCode(websocket.CloseMessageTooBig, "control message is too large")
		return false
	}
	if !c.controlLimiter.allow(time.Now()) {
		c.server.metrics.rateRejected.Add(1)
		c.closeWithCode(websocket.ClosePolicyViolation, "control message rate exceeded")
		return false
	}
	envelope, err := parseDeviceControl(data)
	if err != nil {
		c.server.metrics.controlRejected.Add(1)
		c.closeWithCode(websocket.CloseUnsupportedData, "control message is not valid JSON")
		return false
	}
	if !isDeviceControlType(envelope.Type) {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("unsupported_control", false)
		return false
	}
	runtime, session := c.runtimeRef()
	switch envelope.Type {
	case "vad.start", "vad.end":
		return c.handleVAD(envelope, runtime)
	case "keyword.detected":
		return c.handleKeyword(envelope, runtime)
	case "button.stop":
		return c.handleButtonStop(envelope, runtime)
	case "playback.started", "playback.progress", "playback.ended", "playback.error":
		return c.handlePlaybackReceipt(envelope, runtime)
	case "device.mute_changed", "device.network_changed", "device.telemetry":
		return c.handleDeviceState(envelope)
	case "runtime_profile.applied":
		return c.handleRuntimeProfileApplied(envelope)
	case "session.close":
		return c.handleSessionClose(envelope)
	default:
		_ = session
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("unsupported_control", false)
		return false
	}
}

func (c *DeviceConnection) handleRuntimeProfileApplied(envelope deviceControlEnvelope) bool {
	if err := requireVersion(envelope.Version, 2); err != nil {
		c.server.metrics.controlRejected.Add(1)
		return false
	}
	var event deviceRuntimeProfileApplied
	c.stateMu.Lock()
	expectedProfileVersion := c.runtimeProfileVersion
	expectedSettingsVersion := c.claims.DeviceSettings.SettingsVersion
	c.stateMu.Unlock()
	if err := jsonUnmarshalStrict(envelope.Raw, &event); err != nil || event.validate() != nil ||
		uint64(c.epoch) != event.StreamEpoch || !c.acceptControlSequence(event.ControlSequence) ||
		event.ProfileVersion == 0 || event.ProfileVersion != expectedProfileVersion ||
		event.SettingsVersion != expectedSettingsVersion {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_profile_ack", false)
		return false
	}
	c.stateMu.Lock()
	c.appliedProfileVersion = event.ProfileVersion
	c.appliedSettingsVersion = event.SettingsVersion
	c.profileAppliedAt = time.Now().UTC()
	c.stateMu.Unlock()
	return true
}

func (c *DeviceConnection) acceptControlSequence(sequence uint64) bool {
	if c.hasControlSeq && sequence <= c.lastControlSeq {
		c.server.metrics.controlRejected.Add(1)
		return false
	}
	c.lastControlSeq = sequence
	c.hasControlSeq = true
	return true
}

func (c *DeviceConnection) handleVAD(envelope deviceControlEnvelope, runtime *VoiceCoreMediaRuntime) bool {
	if err := requireVersion(envelope.Version, 2); err != nil {
		c.server.metrics.controlRejected.Add(1)
		return false
	}
	var event deviceVADEvent
	if err := jsonUnmarshalStrict(envelope.Raw, &event); err != nil || event.validate() != nil {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	if uint64(c.epoch) != event.StreamEpoch || !c.acceptControlSequence(event.ControlSequence) {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	if !validUnitInterval(event.Probability) || event.NearEndRMS < 0 {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	if envelope.Type == "vad.end" && event.VoicedEndSample > event.SamplePosition {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	if runtime == nil {
		return true
	}
	start := envelope.Type == "vad.start"
	if start && c.isPlaybackActive() &&
		!bargeInSourceAllowed(c.claims.DeviceSettings.AllowedBargeIn, "voice") {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("barge_source_forbidden", false)
		return false
	}
	if err := runtime.SendVAD(
		event.SamplePosition, event.VoicedEndSample,
		float32(event.Probability), float32(event.NearEndRMS), 0, start,
	); err != nil {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("vad_rejected", true)
		return false
	}
	return true
}

func (c *DeviceConnection) handleKeyword(envelope deviceControlEnvelope, runtime *VoiceCoreMediaRuntime) bool {
	if err := requireVersion(envelope.Version, 2); err != nil {
		c.server.metrics.controlRejected.Add(1)
		return false
	}
	var event deviceKeywordEvent
	if err := jsonUnmarshalStrict(envelope.Raw, &event); err != nil || event.validate() != nil {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	if uint64(c.epoch) != event.StreamEpoch || !c.acceptControlSequence(event.ControlSequence) {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	if !validUnitInterval(event.Confidence) || !validUnitInterval(event.Evidence.VADProbability) ||
		!validInterruptionEvidence(event.Evidence) || !event.ExpectedFence.valid() {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	if !bargeInSourceAllowed(c.claims.DeviceSettings.AllowedBargeIn, "keyword") {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("barge_source_forbidden", false)
		return false
	}
	if runtime == nil {
		return true
	}
	durationSamples := event.Evidence.DurationMS * 16
	if durationSamples == 0 {
		durationSamples = 16
	}
	end := event.Evidence.DetectedSample + durationSamples
	fence := event.ExpectedFence.toFence(c.sessionID)
	if err := runtime.SendKeyword(
		event.KeywordID, float32(event.Confidence),
		event.Evidence.DetectedSample, end, event.HardStop, fence,
	); err != nil {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("keyword_rejected", true)
		return false
	}
	return true
}

func (c *DeviceConnection) handleButtonStop(envelope deviceControlEnvelope, runtime *VoiceCoreMediaRuntime) bool {
	if err := requireVersion(envelope.Version, 2); err != nil {
		c.server.metrics.controlRejected.Add(1)
		return false
	}
	var event deviceButtonStop
	if err := jsonUnmarshalStrict(envelope.Raw, &event); err != nil || event.validate() != nil {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	if uint64(c.epoch) != event.StreamEpoch || !c.acceptControlSequence(event.ControlSequence) {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	if !event.ExpectedFence.valid() {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	if !bargeInSourceAllowed(c.claims.DeviceSettings.AllowedBargeIn, "button") {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("barge_source_forbidden", false)
		return false
	}
	if runtime == nil {
		return true
	}
	fence := event.ExpectedFence.toFence(c.sessionID)
	expected := &fence
	eventID := fmt.Sprintf("device-btn:%d:%d:%d", c.epoch, event.ControlSequence, event.DeviceMonotonicMS)
	if _, err := runtime.CancelGeneration(
		eventID, "device.button.stop", expected, event.DeviceMonotonicMS,
	); err != nil {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("stop_rejected", true)
		return false
	}
	return true
}

func (c *DeviceConnection) handlePlaybackReceipt(envelope deviceControlEnvelope, runtime *VoiceCoreMediaRuntime) bool {
	if err := requireVersion(envelope.Version, 2); err != nil {
		c.server.metrics.controlRejected.Add(1)
		return false
	}
	var receipt devicePlaybackReceipt
	if err := jsonUnmarshalStrict(envelope.Raw, &receipt); err != nil || receipt.validate() != nil {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	if uint64(c.epoch) != receipt.StreamEpoch || !c.acceptControlSequence(receipt.ControlSequence) {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	if !receipt.Fence.valid() || receipt.ReceivedSequence > 1<<32-1 {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	// A device may never upgrade a session's negotiated playback-watermark
	// quality in an individual receipt. In particular, the current board
	// advertises approximate output-commit watermarks; accepting
	// approximate=false would incorrectly promote them into Actual Heard.
	if c.playbackWatermark != "exact" && !receipt.Approximate {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("playback_watermark_precision_mismatch", false)
		return false
	}
	if c.ledger == nil {
		return true
	}
	progress, ok := c.ledger.record(receipt, receipt.DeviceMonotonicMS)
	if !ok {
		c.server.metrics.controlRejected.Add(1)
		return false
	}
	c.stateMu.Lock()
	c.playbackActive = envelope.Type == "playback.started" ||
		envelope.Type == "playback.progress"
	c.stateMu.Unlock()
	if runtime == nil {
		return true
	}
	progress.SessionID = c.sessionID
	progress.StreamEpoch = uint64(c.epoch)
	if err := runtime.SendPlaybackProgress(progress); err != nil {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("playback_receipt_rejected", true)
		return false
	}
	return true
}

func (c *DeviceConnection) handleDeviceState(envelope deviceControlEnvelope) bool {
	if err := requireVersion(envelope.Version, 2); err != nil {
		c.server.metrics.controlRejected.Add(1)
		return false
	}
	var event deviceStateEvent
	if err := jsonUnmarshalStrict(envelope.Raw, &event); err != nil || event.validate() != nil ||
		uint64(c.epoch) != event.StreamEpoch || !c.acceptControlSequence(event.ControlSequence) ||
		len(event.Value) > 32 || !validDeviceStateValue(event.Value) {
		c.server.metrics.controlRejected.Add(1)
		return false
	}
	return true
}

func (c *DeviceConnection) handleSessionClose(envelope deviceControlEnvelope) bool {
	if err := requireVersion(envelope.Version, 2); err != nil {
		c.server.metrics.controlRejected.Add(1)
		return false
	}
	var event deviceSessionClose
	if err := jsonUnmarshalStrict(envelope.Raw, &event); err != nil || event.validate() != nil ||
		uint64(c.epoch) != event.StreamEpoch || !c.acceptControlSequence(event.ControlSequence) ||
		event.Value.Reason == "" || len(event.Value.Reason) > 128 {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_control", false)
		return false
	}
	switch event.Value.Reason {
	case "device_close":
		c.markCloseReason(SessionCloseReasonDeviceClose)
	case SessionCloseReasonProfileInvalidated:
		c.markCloseReason(SessionCloseReasonProfileInvalidated)
	default:
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("invalid_close_reason", false)
		return false
	}
	c.closeWithCode(websocket.CloseNormalClosure, "session closed by device")
	return false
}

func validDeviceStateValue(value map[string]any) bool {
	for _, item := range value {
		switch typed := item.(type) {
		case nil, bool, float64:
			continue
		case string:
			if len(typed) > 1024 {
				return false
			}
		default:
			return false
		}
	}
	return true
}

func validInterruptionEvidence(evidence deviceInterruptionEvidence) bool {
	if evidence.Source != "button" && evidence.Source != "local_kws" &&
		evidence.Source != "local_vad" && evidence.Source != "cloud_asr" {
		return false
	}
	if evidence.AECMode != "none" && evidence.AECMode != "fd_low_cost" &&
		evidence.AECMode != "fd_high_quality" {
		return false
	}
	if evidence.SpeakerClass != "owner" && evidence.SpeakerClass != "guest" &&
		evidence.SpeakerClass != "ambiguous" && evidence.SpeakerClass != "unknown" {
		return false
	}
	if evidence.DurationMS > 60_000 || evidence.NearEndRMS < 0 || evidence.FarEndRMS < 0 {
		return false
	}
	if evidence.ResidualEcho != nil && !validUnitInterval(*evidence.ResidualEcho) {
		return false
	}
	return evidence.ASRPrefix == nil || len(*evidence.ASRPrefix) <= 64
}

// handleAudio validates and decodes one uplink audio frame.
func (c *DeviceConnection) handleAudio(data []byte) bool {
	if len(data) > DeviceMaxAudioFrameBytes {
		c.server.metrics.oversizeRejected.Add(1)
		c.closeWithCode(websocket.CloseMessageTooBig, "audio frame is too large")
		return false
	}
	if !c.audioLimiter.allow(time.Now()) {
		c.server.metrics.rateRejected.Add(1)
		c.closeWithCode(websocket.ClosePolicyViolation, "audio frame rate exceeded")
		return false
	}
	var frame MemoriaAudioFrameV1
	if err := frame.UnmarshalBinary(data); err != nil {
		c.server.metrics.controlRejected.Add(1)
		c.closeWithCode(websocket.CloseProtocolError, "invalid audio frame")
		return false
	}
	if err := frame.Validate(c.epoch, DeviceDirectionUplink); err != nil {
		c.server.metrics.controlRejected.Add(1)
		c.closeWithCode(websocket.CloseProtocolError, "invalid audio frame")
		return false
	}
	c.uplinkMu.Lock()
	if !c.hasUplinkSeq && (frame.Sequence != 0 || frame.SampleStart != 0) {
		c.uplinkMu.Unlock()
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("uplink_initial_clock_invalid", true)
		return false
	}
	if c.hasUplinkSeq && frame.Sequence != c.lastUplinkSeq+1 {
		c.uplinkMu.Unlock()
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("uplink_sequence_gap", true)
		return false
	}
	if c.hasUplinkSeq && frame.SampleStart < c.lastSampleEnd {
		c.uplinkMu.Unlock()
		c.server.metrics.controlRejected.Add(1)
		c.closeWithCode(websocket.CloseProtocolError, "uplink sample range moved backwards")
		return false
	}
	if c.hasUplinkSeq && frame.SampleStart != c.lastSampleEnd {
		gap := frame.SampleStart - c.lastSampleEnd
		c.server.metrics.uplinkGapSamples.Add(gap)
		c.uplinkMu.Unlock()
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("uplink_sample_gap", true)
		return false
	}
	c.lastUplinkSeq = frame.Sequence
	c.hasUplinkSeq = true
	c.lastSampleEnd = frame.SampleStart + uint64(frame.FrameSamples)
	c.uplinkMu.Unlock()

	runtime, session := c.runtimeRef()
	if runtime == nil || session == nil {
		c.server.metrics.uplinkFrames.Add(1)
		return true
	}
	pcm := make([]int16, DeviceUplinkFrameSamples)
	decoded, err := c.uplinkOpus.Decode(frame.Payload, pcm, false)
	if err != nil {
		c.server.metrics.opusErrors.Add(1)
		c.sendSessionError("opus_decode_failed", true)
		return true
	}
	if decoded != DeviceUplinkFrameSamples {
		c.server.metrics.opusErrors.Add(1)
		c.sendSessionError("opus_decode_failed", true)
		return true
	}
	payload := int16ToLittleEndian(pcm)
	c.server.metrics.uplinkFrames.Add(1)
	audioFrame := AudioFrame{
		SessionID:          c.sessionID,
		StreamEpoch:        uint64(c.epoch),
		Sequence:           uint64(frame.Sequence),
		CaptureStartSample: frame.SampleStart,
		FrameSamples:       DeviceUplinkFrameSamples,
		PayloadB64:         base64.StdEncoding.EncodeToString(payload),
	}
	if err := runtime.SendUplink(audioFrame); err != nil {
		c.server.metrics.controlRejected.Add(1)
		c.sendSessionError("uplink_rejected", true)
		return false
	}
	return true
}

func int16ToLittleEndian(samples []int16) []byte {
	payload := make([]byte, len(samples)*2)
	for index, sample := range samples {
		payload[index*2] = byte(sample)
		payload[index*2+1] = byte(sample >> 8)
	}
	return payload
}

// bargeInSourceAllowed reports whether the signed DeviceSettings allowlist
// permits the given barge-in source. The "none" source disables every other
// source even if the claim mixes entries, matching applyBargeInPolicy's
// fail-closed behavior at the event ingress boundary.
func bargeInSourceAllowed(allowed []string, source string) bool {
	allowNone := false
	for _, kind := range allowed {
		if kind == "none" {
			allowNone = true
		}
	}
	if allowNone {
		return false
	}
	for _, kind := range allowed {
		if kind == source {
			return true
		}
	}
	return false
}
