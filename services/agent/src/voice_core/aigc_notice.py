"""Configurable AIGC notice hooks for one device conversation round.

Off unless a deployment turns it on. When on, the notice is a short pure-tone
rhythm generated from parameters. No prompt text and no audio file lives here.
Speech mode only loads a pre-rendered external resource at its native, normal
speed; an unconfigured resource means silence, never a synthesized stand-in.
"""

from __future__ import annotations

import logging
import math
import struct
import wave
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Literal, cast

logger = logging.getLogger(__name__)

AigcNoticeMode = Literal["tone", "speech", "none"]
AigcNoticePosition = Literal["opening", "closing"]

AIGC_NOTICE_SAMPLE_RATE_HZ: Final = 24_000
#: 0.20 of full scale is close to normal companion speech and leaves headroom.
DEFAULT_TONE_VOLUME: Final = 0.20
DEFAULT_TONE_PATTERN: Final = "SL SS"
DEFAULT_LEAD_INTERVAL_MS: Final = 400
#: Upper bound on waiting for one notice to finish; a stalled downlink must not
#: hold the round's close open.
DEFAULT_MARKER_DURATION_MS: Final = 3000


def validate_tone_pattern(pattern: str) -> str:
    """Normalise ``S``/``L`` groups separated by whitespace.

    ``SL SS`` means short-long, then short-short. Any other character, an empty
    group, or a pattern without a group is invalid config, not a silent fallback.
    """

    normalized = " ".join(pattern.upper().split())
    if not normalized:
        raise ValueError("AIGC notice tone pattern must not be blank")
    for group in normalized.split(" "):
        if not group or any(character not in {"S", "L"} for character in group):
            raise ValueError(
                "AIGC notice tone pattern accepts only S, L and single spaces between groups"
            )
    return normalized


