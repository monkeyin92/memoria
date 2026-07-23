from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from services.archive.domain import ContextQuery, EvidenceEvent
from services.archive.life_archive import LifeArchive
from services.archive.memory_catalog import MemoryCatalog
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.object_store import EncryptedLocalObjectStore
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import hash_password
from services.digital_self.preview import (
    FIDELITY_CATEGORIES,
    FidelityTrialSpec,
    SelfPreviewRegistry,
)
from services.governance.account_data import (
    AccountDataGovernance,
    AccountDeletionIncompleteError,
    AccountDeletionWorker,
    SqliteAccountRepository,
)
from services.self_model.registry import SelfModelRegistry
from services.speaker.authority import SpeakerAuthority
from services.speaker.domain import EmbeddingResult, EnrollmentRequest, EnrollmentSample
from services.voice_profile.domain import ProviderVoice, VoiceEnrollmentRequest
from services.voice_profile.manager import VoiceProfileManager


class EmbeddingStub:
    model_version = "speaker-governance-test-v1"

    async def embed(self, pcm: bytes, *, sample_rate: int) -> EmbeddingResult:
        del pcm, sample_rate
        return EmbeddingResult(
            vector=(1.0, 0.0),
            speech_ms=1800,
            snr_db=20,
            quality_score=0.95,
            replay_risk=0.01,
            synthetic_risk=0.01,
        )


class VoiceProviderStub:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.delete_fails = False

    async def create_voice(
        self,
        *,
        target_model: str,
        prefix: str,
        sample_url: str,
    ) -> ProviderVoice:
        del sample_url
        return ProviderVoice(
            voice_id=f"{target_model}-clone-{prefix}",
            target_model=target_model,
        )

    async def delete_voice(self, *, voice_id: str) -> None:
        self.deleted.append(voice_id)
        if self.delete_fails:
            raise RuntimeError("provider unavailable")


class SessionTerminatorStub:
    def __init__(self) -> None:
        self.accounts: list[str] = []
        self.entered: asyncio.Event | None = None
        self.release: asyncio.Event | None = None

    async def terminate_account(self, account_id: str) -> int:
        self.accounts.append(account_id)
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        return 1


class LateArchiveWriteRepository:
    def __init__(self, delegate: SqliteAccountRepository, archive: LifeArchive) -> None:
        self._delegate = delegate
        self._archive = archive
        self.delete_calls = 0

    async def export_account(self, account_id: str):  # type: ignore[no-untyped-def]
        return await self._delegate.export_account(account_id)

    async def object_references(self, account_id: str):  # type: ignore[no-untyped-def]
        return await self._delegate.object_references(account_id)

    async def delete_account(self, account_id: str) -> dict[str, int]:
        counts = await self._delegate.delete_account(account_id)
        self.delete_calls += 1
        if self.delete_calls == 1:
            await self._archive.record(
                EvidenceEvent(
                    event_id="late-governance-evidence",
                    account_id=account_id,
                    event_type="speech.utterance_finalized",
                    occurred_at=datetime.now(UTC),
                    speaker_class="owner",
                    source="late-writer-test",
                    payload={"text": "删除进行中到达的迟到写入。"},
                )
            )
        return counts

    async def remaining_account_rows(self, account_id: str) -> dict[str, int]:
        return await self._delegate.remaining_account_rows(account_id)


