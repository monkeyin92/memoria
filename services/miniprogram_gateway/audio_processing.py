"""Mini Program-only WebRTC audio processing at the PCM gateway seam."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from livekit import rtc

logger = logging.getLogger(__name__)


class MiniProgramAudioProcessor:
    """Feed phone playout and capture through one LiveKit WebRTC APM instance."""

    def __init__(
        self,
        *,
        enabled: bool,
        downlink_sample_rate: int,
        uplink_sample_rate: int,
        stream_delay_ms: int,
        active_window_ms: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            downlink_sample_rate <= 0
            or uplink_sample_rate <= 0
            or downlink_sample_rate % 100
            or uplink_sample_rate % 100
            or stream_delay_ms < 0
            or active_window_ms <= 0
        ):
            raise ValueError("invalid Mini Program audio processor configuration")
        self._downlink_sample_rate = downlink_sample_rate
        self._uplink_sample_rate = uplink_sample_rate
        self._configured_enabled = enabled
        self._stream_delay_ms = stream_delay_ms
        self._active_window_ms = active_window_ms
        self._active_window_s = active_window_ms / 1_000
        self._clock = clock
        self._last_reverse_at: float | None = None
        self._reverse_since_uplink = False
        self._apm: rtc.AudioProcessingModule | None = None
        self._initialize()

    @property
    def aec_ready(self) -> bool:
        return self._apm is not None

    def _initialize(self) -> None:
        if not self._configured_enabled:
            return
        try:
            apm = rtc.AudioProcessingModule(echo_cancellation=True)
            apm.set_stream_delay_ms(self._stream_delay_ms)
            self._apm = apm
            if (
                self._process(
                    bytes(self._downlink_sample_rate // 50),
                    sample_rate=self._downlink_sample_rate,
                    reverse=True,
                )
                is None
                or self._process(
                    bytes(self._uplink_sample_rate // 50),
                    sample_rate=self._uplink_sample_rate,
                    reverse=False,
                )
                is None
            ):
                return
            logger.info(
                "mini_program_aec_ready stream_delay_ms=%s active_window_ms=%s",
                self._stream_delay_ms,
                self._active_window_ms,
            )
        except Exception:
            self._apm = None
            logger.warning(
                "Mini Program echo cancellation is unavailable; using unprocessed PCM",
                exc_info=True,
            )

    def reset(self) -> None:
        """Forget stale reference timing without discarding the learned echo path."""
        self._last_reverse_at = None
        self._reverse_since_uplink = False
        if self._configured_enabled and self._apm is None:
            self._initialize()

    def observe_downlink(self, payload: bytes) -> None:
        if self._process(payload, sample_rate=self._downlink_sample_rate, reverse=True) is not None:
            self._last_reverse_at = self._clock()
            self._reverse_since_uplink = True

    def process_uplink(self, payload: bytes) -> bytes:
        last_reverse_at = self._last_reverse_at
        if last_reverse_at is None or self._clock() - last_reverse_at > self._active_window_s:
            return payload
        if not self._reverse_since_uplink:
            silence_bytes = len(payload) * self._downlink_sample_rate // self._uplink_sample_rate
            if (
                self._process(
                    bytes(silence_bytes),
                    sample_rate=self._downlink_sample_rate,
                    reverse=True,
                )
                is None
            ):
                return payload
        self._reverse_since_uplink = False
        return (
            self._process(
                payload,
                sample_rate=self._uplink_sample_rate,
                reverse=False,
            )
            or payload
        )

    def _process(self, payload: bytes, *, sample_rate: int, reverse: bool) -> bytes | None:
        apm = self._apm
        if apm is None:
            return None
        samples_per_chunk = sample_rate // 100
        bytes_per_chunk = samples_per_chunk * 2
        if not payload or len(payload) % bytes_per_chunk:
            logger.warning(
                "Mini Program APM received non-10ms PCM; using unprocessed PCM direction=%s bytes=%s",
                "downlink" if reverse else "uplink",
                len(payload),
            )
            return None
        processed = bytearray()
        try:
            for offset in range(0, len(payload), bytes_per_chunk):
                frame = rtc.AudioFrame(
                    data=payload[offset : offset + bytes_per_chunk],
                    sample_rate=sample_rate,
                    num_channels=1,
                    samples_per_channel=samples_per_chunk,
                )
                if reverse:
                    apm.process_reverse_stream(frame)
                else:
                    apm.process_stream(frame)
                processed.extend(frame.data)
        except Exception:
            self._apm = None
            logger.warning(
                "Mini Program APM failed; disabling it for this media session",
                exc_info=True,
            )
            return None
        return bytes(processed)
