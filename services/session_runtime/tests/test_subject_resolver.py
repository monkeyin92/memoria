from services.session_runtime.subject_resolver import (
    BindingSnapshot,
    ResolveSubjectCommand,
    SubjectCandidate,
    SubjectResolver,
)


def test_low_confidence_speaker_enters_unknown_safe_without_owner_fallback() -> None:
    binding = BindingSnapshot(
        binding_id="binding-1",
        device_id="device-1",
        binding_version=3,
        declared_mode="family_shared",
        primary_subject_ids=("person-child",),
        member_subject_ids=("person-child", "person-parent"),
    )
    command = ResolveSubjectCommand(
        device_id="device-1",
        candidates=(SubjectCandidate("person-parent", 0.71),),
    )

    resolution = SubjectResolver().resolve(command, binding=binding)

    assert resolution.active_subject_id is None
    assert resolution.speaker_state == "unconfirmed"
    assert resolution.service_mode == "unknown_safe"
    assert resolution.reason_code == "speaker_confidence_low"
    assert resolution.requires_confirmation


def test_high_confidence_bound_member_is_confirmed() -> None:
    binding = BindingSnapshot(
        binding_id="binding-1",
        device_id="device-1",
        binding_version=3,
        declared_mode="family_shared",
        primary_subject_ids=("person-child",),
        member_subject_ids=("person-child", "person-parent"),
    )

    resolution = SubjectResolver().resolve(
        ResolveSubjectCommand(
            device_id="device-1",
            candidates=(SubjectCandidate("person-parent", 0.93),),
        ),
        binding=binding,
    )

    assert resolution.active_subject_id == "person-parent"
    assert resolution.speaker_state == "confirmed"
    assert resolution.speaker_confidence == 0.93
    assert resolution.service_mode == "family_shared"
    assert not resolution.requires_confirmation


def test_multiple_speakers_fail_closed_even_with_one_high_candidate() -> None:
    binding = BindingSnapshot(
        binding_id="binding-1",
        device_id="device-1",
        binding_version=3,
        declared_mode="family_shared",
        primary_subject_ids=("person-child",),
        member_subject_ids=("person-child", "person-parent"),
    )

    resolution = SubjectResolver().resolve(
        ResolveSubjectCommand(
            device_id="device-1",
            candidates=(SubjectCandidate("person-parent", 0.99),),
            multiple_speakers=True,
        ),
        binding=binding,
    )

    assert resolution.active_subject_id is None
    assert resolution.service_mode == "unknown_safe"
    assert resolution.reason_code == "multiple_speakers"


def test_offline_resolution_does_not_reuse_sensitive_subject_authority() -> None:
    binding = BindingSnapshot(
        binding_id="binding-1",
        device_id="device-1",
        binding_version=3,
        declared_mode="self_use",
        primary_subject_ids=("person-adult",),
        member_subject_ids=("person-adult",),
    )

    resolution = SubjectResolver().resolve(
        ResolveSubjectCommand(
            device_id="device-1",
            candidates=(SubjectCandidate("person-adult", 0.99),),
            offline=True,
        ),
        binding=binding,
    )

    assert resolution.active_subject_id is None
    assert resolution.service_mode == "unknown_safe"
    assert resolution.reason_code == "policy_snapshot_unavailable"


def test_app_confirmation_can_select_only_an_active_binding_member() -> None:
    binding = BindingSnapshot(
        binding_id="binding-1",
        device_id="device-1",
        binding_version=3,
        declared_mode="family_shared",
        primary_subject_ids=("person-child",),
        member_subject_ids=("person-child", "person-parent"),
    )
    resolver = SubjectResolver()

    confirmed = resolver.resolve(
        ResolveSubjectCommand(
            device_id="device-1",
            app_claimed_subject_id="person-child",
        ),
        binding=binding,
    )
    rejected = resolver.resolve(
        ResolveSubjectCommand(
            device_id="device-1",
            app_claimed_subject_id="person-stranger",
        ),
        binding=binding,
    )

    assert confirmed.active_subject_id == "person-child"
    assert confirmed.speaker_state == "confirmed"
    assert confirmed.reason_code == "app_subject_confirmed"
    assert rejected.active_subject_id is None
    assert rejected.reason_code == "app_subject_not_bound"


def test_close_speaker_candidates_require_confirmation() -> None:
    binding = BindingSnapshot(
        binding_id="binding-1",
        device_id="device-1",
        binding_version=3,
        declared_mode="family_shared",
        primary_subject_ids=("person-child",),
        member_subject_ids=("person-child", "person-parent"),
    )

    resolution = SubjectResolver().resolve(
        ResolveSubjectCommand(
            device_id="device-1",
            candidates=(
                SubjectCandidate("person-child", 0.93),
                SubjectCandidate("person-parent", 0.89),
            ),
        ),
        binding=binding,
    )

    assert resolution.active_subject_id is None
    assert resolution.reason_code == "speaker_candidates_ambiguous"
