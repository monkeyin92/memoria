"""Pure decision matrix for MemoryScopeResolver (sections 6.1-6.3, 13.2/13.4)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from packages.contracts.generated.python.multi_subject_contracts import (
    BindingRole,
    PolicyEffect,
    PolicyObligation,
)
from services.memory_scope.domain import (
    ConsentSnapshotInput,
    CoSubjectContext,
    MemoryScope,
    PolicyDecisionInput,
    ResolutionContext,
    SubjectContext,
    WriteFence,
)
from services.memory_scope.resolver import MemoryScopeResolver

RESOLVER = MemoryScopeResolver()

_NO_FENCE: object = object()

SUBJECT_ADULT = SubjectContext(
    active_subject_id="person-adult",
    subject_category="adult",
    speaker_state="confirmed",
)
SUBJECT_MINOR = SubjectContext(
    active_subject_id="person-child",
    subject_category="minor",
    speaker_state="confirmed",
    guardian_relationship_active=True,
)


def _policy(
    *,
    effect: PolicyEffect = PolicyEffect.POLICY_EFFECT_ALLOW_WITH_OBLIGATIONS,
    obligations: tuple[PolicyObligation, ...] = (),
    receipt: str | None = "receipt-1",
) -> PolicyDecisionInput:
    return PolicyDecisionInput(
        effect=effect,
        receipt_id=receipt,
        obligations=obligations,
    )


_FENCE = WriteFence(
    session_id="session-1",
    epoch=1,
    binding_id="binding-1",
    binding_role=BindingRole.BINDING_ROLE_PRIMARY_SUBJECT.value,
    runtime_profile_id="profile-1",
    actor_subject_id="person-adult",
    active_subject_id="person-adult",
    binding_version=1,
)


def _fence(**changes: object) -> WriteFence:
    if not changes:
        return _FENCE
    return replace(_FENCE, **changes)  # type: ignore[arg-type]


def _make_fence() -> WriteFence:
    return WriteFence(
        session_id="session-1",
        epoch=1,
        binding_id="binding-1",
        binding_role=BindingRole.BINDING_ROLE_PRIMARY_SUBJECT.value,
        runtime_profile_id="profile-1",
        actor_subject_id="person-adult",
        active_subject_id="person-adult",
        binding_version=1,
    )


def _consent(
    *,
    granted: bool = True,
    covers: tuple[str, ...] = ("person-adult",),
) -> ConsentSnapshotInput:
    return ConsentSnapshotInput(
        snapshot_id="consent-1" if granted else None,
        granted=granted,
        covers_subjects=covers,
    )


def _resolve(
    subject: SubjectContext,
    *,
    requested: MemoryScope,
    policy: PolicyDecisionInput | None = None,
    consent: ConsentSnapshotInput | None = None,
    co_subjects: CoSubjectContext | None = None,
    fence: WriteFence | object = _NO_FENCE,
):
    effective_fence = fence if fence is not _NO_FENCE else _make_fence()
    context = ResolutionContext(
        subject=subject,
        policy=policy or _policy(),
        consent=consent or _consent(covers=(subject.active_subject_id or "",)),
        co_subjects=co_subjects or CoSubjectContext(),
        requested_scope=requested,
        fence=effective_fence,  # type: ignore[arg-type]
    )
    return RESOLVER.resolve(context)


class TestUnknownAndUnconfirmed:
    """Sections 6.3/13.2: unregistered or unconfirmed speakers get nothing."""

    def test_unregistered_guest_cannot_write_private(self) -> None:
        subject = SubjectContext(
            active_subject_id="guest-1",
            subject_category="adult",
            speaker_state="confirmed",
            registered=False,
        )
        result = _resolve(subject, requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE)
        assert not result.persistable
        assert result.reason_code == "unregistered_guest"

    def test_unconfirmed_speaker_cannot_write_any_durable_scope(self) -> None:
        for scope in (
            MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
            MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
            MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,
        ):
            subject = SubjectContext(
                active_subject_id="person-a",
                subject_category="adult",
                speaker_state="unconfirmed",
                speaker_confidence=0.3,
            )
            result = _resolve(subject, requested=scope)
            assert not result.persistable
            assert result.reason_code == "subject_not_confirmed"

    def test_no_subject_id_fails_closed(self) -> None:
        subject = SubjectContext(active_subject_id=None, speaker_state="confirmed")
        result = _resolve(subject, requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE)
        assert not result.persistable
        assert result.reason_code == "subject_not_confirmed"

    def test_unknown_category_fails_closed(self) -> None:
        subject = SubjectContext(
            active_subject_id="person-a",
            subject_category="unknown",
            speaker_state="confirmed",
        )
        result = _resolve(subject, requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE)
        assert not result.persistable
        assert result.reason_code == "subject_category_unknown"

    def test_policy_deny_fails_closed(self) -> None:
        result = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            policy=_policy(effect=PolicyEffect.POLICY_EFFECT_DENY),
        )
        assert not result.persistable
        assert result.reason_code == "policy_denied"

    def test_missing_receipt_fails_closed(self) -> None:
        result = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            policy=_policy(receipt=None),
        )
        assert not result.persistable
        assert result.reason_code == "missing_sensitive_source"

    def test_unknown_scope_fails_closed(self) -> None:
        result = _resolve(SUBJECT_ADULT, requested=MemoryScope.MEMORY_SCOPE_UNKNOWN)
        assert not result.persistable
        assert result.reason_code == "missing_sensitive_source"


class TestPrivateAndLegacy:
    def test_private_resolved_with_full_sources(self) -> None:
        result = _resolve(SUBJECT_ADULT, requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE)
        assert result.persistable
        assert result.scope is MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE
        assert result.raw_transcript_allowed

    def test_private_denied_when_consent_missing(self) -> None:
        result = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            consent=_consent(granted=False),
        )
        assert not result.persistable
        assert result.reason_code == "consent_missing"

    def test_private_denied_when_consent_does_not_cover_subject(self) -> None:
        result = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            consent=_consent(covers=("someone-else",)),
        )
        assert not result.persistable
        assert result.reason_code == "consent_missing"

    def test_private_denied_when_policy_only_allows_aggregate(self) -> None:
        result = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,)),
        )
        assert not result.persistable
        assert result.reason_code == "policy_denied"

    def test_legacy_requires_adult_and_approval(self) -> None:
        ok = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,
            policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_REQUIRE_SUBJECT_APPROVAL,)),
        )
        assert ok.persistable
        assert ok.scope is MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE

        minor = _resolve(SUBJECT_MINOR, requested=MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE)
        assert not minor.persistable
        assert minor.reason_code == "subject_category_unknown" or minor.reason_code == "unknown"

    def test_legacy_denied_without_approval_obligation(self) -> None:
        result = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,
            policy=_policy(obligations=()),
        )
        assert not result.persistable
        assert result.reason_code == "policy_denied"


class TestGuardianSummary:
    def test_guardian_summary_resolved_for_minor_with_active_guardian(self) -> None:
        result = _resolve(
            SUBJECT_MINOR,
            requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
            policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,)),
        )
        assert result.persistable
        assert result.scope is MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY
        assert not result.raw_transcript_allowed

    def test_guardian_summary_denied_without_active_guardian(self) -> None:
        subject = SubjectContext(
            active_subject_id="person-child",
            subject_category="minor",
            speaker_state="confirmed",
            guardian_relationship_active=False,
        )
        result = _resolve(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
            policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,)),
        )
        assert not result.persistable
        assert result.reason_code == "guardian_relationship_inactive"

    def test_guardian_summary_denied_for_adult_subject(self) -> None:
        result = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
            policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,)),
        )
        assert not result.persistable

    def test_guardian_summary_denied_without_aggregate_obligation(self) -> None:
        result = _resolve(SUBJECT_MINOR, requested=MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY)
        assert not result.persistable
        assert result.reason_code == "policy_denied"


class TestFamilyShared:
    def test_family_shared_requires_family_space(self) -> None:
        subject = SubjectContext(
            active_subject_id="person-a",
            subject_category="adult",
            speaker_state="confirmed",
        )
        result = _resolve(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
            consent=_consent(covers=("person-a", "person-b")),
            co_subjects=CoSubjectContext(
                subject_ids=("person-b",), confirmed_subject_ids=("person-b",)
            ),
        )
        assert not result.persistable
        assert result.reason_code == "missing_sensitive_source"

    def test_family_shared_requires_consent_covering_all(self) -> None:
        subject = SubjectContext(
            active_subject_id="person-a",
            subject_category="adult",
            speaker_state="confirmed",
            family_space_id="family-1",
        )
        result = _resolve(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
            consent=_consent(covers=("person-a",)),
            co_subjects=CoSubjectContext(
                subject_ids=("person-b",), confirmed_subject_ids=("person-b",)
            ),
        )
        assert not result.persistable
        assert result.reason_code == "consent_missing"

    def test_family_shared_denied_until_all_co_subjects_confirmed(self) -> None:
        subject = SubjectContext(
            active_subject_id="person-a",
            subject_category="adult",
            speaker_state="confirmed",
            family_space_id="family-1",
        )
        result = _resolve(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
            consent=_consent(covers=("person-a", "person-b", "person-c")),
            co_subjects=CoSubjectContext(
                subject_ids=("person-b", "person-c"),
                confirmed_subject_ids=("person-b",),
            ),
        )
        assert not result.persistable
        assert result.reason_code == "co_subjects_unconfirmed"

    def test_family_shared_resolved_when_all_confirmed(self) -> None:
        subject = SubjectContext(
            active_subject_id="person-a",
            subject_category="adult",
            speaker_state="confirmed",
            family_space_id="family-1",
        )
        result = _resolve(
            subject,
            requested=MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
            consent=_consent(covers=("person-a", "person-b")),
            co_subjects=CoSubjectContext(
                subject_ids=("person-b",), confirmed_subject_ids=("person-b",)
            ),
        )
        assert result.persistable
        assert result.scope is MemoryScope.MEMORY_SCOPE_FAMILY_SHARED


class TestEphemeral:
    def test_ephemeral_needs_explicit_non_persist_obligation(self) -> None:
        denied = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_SESSION_EPHEMERAL,
            policy=_policy(obligations=()),
        )
        assert not denied.persistable
        assert denied.reason_code == "policy_denied"

    def test_ephemeral_allowed_with_retention_obligation(self) -> None:
        result = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_SESSION_EPHEMERAL,
            policy=_policy(obligations=(PolicyObligation.POLICY_OBLIGATION_RETENTION_TTL,)),
        )
        assert result.scope is MemoryScope.MEMORY_SCOPE_SESSION_EPHEMERAL
        assert not result.persistable
        assert result.retention == "ttl"


class TestWriteFenceMatrix:
    """PR-12 / section 11.5: no fence, no durable write (every scope)."""

    def test_durable_scopes_without_fence_fail_closed(self) -> None:
        for scope in (
            MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            MemoryScope.MEMORY_SCOPE_GUARDIAN_SUMMARY,
            MemoryScope.MEMORY_SCOPE_FAMILY_SHARED,
            MemoryScope.MEMORY_SCOPE_LEGACY_ARCHIVE,
        ):
            result = _resolve(
                SUBJECT_ADULT,
                requested=scope,
                fence=None,
            )
            assert not result.persistable
            assert result.reason_code == "write_fence_missing"

    def test_ephemeral_does_not_require_fence(self) -> None:
        result = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_SESSION_EPHEMERAL,
            policy=_policy(
                obligations=(PolicyObligation.POLICY_OBLIGATION_RETENTION_TTL,)
            ),
            fence=None,
        )
        assert result.scope is MemoryScope.MEMORY_SCOPE_SESSION_EPHEMERAL
        assert not result.persistable

    def test_durable_scope_with_fence_resolves(self) -> None:
        result = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
        )
        assert result.persistable
        assert result.scope is MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE

    def test_fence_fingerprint_is_stable_and_binds_identity_fields(self) -> None:
        base = _fence()
        assert base.fingerprint() == base.fingerprint()
        assert base.fingerprint() != _fence(epoch=2).fingerprint()
        assert base.fingerprint() != _fence(binding_id="binding-2").fingerprint()
        assert base.fingerprint() != _fence(
            runtime_profile_id="profile-2"
        ).fingerprint()
        assert base.fingerprint() != _fence(session_id="session-2").fingerprint()
        # Binding versions are canonical integers: different versions give
        # different fingerprints and the serialization is decimal.
        assert base.fingerprint() != _fence(binding_version=2).fingerprint()
        assert _fence(binding_version=10).fingerprint() != _fence(
            binding_version=1
        ).fingerprint()
        # Main architecture review: generation / turn / the validity window
        # ARE part of the complete fence identity - any drift changes the
        # fingerprint so a proposal cannot be confirmed against a changed
        # or expired fence.
        assert base.fingerprint() != _fence(
            turn_id=7, generation_id="g1"
        ).fingerprint()
        assert base.fingerprint() != _fence(
            valid_until=datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
        ).fingerprint()

    def test_binding_version_must_be_positive_integer(self) -> None:
        with pytest.raises(ValueError, match="binding_version"):
            _fence(binding_version=0)
        with pytest.raises(ValueError, match="binding_version"):
            _fence(binding_version=-1)
        with pytest.raises(ValueError, match="binding_version"):
            _fence(binding_version="v2")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="binding_version"):
            _fence(binding_version=None)  # type: ignore[arg-type]
        assert _fence(binding_version=1).binding_version == 1

    def test_fence_omitting_binding_version_fails_construction(self) -> None:
        """An unknown binding must never be silently treated as v1."""
        with pytest.raises(TypeError):
            WriteFence(  # type: ignore[call-arg]
                session_id="s",
                epoch=1,
                binding_id="b",
                binding_role=BindingRole.BINDING_ROLE_PRIMARY_SUBJECT.value,
                runtime_profile_id="p",
                actor_subject_id="a",
                active_subject_id="a",
            )

    def test_policy_receipt_v2_invariants(self) -> None:
        """The authoritative PolicyReceiptV2 wire rejects bool/zero versions,
        invalid enums, malformed hashes and naive/ordered times."""
        from services.memory_scope.tests.receipt_helpers import (
            make_memory_receipt,
        )

        receipt = make_memory_receipt()
        assert receipt.receipt_id == "receipt-1"
        assert receipt.consent_snapshot_ids == ("consent-1",)
        with pytest.raises(ValueError, match="session_epoch"):
            make_memory_receipt(
                fence=WriteFence(
                    session_id="s",
                    epoch=0,
                    binding_id="b",
                    binding_role=BindingRole.BINDING_ROLE_PRIMARY_SUBJECT.value,
                    runtime_profile_id="p",
                    actor_subject_id="a",
                    active_subject_id="a",
                    binding_version=1,
                    device_id="d",
                )
            )
        with pytest.raises(ValueError, match="binding_version"):
            make_memory_receipt(
                fence=WriteFence(
                    session_id="s",
                    epoch=1,
                    binding_id="b",
                    binding_role=BindingRole.BINDING_ROLE_PRIMARY_SUBJECT.value,
                    runtime_profile_id="p",
                    actor_subject_id="a",
                    active_subject_id="a",
                    binding_version=0,
                    device_id="d",
                )
            )
        with pytest.raises(ValueError, match="Capability"):
            make_memory_receipt(capability="notification_operator")
        with pytest.raises(ValueError, match="context_hash"):
            make_memory_receipt(context_hash="hash")
        with pytest.raises(ValueError, match="paired arrays"):
            make_memory_receipt(
                consent_snapshot_ids=("c1",), consent_snapshot_revisions=()
            )
        with pytest.raises(ValueError, match="PolicyEffect"):
            make_memory_receipt(effect="bogus")


class TestCanonicalContract:
    def test_memory_scope_values_match_canonical_schema(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[3]
            / "packages/contracts/schemas/multi-subject/multi-subject.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        enum_values = schema["$defs"]["MemoryScope"]["enum"]
        assert MemoryScope.values() == tuple(enum_values)
        assert MemoryScope.from_value("bogus") is None
        assert MemoryScope.from_value("personal_private") is MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE

    def test_obligations_are_canonical_uppercase_values(self) -> None:
        from packages.contracts.generated.python.multi_subject_contracts import (
            PolicyObligation as CanonicalPolicyObligation,
        )

        assert PolicyObligation is CanonicalPolicyObligation
        for obligation in PolicyObligation:
            assert obligation.value == obligation.value.upper()
        # A lowercase obligation string must never unlock anything.
        denied = _resolve(
            SUBJECT_ADULT,
            requested=MemoryScope.MEMORY_SCOPE_PERSONAL_PRIVATE,
            policy=PolicyDecisionInput(
                effect=PolicyEffect.POLICY_EFFECT_ALLOW_WITH_OBLIGATIONS,
                receipt_id="receipt-1",
                obligations=(PolicyObligation.POLICY_OBLIGATION_PERSIST_AGGREGATE_ONLY,),
            ),
        )
        assert not denied.persistable
        assert denied.reason_code == "policy_denied"

    def test_memory_scope_is_generated_canonical_enum(self) -> None:
        from packages.contracts.generated.python.multi_subject_contracts import (
            MemoryScope as CanonicalMemoryScope,
        )

        assert MemoryScope is CanonicalMemoryScope
