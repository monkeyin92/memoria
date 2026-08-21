package mediaedge

import (
	"context"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/pion/webrtc/v4"
	"github.com/pion/webrtc/v4/pkg/media"
)

// sendDownlink owns the PCM-to-Opus hot path for one peer. Its buffers stay
// peer-scoped so a session has a directly bounded working set.
func (p *webRTCPeer) sendDownlink(ctx context.Context, frame AudioFrame) error {
	payload, err := frame.Payload()
	if err != nil {
		return err
	}
	if len(payload)%2 != 0 {
		return fmt.Errorf("downlink PCM must contain 16-bit samples")
	}
	if cap(p.downlinkPCM) < len(payload)/2 {
		p.downlinkPCM = make([]int16, len(payload)/2)
	}
	samples := p.downlinkPCM[:len(payload)/2]
	for index := range samples {
		samples[index] = int16(binary.LittleEndian.Uint16(payload[index*2:]))
	}
	fence := Fence{SessionID: frame.SessionID, TurnID: frame.TurnID, GenerationID: frame.GenerationID, ToolEpoch: frame.ToolEpoch, SessionEpoch: frame.SessionEpoch}
	p.encoderMu.Lock()
	defer p.encoderMu.Unlock()
	if p.closed.Load() {
		return io.ErrClosedPipe
	}
	if !p.encodeFence.Equal(fence) {
		p.encodeBuf = p.encodeBuf[:0]
		p.encodeFence = fence
		if err := p.encoder.Reset(); err != nil {
			return err
		}
	}
	for _, sample := range samples {
		p.encodeBuf = append(p.encodeBuf, sample, sample)
	}
	for len(p.encodeBuf) >= opusFrameSamples {
		if err := p.writeOpusFrame(ctx, p.encodeBuf[:opusFrameSamples]); err != nil {
			return err
		}
		remaining := len(p.encodeBuf) - opusFrameSamples
		copy(p.encodeBuf, p.encodeBuf[opusFrameSamples:])
		p.encodeBuf = p.encodeBuf[:remaining]
	}
	if frame.Final && len(p.encodeBuf) > 0 {
		clear(p.paddedPCM)
		copy(p.paddedPCM, p.encodeBuf)
		p.encodeBuf = p.encodeBuf[:0]
		if err := p.writeOpusFrame(ctx, p.paddedPCM); err != nil {
			return err
		}
	}
	return nil
}

func (p *webRTCPeer) writeOpusFrame(ctx context.Context, samples []int16) error {
	select {
	case <-ctx.Done():
		return ErrStaleDownlinkGeneration
	default:
	}
	if len(p.opusPacket) < 4_000 {
		p.opusPacket = make([]byte, 4_000)
	}
	n, err := p.encoder.Encode(samples, p.opusPacket)
	if err != nil {
		return fmt.Errorf("encode downlink Opus: %w", err)
	}
	select {
	case <-ctx.Done():
		return ErrStaleDownlinkGeneration
	default:
	}
	if err := p.downlink.WriteSample(media.Sample{Data: p.opusPacket[:n], Duration: mediaFrameDuration}); err != nil {
		return fmt.Errorf("write downlink RTP: %w", err)
	}
	if ctx.Err() != nil {
		return ErrStaleDownlinkGeneration
	}
	return nil
}

