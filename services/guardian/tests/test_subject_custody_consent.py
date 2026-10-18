from datetime import UTC, datetime, timedelta

import pytest
from services.guardian.domain import PersonConsentRecord

_NOW = datetime(2026, 10, 18, tzinfo=UTC)


def _record(kind: str, expires_at: datetime | None) -> PersonConsentRecord:
    return PersonConsentRecord(
        consent_id="c1",
        subject_person_id="p1",
        grantor_person_id="g1",
        consent_kind=kind,  # type: ignore[arg-type]
        policy_version="test-v1",
        granted_at=_NOW,
        evidence_event_id="e1",
        expires_at=expires_at,
    )


def test_subject_custody_requires_an_expiry() -> None:
    with pytest.raises(ValueError, match="requires an expiry"):
        _record("subject_custody", None)


def test_subject_custody_accepts_a_finite_expiry() -> None:
    record = _record("subject_custody", _NOW + timedelta(days=90))
    assert record.expires_at == _NOW + timedelta(days=90)


def test_subject_custody_expiry_must_follow_grant() -> None:
    with pytest.raises(ValueError, match="must follow"):
        _record("subject_custody", _NOW)


def test_other_kinds_still_cannot_expire() -> None:
    with pytest.raises(ValueError, match="may expire"):
        _record("memory_retention", _NOW + timedelta(days=1))
