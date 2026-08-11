from services.session_runtime.service_mode_resolver import ServiceModeResolver
from services.session_runtime.subject_resolver import SubjectResolution


def test_unconfirmed_subject_always_uses_unknown_safe() -> None:
    mode = ServiceModeResolver().resolve(
        declared_mode="self_use",
        resolution=SubjectResolution(
            active_subject_id=None,
            speaker_state="unconfirmed",
            speaker_confidence=0.71,
            service_mode="unknown_safe",
            reason_code="speaker_confidence_low",
            requires_confirmation=True,
        ),
        subject_category="adult",
        age_band="adult",
    )

    assert mode == "unknown_safe"


def test_confirmed_minor_uses_student_policy_even_on_self_use_device() -> None:
    mode = ServiceModeResolver().resolve(
        declared_mode="self_use",
        resolution=SubjectResolution(
            active_subject_id="person-child",
            speaker_state="confirmed",
            speaker_confidence=0.97,
            service_mode="family_shared",
            reason_code="speaker_confirmed",
            requires_confirmation=False,
        ),
        subject_category="minor",
        age_band="under_14",
    )

    assert mode == "student_minor"


def test_confirmed_adult_self_binding_uses_adult_companion() -> None:
    mode = ServiceModeResolver().resolve(
        declared_mode="self_use",
        resolution=SubjectResolution(
            active_subject_id="person-adult",
            speaker_state="confirmed",
            speaker_confidence=0.98,
            service_mode="family_shared",
            reason_code="speaker_confirmed",
            requires_confirmation=False,
        ),
        subject_category="adult",
        age_band="adult",
    )

    assert mode == "adult_companion"


def test_confirmed_parent_subject_uses_senior_companion() -> None:
    mode = ServiceModeResolver().resolve(
        declared_mode="child_for_parent",
        resolution=SubjectResolution(
            active_subject_id="person-parent",
            speaker_state="confirmed",
            speaker_confidence=0.96,
            service_mode="family_shared",
            reason_code="speaker_confirmed",
            requires_confirmation=False,
        ),
        subject_category="adult",
        age_band="adult",
    )

    assert mode == "senior_companion"


def test_unknown_subject_category_never_inherits_declared_adult_mode() -> None:
    mode = ServiceModeResolver().resolve(
        declared_mode="self_use",
        resolution=SubjectResolution(
            active_subject_id="person-unverified",
            speaker_state="confirmed",
            speaker_confidence=0.99,
            service_mode="family_shared",
            reason_code="speaker_confirmed",
            requires_confirmation=False,
        ),
        subject_category="unknown",
        age_band="unknown",
    )

    assert mode == "unknown_safe"
