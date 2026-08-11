"""Small stateful PyAV/libopus adapters for fixed 20 ms mono frames."""

from __future__ import annotations

from fractions import Fraction

import av

from services.device_media_gateway.protocol import ProtocolError


class OpusCodecError(ProtocolError):
    """An Opus packet or codec context cannot satisfy the device contract."""


class OpusEncoder:
    def __init__(self, *, sample_rate: int, frame_samples: int) -> None:
        if sample_rate not in (16_000, 24_000) or frame_samples != sample_rate // 50:
            raise ValueError("unsupported fixed Opus encoder format")
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self._context = av.CodecContext.create("libopus", "w")
        self._context.sample_rate = sample_rate
        self._context.layout = "mono"
        self._context.format = "s16"
        self._context.time_base = Fraction(1, sample_rate)
        self._context.options = {"application": "voip", "frame_duration": "20"}
        self._context.open()
        self._next_pts = 0

    def encode(self, pcm_s16le: bytes) -> bytes:
        expected = self.frame_samples * 2
        if len(pcm_s16le) != expected:
            raise OpusCodecError("PCM frame does not contain exactly 20 ms")
        frame = av.AudioFrame(format="s16", layout="mono", samples=self.frame_samples)
        frame.sample_rate = self.sample_rate
        frame.time_base = Fraction(1, self.sample_rate)
        frame.pts = self._next_pts
        self._next_pts += self.frame_samples
        frame.planes[0].update(pcm_s16le)
        try:
            packets = self._context.encode(frame)
        except av.error.FFmpegError as exc:
            raise OpusCodecError("Opus encoding failed") from exc
        if len(packets) != 1:
            raise OpusCodecError("Opus encoder did not produce one packet")
        return bytes(packets[0])


class OpusDecoder:
    def __init__(self, *, sample_rate: int, frame_samples: int, max_packet_bytes: int) -> None:
        if sample_rate not in (16_000, 24_000) or frame_samples != sample_rate // 50:
            raise ValueError("unsupported fixed Opus decoder format")
        self.sample_rate = sample_rate
        self.frame_samples = frame_samples
        self.max_packet_bytes = max_packet_bytes
        self._context = av.CodecContext.create("libopus", "r")
        self._context.layout = "mono"
        self._context.format = "s16"
        self._context.open()
        self._resampler = av.audio.resampler.AudioResampler(
            format="s16", layout="mono", rate=sample_rate
        )
        self._pending = bytearray()

    def decode(self, packet: bytes) -> tuple[bytes, ...]:
        if not packet or len(packet) > self.max_packet_bytes:
            raise OpusCodecError("Opus packet size is invalid")
        try:
            decoded = self._context.decode(av.Packet(packet))
        except av.error.FFmpegError as exc:
            raise OpusCodecError("Opus decoding failed") from exc
        for frame in decoded:
            converted = self._resampler.resample(frame)
            if not isinstance(converted, list):
                converted = [converted]
            for output in converted:
                if output is not None:
                    self._pending.extend(bytes(output.planes[0]))
        frame_bytes = self.frame_samples * 2
        frames: list[bytes] = []
        while len(self._pending) >= frame_bytes:
            frames.append(bytes(self._pending[:frame_bytes]))
            del self._pending[:frame_bytes]
        return tuple(frames)
