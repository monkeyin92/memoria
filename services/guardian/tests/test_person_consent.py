"""P0-04 person-scoped consents for subjects that have no account.

The subject of a ``parent_for_child`` binding never confirms a guardian link,
so the link-scoped consent table can never hold their consent.  These tests
pin the person-scoped record, the unioned read gate and the account-governance
counting on the SQLite adapter (the PostgreSQL contract lives in
``test_guardian_postgres_store.py``).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from services.guardian.domain import (
    ConsentRecord,
    GuardianAccessDeniedError,
    GuardianConflictError,
    GuardianNotFoundError,
    PersonConsentRecord,
)
from services.guardian.sqlite_store import SqliteGuardianStore

NOW = datetime(2026, 9, 17, 3, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> SqliteGuardianStore:
    store = SqliteGuardianStore(tmp_path / "guardian.sqlite3")
    store.initialize()
    return store


def _record(
    *,
    consent_id: str = "00000000-0000-0000-0000-000000000101",
    subject: str = "child-person",
    grantor: str = "adult-owner",
    kind: str = "memory_retention",
    granted_at: datetime = NOW,
    expires_at: datetime | None = None,
) -> PersonConsentRecord:
    return PersonConsentRecord(
        consent_id=consent_id,
        subject_person_id=subject,
        grantor_person_id=grantor,
        consent_kind=kind,  # type: ignore[arg-type]
        policy_version="minor-memory-v1",
        granted_at=granted_at,
        expires_at=expires_at,
        evidence_event_id=f"evidence-{consent_id}",
    )


@pytest.mark.asyncio
async def test_person_consent_grant_read_revoke_is_idempotent_and_scoped(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    record = _record()
    assert await store.grant_person_consent(
        record, actor_person_id="adult-owner"
    ) == record

    # The read gate sees it for the subject's own person id.
    active = await store.active_consent(
        minor_user_id="child-person", consent_kind="memory_retention"
    )
    assert active is not None
    assert active.consent_id == record.consent_id
    # A different subject never inherits it.
    assert (
        await store.active_consent(
            minor_user_id="other-person", consent_kind="memory_retention"
        )
        is None
    )
    assert (
        await store.active_consent(
            minor_user_id="child-person", consent_kind="minor_voice_session"
        )
        is None
    )

    # Only the granting person may write; the subject may read its own row.
    with pytest.raises(GuardianAccessDeniedError):
        await store.grant_person_consent(
            _record(
                consent_id="00000000-0000-0000-0000-000000000102",
                kind="minor_voice_session",
            ),
            actor_person_id="someone-else",
        )
    listed = await store.list_person_consents(
        subject_person_id="child-person", actor_person_id="child-person"
    )
    assert [item.consent_id for item in listed] == [record.consent_id]
    with pytest.raises(GuardianNotFoundError):
        await store.get_person_consent(
            consent_id=record.consent_id, actor_person_id="unrelated-person"
        )

    # A second active consent for the same kind conflicts.
    with pytest.raises(GuardianConflictError):
        await store.grant_person_consent(
            _record(consent_id="00000000-0000-0000-0000-000000000103"),
            actor_person_id="adult-owner",
        )

    revoked = await store.revoke_person_consent(
        consent_id=record.consent_id,
        grantor_person_id="adult-owner",
        revoked_at=NOW + timedelta(minutes=1),
        revocation_evidence_event_id="evidence-revoked",
    )
    assert revoked.revoked_at == NOW + timedelta(minutes=1)
    assert (
        await store.active_consent(
            minor_user_id="child-person", consent_kind="memory_retention"
        )
        is None
    )
    # Revoking twice with the same evidence replays; a different event conflicts.
    replay = await store.revoke_person_consent(
        consent_id=record.consent_id,
        grantor_person_id="adult-owner",
        revoked_at=NOW + timedelta(minutes=2),
        revocation_evidence_event_id="evidence-revoked",
    )
    assert replay.revoked_at == revoked.revoked_at
    with pytest.raises(GuardianConflictError):
        await store.revoke_person_consent(
            consent_id=record.consent_id,
            grantor_person_id="adult-owner",
            revoked_at=NOW + timedelta(minutes=3),
            revocation_evidence_event_id="evidence-revoked-again",
        )


@pytest.mark.asyncio
async def test_person_consent_expiry_drops_out_of_the_read_gate(
    tmp_path: Path,
) -> None:
    """Only corpus consent may expire; once it does, the gate closes again."""

    store = _store(tmp_path)
    expired = _record(
        consent_id="00000000-0000-0000-0000-000000000201",
        kind="corpus_recording",
        granted_at=NOW - timedelta(days=2),
        expires_at=NOW - timedelta(days=1),
    )
    await store.grant_person_consent(expired, actor_person_id="adult-owner")
    assert (
        await store.active_consent(
            minor_user_id="child-person", consent_kind="corpus_recording"
        )
        is None
    )
    # A non-expiring record of the same kind is refused while the expired row
    # is still un-revoked, so the operator must revoke it explicitly.
    with pytest.raises(GuardianConflictError):
        await store.grant_person_consent(
            _record(
                consent_id="00000000-0000-0000-0000-000000000202",
                kind="corpus_recording",
                expires_at=NOW + timedelta(days=1),
            ),
            actor_person_id="adult-owner",
        )


@pytest.mark.asyncio
async def test_read_gate_unions_link_and_person_consents(tmp_path: Path) -> None:
    """One gate, two key spaces: link-scoped first, person-scoped as fallback."""

    store = _store(tmp_path)
    digest = hashlib.sha256(b"binding-code").hexdigest()
    link = await store.create_link(
        guardian_user_id="guardian-user",
        minor_user_id="minor-user",
        relation="parent",
        verified_via="wechat_identity",
        binding_code_hash=digest,
        binding_expires_at=NOW + timedelta(minutes=15),
        now=NOW,
    )
    await store.confirm_link(
        link_id=link.link_id,
        minor_user_id="minor-user",
        binding_code_hash=digest,
        now=NOW,
    )
    await store.grant_consent(
        ConsentRecord(
            consent_id="00000000-0000-0000-0000-000000000301",
            link_id=link.link_id,
            consent_kind="memory_retention",
            policy_version="minor-memory-v1",
            granted_at=NOW,
            evidence_event_id="evidence-link-scoped",
        ),
        actor_user_id="guardian-user",
    )
    link_scoped = await store.active_consent(
        minor_user_id="minor-user", consent_kind="memory_retention"
    )
    assert isinstance(link_scoped, ConsentRecord)

    await store.grant_person_consent(
        _record(
            consent_id="00000000-0000-0000-0000-000000000302",
            subject="child-person",
        ),
        actor_person_id="adult-owner",
    )
    person_scoped = await store.active_consent(
        minor_user_id="child-person", consent_kind="memory_retention"
    )
    assert isinstance(person_scoped, PersonConsentRecord)


@pytest.mark.asyncio
async def test_account_governance_covers_person_consents(tmp_path: Path) -> None:
    """Export/delete/remaining must count the new table for either person key."""

    store = _store(tmp_path)
    await store.grant_person_consent(_record(), actor_person_id="adult-owner")
    await store.grant_person_consent(
        _record(
            consent_id="00000000-0000-0000-0000-000000000401",
            subject="other-child",
            grantor="adult-owner",
            kind="minor_voice_session",
        ),
        actor_person_id="adult-owner",
    )
    assert await store.remaining_account_rows(account_id="adult-owner") == {
        "person_consents": 2
    }
    assert (
        await store.remaining_account_rows(account_id="child-person")
        == {"person_consents": 1}
    )

    await store.delete_for_account(account_id="adult-owner")
    assert await store.remaining_account_rows(account_id="adult-owner") == {}
    # The subject-keyed row was deleted with its grantor.
    assert await store.remaining_account_rows(account_id="child-person") == {}
