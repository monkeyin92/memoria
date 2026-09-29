"""SenseVoice offline rescue client for empty realtime ASR segments.

When FunASR realtime ends a VAD segment with zero usable finals, the session
sends the whole segment PCM to a one-shot offline SenseVoice endpoint and
re-emits the transcript as a normal provider final.  This module only owns the
HTTP contract; session-level gating and event synthesis live in
``funasr_stt.FunASRSession``.

Contract (both sides are ours):
    POST <endpoint>?format=pcm&sample_rate=<hz>&language=<lang>
    Content-Type: application/octet-stream
    Body: raw signed 16-bit little-endian mono PCM
    200 -> {"text": "<transcript>"}

The rescue must never break the realtime path: every failure mode returns
``None`` and is logged for diagnostics.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

# Punctuation, symbols and whitespace carry no speech.
_NON_SPEECH = re.compile(r"[\W_]+")


def slice_segment_pcm(
    chunks: Iterable[bytes],
    ranges: Iterable[tuple[int, int]],
    from_sample: int | None,
) -> tuple[bytes, int | None]:
    """Return the PCM at or after ``from_sample`` and its first sample index.

    ``ranges`` are the per-chunk ``(start, end)`` sample ranges of ``chunks``.
    Rescuing only the tail keeps the request short (the offline decoder runs at
    ~0.1x realtime) and stops the synthetic final from overlapping a provider
    final that already covers the prefix.  ``None`` keeps the whole segment.
    """

    kept: list[bytes] = []
    first: int | None = None
    for chunk, (start, end) in zip(chunks, ranges, strict=True):
        if from_sample is not None and end <= from_sample:
            continue
        if from_sample is not None and start < from_sample and end - start == len(chunk) // 2:
            chunk = chunk[(from_sample - start) * 2 :]
            start = from_sample
        kept.append(chunk)
        if first is None:
            first = start
    return b"".join(kept), first


@dataclass(frozen=True, slots=True)
class SenseVoiceRescueConfig:
    endpoint: str
    timeout_s: float = 2.5
    min_rms: int = 100
    min_peak_abs: int = 350
    # ~0.1-0.16 s decode per audio second on the 2-CPU sidecar plus transport:
    # 12 s stays inside the 2.5 s timeout (docs/runbooks/sensevoice-asr.md).
    max_audio_s: float = 12.0
    min_text_chars: int = 2

    def __post_init__(self) -> None:
        url = httpx.URL(self.endpoint)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("SenseVoice rescue endpoint must be HTTP(S)")
        if not 0 < self.timeout_s <= 10:
            raise ValueError("SenseVoice rescue timeout must be within (0, 10] seconds")
        if self.min_rms < 0:
            raise ValueError("SenseVoice rescue minimum RMS must be non-negative")
        if self.min_peak_abs < 0:
            raise ValueError("SenseVoice rescue minimum peak must be non-negative")
        if not 0 < self.max_audio_s <= 300:
            raise ValueError("SenseVoice rescue audio cap must be within (0, 300] seconds")
        if self.min_text_chars < 1:
            raise ValueError("SenseVoice rescue minimum text length must be positive")

    def accepts_text(self, text: str) -> bool:
        """Whether a rescue transcript has enough speech to become a final.

        Only word characters count toward ``min_text_chars``: SenseVoice
        answers silence and noise with a punctuated "我。" (measured on the
        production model, P1-02), which a raw length of 2 let through as a
        user turn.
        """

        return len(_NON_SPEECH.sub("", text)) >= self.min_text_chars

    @classmethod
    def from_env(cls, env: dict[str, str]) -> SenseVoiceRescueConfig:
        return cls(
            endpoint=env.get("SENSEVOICE_URL", "").strip(),
            timeout_s=float(env.get("SENSEVOICE_TIMEOUT_S", "2.5")),
            min_rms=int(env.get("SENSEVOICE_MIN_RMS", "100")),
            min_peak_abs=int(env.get("SENSEVOICE_MIN_PEAK_ABS", "350")),
            max_audio_s=float(env.get("SENSEVOICE_MAX_AUDIO_S", "12")),
            min_text_chars=int(env.get("SENSEVOICE_MIN_TEXT_CHARS", "2")),
        )


class SenseVoiceRescue:
    """One-shot offline transcription for realtime segments that ended empty."""

    def __init__(
        self,
        config: SenseVoiceRescueConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client

    async def transcribe(
        self,
        pcm: bytes,
        *,
        sample_rate: int,
        language: str,
    ) -> str | None:
        """Return the offline transcript, or None on any error/empty result."""

        if not pcm or len(pcm) % 2 or sample_rate <= 0:
            return None
        if self._client is not None:
            return await self._request(self._client, pcm, sample_rate, language)
        try:
            async with httpx.AsyncClient(timeout=self._config.timeout_s) as client:
                return await self._request(client, pcm, sample_rate, language)
        except (httpx.HTTPError, TypeError, ValueError):
            logger.warning("sensevoice rescue request failed", exc_info=True)
            return None

    async def _request(
        self,
        client: httpx.AsyncClient,
        pcm: bytes,
        sample_rate: int,
        language: str,
    ) -> str | None:
        try:
            response = await client.post(
                self._config.endpoint,
                params={
                    "format": "pcm",
                    "sample_rate": str(sample_rate),
                    "language": language,
                },
                content=pcm,
                headers={"Content-Type": "application/octet-stream"},
                timeout=self._config.timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, TypeError, ValueError):
            logger.warning("sensevoice rescue request failed", exc_info=True)
            return None
        if not isinstance(payload, dict):
            return None
        text = payload.get("text")
        if not isinstance(text, str):
            return None
        return text


__all__ = ["SenseVoiceRescue", "SenseVoiceRescueConfig"]