// receiveTrack owns RTP validation, Opus decoding, PLC/FEC and VAD ingress.
// Its output is range-stamped PCM; all conversation decisions stay in Core.
func (p *webRTCPeer) receiveTrack(track *webrtc.TrackRemote) {
	if !strings.EqualFold(track.Codec().MimeType, webrtc.MimeTypeOpus) || track.Codec().ClockRate != opusSampleRate {
		p.terminator.failPeer(p, fmt.Errorf("uplink must be 48 kHz Opus"))
		return
	}
	decoder, err := newOpusDecoder(uplinkSampleRate, 1)
	if err != nil {
		p.terminator.failPeer(p, err)
		return
	}
	defer decoder.close()
	vad, err := newMediaVAD(p.terminator.config.VAD)
	if err != nil {
		p.terminator.failPeer(p, err)
		return
	}
	runtime, err := p.waitRuntime(5 * time.Second)
	if err != nil {
		p.terminator.failPeer(p, err)
		return
	}
	buffer := make([]int16, 0, uplinkFrameSamples*2)
	decoded := make([]int16, 1_920)
	concealed := make([]int16, 1_920)
	var captureSample, frameSequence uint64
	var lastRTPSequence uint16
	var lastTimestamp uint32
	var lastDecoded int
	// Keep the concealed interval relative to the pending PCM buffer so only
	// frames that actually contain reconstructed samples are marked.
	concealedStart, concealedEnd := -1, -1
	haveRTP := false
	flushFrame := func(samples []int16, final, lossConcealed bool) error {
		payload := make([]byte, len(samples)*2)
		for index, sample := range samples {
			binary.LittleEndian.PutUint16(payload[index*2:], uint16(sample))
		}
		frame := AudioFrame{
			SessionID: p.request.SessionID, StreamEpoch: p.request.StreamEpoch,
			Sequence: frameSequence, CaptureStartSample: captureSample,
			FrameSamples: uint64(len(samples)), PayloadB64: base64.StdEncoding.EncodeToString(payload),
			Final: final, LossConcealed: lossConcealed,
		}
		if event := vad.observe(captureSample, samples); event != nil {
			if err := runtime.SendVAD(event.Sample, event.VoicedEnd, event.Probability, event.RMS, event.NoiseFloor, event.Start); err != nil {
				return err
			}
		}
		if err := runtime.SendUplink(frame); err != nil {
			return err
		}
		frameSequence++
		captureSample += uint64(len(samples))
		return nil
	}
	appendSamples := func(samples []int16, lossConcealed bool) error {
		if lossConcealed && len(samples) > 0 {
			if concealedStart < 0 {
				concealedStart = len(buffer)
			}
			concealedEnd = len(buffer) + len(samples)
		}
		buffer = append(buffer, samples...)
		for len(buffer) >= uplinkFrameSamples {
			frameLossConcealed := concealedStart >= 0 && concealedStart < uplinkFrameSamples && concealedEnd > 0
			if err := flushFrame(buffer[:uplinkFrameSamples], false, frameLossConcealed); err != nil {
				return err
			}
			buffer = buffer[uplinkFrameSamples:]
			if concealedStart >= 0 {
				concealedStart -= uplinkFrameSamples
				concealedEnd -= uplinkFrameSamples
				if concealedEnd <= 0 {
					concealedStart, concealedEnd = -1, -1
				} else if concealedStart < 0 {
					concealedStart = 0
				}
			}
		}
		return nil
	}
	concealGap := func(gap int, packet []byte, tryFEC bool) error {
		remaining := gap
		if tryFEC && remaining > 0 && remaining <= len(concealed) {
			n, fecErr := decoder.Decode(packet, concealed[:remaining], true)
			if fecErr == nil && n > 0 && n <= remaining {
				if err := appendSamples(concealed[:n], true); err != nil {
					return err
				}
				remaining -= n
				p.terminator.server.OpusFECFrames.Add(1)
			}
		}
		for remaining > 0 {
			chunk := remaining
			if chunk > uplinkFrameSamples {
				chunk = uplinkFrameSamples
			}
			clear(concealed[:chunk])
			n, plcErr := decoder.Decode(nil, concealed[:chunk], false)
			if plcErr == nil && n == chunk {
				if err := appendSamples(concealed[:n], true); err != nil {
					return err
				}
				p.terminator.server.OpusPLCFrames.Add(1)
			} else {
				if err := appendSamples(concealed[:chunk], true); err != nil {
					return err
				}
				p.terminator.server.OpusSilenceFrames.Add(1)
			}
			remaining -= chunk
		}
		return nil
	}
	for {
		packet, _, err := track.ReadRTP()
		if err != nil {
			if !errors.Is(err, io.EOF) && !p.closed.Load() {
				p.terminator.report(err)
			}
			break
		}
		sequenceDelta := uint16(1)
		if haveRTP {
			sequenceDelta = uint16(packet.SequenceNumber - lastRTPSequence)
			if sequenceDelta == 0 || sequenceDelta > 0x8000 {
				continue
			}
		}
		gap := 0
		tryFEC := false
		if haveRTP {
			timestampDelta := uint32(packet.Timestamp - lastTimestamp)
			if timestampDelta > opusSampleRate*2 || timestampDelta%3 != 0 {
				p.terminator.failPeer(p, fmt.Errorf("uplink RTP timestamp discontinuity"))
				return
			}
			gap = int(timestampDelta/3) - lastDecoded
			if gap < 0 {
				continue
			}
			if gap > maxUplinkGapSamples {
				p.terminator.failPeer(p, fmt.Errorf("uplink RTP gap exceeds one second"))
				return
			}
			if gap > 0 {
				tryFEC = sequenceDelta == 2
				if err := concealGap(gap, packet.Payload, tryFEC); err != nil {
					p.terminator.failPeer(p, err)
					return
				}
			}
		}
		n, err := decoder.Decode(packet.Payload, decoded, false)
		if err != nil {
			p.terminator.failPeer(p, fmt.Errorf("decode uplink Opus: %w", err))
			return
		}
		if n <= 0 {
			p.terminator.failPeer(p, fmt.Errorf("decode uplink Opus returned no samples"))
			return
		}
		if err := appendSamples(decoded[:n], false); err != nil {
			p.terminator.failPeer(p, err)
			return
		}
		haveRTP = true
		lastRTPSequence = packet.SequenceNumber
		lastTimestamp = packet.Timestamp
		lastDecoded = n
	}
	if len(buffer) > 0 && !p.closed.Load() {
		padded := make([]int16, uplinkFrameSamples)
		copy(padded, buffer)
		frameLossConcealed := concealedStart >= 0 && concealedStart < len(buffer) && concealedEnd > 0
		if err := flushFrame(padded, true, frameLossConcealed); err != nil {
			p.terminator.failPeer(p, err)
			return
		}
	}
	if event := vad.flush(captureSample); event != nil {
		if err := runtime.SendVAD(event.Sample, event.VoicedEnd, event.Probability, event.RMS, event.NoiseFloor, false); err != nil {
			p.terminator.failPeer(p, err)
		}
	}
}
