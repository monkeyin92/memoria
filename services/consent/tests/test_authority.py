"""ConsentAuthority rules 1-8: fail-closed matrix, versioned lifecycle, idempotency."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from services.consent.authority import ConsentDeniedError, SubjectProof
from services.consent.evidence import (
    CANONICAL_MEMORY_CAPABILITY_PURPOSE_PAIRS,
    BindingEvidence,
    ConsentOffer,
    ConsentParams,
    RelationshipEvidence,
)
from services.consent.in_memory_store import InMemoryConsentStore
from services.consent.store import ConsentConflictError, ConsentNotFoundError
from services.consent.tests.fakes import PersistingFixtureAuthority as ConsentAuthority


def utc(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


NOW = utc("2026-08-09T10:00:00+00:00")


def binding(
    *,
    version: int = 1,
    status: str = "active",
    binding_id: str = "bd_1",
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> BindingEvidence:
    return BindingEvidence(
        binding_id=binding_id,
        version=version,
        device_id="dev_1",
        status=status,  # type: ignore[arg-type]
        declared_mode="parent_for_child",
        valid_from=valid_from or (NOW - timedelta(days=1)),
        valid_until=valid_until or (NOW + timedelta(days=365)),
        canonical_hash="",
    )


def relationship(
    *,
    status: str = "active",
    relation_type: str = "guardian_of",
    guardian: str = "person_guardian",
    subject: str = "person_minor",
    binding_id: str = "bd_1",
    revision: int = 1,
) -> RelationshipEvidence:
    return RelationshipEvidence(
        relationship_id="rel_1",
        snapshot_id="rs_1",
        revision=revision,
        relation_type=relation_type,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        source_person_id=guardian,
        target_person_id=subject,
        binding_id=binding_id,
        valid_from=NOW - timedelta(days=1),
        valid_until=NOW + timedelta(days=365),
        canonical_hash="",
    )


def offer(
    *,
    capability: str = "chat",
    actor: str = "person_guardian",
    subject: str = "person_minor",
    resource_owner: str = "person_minor",
    purpose: str | None = None,
    extras: tuple[tuple[str, str], ...] = (),
    offer_id: str = "of_1",
    valid_until: datetime | None = None,
    valid_from: datetime | None = None,
) -> ConsentOffer:
    return ConsentOffer(
        offer_id=offer_id,
        capability=capability,  # type: ignore[arg-type]
        subject_id=subject,
        actor_id=actor,
        resource_owner_id=resource_owner,
        purpose=(
            purpose
            if purpose is not None
            else dict(CANONICAL_MEMORY_CAPABILITY_PURPOSE_PAIRS).get(
                capability, "user_request"
            )
        ),  # type: ignore[arg-type]
        params=ConsentParams(
            max_session_seconds=3600,
            retention_ttl_seconds=90 * 86400,
            extras=extras,
        ),
        valid_from=valid_from or (NOW - timedelta(days=1)),
        valid_until=valid_until or (NOW + timedelta(days=365)),
        created_at=NOW,
        policy_version="policy-cn-minor-v5",
    )


def subject(
    *,
    category: str = "minor",
    age_evidence: str = "verified",
    subject_id: str = "person_minor",
) -> SubjectProof:
    return SubjectProof(
        subject_id=subject_id,
        subject_category=category,  # type: ignore[arg-type]
        age_evidence_status=age_evidence,  # type: ignore[arg-type]
    )


def authority() -> ConsentAuthority:
    return ConsentAuthority(InMemoryConsentStore())


def denied(exc: ConsentDeniedError) -> str:
    return exc.reason


class TestRule1UnknownSubjectFailClosed:
    def test_category_unknown_denied(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(),
                actor_kind="guardian",
                subject=subject(category="unknown"),
                binding=binding(),
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "unknown_subject"

    def test_age_evidence_unverified_denied(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(),
                actor_kind="guardian",
                subject=subject(age_evidence="unverified"),
                binding=binding(),
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "age_evidence_unverified"

    def test_age_evidence_disputed_denied(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(),
                actor_kind="guardian",
                subject=subject(age_evidence="disputed"),
                binding=binding(),
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "age_evidence_unverified"

    def test_empty_subject_id_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError):
            subject(subject_id="")


class TestRule2AdultSensitiveSelfOnly:
    SENSITIVE = (
        "memory_promotion",
        "family_shared_memory_proposal",
        "family_shared_memory_approval",
        "family_shared_memory_promotion",
        "voice_clone_use",
        "digital_self_preview",
        "legacy_grant_create",
        "payment",
        "raw_audio_retention",
        "model_training_contribution",
        "device_ownership_transfer",
    )

    def test_self_grant_adult_verified_allowed(self) -> None:
        result = authority().grant(
            offer(
                capability="voice_clone_use",
                actor="person_adult",
                subject="person_adult",
                resource_owner="person_adult",
            ),
            actor_kind="subject",
            subject=subject(category="adult", subject_id="person_adult"),
            binding=binding(),
            now=NOW,
        )
        assert result.evidence.status == "active"
        assert result.evidence.actor_kind == "subject"

    @pytest.mark.parametrize("capability", SENSITIVE)
    def test_guardian_cannot_grant_sensitive(self, capability: str) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(capability=capability),
                actor_kind="guardian",
                subject=subject(),
                binding=binding(),
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "sensitive_requires_verified_adult"

    def test_minor_cannot_self_grant_sensitive(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(
                    capability="voice_clone_use",
                    actor="person_minor",
                    subject="person_minor",
                    resource_owner="person_minor",
                ),
                actor_kind="subject",
                subject=subject(),
                binding=binding(),
                now=NOW,
            )
        assert denied(caught.value) == "sensitive_requires_verified_adult"

    def test_actor_not_subject_denied(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(
                    capability="payment",
                    actor="person_other",
                    subject="person_adult",
                    resource_owner="person_adult",
                ),
                actor_kind="subject",
                subject=subject(category="adult", subject_id="person_adult"),
                binding=binding(),
                now=NOW,
            )
        assert denied(caught.value) == "subject_claim_mismatch"

    def test_resource_owner_not_subject_denied(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(
                    capability="payment",
                    actor="person_adult",
                    subject="person_adult",
                    resource_owner="person_other",
                ),
                actor_kind="subject",
                subject=subject(category="adult", subject_id="person_adult"),
                binding=binding(),
                now=NOW,
            )
        assert denied(caught.value) == "subject_claim_mismatch"

    def test_binding_must_be_active(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(
                    capability="voice_clone_use",
                    actor="person_adult",
                    subject="person_adult",
                    resource_owner="person_adult",
                ),
                actor_kind="subject",
                subject=subject(category="adult", subject_id="person_adult"),
                binding=binding(status="revoked"),
                now=NOW,
            )
        assert denied(caught.value) == "binding_not_active"


class TestRule3GuardianWhitelist:
    WHITELIST = (
        "chat",
        "tutor",
        "english_practice",
        "voice_profile_create",
        "memory_capture",
        "memory_recall_private",
        "guardian_summary_view",
    )
    FORBIDDEN = (
        "memory_promotion",
        "family_shared_memory_proposal",
        "family_shared_memory_approval",
        "family_shared_memory_promotion",
        "voice_clone_use",
        "digital_self_preview",
        "legacy_grant_create",
        "device_ownership_transfer",
        "model_training_contribution",
        "payment",
        "raw_audio_retention",
    )

    def _voice_offer(self, capability: str) -> ConsentOffer:
        extras = (("scope", "voice_recognition"),) if capability == "voice_profile_create" else ()
        return offer(capability=capability, extras=extras)

    @pytest.mark.parametrize("capability", WHITELIST)
    def test_guardian_whitelist_allowed(self, capability: str) -> None:
        result = authority().grant(
            self._voice_offer(capability),
            actor_kind="guardian",
            subject=subject(),
            binding=binding(),
            relationships=(relationship(),),
            now=NOW,
        )
        assert result.evidence.capability == capability
        assert result.evidence.actor_kind == "guardian"

    @pytest.mark.parametrize("capability", FORBIDDEN)
    def test_guardian_never_grants_forbidden(self, capability: str) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                self._voice_offer(capability),
                actor_kind="guardian",
                subject=subject(),
                binding=binding(),
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "sensitive_requires_verified_adult"

    def test_voice_profile_create_is_enrollment_only_not_cloning(self) -> None:
        # Cloning scope is rejected even though the capability is whitelisted.
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(
                    capability="voice_profile_create",
                    extras=(("scope", "voice_cloning"),),
                ),
                actor_kind="guardian",
                subject=subject(),
                binding=binding(),
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "voice_profile_create_requires_recognition_scope"
        # Missing scope is rejected too.
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(capability="voice_profile_create"),
                actor_kind="guardian",
                subject=subject(),
                binding=binding(),
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "voice_profile_create_requires_recognition_scope"

    def test_guardian_requires_active_relationship(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(),
                actor_kind="guardian",
                subject=subject(),
                binding=binding(),
                relationships=(relationship(status="suspended"),),
                now=NOW,
            )
        assert denied(caught.value) == "relationship_not_active"

    def test_guardian_relationship_must_match_actor_and_subject(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(),
                actor_kind="guardian",
                subject=subject(),
                binding=binding(),
                relationships=(relationship(guardian="person_other"),),
                now=NOW,
            )
        assert denied(caught.value) == "relationship_not_active"

    def test_guardian_relationship_must_match_binding(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(),
                actor_kind="guardian",
                subject=subject(),
                binding=binding(),
                relationships=(relationship(binding_id="bd_other"),),
                now=NOW,
            )
        assert denied(caught.value) == "relationship_not_active"

    def test_guardian_requires_active_binding(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(),
                actor_kind="guardian",
                subject=subject(),
                binding=binding(status="expired"),
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "binding_not_active"

    def test_minor_cannot_self_grant_regular(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(
                    actor="person_minor",
                    subject="person_minor",
                    resource_owner="person_minor",
                ),
                actor_kind="subject",
                subject=subject(),
                binding=binding(),
                now=NOW,
            )
        assert denied(caught.value) == "minor_requires_guardian_grant"

    def test_guardian_cannot_grant_to_adult(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(
                    actor="person_guardian", subject="person_adult", resource_owner="person_adult"
                ),
                actor_kind="guardian",
                subject=subject(category="adult", subject_id="person_adult"),
                binding=binding(),
                relationships=(relationship(guardian="person_guardian", subject="person_adult"),),
                now=NOW,
            )
        assert denied(caught.value) == "guardian_minor_only"


class TestRule4FamilyAdmin:
    def test_family_admin_never_grants(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(),
                actor_kind="family_admin",
                subject=subject(),
                binding=binding(),
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "family_admin_cannot_grant"

    def test_family_admin_never_revokes(self) -> None:
        auth = authority()
        grant = auth.grant(
            offer(),
            actor_kind="guardian",
            subject=subject(),
            binding=binding(),
            relationships=(relationship(),),
            now=NOW,
        )
        with pytest.raises(ConsentDeniedError) as caught:
            auth.revoke(
                grant.evidence.consent_id,
                actor_id="person_family_admin",
                actor_kind="family_admin",
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "family_admin_cannot_grant"

    def test_service_and_other_never_grant(self) -> None:
        for kind in ("service", "other"):
            with pytest.raises(ConsentDeniedError) as caught:
                authority().grant(
                    offer(),
                    actor_kind=kind,  # type: ignore[arg-type]
                    subject=subject(),
                    binding=binding(),
                    relationships=(relationship(),),
                    now=NOW,
                )
            assert denied(caught.value) == "actor_kind_not_authorized"


class TestActorKindDerivation:
    def test_self_derived_from_identity_equality(self) -> None:
        result = authority().grant(
            offer(
                capability="memory_capture",
                actor="person_adult",
                subject="person_adult",
                resource_owner="person_adult",
            ),
            subject=subject(category="adult", subject_id="person_adult"),
            binding=binding(),
            now=NOW,
        )
        assert result.evidence.actor_kind == "subject"

    def test_guardian_derived_from_relationship_evidence(self) -> None:
        result = authority().grant(
            offer(),
            subject=subject(),
            binding=binding(),
            relationships=(relationship(),),
            now=NOW,
        )
        assert result.evidence.actor_kind == "guardian"

    def test_derivation_fails_closed_without_evidence(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(
                    actor="person_guardian", subject="person_other", resource_owner="person_other"
                ),
                subject=subject(subject_id="person_other"),
                binding=binding(),
                now=NOW,
            )
        assert denied(caught.value) == "actor_kind_unverifiable"


class TestCapabilityGate:
    def test_crisis_notification_not_grantable(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(capability="crisis_notification"),
                actor_kind="guardian",
                subject=subject(),
                binding=binding(),
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "capability_not_grantable"

    def test_offer_expired_denied(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(valid_until=NOW - timedelta(minutes=1)),
                actor_kind="guardian",
                subject=subject(),
                binding=binding(),
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "offer_expired"

    def test_offer_not_yet_valid_denied(self) -> None:
        with pytest.raises(ConsentDeniedError) as caught:
            authority().grant(
                offer(valid_from=NOW + timedelta(minutes=1)),
                actor_kind="guardian",
                subject=subject(),
                binding=binding(),
                relationships=(relationship(),),
                now=NOW,
            )
        assert denied(caught.value) == "offer_not_yet_valid"


class TestRule5SnapshotInvalidation:
    def test_binding_version_change_invalidates_old_snapshot(self) -> None:
        auth = authority()
        first = auth.grant(
            offer(),
            actor_kind="guardian",
            subject=subject(),
            binding=binding(version=1),
            relationships=(relationship(),),
            now=NOW,
        )
        later = NOW + timedelta(minutes=10)
        second = auth.grant(
            offer(offer_id="of_2"),
            actor_kind="guardian",
            subject=subject(),
            binding=binding(version=2, valid_from=later),
            relationships=(relationship(revision=2),),
            now=later,
        )
        assert second.snapshot.binding_version == 2
        assert second.snapshot.version == 1
        # The old snapshot is preserved but bound to binding version 1.
        assert first.snapshot.binding_version == 1
        assert first.snapshot.snapshot_id != second.snapshot.snapshot_id

    def test_relationship_change_produces_new_snapshot(self) -> None:
        auth = authority()
        first = auth.snapshot(
            "person_minor",
            binding=binding(),
            relationships=(relationship(revision=1),),
            policy_version="policy-cn-minor-v5",
            now=NOW,
        )
        second = auth.snapshot(
            "person_minor",
            binding=binding(),
            relationships=(relationship(revision=2),),
            policy_version="policy-cn-minor-v5",
            now=NOW + timedelta(minutes=1),
        )
        assert second.version == 2
        assert second.snapshot_id != first.snapshot_id

    def test_snapshot_idempotent_when_nothing_changed(self) -> None:
        auth = authority()
        first = auth.snapshot(
            "person_minor",
            binding=binding(),
            relationships=(relationship(),),
            policy_version="policy-cn-minor-v5",
            now=NOW,
        )
        second = auth.snapshot(
            "person_minor",
            binding=binding(),
            relationships=(relationship(),),
            policy_version="policy-cn-minor-v5",
            now=NOW + timedelta(minutes=1),
        )
        assert second == first


class TestRule6VersionedLifecycle:
    def test_revoke_and_dispute_never_overwrite_history(self) -> None:
        store = InMemoryConsentStore()
        auth = ConsentAuthority(store)
        grant = auth.grant(
            offer(),
            actor_kind="guardian",
            subject=subject(),
            binding=binding(),
            relationships=(relationship(),),
            now=NOW,
        )
        revoked = auth.revoke(
            grant.evidence.consent_id,
            actor_id="person_guardian",
            actor_kind="guardian",
            relationships=(relationship(),),
            now=NOW + timedelta(minutes=1),
        )
        assert revoked.evidence.version == 2
        assert revoked.evidence.status == "revoked"
        # A terminated chain cannot be disputed again -> conflict is the only path.
        with pytest.raises(ConsentConflictError):
            auth.dispute(
                grant.evidence.consent_id,
                actor_id="person_minor",
                actor_kind="subject",
                reason="本人否认",
                now=NOW + timedelta(minutes=2),
            )
        # History is append-only: both rows remain, each with its own status.
        with store.transaction() as uow:
            v1 = uow.get_consent(grant.evidence.consent_id, 1)
            v2 = uow.get_consent(grant.evidence.consent_id, 2)
        assert v1 is not None and v1.status == "active"
        assert v2 is not None and v2.status == "revoked"

    def test_subject_revocation_keeps_the_grant_authority_head_actor(self) -> None:
        auth = authority()
        grant = auth.grant(
            offer(),
            actor_kind="guardian",
            subject=subject(),
            binding=binding(),
            relationships=(relationship(),),
            now=NOW,
        )
        revoked = auth.revoke(
            grant.evidence.consent_id,
            actor_id="person_minor",
            actor_kind="subject",
            now=NOW + timedelta(minutes=1),
        )
        assert revoked.evidence.actor_id == grant.evidence.actor_id
        assert revoked.evidence.actor_kind == grant.evidence.actor_kind
        assert revoked.audit_entry.actor_id == "person_minor"

    def test_dispute_new_version_and_revoke_conflicts(self) -> None:
        auth = authority()
        grant = auth.grant(
            offer(),
            actor_kind="guardian",
            subject=subject(),
            binding=binding(),
            relationships=(relationship(),),
            now=NOW,
        )
        disputed = auth.dispute(
            grant.evidence.consent_id,
            actor_id="person_guardian",
            actor_kind="guardian",
            relationships=(relationship(),),
            reason="监护人争议",
            now=NOW + timedelta(minutes=1),
        )
        assert disputed.evidence.status == "disputed"
        assert disputed.evidence.version == 2
        with pytest.raises(ConsentConflictError):
            auth.revoke(
                grant.evidence.consent_id,
                actor_id="person_guardian",
                actor_kind="guardian",
                relationships=(relationship(),),
                now=NOW + timedelta(minutes=2),
            )

    def test_revoke_unknown_consent_not_found(self) -> None:
        auth = authority()
        with pytest.raises(ConsentNotFoundError):
            auth.revoke(
                "c_missing",
                actor_id="person_minor",
                actor_kind="subject",
                now=NOW,
            )

    def test_subject_can_revoke_own_consent(self) -> None:
        auth = authority()
        grant = auth.grant(
            offer(
                capability="memory_recall_private",
                actor="person_adult",
                subject="person_adult",
                resource_owner="person_adult",
            ),
            actor_kind="subject",
            subject=subject(category="adult", subject_id="person_adult"),
            binding=binding(),
            now=NOW,
        )
        revoked = auth.revoke(
            grant.evidence.consent_id,
            actor_id="person_adult",
            actor_kind="subject",
            now=NOW + timedelta(minutes=1),
        )
        assert revoked.evidence.status == "revoked"

    def test_other_person_cannot_revoke(self) -> None:
        auth = authority()
        grant = auth.grant(
            offer(
                capability="memory_capture",
                actor="person_adult",
                subject="person_adult",
                resource_owner="person_adult",
            ),
            actor_kind="subject",
            subject=subject(category="adult", subject_id="person_adult"),
            binding=binding(),
            now=NOW,
        )
        with pytest.raises(ConsentDeniedError) as caught:
            auth.revoke(
                grant.evidence.consent_id,
                actor_id="person_other",
                actor_kind="subject",
                now=NOW + timedelta(minutes=1),
            )
        assert denied(caught.value) == "revoke_subject_only"

    def test_guardian_cannot_revoke_self_granted_chain(self) -> None:
        auth = authority()
        grant = auth.grant(
            offer(
                capability="memory_capture",
                actor="person_adult",
                subject="person_adult",
                resource_owner="person_adult",
            ),
            actor_kind="subject",
            subject=subject(category="adult", subject_id="person_adult"),
            binding=binding(),
            now=NOW,
        )
        with pytest.raises(ConsentDeniedError) as caught:
            auth.revoke(
                grant.evidence.consent_id,
                actor_id="person_guardian",
                actor_kind="guardian",
                relationships=(relationship(guardian="person_guardian", subject="person_adult"),),
                now=NOW + timedelta(minutes=1),
            )
        assert denied(caught.value) == "guardian_revoke_scope"


class TestCreateOffer:
    def test_create_offer_validates(self) -> None:
        auth = authority()
        created = auth.create_offer(
            capability="chat",
            subject_id="person_minor",
            actor_id="person_guardian",
            resource_owner_id="person_minor",
            purpose="user_request",
            params=ConsentParams(max_session_seconds=3600),
            valid_from=NOW,
            valid_until=NOW + timedelta(days=30),
            policy_version="policy-cn-minor-v5",
        )
        assert created.offer_id
        assert created.capability == "chat"

    def test_create_offer_rejects_naive_times(self) -> None:
        auth = authority()
        with pytest.raises(ValueError):
            auth.create_offer(
                capability="chat",
                subject_id="person_minor",
                actor_id="person_guardian",
                resource_owner_id="person_minor",
                purpose="user_request",
                params=ConsentParams(),
                valid_from=datetime(2026, 8, 9),
                valid_until=NOW + timedelta(days=30),
                policy_version="policy-cn-minor-v5",
            )

    def test_grant_of_offer_reuses_exact_offer_fields(self) -> None:
        auth = authority()
        created = auth.create_offer(
            capability="memory_capture",
            subject_id="person_minor",
            actor_id="person_guardian",
            resource_owner_id="person_minor",
            purpose="memory_capture",
            params=ConsentParams(max_session_seconds=1800),
            valid_from=NOW - timedelta(days=1),
            valid_until=NOW + timedelta(days=30),
            policy_version="policy-cn-minor-v5",
        )
        result = auth.grant(
            created,
            actor_kind="guardian",
            subject=subject(),
            binding=binding(),
            relationships=(relationship(),),
            now=NOW,
        )
        assert result.evidence.offer_id == created.offer_id
        assert result.evidence.purpose == "memory_capture"
        assert result.evidence.valid_until == created.valid_until


class TestIdempotencyConflicts:
    def test_revocation_replay_same_key_different_reason_conflicts(self) -> None:
        auth = authority()
        grant = auth.grant(
            offer(),
            actor_kind="guardian",
            subject=subject(),
            binding=binding(),
            relationships=(relationship(),),
            now=NOW,
        )
        auth.revoke(
            grant.evidence.consent_id,
            actor_id="person_guardian",
            actor_kind="guardian",
            relationships=(relationship(),),
            reason="第一次",
            now=NOW + timedelta(minutes=1),
            idempotency_key="rev-1",
        )
        with pytest.raises(ConsentConflictError):
            auth.revoke(
                grant.evidence.consent_id,
                actor_id="person_guardian",
                actor_kind="guardian",
                relationships=(relationship(),),
                reason="第二次",
                now=NOW + timedelta(minutes=2),
                idempotency_key="rev-1",
            )
