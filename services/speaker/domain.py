"""Public contracts for owner, guest, and uncertain speaker decisions."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol

SpeakerClassification = Literal["owner", "guest", "uncertain"]
RiskAssessment = Literal["verified", "unavailable"]


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vector: tuple[float, ...]
    speech_ms: int
    snr_db: float
    quality_score: float
    replay_risk: float
    synthetic_risk: float
    risk_assessment: RiskAssessment = "verified"

    def __post_init__(self) -> None:
        if not self.vector:
            raise ValueError("speaker embedding must not be empty")
        if not all(math.isfinite(value) for value in self.vector):
            raise ValueError("speaker embedding must contain finite values")
        if self.speech_ms < 0 or not math.isfinite(self.snr_db):
            raise ValueError("speaker embedding metrics must be finite and non-negative")
        if not all(
            0 <= value <= 1 for value in (self.quality_score, self.replay_risk, self.synthetic_risk)
        ):
            raise ValueError("quality and spoof risks must be between 0 and 1")
        if self.risk_assessment not in {"verified", "unavailable"}:
            raise ValueError("speaker risk assessment state is invalid")


class SpeakerEmbeddingAdapter(Protocol):
    model_version: str

    async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult: ...


@dataclass(frozen=True, slots=True)
class EnrollmentSample:
    pcm: bytes
    sample_rate: int
    device: str = "unknown"
    scene: str = "unknown"

    def __post_init__(self) -> None:
        if not self.pcm or self.sample_rate < 8000:
            raise ValueError("enrollment sample must contain supported PCM audio")


@dataclass(frozen=True, slots=True)
class EnrollmentRequest:
    account_id: str
    consent_grant_id: str
    samples: tuple[EnrollmentSample, ...]

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.consent_grant_id.strip():
            raise ValueError("account_id and consent_grant_id must not be blank")
        if len(self.samples) < 3:
            raise ValueError("speaker enrollment requires at least three samples")


@dataclass(frozen=True, slots=True)
class EnrollmentResult:
    profile_id: str
    identity_id: str
    template_version: int
    model_version: str
    sample_count: int
    status: Literal["shadow", "active"]
    consent_grant_id: str


@dataclass(frozen=True, slots=True)
class SpeakerEvaluation:
    report_ref: str
    sample_count: int
    far: float
    frr: float
    eer: float
    unknown_rejection: float
    passed: bool

    def __post_init__(self) -> None:
        if not self.report_ref.strip():
            raise ValueError("speaker evaluation report_ref must not be blank")
        if self.sample_count < 200:
            raise ValueError("speaker activation requires at least 200 evaluation samples")
        if not all(
            math.isfinite(value) and 0 <= value <= 1
            for value in (self.far, self.frr, self.eer, self.unknown_rejection)
        ):
            raise ValueError("speaker evaluation metrics must be between 0 and 1")
        if not self.passed:
            raise ValueError("speaker evaluation must pass before activation")


@dataclass(frozen=True, slots=True)
class SpeakerProfileSummary:
    profile_id: str
    identity_id: str
    template_version: int
    model_version: str
    sample_count: int
    status: Literal["shadow", "active", "revoked"]
    evaluation_ref: str | None
    evaluation_sample_count: int | None
    far: float | None
    frr: float | None
    eer: float | None
    unknown_rejection: float | None
    created_at: str
    activated_at: str | None
    revoked_at: str | None


@dataclass(frozen=True, slots=True)
class SpeakerSample:
    account_id: str
    pcm: bytes
    sample_rate: int
    session_id: str | None = None

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.pcm or self.sample_rate < 8000:
            raise ValueError("speaker sample must include account and supported PCM audio")


@dataclass(frozen=True, slots=True)
class SpeakerPermissions:
    normal_conversation: bool
    read_private_memory: bool
    write_long_term_memory: bool
    sensitive_actions: bool


def permissions_for_speaker(classification: SpeakerClassification) -> SpeakerPermissions:
    if classification == "owner":
        return SpeakerPermissions(True, True, True, False)
    return SpeakerPermissions(True, False, False, False)


@dataclass(frozen=True, slots=True)
class SpeakerDecision:
    classification: SpeakerClassification
    score: float | None
    quality_score: float
    reason_code: str
    model_version: str
    template_version: int | None
    profile_id: str | None
    permissions: SpeakerPermissions

    def __post_init__(self) -> None:
        if self.score is not None and (not math.isfinite(self.score) or not -1 <= self.score <= 1):
            raise ValueError("speaker score must be finite and between -1 and 1")
        if not math.isfinite(self.quality_score) or not 0 <= self.quality_score <= 1:
            raise ValueError("speaker quality_score must be between 0 and 1")
        if not self.reason_code.strip() or not self.model_version.strip():
            raise ValueError("speaker decision reason and model version are required")
        if self.permissions != permissions_for_speaker(self.classification):
            raise ValueError("speaker permissions do not match classification")


@dataclass(frozen=True, slots=True)
class RevokeSpeakerProfile:
    account_id: str
    profile_id: str
    reason: str


class EnrollmentQualityError(ValueError):
    pass


class SpeakerProfileNotFoundError(LookupError):
    pass


class SpeakerAuthorityPort(Protocol):
    async def enroll(self, request: EnrollmentRequest) -> EnrollmentResult: ...

    async def activate(
        self,
        profile_id: str,
        *,
        account_id: str,
        evaluation: SpeakerEvaluation,
    ) -> None: ...

    async def classify(self, sample: SpeakerSample) -> SpeakerDecision: ...

    async def profiles(self, account_id: str) -> tuple[SpeakerProfileSummary, ...]: ...

    async def revoke(self, request: RevokeSpeakerProfile) -> None: ...
