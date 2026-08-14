"""Hardware-agnostic interruption evidence (plan 8.4).

The device WSS/Edge path, the media-v1 gRPC seam and the H5/LiveKit path all
project their acoustic facts into this one model so the shared
``InterruptionPolicy`` never depends on a transport.  Absent acoustic fields
stay ``None`` and must fail conservative inside the policy; they are never
defaulted to "trusted".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.agent.src.orchestration.speech_timeline import SegmentKind, SpeechSegment


class InterruptionSource(StrEnum):
    BUTTON = "button"
    LOCAL_KWS = "local_kws"
    LOCAL_VAD = "local_vad"
    CLOUD_ASR = "cloud_asr"


@dataclass(frozen=True, slots=True)
class InterruptionEvidence:
    """One interruption candidate with its transport-level acoustic evidence.

    ``active_generation_id`` is the generation that was playing when the
    evidence was captured (0 = none).  ``detected_sample`` and
    ``device_monotonic_ms`` are device-clock facts used for tracing and
    SLO anchoring; policy logic must only compare like clocks.
    """

    session_id: str
    stream_epoch: int
    active_generation_id: int
    detected_sample: int
    source: InterruptionSource
    aec_mode: str = "unverified"
    aec_verified: bool = False
    vad_probability: float | None = None
    near_end_rms: float | None = None
    far_end_rms: float | None = None
    residual_echo_score: float | None = None
    speaker_class: str = "uncertain"
    asr_prefix: str | None = None
    duration_ms: int = 0
    device_monotonic_ms: int | None = None

    def __post_init__(self) -> None:
        if not self.session_id:
            raise ValueError("session_id is required")
        if self.stream_epoch < 1:
            raise ValueError("stream_epoch must be positive")
        if self.active_generation_id < 0:
            raise ValueError("active_generation_id must be non-negative")
        if self.detected_sample < 0:
            raise ValueError("detected_sample must be non-negative")
        if self.source not in InterruptionSource:
            raise ValueError("source must be an InterruptionSource")
        for name, value in (
            ("vad_probability", self.vad_probability),
            ("residual_echo_score", self.residual_echo_score),
        ):
            if value is not None and not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        for name, value in (
            ("near_end_rms", self.near_end_rms),
            ("far_end_rms", self.far_end_rms),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")
        if self.device_monotonic_ms is not None and self.device_monotonic_ms < 0:
            raise ValueError("device_monotonic_ms must be non-negative")

    @property
    def has_acoustic_evidence(self) -> bool:
        return (
            self.vad_probability is not None
            or self.near_end_rms is not None
            or self.residual_echo_score is not None
        )


_ASR_SEGMENT_KINDS = frozenset({SegmentKind.ASR_PARTIAL, SegmentKind.ASR_FINAL})


def source_for_segment_kind(kind: SegmentKind) -> InterruptionSource:
    """Map a speech-timeline segment kind to its evidence source."""

    if kind is SegmentKind.KWS:
        return InterruptionSource.LOCAL_KWS
    if kind is SegmentKind.VAD:
        return InterruptionSource.LOCAL_VAD
    if kind in _ASR_SEGMENT_KINDS:
        return InterruptionSource.CLOUD_ASR
    raise ValueError(f"segment kind cannot produce interruption evidence: {kind}")


_SAMPLE_RATE_HZ = 16_000


def evidence_from_speech_segment(
    segment: SpeechSegment,
    *,
    active_generation_id: int,
    aec_mode: str = "unverified",
    aec_verified: bool = False,
    duration_ms: int | None = None,
    device_monotonic_ms: int | None = None,
) -> InterruptionEvidence:
    """Project one sample-clock speech segment onto the shared model.

    VAD/KWS probability maps to ``vad_probability``; ASR text becomes the
    ``asr_prefix``.  Segment RMS/echo fields pass through untouched so the
    policy can decide how much acoustic weight a transport is entitled to.
    """

    kind = segment.kind
    source = source_for_segment_kind(kind)
    if duration_ms is None:
        duration_ms = max(
            0,
            (segment.capture_end_sample - segment.capture_start_sample)
            * 1_000
            // _SAMPLE_RATE_HZ,
        )
    text = (
        segment.text.strip()
        if kind in _ASR_SEGMENT_KINDS or kind is SegmentKind.KWS
        else None
    )
    return InterruptionEvidence(
        session_id=segment.session_id,
        stream_epoch=segment.stream_epoch,
        active_generation_id=active_generation_id,
        detected_sample=segment.capture_start_sample,
        source=source,
        aec_mode=aec_mode,
        aec_verified=aec_verified,
        vad_probability=(
            segment.confidence if kind in {SegmentKind.VAD, SegmentKind.KWS} else None
        ),
        near_end_rms=segment.near_end_rms,
        far_end_rms=segment.far_end_rms,
        residual_echo_score=segment.residual_echo_score,
        speaker_class=segment.speaker_class or "uncertain",
        asr_prefix=text or None,
        duration_ms=duration_ms,
        device_monotonic_ms=device_monotonic_ms,
    )


__all__ = [
    "InterruptionEvidence",
    "InterruptionSource",
    "evidence_from_speech_segment",
    "source_for_segment_kind",
]