async def _fixture(
    tmp_path: Path,
) -> tuple[
    AccountDataGovernance,
    MemoryStore,
    LifeArchive,
    SpeakerAuthority,
    VoiceProfileManager,
    VoiceProviderStub,
    SessionTerminatorStub,
    EncryptedLocalObjectStore,
    object,
]:
    account_id = "account-governance"
    database_path = tmp_path / "memoria.sqlite3"
    speaker_path = tmp_path / "speakers.sqlite3"
    store = MemoryStore(str(database_path))
    store.register_account(
        user_id=account_id,
        username="governance-owner",
        username_normalized="governance-owner",
        password_hash=hash_password("safe-passphrase"),
        now=datetime.now(UTC).isoformat(),
    )
    archive = LifeArchive.sqlite(database_path)
    event = EvidenceEvent(
        event_id="governance-evidence",
        account_id=account_id,
        event_type="speech.utterance_finalized",
        occurred_at=datetime.now(UTC),
        speaker_class="owner",
        source="test",
        payload={
            "text": "删除传播测试。",
            "interaction_mode": "companion",
            "simulated_output": False,
            "owner_projection_eligible": True,
        },
    )
    await archive.record(event)
    MemoryCatalog.sqlite(
        database_path,
        extractor=RuleBasedMemoryExtractor(),
    ).initialize()
    self_model = SelfModelRegistry.sqlite(database_path)
    claim = await self_model.create_cognitive_claim(
        account_id=account_id,
        claim_type="belief",
        statement="删除账户时，认知模型也必须完整清除。",
        confidence=0.9,
        idempotency_key="governance-self-model-create",
    )
    await self_model.add_source(
        account_id=account_id,
        item_kind="cognitive_claim",
        item_id=claim.claim_id,
        source_event_id=event.event_id,
        relation="support",
        adopted=True,
        negative=False,
        expected_version=claim.version,
        idempotency_key="governance-self-model-source",
    )
    decision = await self_model.create_decision_case(
        account_id=account_id,
        kind="real",
        context="是否完整删除数字自我数据",
        options=("完整删除", "只隐藏"),
        constraints=("必须可验证",),
        chosen_option="完整删除",
        rejected_options=("只隐藏",),
        outcome="等待删除流程验证",
        reflection="删除必须覆盖所有派生模型。",
        still_endorsed=True,
        idempotency_key="governance-decision-create",
    )
    await self_model.add_source(
        account_id=account_id,
        item_kind="decision_case",
        item_id=decision.case_id,
        source_event_id=event.event_id,
        relation="support",
        adopted=True,
        negative=False,
        expected_version=decision.version,
        idempotency_key="governance-decision-source",
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO person_entities (
                person_id, account_id, canonical_key, display_name,
                relationship_to_owner, status, source_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?)
            """,
            (
                "governance-person",
                account_id,
                "family:governance-person",
                "家人",
                "family",
                event.event_id,
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO relationships (
                relationship_id, account_id, person_id, relationship_type,
                status, source_event_id, valid_at
            ) VALUES (?, ?, ?, ?, 'confirmed', ?, ?)
            """,
            (
                "governance-relationship",
                account_id,
                "governance-person",
                "family",
                event.event_id,
                datetime.now(UTC).isoformat(),
            ),
        )
    relationship_profile = await self_model.create_relationship_profile(
        account_id=account_id,
        person_id="governance-person",
        relationship_id="governance-relationship",
        salutation="家人",
        tone="温和坦诚",
        advice_style="先听完再建议",
        boundaries=("不透露第三方私密内容",),
        idempotency_key="governance-relationship-profile-create",
    )
    await self_model.add_source(
        account_id=account_id,
        item_kind="relationship_profile",
        item_id=relationship_profile.profile_id,
        source_event_id=event.event_id,
        relation="support",
        adopted=True,
        negative=False,
        expected_version=relationship_profile.version_number,
        idempotency_key="governance-relationship-profile-source",
    )

    archive_objects = EncryptedLocalObjectStore(
        root=tmp_path / "archive-objects",
        key=Fernet.generate_key().decode("ascii"),
        key_version="archive-key-v1",
    )
    archive_reference = await archive_objects.put(
        account_id=account_id,
        purpose="source-audio",
        data=b"archive-audio",
        media_type="audio/wav",
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO evidence_blobs (
                blob_id, account_id, evidence_event_id, object_key, media_type,
                byte_count, content_sha256, encryption_key_version,
                retention_policy, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "governance-blob",
                account_id,
                event.event_id,
                archive_reference.object_key,
                archive_reference.media_type,
                archive_reference.byte_count,
                archive_reference.content_sha256,
                archive_reference.encryption_key_version,
                "account-lifetime",
                datetime.now(UTC).isoformat(),
            ),
        )

    speaker = SpeakerAuthority.sqlite(
        speaker_path,
        template_key=Fernet.generate_key().decode("ascii"),
        adapter=EmbeddingStub(),
    )
    await speaker.enroll(
        EnrollmentRequest(
            account_id=account_id,
            consent_grant_id="speaker-consent",
            samples=tuple(
                EnrollmentSample(pcm=f"sample-{index}".encode(), sample_rate=16000)
                for index in range(3)
            ),
        )
    )

    voice_objects = EncryptedLocalObjectStore(
        root=tmp_path / "voice-objects",
        key=Fernet.generate_key().decode("ascii"),
        key_version="voice-key-v1",
    )
    provider = VoiceProviderStub()
    session_terminator = SessionTerminatorStub()
    voice = VoiceProfileManager.sqlite(
        database_path,
        object_store=voice_objects,
        provider=provider,
        sample_url_factory=lambda sample_id: f"https://control.test/samples/{sample_id}",
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
    )
    await voice.grant_consent(account_id=account_id, policy_version="voice-clone-v1")
    await voice.enroll(
        VoiceEnrollmentRequest(
            account_id=account_id,
            audio=b"RIFF" + b"\x01\x02" * 16_000,
            media_type="audio/wav",
            duration_ms=12_000,
            sample_rate=24_000,
        )
    )
    preview = SelfPreviewRegistry.sqlite(database_path)
    now = datetime.now(UTC)
    grant = await preview.issue_grant(
        account_id=account_id,
        version_id="governance-version",
        manifest_sha256="a" * 64,
        perspective="owner",
        expires_at=now + timedelta(minutes=10),
        idempotency_key="governance-preview-grant",
        now=now,
    )
    evaluation = await preview.start_evaluation(
        account_id=account_id,
        version_id="governance-version",
        manifest_sha256="a" * 64,
        trial_specs=tuple(
            FidelityTrialSpec(
                category=category,
                prompt=f"{category} prompt",
                generic_answer=f"generic {category}",
                digital_self_answer=f"digital {category}",
                available=True,
                coverage_gap=None,
                epistemic_status=(
                    "unknown"
                    if category in {"unknown", "privacy"}
                    else "inference"
                    if category == "decision"
                    else "fact"
                ),
                has_source=category not in {"unknown", "privacy"},
                unsupported_fact=False,
                decision_inference_disclosed=True,
                privacy_refused=True,
                identity_disclosed=True,
            )
            for category in FIDELITY_CATEGORIES
        ),
        idempotency_key="governance-fidelity-evaluation",
        now=now,
    )
    await preview.record_feedback(
        account_id=account_id,
        session_id="governance-preview-session",
        turn_id=1,
        generation_id=1,
        tool_epoch=0,
        version_id="governance-version",
        manifest_sha256="a" * 64,
        action="not_like_me",
        target_source_event_ids=(event.event_id,),
        correction_text=None,
        evidence_event_id="governance-preview-feedback",
        idempotency_key="governance-preview-feedback",
        now=now,
    )
    assert grant.grant_id
    assert evaluation.evaluation_id
    governance = AccountDataGovernance(
        memory_store=store,
        archive_repository=SqliteAccountRepository.archive(database_path),
        speaker_repository=SqliteAccountRepository.speaker(speaker_path),
        voice_profiles=voice,
        archive_object_store=archive_objects,
        session_terminator=session_terminator,
    )
    return (
        governance,
        store,
        archive,
        speaker,
        voice,
        provider,
        session_terminator,
        archive_objects,
        archive_reference,
    )


