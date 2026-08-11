"""Resolve the current product goal independently from Persona and policy."""

from __future__ import annotations

from packages.contracts.generated.python.multi_subject_contracts import (
    AgeBandValue,
    DeviceDeclaredModeValue,
    ServiceModeValue,
    SubjectCategoryValue,
)

from services.session_runtime.subject_resolver import SubjectResolution


class ServiceModeResolver:
    def resolve(
        self,
        *,
        declared_mode: DeviceDeclaredModeValue,
        resolution: SubjectResolution,
        subject_category: SubjectCategoryValue,
        age_band: AgeBandValue,
    ) -> ServiceModeValue:
        del age_band
        if (
            resolution.active_subject_id is None
            or resolution.speaker_state != "confirmed"
        ):
            return "unknown_safe"
        if subject_category == "unknown":
            return "unknown_safe"
        if subject_category == "minor":
            return "student_minor"
        if declared_mode == "self_use" and subject_category == "adult":
            return "adult_companion"
        if declared_mode == "child_for_parent" and subject_category == "adult":
            return "senior_companion"
        return "family_shared"
