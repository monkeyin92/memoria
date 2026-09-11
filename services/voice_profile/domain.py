"""Public voice-profile records and provider boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

VoiceProfileStatus = Literal["enrolling", "candidate", "active", "failed", "revoked"]
VoiceDeletionStatus = Literal["not_requested", "pending", "completed", "failed"]
VoiceBlindSlot = Literal["A", "B"]
VoiceEnrollmentOperationState = Literal[
    "intent",
    "upload_submitted",
    "sample_uploaded",
    "provider_submitted",
    "provider_created",
    "completed",
    "reconciliation_required",
    "revoked",
]

MIN_SUBJECTIVE_SCORE = 3.5
MAX_UNCANNY_SCORE = 2.5
MAX_FIRST_AUDIO_MS = 1500
MAX_CANCEL_TAIL_MS = 250
MAX_TIMESTAMP_ERROR_MS = 250
MIN_LONG_SENTENCE_CHARS = 200
MIN_LONG_SENTENCE_COMPLETION_RATIO = 0.98


def subjective_voice_evaluation_passes(
    *,
    candidate_preferred: bool | None,
    similarity: float,
    naturalness: float,
    accent_similarity: float,
    emotion_adherence: float,
    instruction_adherence: float,
    uncanny: float,
) -> bool:
    return (
        candidate_preferred is True
        and min(
            similarity,
            naturalness,
            accent_similarity,
            emotion_adherence,
            instruction_adherence,
        )
        >= MIN_SUBJECTIVE_SCORE
        and uncanny <= MAX_UNCANNY_SCORE
    )


def objective_voice_quality_passes(
    *,
    first_audio_ms: int,
    cancel_tail_ms: int,
    timestamp_error_ms: int,
    long_sentence_chars: int,
    long_sentence_completion_ratio: float,
) -> bool:
    """Evaluate a trusted long-sentence probe against fixed release thresholds."""
    return (
        first_audio_ms <= MAX_FIRST_AUDIO_MS
        and cancel_tail_ms <= MAX_CANCEL_TAIL_MS
        and timestamp_error_ms <= MAX_TIMESTAMP_ERROR_MS
        and long_sentence_chars >= MIN_LONG_SENTENCE_CHARS
        and long_sentence_completion_ratio >= MIN_LONG_SENTENCE_COMPLETION_RATIO
    )


def voice_profile_delivery_admitted(
    *,
    evaluation_status: str,
    quality_status: str,
    sample_validation_status: str,
) -> bool:
    """The one rule deciding whether a cloned voice may reach a device.

    Two honest admissions exist and a profile needs one of them:

    * ``sample_validation_status == "passed"`` -- the consumer path, where
      the submitted recording itself was decoded and measured
      (``services/voice_profile/sample_validation.py``);
    * ``evaluation_status == "passed"`` **and**
      ``quality_status == "passed"`` -- the lab path, where a human A/B
      evaluation and a TTS-output quality probe both ran.

    An explicit ``failed`` on any of the three vetoes, so a measured
    rejection can never be overwritten into delivery by a later read.
    """
    if "failed" in (evaluation_status, quality_status, sample_validation_status):
        return False
    if sample_validation_status == "passed":
        return True
    return evaluation_status == "passed" and quality_status == "passed"


class VoiceConsentRequiredError(PermissionError):
    pass


class VoiceSampleRejectedError(ValueError):
    """The submitted recording cannot be cloned; the client must re-record."""

    def __init__(self, validation: object) -> None:
        super().__init__("the voice sample did not pass validation")
        self.validation = validation


class EvaluationRequiredError(RuntimeError):
    pass


class VoicePreviewUnavailableError(RuntimeError):
    pass


class VoiceEnrollmentReconciliationRequiredError(RuntimeError):
    pass


class ProviderVoiceDeletionUnsupportedError(RuntimeError):
    """Provider has no confirmed self-service delete contract."""


@dataclass(frozen=True, slots=True)
class VoiceConsent:
    account_id: str
    policy_version: str
    granted_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class VoiceEnrollmentRequest:
    account_id: str
    audio: bytes
    media_type: str
    duration_ms: int
    sample_rate: int
    enrollment_key: str | None = None
    #: Consumer enrollments (the mini-program) cannot run an A/B comparison or
    #: a TTS-output quality probe, so the submitted recording is measured and
    #: admitted instead -- and a recording that cannot be cloned is refused in
    #: the same request. Lab enrollments leave this off and keep their own
    #: gates: a human evaluation plus an objective quality measurement.
    require_sample_validation: bool = False

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.audio:
            raise ValueError("voice enrollment requires account_id and audio")
        if self.media_type not in {
            "audio/wav",
            "audio/x-wav",
            "audio/mpeg",
            "audio/mp4",
            "audio/aac",
            "audio/ogg",
            "audio/flac",
        }:
            raise ValueError("unsupported voice enrollment media type")
        if not 10_000 <= self.duration_ms <= 60_000:
            raise ValueError("voice enrollment duration must be 10..60 seconds")
        if self.sample_rate < 16_000:
            raise ValueError("voice enrollment sample rate must be at least 16 kHz")
        if len(self.audio) > 15 * 1024 * 1024:
            raise ValueError("voice enrollment audio must not exceed 15 MiB")
        if self.enrollment_key is not None and not 1 <= len(self.enrollment_key.strip()) <= 128:
            raise ValueError("voice enrollment key must contain 1..128 characters")


@dataclass(frozen=True, slots=True)
class ProviderVoice:
    voice_id: str
    target_model: str
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.voice_id.strip() or not self.target_model.strip():
            raise ValueError("provider voice requires voice_id and target_model")


class VoiceEnrollmentProvider(Protocol):
    async def create_voice(
        self,
        *,
        target_model: str,
        prefix: str,
        sample_url: str,
    ) -> ProviderVoice: ...

    async def delete_voice(self, *, voice_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class VoiceProfile:
    profile_id: str
    sample_id: str
    version_number: int
    provider: str
    provider_region: str
    target_model: str
    provider_voice_id: str | None
    status: VoiceProfileStatus
    evaluation_status: Literal["pending", "passed", "failed"]
    quality_status: Literal["pending", "passed", "failed"]
    deletion_status: VoiceDeletionStatus
    provider_expires_at: datetime | None
    created_at: datetime
    activated_at: datetime | None = None
    revoked_at: datetime | None = None
    #: Consumer-path admission: the submitted recording was measured. Lab
    #: enrollments leave this ``pending`` and rely on the A/B evaluation plus
    #: the TTS-output quality probe instead.
    sample_validation_status: Literal["pending", "passed", "failed"] = "pending"


@dataclass(frozen=True, slots=True)
class VoiceEvaluationRequest:
    account_id: str
    profile_id: str
    similarity: float
    naturalness: float
    accent_similarity: float
    emotion_adherence: float
    instruction_adherence: float
    uncanny: float
    candidate_preferred: bool | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.profile_id.strip():
            raise ValueError("voice evaluation requires account_id and profile_id")
        if any(
            not 1 <= score <= 5
            for score in (
                self.similarity,
                self.naturalness,
                self.accent_similarity,
                self.emotion_adherence,
                self.instruction_adherence,
                self.uncanny,
            )
        ):
            raise ValueError("voice subjective scores must be between 1 and 5")
        if len(self.notes) > 2000:
            raise ValueError("voice evaluation notes must not exceed 2000 characters")


@dataclass(frozen=True, slots=True)
class VoiceEvaluation:
    evaluation_id: str
    profile_id: str
    status: Literal["passed", "failed"]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VoiceBlindTrial:
    trial_id: str
    profile_id: str
    slots: tuple[VoiceBlindSlot, VoiceBlindSlot]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VoiceBlindPreviewTarget:
    model: str | None
    voice_id: str | None


@dataclass(frozen=True, slots=True)
class VoiceQualityMeasurementRequest:
    account_id: str
    profile_id: str
    source_run_id: str
    first_audio_ms: int
    cancel_tail_ms: int
    timestamp_error_ms: int
    long_sentence_chars: int
    long_sentence_completion_ratio: float

    def __post_init__(self) -> None:
        if not self.account_id.strip() or not self.profile_id.strip():
            raise ValueError("voice quality measurement requires account_id and profile_id")
        if not 1 <= len(self.source_run_id.strip()) <= 128:
            raise ValueError("voice quality measurement requires a source_run_id")
        if any(
            value < 0
            for value in (self.first_audio_ms, self.cancel_tail_ms, self.timestamp_error_ms)
        ):
            raise ValueError("voice objective metrics must not be negative")
        if not 1 <= self.long_sentence_chars <= 10_000:
            raise ValueError("long sentence probe must contain 1..10000 characters")
        if not 0 <= self.long_sentence_completion_ratio <= 1:
            raise ValueError("long sentence completion ratio must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class VoiceQualityMeasurement:
    measurement_id: str
    profile_id: str
    source_run_id: str
    status: Literal["passed", "failed"]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class VoiceEnrollmentOperation:
    operation_id: str
    enrollment_key: str
    account_id: str
    profile_id: str
    sample_id: str
    version_number: int
    provider_prefix: str
    sample_purpose: str
    state: VoiceEnrollmentOperationState
    provider_voice_id: str | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ProviderSample:
    account_id: str
    data: bytes
    media_type: str


@dataclass(frozen=True, slots=True)
class VoiceResolution:
    mode: Literal["active", "fallback"]
    profile_id: str | None = None
    version_number: int | None = None
    provider: str | None = None
    voice_kind: Literal["personal"] | None = None
    model: str | None = None
    resource_id: str | None = None
    voice_id: str | None = None
    provider_expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class VoicePreviewTarget:
    model: str
    voice_id: str


class VoicePreviewRenderer(Protocol):
    async def render(
        self,
        *,
        text: str,
        model: str | None,
        voice_id: str | None,
    ) -> bytes: ...


class VoiceProfilePort(Protocol):
    async def grant_consent(
        self,
        *,
        account_id: str,
        policy_version: str,
    ) -> VoiceConsent: ...

    async def revoke_consent(self, *, account_id: str) -> VoiceConsent: ...

    async def consent(self, *, account_id: str) -> VoiceConsent | None: ...

    async def enroll(self, request: VoiceEnrollmentRequest) -> VoiceProfile: ...

    async def accept_enrollment(self, request: VoiceEnrollmentRequest) -> VoiceProfile:
        """Measure, store and register one enrollment; no provider call."""
        ...

    async def complete_enrollment(
        self, *, account_id: str, profile_id: str
    ) -> VoiceProfile:
        """Drive an accepted enrollment through the provider and finalize it."""
        ...

    async def provider_sample(self, *, sample_id: str) -> ProviderSample: ...

    async def evaluate(self, request: VoiceEvaluationRequest) -> VoiceEvaluation: ...

    async def create_blind_trial(
        self,
        *,
        account_id: str,
        profile_id: str,
    ) -> VoiceBlindTrial: ...

    async def blind_preview_target(
        self,
        *,
        account_id: str,
        trial_id: str,
        slot: VoiceBlindSlot,
        text: str,
    ) -> VoiceBlindPreviewTarget: ...

    async def resolve_blind_preference(
        self,
        *,
        account_id: str,
        profile_id: str,
        trial_id: str,
        preferred_slot: VoiceBlindSlot,
    ) -> bool: ...

    async def record_quality_measurement(
        self,
        request: VoiceQualityMeasurementRequest,
    ) -> VoiceQualityMeasurement: ...

    async def account_id_for_profile(self, *, profile_id: str) -> str: ...

    async def activate(self, *, account_id: str, profile_id: str) -> VoiceProfile: ...

    async def resolve(self, *, account_id: str) -> VoiceResolution: ...

    async def profiles(self, *, account_id: str) -> tuple[VoiceProfile, ...]: ...

    async def pending_enrollments(
        self,
        *,
        account_id: str,
    ) -> tuple[VoiceEnrollmentOperation, ...]: ...

    async def reconcile_enrollment(
        self,
        *,
        account_id: str,
        enrollment_key: str,
        provider_voice: ProviderVoice | None = None,
        provider_asset_absent: bool = False,
        sample_asset_absent: bool = False,
    ) -> VoiceProfile: ...

    async def preview_target(
        self,
        *,
        account_id: str,
        profile_id: str,
    ) -> VoicePreviewTarget: ...

    async def revoke_profile(
        self,
        *,
        account_id: str,
        profile_id: str,
    ) -> VoiceProfile: ...

    async def confirm_provider_deletion(
        self,
        *,
        account_id: str,
        profile_id: str,
        evidence_reference: str,
    ) -> VoiceProfile: ...
