"""Adapter tests: old-source rows are mapped into uniform RawLegacyConsent rows.

All fixtures are fake rows; no database is touched.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import cast

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
from services.guardian.domain import ConsentKind, ConsentRecord, GuardianLink, GuardianLinkStatus


class FakeGuardianSource:
    def __init__(self, rows: tuple[GuardianConsentRow, ...]) -> None:
        self._rows = rows

    def iter_rows(self) -> Iterator[GuardianConsentRow]:
        return iter(self._rows)


class FakeVoiceCloneSource:
    def __init__(self, rows: tuple[VoiceCloneConsentRow, ...]) -> None:
        self._rows = rows

    def iter_rows(self) -> Iterator[VoiceCloneConsentRow]:
        return iter(self._rows)


class FakeRawAudioSource:
    def __init__(self, rows: tuple[RawAudioConsentRow, ...]) -> None:
        self._rows = rows

    def iter_rows(self) -> Iterator[RawAudioConsentRow]:
        return iter(self._rows)


class FakePersonaSource:
    def __init__(self, rows: tuple[PersonaLearningConsentRow, ...]) -> None:
        self._rows = rows

    def iter_rows(self) -> Iterator[PersonaLearningConsentRow]:
        return iter(self._rows)


def make_link(
    *,
    link_id: str = "link_1",
    minor: str = "minor_1",
    guardian: str = "guardian_1",
    status: str = "active",
) -> GuardianLink:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    return GuardianLink(
        link_id=link_id,
        guardian_user_id=guardian,
        minor_user_id=minor,
        relation="parent",
        status=cast(GuardianLinkStatus, status),
        verified_via="wechat_identity",
        created_at=base,
        binding_expires_at=base + timedelta(days=30),
        activated_at=base if status == "active" else None,
        revoked_at=base + timedelta(days=1) if status == "revoked" else None,
    )


def make_guardian_record(
    *,
    consent_id: str = "consent_1",
    kind: ConsentKind = "memory_retention",
    revoked: bool = False,
) -> ConsentRecord:
    granted = datetime(2026, 1, 10, tzinfo=UTC)
    return ConsentRecord(
        consent_id=consent_id,
        link_id="link_1",
        consent_kind=kind,
        policy_version="policy-v1",
        granted_at=granted,
        evidence_event_id="evt_grant_1",
        expires_at=granted + timedelta(days=10) if kind == "corpus_recording" else None,
        revoked_at=granted + timedelta(days=2) if revoked else None,
        revocation_evidence_event_id="evt_revoke_1" if revoked else None,
    )


def test_guardian_adapter_maps_memory_retention_row() -> None:
    row = GuardianConsentRow(record=make_guardian_record(), link=make_link())
    mapped = list(GuardianConsentAdapter(FakeGuardianSource((row,))).iter_rows())

    assert len(mapped) == 1
    legacy = mapped[0]
    assert legacy.source == "guardian"
    assert legacy.legacy_id == "consent_1"
    assert legacy.subject_clue == "minor_1"
    assert legacy.actor_clue == "guardian_1"
    assert legacy.capability_clue == "memory_retention"
    assert legacy.purpose_clue == "memory_retention"
    assert legacy.evidence_clue == "evt_grant_1"
    assert legacy.policy_version == "policy-v1"
    assert legacy.granted_at == datetime(2026, 1, 10, tzinfo=UTC)
    assert legacy.expires_at is None
    assert legacy.revoked_at is None
    assert legacy.binding_clue == "link_1"
    assert legacy.binding_status == "active"
    assert legacy.subject_account_clue is None
    assert legacy.raw["record"] is row.record
    assert legacy.raw["link"] is row.link


@pytest.mark.parametrize(
    ("kind", "expected_clue"),
    [
        ("memory_retention", "memory_retention"),
        ("weekly_report", "weekly_report"),
        ("minor_voice_session", "minor_voice_session"),
        ("corpus_recording", "corpus_recording"),
    ],
)
def test_guardian_adapter_capability_clue_preserves_source_kind(
    kind: ConsentKind, expected_clue: str
) -> None:
    row = GuardianConsentRow(record=make_guardian_record(kind=kind), link=make_link())
    legacy = list(GuardianConsentAdapter(FakeGuardianSource((row,))).iter_rows())[0]
    assert legacy.capability_clue == expected_clue


def test_guardian_adapter_without_link_leaves_identity_clues_empty() -> None:
    row = GuardianConsentRow(record=make_guardian_record(), link=None)
    legacy = list(GuardianConsentAdapter(FakeGuardianSource((row,))).iter_rows())[0]
    assert legacy.subject_clue == ""
    assert legacy.actor_clue == ""
    assert legacy.binding_clue is None
    assert legacy.binding_status is None


def test_guardian_adapter_carries_revocation() -> None:
    row = GuardianConsentRow(record=make_guardian_record(revoked=True), link=make_link())
    legacy = list(GuardianConsentAdapter(FakeGuardianSource((row,))).iter_rows())[0]
    assert legacy.revoked_at == datetime(2026, 1, 12, tzinfo=UTC)


def test_voice_clone_adapter_maps_row() -> None:
    granted = datetime(2026, 2, 1, tzinfo=UTC)
    row = VoiceCloneConsentRow(
        account_id="acct_1",
        policy_version="voice-policy-v2",
        granted_at=granted,
        revoked_at=None,
        grant_event_id="evt_voice_grant",
        revoke_event_id=None,
    )
    legacy = list(VoiceCloneConsentAdapter(FakeVoiceCloneSource((row,))).iter_rows())[0]
    assert legacy.source == "voice_clone"
    assert legacy.legacy_id == "acct_1"
    assert legacy.subject_clue == "acct_1"
    assert legacy.actor_clue == "acct_1"
    assert legacy.capability_clue == "voice_clone"
    assert legacy.evidence_clue == "evt_voice_grant"
    assert legacy.granted_at == granted
    assert legacy.revoked_at is None
    assert legacy.policy_version == "voice-policy-v2"


def test_voice_clone_adapter_carries_revocation() -> None:
    granted = datetime(2026, 2, 1, tzinfo=UTC)
    revoked = datetime(2026, 3, 1, tzinfo=UTC)
    row = VoiceCloneConsentRow(
        account_id="acct_1",
        policy_version="voice-policy-v2",
        granted_at=granted,
        revoked_at=revoked,
        grant_event_id="evt_voice_grant",
        revoke_event_id="evt_voice_revoke",
    )
    legacy = list(VoiceCloneConsentAdapter(FakeVoiceCloneSource((row,))).iter_rows())[0]
    assert legacy.revoked_at == revoked


def test_raw_audio_adapter_maps_row_with_evidence() -> None:
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
    legacy = list(RawAudioConsentAdapter(FakeRawAudioSource((row,))).iter_rows())[0]
    assert legacy.source == "raw_audio"
    assert legacy.legacy_id == "raw_grant_1"
    assert legacy.subject_clue == "acct_1"
    assert legacy.actor_clue == "acct_1"
    assert legacy.capability_clue == "raw_audio_retention"
    assert legacy.purpose_clue == "raw_audio_retention"
    assert legacy.evidence_clue == "evt_raw_grant"
    assert legacy.policy_version == "raw-policy-v1"


def test_raw_audio_adapter_uses_row_purpose_when_present() -> None:
    row = RawAudioConsentRow(
        consent_grant_id="raw_grant_1",
        account_id="acct_1",
        policy_version="raw-policy-v1",
        retention_policy="account_lifetime",
        purpose="voice_corpus_study",
        granted_at=datetime(2026, 2, 15, tzinfo=UTC),
        expires_at=None,
        revoked_at=None,
        evidence_event_id="evt_raw_grant",
    )
    legacy = list(RawAudioConsentAdapter(FakeRawAudioSource((row,))).iter_rows())[0]
    assert legacy.purpose_clue == "voice_corpus_study"


def test_raw_audio_adapter_without_evidence_leaves_clue_empty() -> None:
    row = RawAudioConsentRow(
        consent_grant_id="raw_grant_1",
        account_id="acct_1",
        policy_version="raw-policy-v1",
        retention_policy="account_lifetime",
        purpose=None,
        granted_at=datetime(2026, 2, 15, tzinfo=UTC),
        expires_at=None,
        revoked_at=None,
        evidence_event_id=None,
    )
    legacy = list(RawAudioConsentAdapter(FakeRawAudioSource((row,))).iter_rows())[0]
    assert legacy.evidence_clue == ""
    assert legacy.purpose_clue == "raw_audio_retention"


def test_persona_adapter_with_person_identity_maps_person_subject() -> None:
    row = PersonaLearningConsentRow(
        account_id="acct_1",
        policy_version="persona-policy-v3",
        granted_at=datetime(2026, 2, 20, tzinfo=UTC),
        revoked_at=None,
        grant_event_id="evt_persona_grant",
        revoke_event_id=None,
        person_id="person_9",
    )
    legacy = list(PersonaConsentAdapter(FakePersonaSource((row,))).iter_rows())[0]
    assert legacy.source == "persona"
    assert legacy.legacy_id == "acct_1"
    assert legacy.subject_clue == "person_9"
    assert legacy.actor_clue == "person_9"
    assert legacy.subject_account_clue is None
    assert legacy.capability_clue == "persona_learning"
    assert legacy.evidence_clue == "evt_persona_grant"


def test_persona_adapter_without_person_identity_keeps_account_clue_only() -> None:
    row = PersonaLearningConsentRow(
        account_id="acct_1",
        policy_version="persona-policy-v3",
        granted_at=datetime(2026, 2, 20, tzinfo=UTC),
        revoked_at=None,
        grant_event_id="evt_persona_grant",
        revoke_event_id=None,
        person_id=None,
    )
    legacy = list(PersonaConsentAdapter(FakePersonaSource((row,))).iter_rows())[0]
    assert legacy.subject_clue == ""
    assert legacy.actor_clue == ""
    assert legacy.subject_account_clue == "acct_1"


def test_adapters_preserve_source_order() -> None:
    rows = (
        GuardianConsentRow(
            record=make_guardian_record(consent_id="consent_a"),
            link=make_link(),
        ),
        GuardianConsentRow(
            record=make_guardian_record(consent_id="consent_b"),
            link=make_link(),
        ),
    )
    legacy_rows = list(GuardianConsentAdapter(FakeGuardianSource(rows)).iter_rows())
    assert [row.legacy_id for row in legacy_rows] == ["consent_a", "consent_b"]


def test_raw_legacy_consent_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        RawLegacyConsent(
            source="guardian",
            legacy_id="consent_1",
            subject_clue="minor_1",
            subject_account_clue=None,
            actor_clue="guardian_1",
            capability_clue="memory_retention",
            purpose_clue="memory_retention",
            evidence_clue="evt_1",
            granted_at=datetime(2026, 1, 10),
            expires_at=None,
            revoked_at=None,
            policy_version="policy-v1",
            binding_clue="link_1",
            binding_status="active",
            raw={},
        )


def test_raw_legacy_consent_rejects_empty_legacy_id() -> None:
    with pytest.raises(ValueError):
        RawLegacyConsent(
            source="guardian",
            legacy_id="",
            subject_clue="minor_1",
            subject_account_clue=None,
            actor_clue="guardian_1",
            capability_clue="memory_retention",
            purpose_clue="memory_retention",
            evidence_clue="evt_1",
            granted_at=datetime(2026, 1, 10, tzinfo=UTC),
            expires_at=None,
            revoked_at=None,
            policy_version="policy-v1",
            binding_clue="link_1",
            binding_status="active",
            raw={},
        )
