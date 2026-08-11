"""Read-only migration adapters for legacy consent sources (Agent C scope).

This package never writes to any store and never executes a migration.  It maps
old consent rows (guardian / voice clone / raw audio / persona learning) into a
uniform ``RawLegacyConsent`` shape, classifies each row as a provable candidate
or a quarantine entry, and produces dry-run plans plus rollback manifests with
``executed: false``.
"""

from services.consent.migration.adapters import (
    GuardianConsentAdapter,
    GuardianConsentRow,
    GuardianConsentRowSource,
    PersonaConsentAdapter,
    PersonaLearningConsentRow,
    PersonaLearningConsentRowSource,
    RawAudioConsentAdapter,
    RawAudioConsentRow,
    RawAudioConsentRowSource,
    RawLegacyConsent,
    VoiceCloneConsentAdapter,
    VoiceCloneConsentRow,
    VoiceCloneConsentRowSource,
)
from services.consent.migration.manifest import MANIFEST_NOTE, render_manifest
from services.consent.migration.normalizer import (
    NormalizationResult,
    NormalizedConsent,
    QuarantineEntry,
    QuarantineReason,
    normalize_row,
    normalize_rows,
)
from services.consent.migration.plan import DryRunPlan, PlanItem, build_plan

__all__ = [
    "DryRunPlan",
    "GuardianConsentAdapter",
    "GuardianConsentRow",
    "GuardianConsentRowSource",
    "MANIFEST_NOTE",
    "NormalizationResult",
    "NormalizedConsent",
    "PersonaConsentAdapter",
    "PersonaLearningConsentRow",
    "PersonaLearningConsentRowSource",
    "PlanItem",
    "QuarantineEntry",
    "QuarantineReason",
    "RawAudioConsentAdapter",
    "RawAudioConsentRow",
    "RawAudioConsentRowSource",
    "RawLegacyConsent",
    "VoiceCloneConsentAdapter",
    "VoiceCloneConsentRow",
    "VoiceCloneConsentRowSource",
    "build_plan",
    "normalize_row",
    "normalize_rows",
    "render_manifest",
]