@dataclass(frozen=True, slots=True)
class AigcNoticeConfig:
    """Runtime policy for the opening and closing AIGC notice hooks.

    Off unless a deployment turns it on. The tone, its rhythm and both
    positions stay configured, so enabling it restores the previous notice
    without any other change.
    """

    enabled: bool = False
    mode: AigcNoticeMode = "tone"
    opening_enabled: bool = True
    closing_enabled: bool = True
    tone_pattern: str = DEFAULT_TONE_PATTERN
    tone_frequency_hz: float = 880.0
    tone_short_ms: int = 120
    tone_long_ms: int = 300
    tone_gap_ms: int = 90
    tone_group_gap_ms: int = 260
    tone_volume: float = DEFAULT_TONE_VOLUME
    # Default length of one notice clip; the environment clips it to the
    # (speech-mode only) resource length.
    marker_duration_ms: int = DEFAULT_MARKER_DURATION_MS
    lead_interval_ms: int = DEFAULT_LEAD_INTERVAL_MS
    speech_resource: str = ""
    speech_root: str = ""
    speech_sample_rate_hz: int = AIGC_NOTICE_SAMPLE_RATE_HZ

    def __post_init__(self) -> None:
        object.__setattr__(self, "tone_pattern", validate_tone_pattern(self.tone_pattern))
        if self.mode not in {"tone", "speech", "none"}:
            raise ValueError("AIGC notice mode must be tone, speech or none")
        if not math.isfinite(self.tone_frequency_hz) or self.tone_frequency_hz <= 0:
            raise ValueError("AIGC notice tone frequency must be finite and positive")
        if not 0.0 <= self.tone_volume <= 1.0:
            raise ValueError("AIGC notice tone volume must be between 0 and 1")
        if self.speech_sample_rate_hz != AIGC_NOTICE_SAMPLE_RATE_HZ:
            raise ValueError(
                f"AIGC notice speech must be {AIGC_NOTICE_SAMPLE_RATE_HZ} Hz mono PCM; "
                "the media downlink has no resampler"
            )
        for name in (
            "tone_short_ms",
            "tone_long_ms",
            "tone_gap_ms",
            "tone_group_gap_ms",
            "marker_duration_ms",
            "lead_interval_ms",
        ):
            value = cast(int, getattr(self, name))
            if value < 0:
                raise ValueError(f"AIGC notice {name} must be non-negative")
        if self.tone_short_ms == 0 or self.tone_long_ms == 0:
            raise ValueError("AIGC notice tone durations must be positive")

    @classmethod
    def from_settings(cls, settings: object) -> AigcNoticeConfig:
        def value(name: str, default: object) -> Any:
            return getattr(settings, name, default)

        return cls(
            enabled=bool(value("aigc_notice_enabled", False)),
            mode=cast(AigcNoticeMode, str(value("aigc_notice_mode", "tone"))),
            opening_enabled=bool(value("aigc_notice_opening_enabled", True)),
            closing_enabled=bool(value("aigc_notice_closing_enabled", True)),
            tone_pattern=str(value("aigc_notice_tone_pattern", DEFAULT_TONE_PATTERN)),
            tone_frequency_hz=float(value("aigc_notice_tone_frequency_hz", 880.0)),
            tone_short_ms=int(value("aigc_notice_tone_short_ms", 120)),
            tone_long_ms=int(value("aigc_notice_tone_long_ms", 300)),
            tone_gap_ms=int(value("aigc_notice_tone_gap_ms", 90)),
            tone_group_gap_ms=int(value("aigc_notice_tone_group_gap_ms", 260)),
            tone_volume=float(value("aigc_notice_tone_volume", DEFAULT_TONE_VOLUME)),
            marker_duration_ms=int(
                value("aigc_notice_marker_duration_ms", DEFAULT_MARKER_DURATION_MS)
            ),
            lead_interval_ms=int(value("aigc_notice_lead_interval_ms", DEFAULT_LEAD_INTERVAL_MS)),
            speech_resource=str(value("aigc_notice_speech_resource", "")).strip(),
            speech_root=str(value("aigc_notice_speech_root", "")).strip(),
            speech_sample_rate_hz=int(
                value("aigc_notice_speech_sample_rate_hz", AIGC_NOTICE_SAMPLE_RATE_HZ)
            ),
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AigcNoticeConfig:
        import os

        values = os.environ if env is None else env

        def enabled(name: str, default: bool) -> bool:
            raw = values.get(name)
            if raw is None:
                return default
            return raw.strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            enabled=enabled("AIGC_NOTICE_ENABLED", False),
            mode=cast(AigcNoticeMode, values.get("AIGC_NOTICE_MODE", "tone").strip()),
            opening_enabled=enabled("AIGC_NOTICE_OPENING_ENABLED", True),
            closing_enabled=enabled("AIGC_NOTICE_CLOSING_ENABLED", True),
            tone_pattern=values.get("AIGC_NOTICE_TONE_PATTERN", DEFAULT_TONE_PATTERN),
            tone_frequency_hz=float(values.get("AIGC_NOTICE_TONE_FREQUENCY_HZ", "880")),
            tone_short_ms=int(values.get("AIGC_NOTICE_TONE_SHORT_MS", "120")),
            tone_long_ms=int(values.get("AIGC_NOTICE_TONE_LONG_MS", "300")),
            tone_gap_ms=int(values.get("AIGC_NOTICE_TONE_GAP_MS", "90")),
            tone_group_gap_ms=int(values.get("AIGC_NOTICE_TONE_GROUP_GAP_MS", "260")),
            tone_volume=float(values.get("AIGC_NOTICE_TONE_VOLUME", str(DEFAULT_TONE_VOLUME))),
            marker_duration_ms=int(
                values.get("AIGC_NOTICE_MARKER_DURATION_MS", str(DEFAULT_MARKER_DURATION_MS))
            ),
            lead_interval_ms=int(values.get("AIGC_NOTICE_LEAD_INTERVAL_MS", "400")),
            speech_resource=values.get("AIGC_NOTICE_SPEECH_RESOURCE", "").strip(),
            speech_root=values.get("AIGC_NOTICE_SPEECH_ROOT", "").strip(),
            speech_sample_rate_hz=int(
                values.get("AIGC_NOTICE_SPEECH_SAMPLE_RATE_HZ", str(AIGC_NOTICE_SAMPLE_RATE_HZ))
            ),
        )

    def position_enabled(self, position: AigcNoticePosition) -> bool:
        if not self.enabled or self.mode == "none":
            return False
        return self.opening_enabled if position == "opening" else self.closing_enabled
    def marker_pcm(self, position: AigcNoticePosition) -> bytes | None:
        """Return playable 24 kHz mono s16le PCM, or ``None`` when unconfigured."""

        if not self.position_enabled(position):
            return None
        if self.mode == "tone":
            return self.tone_pcm()
        pcm = _load_speech_resource(self.speech_resource, self.speech_root)
        if pcm is None:
            logger.warning(
                "AIGC notice speech mode has no playable external resource; "
                "playing no notice position=%s",
                position,
            )
        return pcm

    def tone_pcm(self) -> bytes:
        """Render the configured rhythm as a pure sine tone, without writing a file."""

        samples = bytearray()
        amplitude = int(round(32767 * self.tone_volume))
        groups = self.tone_pattern.split(" ")
        for group_index, group in enumerate(groups):
            for tone_index, character in enumerate(group):
                duration_ms = self.tone_short_ms if character == "S" else self.tone_long_ms
                samples.extend(_sine_pcm(self.tone_frequency_hz, duration_ms, amplitude))
                if tone_index + 1 < len(group):
                    samples.extend(_silence_pcm(self.tone_gap_ms))
            if group_index + 1 < len(groups):
                samples.extend(_silence_pcm(self.tone_group_gap_ms))
        return bytes(samples)