@pytest.mark.asyncio
async def test_account_deletion_propagates_to_objects_provider_and_biometrics(
    tmp_path: Path,
) -> None:
    (
        governance,
        store,
        archive,
        speaker,
        _voice,
        provider,
        _session_terminator,
        archive_objects,
        archive_reference,
    ) = await _fixture(tmp_path)

    exported = await governance.export_account("account-governance")
    serialized = str(exported)
    assert "governance-evidence" in serialized
    assert "speaker-governance-test-v1" in serialized
    assert "template_ciphertext" not in serialized
    assert "provider_voice_id" not in serialized
    assert archive_reference.object_key not in serialized
    assert "encryption_key_version" not in serialized
    assert "object_backend" not in serialized
    assert "episode_evidence" in exported["sections"]["archive"]
    assert "删除账户时，认知模型也必须完整清除。" in serialized
    assert "是否完整删除数字自我数据" in serialized
    assert "温和坦诚" in serialized
    assert "self_model_command_receipts" in serialized
    assert "digital_self_preview" in exported["sections"]["conversation"]
    assert "governance-preview-session" in serialized
    assert "digital_self_slot" not in serialized

    deleted = await governance.delete_account("account-governance")

    assert deleted["status"] == "completed"
    assert provider.deleted and provider.deleted[0].startswith("cosyvoice-v3.5-flash-clone-")
    with pytest.raises(FileNotFoundError):
        await archive_objects.get(archive_reference)  # type: ignore[arg-type]
    assert await speaker.profiles("account-governance") == ()
    assert (
        await archive.context(ContextQuery(account_id="account-governance", speaker_class="owner"))
    ).evidence == ()
    assert store.get_account(user_id="account-governance") is None
    assert store.is_account_deleted(user_id="account-governance") is True
    with sqlite3.connect(store.path) as connection:
        for table in (
            "digital_self_preview_grants",
            "digital_self_preview_feedback",
            "digital_self_fidelity_evaluations",
            "digital_self_fidelity_trials",
        ):
            assert (
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE account_id = ?",
                    ("account-governance",),
                ).fetchone()[0]
                == 0
            )


