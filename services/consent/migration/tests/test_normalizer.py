"""Normalizer tests: provable old consents become candidates, everything else quarantines.

Fail-closed is the default: any unprovable old consent must quarantine and must
never become an effective grant candidate.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from services.consent.migration.adapters import (
    GuardianConsentAdapter,
    GuardianConsentRow,
    PersonaConsentAdapter,
    PersonaLearningConsentRow,
    RawAudioConsentAdapter,
    RawAudioConsentRow,
    RawLegacyConsent,
    VoiceCloneConsentAdapter,
    VoiceCloneConsentRow,
)
from services.consent.migration.normalizer import (
    NormalizationResult,
    NormalizedConsent,
    QuarantineEntry,
    normalize_row,
    normalize_rows,
)
from services.guardian.domain import ConsentRecord, GuardianLink


class _FakeGuardianSource:
    def __init__(self, rows: tuple[GuardianConsentRow, ...]) -> None:
        self._rows = rows

    def iter_rows(self) -> Iterator[GuardianConsentRow]:
        return iter(self._rows)


class _FakeVoiceSource:
    def __init__(self, rows: tuple[VoiceCloneConsentRow, ...]) -> None:
        self._rows = rows

    def iter_rows(self) -> Iterator[VoiceCloneConsentRow]:
        return iter(self._rows)


class _FakeRawSource:
    def __init__(self, rows: tuple[RawAudioConsentRow, ...]) -> None:
        self._rows = rows

    def iter_rows(self) -> Iterator[RawAudioConsentRow]:
        return iter(self._rows)


class _FakePersonaSource:
    def __init__(self, rows: tuple[PersonaLearningConsentRow, ...]) -> None:
        self._rows = rows

    def iter_rows(self) -> Iterator[PersonaLearningConsentRow]:
        return iter(self._rows)


def legacy_row(
    *,
    source: str = "guardian",
    legacy_id: str = "consent_1",
    subject_clue: str = "minor_1",
    subject_account_clue: str | None = None,
    actor_clue: str = "guardian_1",
    capability_clue: str = "memory_retention",
    purpose_clue: str = "memory_retention",
    evidence_clue: str = "evt_1",
    granted_at: datetime | None = None,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    policy_version: str = "policy-v1",
    binding_clue: str | None = "link_1",
    binding_status: str | None = "active",
) -> RawLegacyConsent:
    return RawLegacyConsent(
        source=source,
        legacy_id=legacy_id,
        subject_clue=subject_clue,
        subject_account_clue=subject_account_clue,
        actor_clue=actor_clue,
        capability_clue=capability_clue,
        purpose_clue=purpose_clue,
        evidence_clue=evidence_clue,
        granted_at=granted_at or datetime(2026, 1, 10, tzinfo=UTC),
        expires_at=expires_at,
        revoked_at=revoked_at,
        policy_version=policy_version,
        binding_clue=binding_clue,
        binding_status=binding_status,
        raw={},
    )


NOW = datetime(2026, 6, 1, tzinfo=UTC)


def make_guardian_legacy(kind: str = "memory_retention", **kwargs: object) -> RawLegacyConsent:
    return legacy_row(capability_clue=kind, purpose_clue=kind, **kwargs)


def make_link() -> GuardianLink:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return GuardianLink(
        link_id="link_1",
        guardian_user_id="guardian_1",
        minor_user_id="minor_1",
        relation="parent",
        status="active",
        verified_via="wechat_identity",
        created_at=base,
        binding_expires_at=base + timedelta(days=30),
        activated_at=base,
    )


def make_record(kind: str = "memory_retention") -> ConsentRecord:
    granted = datetime(2026, 1, 10, tzinfo=UTC)
    return ConsentRecord(
        consent_id="consent_1",
        link_id="link_1",
        consent_kind=kind,  # type: ignore[arg-type]
        policy_version="policy-v1",
        granted_at=granted,
        evidence_event_id="evt_1",
        expires_at=granted + timedelta(days=10) if kind == "corpus_recording" else None,
    )


def test_guardian_memory_retention_is_provable() -> None:
    row = make_guardian_legacy()
    outcome = normalize_row(row, now=NOW)
    assert isinstance(outcome, NormalizedConsent)
    assert outcome.source == "guardian"
    assert outcome.legacy_id == "consent_1"
    assert outcome.subject_id == "minor_1"
    assert outcome.actor_id == "guardian_1"
    assert outcome.actor_kind == "guardian"
    assert outcome.resource_owner_id == "minor_1"
    assert outcome.capability == "memory_capture"
    assert outcome.purpose == "guardian_memory_retention"
    assert outcome.evidence_ref == "evt_1"
    assert outcome.policy_version == "policy-v1"
    assert outcome.granted_at == datetime(2026, 1, 10, tzinfo=UTC)
    assert outcome.expires_at is None
    assert outcome.consent_id != outcome.offer_id


@pytest.mark.parametrize(
    ("kind", "expected_capability", "expected_purpose"),
    [
        ("memory_retention", "memory_capture", "guardian_memory_retention"),
        ("weekly_report", "guardian_summary_view", "guardian_weekly_report"),
        ("minor_voice_session", "chat", "guardian_minor_voice_session"),
    ],
)
def test_guardian_kinds_map_to_whitelisted_capabilities(
    kind: str, expected_capability: str, expected_purpose: str
) -> None:
    outcome = normalize_row(make_guardian_legacy(kind), now=NOW)
    assert isinstance(outcome, NormalizedConsent)
    assert outcome.capability == expected_capability
    assert outcome.purpose == expected_purpose


def test_guardian_corpus_recording_quarantines_for_human_review() -> None:
    row = make_guardian_legacy("corpus_recording")
    outcome = normalize_row(row, now=NOW)
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason == "unsupported_capability"
    assert "human" in outcome.detail.lower() or "人工" in outcome.detail


def test_guardian_consent_end_to_end_via_adapter() -> None:
    row = GuardianConsentRow(record=make_record("weekly_report"), link=make_link())
    legacy = list(GuardianConsentAdapter(_FakeGuardianSource((row,))).iter_rows())[0]
    outcome = normalize_row(legacy, now=NOW)
    assert isinstance(outcome, NormalizedConsent)
    assert outcome.capability == "guardian_summary_view"


def test_voice_clone_consent_is_provable_when_self_granted() -> None:
    granted = datetime(2026, 2, 1, tzinfo=UTC)
    row = VoiceCloneConsentRow(
        account_id="acct_1",
        policy_version="voice-policy-v2",
        granted_at=granted,
        revoked_at=None,
        grant_event_id="evt_voice_grant",
        revoke_event_id=None,
    )
    legacy = list(VoiceCloneConsentAdapter(_FakeVoiceSource((row,))).iter_rows())[0]
    outcome = normalize_row(legacy, now=NOW)
    assert isinstance(outcome, NormalizedConsent)
    assert outcome.capability == "voice_clone_use"
    assert outcome.actor_kind == "subject"
    assert outcome.actor_id == "acct_1"
    assert outcome.subject_id == "acct_1"
    assert outcome.purpose == "voice_clone_use"


def test_sensitive_capability_with_non_subject_actor_quarantines() -> None:
    row = legacy_row(
        source="voice_clone",
        legacy_id="acct_1",
        subject_clue="acct_1",
        actor_clue="acct_2",
        capability_clue="voice_clone",
        purpose_clue="voice_clone",
        evidence_clue="evt_1",
        binding_clue=None,
        binding_status=None,
    )
    outcome = normalize_row(row, now=NOW)
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason == "actor_not_subject_for_sensitive"


def test_raw_audio_consent_is_provable() -> None:
    granted = datetime(2026, 2, 15, tzinfo=UTC)
    row = RawAudioConsentRow(
        consent_grant_id="raw_grant_1",
        account_id="acct_1",
        policy_version="raw-policy-v1",
        retention_policy="account_lifetime",
        purpose="raw_audio_retention",
        granted_at=granted,
        expires_at=None,
        revoked_at=None,
        evidence_event_id="evt_raw_grant",
    )
    legacy = list(RawAudioConsentAdapter(_FakeRawSource((row,))).iter_rows())[0]
    outcome = normalize_row(legacy, now=NOW)
    assert isinstance(outcome, NormalizedConsent)
    assert outcome.capability == "raw_audio_retention"
    assert outcome.subject_id == "acct_1"
    assert outcome.actor_id == "acct_1"
    assert outcome.evidence_ref == "evt_raw_grant"


def test_raw_audio_revoked_consent_does_not_migrate() -> None:
    row = legacy_row(
        source="raw_audio",
        legacy_id="raw_grant_1",
        subject_clue="acct_1",
        actor_clue="acct_1",
        capability_clue="raw_audio_retention",
        purpose_clue="raw_audio_retention",
        evidence_clue="evt_raw_grant",
        revoked_at=datetime(2026, 3, 1, tzinfo=UTC),
        binding_clue=None,
        binding_status=None,
    )
    outcome = normalize_row(row, now=NOW)
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason == "expired_only"


def test_raw_audio_expired_consent_quarantines_as_expired_only() -> None:
    row = legacy_row(
        source="raw_audio",
        legacy_id="raw_grant_1",
        subject_clue="acct_1",
        actor_clue="acct_1",
        capability_clue="raw_audio_retention",
        purpose_clue="raw_audio_retention",
        evidence_clue="evt_raw_grant",
        expires_at=datetime(2026, 4, 1, tzinfo=UTC),
        binding_clue=None,
        binding_status=None,
    )
    outcome = normalize_row(row, now=NOW)
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason == "expired_only"


def test_persona_consent_with_person_identity_is_provable() -> None:
    row = PersonaLearningConsentRow(
        account_id="acct_1",
        policy_version="persona-policy-v3",
        granted_at=datetime(2026, 2, 20, tzinfo=UTC),
        revoked_at=None,
        grant_event_id="evt_persona_grant",
        revoke_event_id=None,
        person_id="person_9",
    )
    legacy = list(PersonaConsentAdapter(_FakePersonaSource((row,))).iter_rows())[0]
    outcome = normalize_row(legacy, now=NOW)
    assert isinstance(outcome, NormalizedConsent)
    assert outcome.capability == "model_training_contribution"
    assert outcome.subject_id == "person_9"
    assert outcome.actor_id == "person_9"
    assert outcome.purpose == "persona_learning"


def test_persona_consent_without_person_proof_quarantines() -> None:
    row = legacy_row(
        source="persona",
        legacy_id="acct_1",
        subject_clue="",
        subject_account_clue="acct_1",
        actor_clue="",
        capability_clue="persona_learning",
        purpose_clue="persona_learning",
        evidence_clue="evt_persona_grant",
        binding_clue=None,
        binding_status=None,
    )
    outcome = normalize_row(row, now=NOW)
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason == "missing_subject_proof"


def test_missing_purpose_quarantines() -> None:
    outcome = normalize_row(legacy_row(purpose_clue=""), now=NOW)
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason == "missing_purpose"


def test_missing_evidence_quarantines() -> None:
    outcome = normalize_row(legacy_row(evidence_clue=""), now=NOW)
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason == "missing_evidence"


def test_subject_unknown_quarantines() -> None:
    outcome = normalize_row(
        legacy_row(subject_clue="", subject_account_clue=None, actor_clue=""),
        now=NOW,
    )
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason == "subject_unknown"


def test_guardian_without_link_quarantines_as_ambiguous_binding() -> None:
    outcome = normalize_row(legacy_row(binding_clue=None, binding_status=None), now=NOW)
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason == "ambiguous_binding"


def test_guardian_with_inactive_link_quarantines_as_ambiguous_binding() -> None:
    outcome = normalize_row(legacy_row(binding_status="pending"), now=NOW)
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason == "ambiguous_binding"


def test_guardian_revoked_consent_quarantines_as_expired_only() -> None:
    outcome = normalize_row(
        legacy_row(revoked_at=datetime(2026, 2, 1, tzinfo=UTC)),
        now=NOW,
    )
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason == "expired_only"


def test_unknown_capability_quarantines() -> None:
    outcome = normalize_row(legacy_row(capability_clue="time_machine"), now=NOW)
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason == "unsupported_capability"


def test_fail_closed_default_for_completely_unknown_row() -> None:
    outcome = normalize_row(
        RawLegacyConsent(
            source="mystery",
            legacy_id="row_1",
            subject_clue="",
            subject_account_clue=None,
            actor_clue="",
            capability_clue="mystery_capability",
            purpose_clue="",
            evidence_clue="",
            granted_at=datetime(2026, 1, 1, tzinfo=UTC),
            expires_at=None,
            revoked_at=None,
            policy_version="v0",
            binding_clue=None,
            binding_status=None,
            raw={},
        ),
        now=NOW,
    )
    assert isinstance(outcome, QuarantineEntry)
    assert outcome.reason in {
        "unsupported_capability",
        "subject_unknown",
        "missing_purpose",
        "missing_evidence",
    }


def test_normalize_row_rejects_naive_now() -> None:
    with pytest.raises(ValueError):
        normalize_row(legacy_row(), now=datetime(2026, 6, 1))


def test_normalize_rows_sorts_and_counts() -> None:
    rows = [
        legacy_row(legacy_id="consent_b", capability_clue="weekly_report"),
        legacy_row(legacy_id="consent_a"),
        legacy_row(legacy_id="consent_c", capability_clue="corpus_recording"),
    ]
    result = normalize_rows(rows, now=NOW)
    assert isinstance(result, NormalizationResult)
    assert [item.legacy_id for item in result.normalized] == ["consent_a", "consent_b"]
    assert [item.legacy_id for item in result.quarantined] == ["consent_c"]


def test_normalize_rows_deterministic_consent_ids() -> None:
    first = normalize_rows([legacy_row()], now=NOW).normalized[0]
    second = normalize_rows([legacy_row()], now=NOW).normalized[0]
    assert first.consent_id == second.consent_id
    assert first.offer_id == second.offer_id