def opening_marker_ready_at(
    *,
    started_at: float,
    pcm: bytes,
    sample_rate_hz: int,
    lead_interval_ms: int,
) -> float:
    """When the wake acknowledgement may start after the opening marker.

    The configured gap follows the whole clip at its native playback rate, not
    the clip's first sample, so a 1.1 s tone plus a 400 ms gap delays 「我在呢」
    by 1.5 s rather than overlapping the tone.
    """

    if sample_rate_hz <= 0:
        raise ValueError("opening marker sample rate must be positive")
    clip_s = len(pcm) / 2 / sample_rate_hz
    return started_at + clip_s + max(0, lead_interval_ms) / 1_000


def _sine_pcm(frequency_hz: float, duration_ms: int, amplitude: int) -> bytes:
    sample_count = max(1, round(AIGC_NOTICE_SAMPLE_RATE_HZ * duration_ms / 1_000))
    fade_samples = min(sample_count // 2, max(1, AIGC_NOTICE_SAMPLE_RATE_HZ // 200))
    out = bytearray()
    for index in range(sample_count):
        envelope = 1.0
        if index < fade_samples:
            envelope = index / fade_samples
        elif index >= sample_count - fade_samples:
            envelope = (sample_count - index - 1) / fade_samples
        value = int(
            round(
                amplitude
                * envelope
                * math.sin(2.0 * math.pi * frequency_hz * index / AIGC_NOTICE_SAMPLE_RATE_HZ)
            )
        )
        out.extend(struct.pack("<h", max(-32768, min(32767, value))))
    return bytes(out)


def _silence_pcm(duration_ms: int) -> bytes:
    return b"\x00\x00" * max(0, round(AIGC_NOTICE_SAMPLE_RATE_HZ * duration_ms / 1_000))


def _resource_path(resource: str, root: str) -> Path | None:
    candidate = Path(resource)
    if candidate.is_file():
        return candidate
    if not root:
        return None
    base = Path(root)
    for suffix in (".wav", ".s16le"):
        path = base / f"{resource}{suffix}"
        if path.is_file():
            return path
    return None


@lru_cache(maxsize=8)
def _load_speech_resource(resource: str, root: str) -> bytes | None:
    if not resource:
        return None
    path = _resource_path(resource, root)
    if path is None:
        logger.warning("AIGC notice speech resource not found resource=%s", resource)
        return None
    try:
        if path.suffix.lower() == ".wav":
            return _read_wav(path)
        if path.suffix.lower() == ".s16le":
            pcm = path.read_bytes()
            if not pcm or len(pcm) % 2:
                raise ValueError("raw PCM must be non-empty 16-bit audio")
            return pcm
        raise ValueError("speech resource must be .wav or .s16le")
    except (OSError, ValueError, wave.Error) as exc:
        logger.warning(
            "AIGC notice speech resource is unreadable resource=%s error=%s",
            resource,
            type(exc).__name__,
        )
        return None


def _read_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1:
            raise ValueError("speech WAV must be mono")
        if handle.getsampwidth() != 2:
            raise ValueError("speech WAV must be 16-bit PCM")
        if handle.getframerate() != AIGC_NOTICE_SAMPLE_RATE_HZ:
            raise ValueError(
                f"speech WAV must be {AIGC_NOTICE_SAMPLE_RATE_HZ} Hz to play at normal speed"
            )
        pcm = handle.readframes(handle.getnframes())
    if not pcm or len(pcm) % 2:
        raise ValueError("speech WAV must contain non-empty 16-bit audio")
    return pcm


__all__ = [
    "AIGC_NOTICE_SAMPLE_RATE_HZ",
    "AigcNoticeConfig",
    "AigcNoticeMode",
    "AigcNoticePosition",
    "DEFAULT_LEAD_INTERVAL_MS",
    "DEFAULT_MARKER_DURATION_MS",
    "DEFAULT_TONE_PATTERN",
    "DEFAULT_TONE_VOLUME",
    "opening_marker_ready_at",
    "validate_tone_pattern",
]
