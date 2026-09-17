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
            consent_id=record.consent_id,
            actor_person_id="unrelated-person",
            subject_person_id="child-person",
        )
    # The trusted subject context cannot be swapped to another child: the
    # grantor still only sees the record for the subject it granted.
    with pytest.raises(GuardianNotFoundError):
        await store.get_person_consent(
            consent_id=record.consent_id,
            actor_person_id="adult-owner",
            subject_person_id="other-child",
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
        subject_person_id="child-person",
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
        subject_person_id="child-person",
        revoked_at=NOW + timedelta(minutes=2),
        revocation_evidence_event_id="evidence-revoked",
    )
    assert replay.revoked_at == revoked.revoked_at
    with pytest.raises(GuardianConflictError):
        await store.revoke_person_consent(
            consent_id=record.consent_id,
            grantor_person_id="adult-owner",
            subject_person_id="child-person",
            revoked_at=NOW + timedelta(minutes=3),
            revocation_evidence_event_id="evidence-revoked-again",
        )
    # A revoke attempted for another subject cannot touch the row.
    with pytest.raises(GuardianNotFoundError):
        await store.revoke_person_consent(
            consent_id=record.consent_id,
            grantor_person_id="adult-owner",
            subject_person_id="other-child",
            revoked_at=NOW + timedelta(minutes=3),
            revocation_evidence_event_id="evidence-revoked-wrong-subject",
        )


@pytest.mark.asyncio
async def test_person_consent_grant_replay_returns_the_original_record(
    tmp_path: Path,
) -> None:
    """An idempotent retry must not turn into a conflict over its new clock."""

    store = _store(tmp_path)
    record = _record()
    original = await store.grant_person_consent(record, actor_person_id="adult-owner")

    retried = await store.grant_person_consent(
        _record(granted_at=NOW + timedelta(minutes=5)),
        actor_person_id="adult-owner",
    )
    assert retried == original
    assert retried.granted_at == NOW
    assert retried.evidence_event_id == record.evidence_event_id

    # The same deterministic id with a different request payload still
    # conflicts instead of silently returning the old grant.
    with pytest.raises(GuardianConflictError):
        await store.grant_person_consent(
            _record(
                kind="minor_voice_session",
                granted_at=NOW + timedelta(minutes=5),
            ),
            actor_person_id="adult-owner",
        )


@pytest.mark.asyncio
async def test_person_consent_grant_replay_respects_the_retention_span(
    tmp_path: Path,
) -> None:
    """Only corpus consent may expire, so a changed retention span conflicts."""

    store = _store(tmp_path)
    record = _record(
        kind="corpus_recording",
        expires_at=NOW + timedelta(days=2),
    )
    original = await store.grant_person_consent(record, actor_person_id="adult-owner")

    retried = await store.grant_person_consent(
        _record(
            kind="corpus_recording",
            granted_at=NOW + timedelta(minutes=5),
            expires_at=NOW + timedelta(days=2, minutes=5),
        ),
        actor_person_id="adult-owner",
    )
    assert retried == original

    with pytest.raises(GuardianConflictError):
        await store.grant_person_consent(
            _record(
                kind="corpus_recording",
                granted_at=NOW + timedelta(minutes=5),
                expires_at=NOW + timedelta(days=3, minutes=5),
            ),
            actor_person_id="adult-owner",
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
    first_id = "00000000-0000-0000-0000-000000000101"
    second_id = "00000000-0000-0000-0000-000000000401"

    owner_export = await store.export_for_account(account_id="adult-owner")
    assert {row["consent_id"] for row in owner_export["person_consents"]} == {  # type: ignore[union-attr]
        first_id,
        second_id,
    }
    child_export = await store.export_for_account(account_id="child-person")
    assert [row["consent_id"] for row in child_export["person_consents"]] == [  # type: ignore[union-attr]
        first_id
    ]
    stranger_export = await store.export_for_account(account_id="stranger")
    assert stranger_export["person_consents"] == []

    assert await store.remaining_account_rows(account_id="adult-owner") == {
        "person_consents": 2
    }
    assert (
        await store.remaining_account_rows(account_id="child-person")
        == {"person_consents": 1}
    )

    deleted = await store.delete_for_account(account_id="adult-owner")
    assert deleted["person_consents"] == 2
    assert await store.remaining_account_rows(account_id="adult-owner") == {}
    # The subject-keyed row was deleted with its grantor.
    assert await store.remaining_account_rows(account_id="child-person") == {}
    assert (
        await store.export_for_account(account_id="child-person")
    )["person_consents"] == []

@pytest.mark.asyncio
async def test_person_consent_grant_recovers_after_evidence_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evidence-first grant: a ledger failure leaves no usable consent.

    The retry reuses the same deterministic consent/evidence ids, and a later
    replay returns the original record without appending a second grant
    event.
    """

    from services.archive.life_archive import LifeArchive
    from services.guardian.consent import GuardianConsentService

    store = _store(tmp_path)
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    original_record = archive.record
    attempts = {"count": 0}

    async def flaky_record(event: object) -> object:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("evidence ledger unavailable")
        return await original_record(event)  # type: ignore[arg-type]

    monkeypatch.setattr(archive, "record", flaky_record)
    service = GuardianConsentService(store, archive)
    record = _record(consent_id="00000000-0000-0000-0000-000000000501")

    with pytest.raises(RuntimeError, match="ledger unavailable"):
        await service.grant_for_person(
            subject_person_id=record.subject_person_id,
            grantor_person_id=record.grantor_person_id,
            consent_kind=record.consent_kind,
            policy_version=record.policy_version,
            evidence_event_id=record.evidence_event_id,
            consent_id=record.consent_id,
            now=NOW,
        )
    assert (
        await store.active_consent(
            minor_user_id=record.subject_person_id,
            consent_kind=record.consent_kind,
        )
        is None
    )
    assert (
        await store.list_person_consents(
            subject_person_id=record.subject_person_id,
            actor_person_id=record.grantor_person_id,
        )
        == ()
    )

    created = await service.grant_for_person(
        subject_person_id=record.subject_person_id,
        grantor_person_id=record.grantor_person_id,
        consent_kind=record.consent_kind,
        policy_version=record.policy_version,
        evidence_event_id=record.evidence_event_id,
        consent_id=record.consent_id,
        now=NOW + timedelta(minutes=1),
    )
    assert created.consent_id == record.consent_id
    assert created.evidence_event_id == record.evidence_event_id
    assert (
        await store.active_consent(
            minor_user_id=record.subject_person_id,
            consent_kind=record.consent_kind,
        )
        is not None
    )

    replay = await service.grant_for_person(
        subject_person_id=record.subject_person_id,
        grantor_person_id=record.grantor_person_id,
        consent_kind=record.consent_kind,
        policy_version=record.policy_version,
        evidence_event_id=record.evidence_event_id,
        consent_id=record.consent_id,
        now=NOW + timedelta(minutes=5),
    )
    assert replay == created
    events = await archive.evidence_window(
        account_id=record.subject_person_id,
        occurred_after=NOW - timedelta(minutes=1),
        occurred_before=NOW + timedelta(hours=1),
        event_types=("guardian.person_consent_granted",),
    )
    assert len(events) == 1
    assert events[0].event_id == record.evidence_event_id
