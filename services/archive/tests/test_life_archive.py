from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime

import pytest
from services.archive.domain import (
    ContextQuery,
    EvidenceEvent,
    IdempotencyConflictError,
    MemoryReview,
    RawVoiceConsentRequiredError,
)
from services.archive.life_archive import LifeArchive
from services.archive.object_store import ObjectRef


@pytest.mark.asyncio
async def test_recording_the_same_event_is_idempotent(tmp_path) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    event = EvidenceEvent(
        event_id="event-001",
        account_id="account-001",
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
        speaker_class="owner",
        source="funasr",
        payload={"text": "我小时候住在杭州。"},
    )

    results = [await archive.record(event) for _ in range(100)]

    assert results[0].duplicate is False
    assert all(result.event_id == "event-001" for result in results)
    assert all(result.outbox_id == results[0].outbox_id for result in results)
    assert sum(not result.duplicate for result in results) == 1
    context = await archive.context(
        ContextQuery(account_id="account-001", speaker_class="owner")
    )
    assert [item.event_id for item in context.evidence] == ["event-001"]


@pytest.mark.asyncio
async def test_reusing_an_event_id_for_different_content_is_rejected(tmp_path) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    original = EvidenceEvent(
        event_id="event-001",
        account_id="account-001",
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
        speaker_class="owner",
        source="funasr",
        payload={"text": "原始内容"},
    )
    conflicting = EvidenceEvent(
        event_id="event-001",
        account_id="account-001",
        event_type="speech.utterance_finalized",
        occurred_at=original.occurred_at,
        speaker_class="owner",
        source="funasr",
        payload={"text": "被替换的内容"},
    )
    await archive.record(original)

    with pytest.raises(IdempotencyConflictError):
        await archive.record(conflicting)


@pytest.mark.asyncio
async def test_review_correction_supersedes_without_rewriting_original_evidence(tmp_path) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    original = EvidenceEvent(
        event_id="event-original",
        account_id="account-001",
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
        speaker_class="owner",
        source="funasr",
        payload={"text": "我在苏州读过书。"},
    )
    await archive.record(original)

    reviewed = await archive.review(
        MemoryReview(
            review_event_id="event-correction",
            account_id="account-001",
            target_id="event-original",
            action="correct",
            corrected_text="我在杭州读过书。",
            occurred_at=datetime(2026, 7, 19, 8, 1, tzinfo=UTC),
        )
    )

    assert reviewed.status == "corrected"
    assert reviewed.current_text == "我在杭州读过书。"
    context = await archive.context(
        ContextQuery(account_id="account-001", speaker_class="owner")
    )
    assert {item.event_id for item in context.evidence} == {
        "event-original",
        "event-correction",
    }
    correction = next(item for item in context.evidence if item.event_id == "event-correction")
    assert correction.supersedes_event_id == "event-original"


@pytest.mark.asyncio
async def test_raw_voice_consent_controls_atomic_idempotent_blob_metadata(tmp_path) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    consent = await archive.grant_raw_voice_consent(
        account_id="account-001",
        policy_version="raw-voice-v1",
        retention_policy="account_lifetime",
        granted_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
    )
    event = EvidenceEvent(
        event_id="raw-voice-event",
        account_id="account-001",
        session_id="session-001",
        turn_id=1,
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 8, 1, tzinfo=UTC),
        speaker_class="owner",
        source="funasr.authoritative_final",
        consent_grant_id=consent.consent_grant_id,
        payload={"text": "这段主人话轮允许保存原始声音。"},
    )
    first_ref = ObjectRef(
        account_id="account-001",
        object_key="account/raw-voice/first.fernet",
        media_type="audio/wav",
        byte_count=32044,
        content_sha256="a" * 64,
        encryption_key_version="archive-v1",
        backend="archive",
    )
    retry_ref = ObjectRef(
        account_id="account-001",
        object_key="account/raw-voice/retry.fernet",
        media_type="audio/wav",
        byte_count=32044,
        content_sha256="a" * 64,
        encryption_key_version="archive-v1",
        backend="archive",
    )

    first = await archive.record_with_blob(
        event,
        first_ref,
        retention_policy="account_lifetime",
    )
    event_after_blob = await archive.record(event)
    retry = await archive.record_with_blob(
        event,
        retry_ref,
        retention_policy="account_lifetime",
    )

    assert first.duplicate is False
    assert first.blob_duplicate is False
    assert first.retained_object_key == first_ref.object_key
    assert event_after_blob.duplicate is True
    assert event_after_blob.blob_duplicate is None
    assert event_after_blob.retained_object_key is None
    assert retry.duplicate is True
    assert retry.blob_duplicate is True
    assert retry.retained_object_key == first_ref.object_key
    assert await archive.raw_voice_blobs(account_id="account-001") == (first_ref,)

    replacement_consent = await archive.grant_raw_voice_consent(
        account_id="account-001",
        policy_version="raw-voice-v2",
        retention_policy="account_lifetime",
        granted_at=datetime(2026, 7, 19, 8, 1, 30, tzinfo=UTC),
    )
    replacement_event = EvidenceEvent(
        event_id="raw-voice-event-v2",
        account_id="account-001",
        session_id="session-001",
        turn_id=2,
        event_type="speech.utterance_finalized",
        occurred_at=datetime(2026, 7, 19, 8, 1, 30, tzinfo=UTC),
        speaker_class="owner",
        source="funasr.authoritative_final",
        consent_grant_id=replacement_consent.consent_grant_id,
        payload={"text": "新版本授权下的声音。"},
    )
    replacement_ref = ObjectRef(
        account_id="account-001",
        object_key="account/raw-voice/replacement.fernet",
        media_type="audio/wav",
        byte_count=16044,
        content_sha256="b" * 64,
        encryption_key_version="archive-v1",
        backend="archive",
    )
    event_before_blob = await archive.record(replacement_event)
    blob_after_event = await archive.record_with_blob(
        replacement_event,
        replacement_ref,
        retention_policy="account_lifetime",
    )
    assert event_before_blob.duplicate is False
    assert event_before_blob.blob_duplicate is None
    assert blob_after_event.duplicate is True
    assert blob_after_event.blob_duplicate is False
    assert blob_after_event.retained_object_key == replacement_ref.object_key
    assert await archive.raw_voice_blobs(account_id="account-001") == (
        first_ref,
        replacement_ref,
    )

    revocation = await archive.revoke_raw_voice_consent(
        account_id="account-001",
        revoked_at=datetime(2026, 7, 19, 8, 2, tzinfo=UTC),
    )
    assert revocation.consent.revoked_at is not None
    assert revocation.references == (first_ref, replacement_ref)
    assert await archive.raw_voice_blobs(account_id="account-001") == (
        first_ref,
        replacement_ref,
    )
    await archive.purge_raw_voice_blobs(
        account_id="account-001",
        object_keys=tuple(item.object_key for item in revocation.references),
    )
    assert await archive.raw_voice_blobs(account_id="account-001") == ()
    assert [
        item.event_id
        for item in (
            await archive.context(ContextQuery(account_id="account-001", speaker_class="owner"))
        ).evidence
    ] == ["raw-voice-event-v2", "raw-voice-event"]
    retry = await archive.revoke_raw_voice_consent(
        account_id="account-001",
        revoked_at=datetime(2026, 7, 19, 8, 2, tzinfo=UTC),
    )
    assert retry.consent == revocation.consent
    assert retry.references == ()
    with pytest.raises(RawVoiceConsentRequiredError):
        await archive.record_with_blob(
            EvidenceEvent(
                event_id="late-raw-voice-event",
                account_id="account-001",
                session_id="session-001",
                turn_id=2,
                event_type="speech.utterance_finalized",
                occurred_at=datetime(2026, 7, 19, 8, 3, tzinfo=UTC),
                speaker_class="owner",
                source="funasr.authoritative_final",
                consent_grant_id=consent.consent_grant_id,
                payload={"text": "撤销后到达。"},
            ),
            retry_ref,
            retention_policy="account_lifetime",
        )


