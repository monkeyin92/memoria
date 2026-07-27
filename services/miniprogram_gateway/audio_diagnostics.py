"""Explicit, bounded AEC before/after capture for one diagnostic session."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import wave
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class AecPcmCapture:
    """Buffer one short aligned PCM window and persist it as root-readable WAV."""

    def __init__(
        self,
        *,
        directory: Path,
        session_id: str,
        sample_rate: int,
        max_ms: int,
    ) -> None:
        if sample_rate <= 0 or max_ms <= 0:
            raise ValueError("invalid AEC diagnostic capture configuration")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self._directory = directory
        self._sample_rate = sample_rate
        self._max_bytes = sample_rate * 2 * max_ms // 1_000
        self._session_hash = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]
        self._started_at = datetime.now(UTC)
        self._pre = bytearray()
        self._post = bytearray()
        self._closed = False

    @classmethod
    def try_create(
        cls,
        *,
        directory: Path,
        session_id: str,
        capture_session_id: str,
        sample_rate: int,
        max_ms: int,
    ) -> AecPcmCapture | None:
        if not capture_session_id or capture_session_id != session_id:
            return None
        try:
            capture = cls(
                directory=directory,
                session_id=session_id,
                sample_rate=sample_rate,
                max_ms=max_ms,
            )
        except Exception:
            logger.warning("AEC diagnostic capture unavailable", exc_info=True)
            return None
        logger.info(
            "aec_diagnostic_capture_started session_hash=%s max_ms=%s",
            capture._session_hash,
            max_ms,
        )
        return capture

    def write(self, pre_aec: bytes, post_aec: bytes) -> None:
        if self._closed:
            return
        remaining = self._max_bytes - len(self._pre)
        size = min(remaining, len(pre_aec), len(post_aec))
        size -= size % 2
        if size <= 0:
            return
        self._pre.extend(pre_aec[:size])
        self._post.extend(post_aec[:size])
        if len(self._pre) >= self._max_bytes:
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._pre:
            return
        stamp = self._started_at.strftime("%Y%m%dT%H%M%S%fZ")
        stem = f"aec-{stamp}-{self._session_hash}"
        pre_path = self._directory / f"{stem}-pre.wav"
        post_path = self._directory / f"{stem}-post.wav"
        manifest_path = self._directory / f"{stem}.json"
        try:
            self._write_wav(pre_path, bytes(self._pre))
            self._write_wav(post_path, bytes(self._post))
            captured_ms = len(self._pre) * 1_000 // (self._sample_rate * 2)
            self._write_json(
                manifest_path,
                {
                    "created_at": self._started_at.isoformat(),
                    "session_hash": self._session_hash,
                    "sample_rate": self._sample_rate,
                    "channels": 1,
                    "sample_width_bytes": 2,
                    "captured_ms": captured_ms,
                    "pre_file": pre_path.name,
                    "post_file": post_path.name,
                    "pre_sha256": hashlib.sha256(self._pre).hexdigest(),
                    "post_sha256": hashlib.sha256(self._post).hexdigest(),
                },
            )
            logger.info(
                "aec_diagnostic_capture_saved session_hash=%s captured_ms=%s",
                self._session_hash,
                captured_ms,
            )
        except Exception:
            logger.warning("AEC diagnostic capture save failed", exc_info=True)

    def _write_wav(self, path: Path, pcm: bytes) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as raw:
                with wave.open(raw, "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(self._sample_rate)
                    output.writeframes(pcm)
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
