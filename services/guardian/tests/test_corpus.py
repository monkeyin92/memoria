from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from services.archive.object_store import EncryptedLocalObjectStore, ObjectRef
from services.guardian.corpus import (
    MAX_ACTIVE_CORPUS_SAMPLES_PER_MINOR,
    CorpusConsentInactiveError,
    CorpusRetentionService,
    CorpusSample,
    CorpusSampleLimitError,
)
from services.guardian.domain import ConsentRecord
from services.guardian.sqlite_store import SqliteGuardianStore


@pytest.mark.asyncio
async def test_expired_corpus_sample_is_deleted_from_object_store_and_projection(
    tmp_path: Path,
) -> None:
    store = SqliteGuardianStore(tmp_path / "guardian.sqlite3")
    store.initialize()
    objects = EncryptedLocalObjectStore(
        root=tmp_path / "objects",
        key="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA=",
        key_version="corpus-test-v1",
    )
    now = datetime.now(UTC)
    digest = hashlib.sha256(b"binding-code").hexdigest()
    link = await store.create_link(
        guardian_user_id="guardian-a",
        minor_user_id="minor-a",
        relation="parent",
        verified_via="wechat_identity",
        binding_code_hash=digest,
        binding_expires_at=now + timedelta(minutes=15),
        now=now,
    )
    await store.confirm_link(
        link_id=link.link_id,
        minor_user_id="minor-a",
        binding_code_hash=digest,
        now=now,
    )
    consent = await store.grant_consent(
        ConsentRecord(
            consent_id="consent-a",
            link_id=link.link_id,
            consent_kind="corpus_recording",
            policy_version="authorized-child-corpus-v1",
            granted_at=now - timedelta(days=2),
            expires_at=now + timedelta(days=1),
            evidence_event_id="corpus-consent-a",
        )
    )
    reference = await objects.put(
        account_id="minor-a",
        purpose="authorized-child-corpus",
        data=b"encrypted child corpus sample",
        media_type="audio/wav",
    )
    sample = CorpusSample(
        sample_id="sample-a",
        minor_user_id="minor-a",
        consent_id=consent.consent_id,
        source_event_id="event-a",
        reference=reference,
        created_at=now - timedelta(days=2),
        expires_at=now - timedelta(days=1),
    )
    await store.record_corpus_sample(sample)

    assert await CorpusRetentionService(store, objects).purge_expired(now=now) == 1
    deleted = await store.corpus_sample_by_event(
        minor_user_id="minor-a",
        source_event_id="event-a",
    )
    assert deleted is not None and deleted.deleted_at == now
    with pytest.raises(FileNotFoundError):
        await objects.get(reference)


def test_corpus_retention_never_exceeds_thirty_days() -> None:
    now = datetime.now(UTC)
    reference = ObjectRef(
        account_id="minor-a",
        object_key="hash/authorized-child-corpus/sample.fernet",
        media_type="audio/wav",
        byte_count=10,
        content_sha256="0" * 64,
        encryption_key_version="v1",
        backend="local",
    )
    sample = CorpusSample(
        sample_id="sample-a",
        minor_user_id="minor-a",
        consent_id="consent-a",
        source_event_id="event-a",
        reference=reference,
        created_at=now,
        expires_at=now + timedelta(days=30),
    )
    with pytest.raises(ValueError, match="30 days"):
        replace(sample, expires_at=now + timedelta(days=30, seconds=1))


@pytest.mark.asyncio
async def test_corpus_store_rechecks_consent_and_enforces_atomic_limit(
    tmp_path: Path,
) -> None:
    store = SqliteGuardianStore(tmp_path / "guardian-limit.sqlite3")
    store.initialize()
    now = datetime.now(UTC)
    digest = hashlib.sha256(b"limit-binding-code").hexdigest()
    link = await store.create_link(
        guardian_user_id="guardian-limit",
        minor_user_id="minor-limit",
        relation="parent",
        verified_via="wechat_identity",
        binding_code_hash=digest,
        binding_expires_at=now + timedelta(minutes=15),
        now=now,
    )
    await store.confirm_link(
        link_id=link.link_id,
        minor_user_id="minor-limit",
        binding_code_hash=digest,
        now=now,
    )
    consent = await store.grant_consent(
        ConsentRecord(
            consent_id="consent-limit",
            link_id=link.link_id,
            consent_kind="corpus_recording",
            policy_version="authorized-child-corpus-v1",
            granted_at=now,
            expires_at=now + timedelta(days=2),
            evidence_event_id="corpus-consent-limit",
        )
    )

    def sample(index: int) -> CorpusSample:
        return CorpusSample(
            sample_id=f"sample-{index}",
            minor_user_id="minor-limit",
            consent_id=consent.consent_id,
            source_event_id=f"event-{index}",
            reference=ObjectRef(
                account_id="minor-limit",
                object_key=f"hash/authorized-child-corpus/sample-{index}.fernet",
                media_type="audio/wav",
                byte_count=10,
                content_sha256=f"{index:064x}",
                encryption_key_version="v1",
                backend="local",
            ),
            created_at=now,
            expires_at=now + timedelta(days=1),
        )

    for index in range(MAX_ACTIVE_CORPUS_SAMPLES_PER_MINOR):
        await store.record_corpus_sample(sample(index))

    with pytest.raises(CorpusSampleLimitError):
        await store.record_corpus_sample(sample(MAX_ACTIVE_CORPUS_SAMPLES_PER_MINOR))

    await store.revoke_consent(
        consent_id=consent.consent_id,
        guardian_user_id="guardian-limit",
        revoked_at=now + timedelta(minutes=1),
        revocation_evidence_event_id="corpus-consent-limit-revoked",
    )
    with pytest.raises(CorpusConsentInactiveError):
        await store.record_corpus_sample(sample(MAX_ACTIVE_CORPUS_SAMPLES_PER_MINOR + 1))