@pytest.mark.asyncio
async def test_expired_raw_voice_consent_fails_closed_and_can_be_replaced(tmp_path) -> None:
    path = tmp_path / "archive.sqlite3"
    archive = LifeArchive.sqlite(path)
    archive.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO consent_grants (
                consent_grant_id, account_id, purpose, policy_version,
                retention_policy, granted_at, expires_at
            ) VALUES (?, ?, 'raw_voice_archive', ?, 'account_lifetime', ?, ?)
            """,
            (
                "expired-consent",
                "account-expired",
                "raw-voice-v1",
                datetime(2026, 7, 18, 8, 0, tzinfo=UTC).isoformat(),
                datetime(2026, 7, 18, 9, 0, tzinfo=UTC).isoformat(),
            ),
        )

    assert await archive.active_raw_voice_consent(account_id="account-expired") is None

    replacement = await archive.grant_raw_voice_consent(
        account_id="account-expired",
        policy_version="raw-voice-v1",
        retention_policy="account_lifetime",
        granted_at=datetime(2026, 7, 19, 8, 0, tzinfo=UTC),
    )

    assert replacement.consent_grant_id != "expired-consent"
    assert await archive.active_raw_voice_consent(account_id="account-expired") == replacement


@pytest.mark.asyncio
async def test_concurrent_raw_voice_grants_return_one_active_consent(tmp_path) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    granted_at = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)

    def grant_in_thread():
        return asyncio.run(
            archive.grant_raw_voice_consent(
                account_id="account-concurrent",
                policy_version="raw-voice-v1",
                retention_policy="account_lifetime",
                granted_at=granted_at,
            )
        )

    grants = await asyncio.gather(
        *(asyncio.to_thread(grant_in_thread) for _ in range(8))
    )

    assert len({grant.consent_grant_id for grant in grants}) == 1
    assert await archive.active_raw_voice_consent(account_id="account-concurrent") == grants[0]


@pytest.mark.asyncio
async def test_owner_context_hides_legacy_guest_and_uncertain_evidence(tmp_path) -> None:
    archive = LifeArchive.sqlite(tmp_path / "archive.sqlite3")
    occurred_at = datetime(2026, 7, 19, 8, 0, tzinfo=UTC)
    for speaker_class in ("owner", "guest", "uncertain"):
        await archive.record(
            EvidenceEvent(
                event_id=f"legacy-{speaker_class}",
                account_id="account-legacy-speakers",
                event_type="speech.utterance_finalized",
                occurred_at=occurred_at,
                speaker_class=speaker_class,  # type: ignore[arg-type]
                source="legacy-import",
                payload={"text": speaker_class},
            )
        )

    context = await archive.context(
        ContextQuery(account_id="account-legacy-speakers", speaker_class="owner")
    )

    assert [event.event_id for event in context.evidence] == ["legacy-owner"]