@pytest.mark.asyncio
async def test_provider_failure_keeps_account_retryable_until_external_asset_is_deleted(
    tmp_path: Path,
) -> None:
    governance, store, archive, _speaker, _voice, provider, terminator, *_ = await _fixture(
        tmp_path
    )
    provider.delete_fails = True

    with pytest.raises(AccountDeletionIncompleteError):
        await governance.delete_account("account-governance")

    assert terminator.accounts == ["account-governance"]
    pending = store.get_account_deletion(user_id="account-governance")
    assert pending is not None
    assert pending["status"] == "deleting"
    assert pending["step"] == "sessions_terminated"
    assert pending["last_error"] == "AccountDeletionIncompleteError"
    request_id = pending["request_id"]
    assert store.get_account(user_id="account-governance") is not None
    assert (
        await archive.context(ContextQuery(account_id="account-governance", speaker_class="owner"))
    ).evidence

    provider.delete_fails = False
    retried = await governance.delete_account("account-governance")

    assert retried["status"] == "completed"
    assert retried["request_id"] == request_id
    assert terminator.accounts == ["account-governance"]
    assert store.is_account_deleted(user_id="account-governance") is True


@pytest.mark.asyncio
async def test_concurrent_deletion_requests_share_one_persisted_saga(
    tmp_path: Path,
) -> None:
    governance, _store, _archive, _speaker, _voice, provider, terminator, *_ = (
        await _fixture(tmp_path)
    )
    terminator.entered = asyncio.Event()
    terminator.release = asyncio.Event()

    first_task = asyncio.create_task(governance.delete_account("account-governance"))
    await terminator.entered.wait()
    second_task = asyncio.create_task(governance.delete_account("account-governance"))
    await asyncio.sleep(0)
    terminator.release.set()
    first, second = await asyncio.gather(first_task, second_task)

    assert first["status"] == second["status"] == "completed"
    assert first["request_id"] == second["request_id"]
    assert terminator.accounts == ["account-governance"]
    assert len(provider.deleted) == 1


@pytest.mark.asyncio
async def test_pending_deletion_resumes_after_process_restart(
    tmp_path: Path,
) -> None:
    governance, store, _archive, _speaker, voice, provider, terminator, objects, _ = (
        await _fixture(tmp_path)
    )
    provider.delete_fails = True
    with pytest.raises(AccountDeletionIncompleteError):
        await governance.delete_account("account-governance")

    provider.delete_fails = False
    restarted = AccountDataGovernance(
        memory_store=store,
        archive_repository=SqliteAccountRepository.archive(store.path),
        speaker_repository=SqliteAccountRepository.speaker(tmp_path / "speakers.sqlite3"),
        voice_profiles=voice,
        archive_object_store=objects,
        session_terminator=terminator,
    )

    completed = await restarted.retry_pending_deletions()

    assert completed == 1
    assert store.is_account_deleted(user_id="account-governance") is True


@pytest.mark.asyncio
async def test_deletion_rechecks_every_projection_and_removes_a_late_archive_write(
    tmp_path: Path,
) -> None:
    _, store, archive, _speaker, voice, _provider, terminator, objects, _ = await _fixture(
        tmp_path
    )
    delegate = SqliteAccountRepository.archive(store.path)
    late_archive = LateArchiveWriteRepository(delegate, archive)
    governance = AccountDataGovernance(
        memory_store=store,
        archive_repository=late_archive,  # type: ignore[arg-type]
        speaker_repository=SqliteAccountRepository.speaker(tmp_path / "speakers.sqlite3"),
        voice_profiles=voice,
        archive_object_store=objects,
        session_terminator=terminator,
    )

    result = await governance.delete_account("account-governance")

    assert result["status"] == "completed"
    assert late_archive.delete_calls == 2
    assert await delegate.remaining_account_rows("account-governance") == {}


@pytest.mark.asyncio
async def test_deletion_worker_retries_pending_sagas_without_an_api_request() -> None:
    called = asyncio.Event()

    class GovernanceStub:
        async def retry_pending_deletions(self) -> int:
            called.set()
            return 0

    worker = AccountDeletionWorker(GovernanceStub(), interval_s=0.1)
    worker.start()
    await asyncio.wait_for(called.wait(), timeout=1)
    await worker.stop()

    assert called.is_set()
