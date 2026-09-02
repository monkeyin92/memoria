from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from services.control_api.app.database import MemoryStore
from services.control_api.app.subject_verification import (
    maybe_verify_adult_from_wechat_phone,
    speaker_enrollment_remediation,
)


def _store(tmp_path: Path) -> MemoryStore:
    store = MemoryStore(str(tmp_path / "memoria.sqlite3"))
    now = datetime.now(UTC).isoformat()
    store.register_account(
        user_id="owner-1",
        username="owner",
        username_normalized="owner",
        password_hash="hash",
        now=now,
    )
    return store


def test_maybe_verify_adult_from_wechat_phone_upgrades_unknown_subject(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.bind_external_identities(
        preferred_user_id="owner-1",
        identities={"wechat_phone": "phone-hash"},
        now=datetime.now(UTC).isoformat(),
    )
    updated = maybe_verify_adult_from_wechat_phone(
        store,
        user_id="owner-1",
        now=datetime.now(UTC).isoformat(),
    )
    assert updated is not None
    assert updated["subject_category"] == "adult"
    assert updated["birth_year_band"] == "adult"
    assert updated["age_evidence_status"] == "verified"


def test_maybe_verify_adult_from_wechat_phone_is_idempotent_for_adult(tmp_path: Path) -> None:
    store = _store(tmp_path)
    now = datetime.now(UTC).isoformat()
    store.bind_external_identities(
        preferred_user_id="owner-1",
        identities={"wechat_phone": "phone-hash"},
        now=now,
    )
    store.update_subject_profile(
        user_id="owner-1",
        subject_category="adult",
        birth_year_band="adult",
        age_evidence_status="verified",
        now=now,
    )
    assert maybe_verify_adult_from_wechat_phone(store, user_id="owner-1", now=now) is None


def test_speaker_enrollment_remediation_for_missing_phone() -> None:
    remediation = speaker_enrollment_remediation(
        block_code="subject_capability_forbidden",
        registered=True,
        has_wechat_phone=False,
        subject={
            "subject_category": "unknown",
            "birth_year_band": "unknown",
            "age_evidence_status": "unverified",
        },
    )
    assert remediation is not None
    assert remediation["steps"] == ["authorize_wechat_phone", "refresh_speaker_status"]
