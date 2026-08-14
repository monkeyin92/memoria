"""Hardware-agnostic interruption evidence and the shared interruption policy.

The models in this package are transport-neutral: the device WSS/Edge path,
the media-v1 gRPC seam and the H5/LiveKit path all project their acoustic
facts into ``InterruptionEvidence`` and consume exactly one
``InterruptionPolicy``, so interruption semantics stay converged instead of
growing parallel ``if`` chains inside a runtime.
"""

from services.agent.src.voice_core.interruption.evidence import (
    InterruptionEvidence,
    InterruptionSource,
    evidence_from_speech_segment,
    source_for_segment_kind,
)
from services.agent.src.voice_core.interruption.policy import (
    InterruptionPolicy,
    InterruptionPolicyDecision,
    InterruptionVerdict,
    SpeakerProfile,
)

__all__ = [
    "InterruptionEvidence",
    "InterruptionPolicy",
    "InterruptionPolicyDecision",
    "InterruptionSource",
    "InterruptionVerdict",
    "SpeakerProfile",
    "evidence_from_speech_segment",
    "source_for_segment_kind",
]
