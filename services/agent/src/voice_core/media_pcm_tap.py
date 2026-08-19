"""Env-gated PCM tap capturing admitted Media ingress audio for diagnostics.

The tap is disabled unless ``MEDIA_PCM_TAP_DIR`` points at a writable
directory.  When enabled, each session/stream-epoch pair gets one capped WAV
file containing exactly the PCM frames admitted toward the ASR provider, so
uplink corruption (energy, spectrum, timing) can be inspected offline.  The
tap is strictly fail-open: any I/O error closes and disables the tap for that
session without touching the audio path.
"""

from __future__ import annotations

import logging
import os
import wave
from dataclasses import dataclass

logger = logging.getLogger(__name__)

TAP_DIR_ENV = "MEDIA_PCM_TAP_DIR"
TAP_MAX_BYTES_ENV = "MEDIA_PCM_TAP_MAX_BYTES"
DEFAULT_TAP_MAX_BYTES = 10 * 1024 * 1024
SAMPLE_RATE_HZ = 16_000
_SAMPLE_WIDTH_BYTES = 2


def tap_directory() -> str:
    return os.getenv(TAP_DIR_ENV, "").strip()


def tap_max_bytes() -> int:
    raw = os.getenv(TAP_MAX_BYTES_ENV, "").strip()
    if not raw:
        return DEFAULT_TAP_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning("invalid %s=%r; using default", TAP_MAX_BYTES_ENV, raw)
        return DEFAULT_TAP_MAX_BYTES
    return max(0, value)


@dataclass(slots=True)
class _TapFile:
    writer: wave.Wave_write
    written_bytes: int


class MediaPcmTap:
    """Append admitted PCM to one capped WAV per session/stream epoch."""

    def __init__(
        self,
        *,
        directory: str,
        session_id: str,
        stream_epoch: int,
        max_bytes: int,
    ) -> None:
        self._directory = directory
        self._session_id = session_id
        self._stream_epoch = stream_epoch
        self._max_bytes = max(0, max_bytes)
        self._file: _TapFile | None = None
        self._disabled = self._max_bytes == 0

    @property
    def path(self) -> str:
        safe_epoch = max(0, self._stream_epoch)
        name = f"media-uplink-{self._session_id}-epoch{safe_epoch}.wav"
        return os.path.join(self._directory, name)

    @property
    def disabled(self) -> bool:
        return self._disabled

    @property
    def written_bytes(self) -> int:
        file = self._file
        return file.written_bytes if file is not None else 0

    def write(self, payload: bytes) -> None:
        if self._disabled or not payload:
            return
        try:
            file = self._file
            if file is None:
                file = self._open()
                self._file = file
            budget = self._max_bytes - file.written_bytes
            if budget <= 0:
                self._disabled = True
                self.close()
                return
            chunk = payload if len(payload) <= budget else payload[:budget]
            # Keep WAV PCM frame-aligned at 16-bit samples.
            if len(chunk) % _SAMPLE_WIDTH_BYTES:
                chunk = chunk[: len(chunk) - 1]
            if not chunk:
                self._disabled = True
                self.close()
                return
            file.writer.writeframesraw(chunk)
            file.written_bytes += len(chunk)
            if self._max_bytes - file.written_bytes < _SAMPLE_WIDTH_BYTES:
                self._disabled = True
                self.close()
        except Exception:
            logger.exception(
                "media PCM tap disabled after write failure session=%s stream_epoch=%s",
                self._session_id,
                self._stream_epoch,
            )
            self._disabled = True
            self.close()

    def close(self) -> None:
        file = self._file
        self._file = None
        if file is None:
            return
        try:
            file.writer.close()
        except Exception:
            logger.warning(
                "media PCM tap close failed session=%s stream_epoch=%s path=%s",
                self._session_id,
                self._stream_epoch,
                self.path,
            )

    def _open(self) -> _TapFile:
        os.makedirs(self._directory, exist_ok=True)
        writer = wave.open(self.path, "wb")
        writer.setnchannels(1)
        writer.setsampwidth(_SAMPLE_WIDTH_BYTES)
        writer.setframerate(SAMPLE_RATE_HZ)
        logger.info(
            "media PCM tap opened session=%s stream_epoch=%s path=%s max_bytes=%s",
            self._session_id,
            self._stream_epoch,
            self.path,
            self._max_bytes,
        )
        return _TapFile(writer=writer, written_bytes=0)


def maybe_create_tap(session_id: str, stream_epoch: int) -> MediaPcmTap | None:
    """Create a tap when enabled; never raise."""

    directory = tap_directory()
    if not directory:
        return None
    try:
        return MediaPcmTap(
            directory=directory,
            session_id=session_id,
            stream_epoch=stream_epoch,
            max_bytes=tap_max_bytes(),
        )
    except Exception:
        logger.exception("media PCM tap creation failed session=%s", session_id)
        return None
