"""Account export and deletion across every store (archive family on PostgreSQL).

The archive-family stores (archive, skills, self model, digital self, Legacy,
voice profiles) run on ``postgres_database``, a production-shaped clone, as the
archive role ``memoria_app`` on one shared pool, as production wires them. The
Control store, speaker and evolution stores stay on their SQLite files.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import pytest
from cryptography.fernet import Fernet
from services.archive.domain import ContextQuery, EvidenceEvent
from services.archive.object_store import EncryptedLocalObjectStore
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_skill_catalog import PostgresSkillCatalog
from services.archive.skill_domain import (
    SkillApproval,
    SkillProposal,
    SkillRunRequest,
    SkillStepDefinition,
    skill_input_sha256,
)
from services.archive.skill_executor import SkillExecutor
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import hash_password
from services.digital_self.compiler import build_manifest
from services.digital_self.domain import (
    DigitalSelfVersion,
    MemoryClaimManifestEntry,
    RelationshipProfileManifestEntry,
)
from services.digital_self.postgres_registry import PostgresDigitalSelfRegistry
from services.digital_self.preview import (
    FIDELITY_CATEGORIES,
    FidelityTrialSpec,
    SelfPreviewRegistry,
)
from services.evolution.account_fence import AccountWriteBlockedError
from services.evolution.account_repository import SqliteEvolutionAccountRepository
from services.evolution.curation import EvolutionControlPlane
from services.evolution.domain import FenceSnapshot, LayerVerdict, LearningSignal, SpeakerSnapshot
from services.evolution.store import EvolutionStore
from services.governance.account_data import (
    AccountDataGovernance,
    AccountDeletionIncompleteError,
    AccountDeletionWorker,
    PostgresAccountRepository,
    SqliteAccountRepository,
)
from services.legacy.domain import (
    LegacyAccountExport,
    LegacyFence,
    LegacyGrant,
    LegacyManifestItemRef,
    RegisteredGranteeSnapshot,
)
from services.legacy.postgres_registry import PostgresLegacyRegistry
from services.self_model.domain import RelationshipProfile
from services.self_model.postgres_registry import PostgresSelfModelRegistry
from services.speaker.authority import SpeakerAuthority
from services.speaker.domain import EmbeddingResult, EnrollmentRequest, EnrollmentSample
from services.voice_profile.domain import ProviderVoice, VoiceEnrollmentRequest
from services.voice_profile.postgres_manager import PostgresVoiceProfileManager
from services.voice_profile.testing_audio import voice_sample_wav
from testing.postgres_harness import TestDatabase

_LEGACY_NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)
# person_entities / relationships key on UUIDs in PostgreSQL.
_PERSON_ID = "44444444-4444-4444-8444-444444444444"
_RELATIONSHIP_ID = "55555555-5555-4555-8555-555555555555"
_SKILL_TABLES = (
    "skill_definitions",
    "skill_versions",
    "skill_version_evidence",
    "skill_runs",
    "skill_run_steps",
)


@dataclass(frozen=True, slots=True)
class ArchiveDatabase:
    """The archive DSN (``memoria_app``), its shared pool, and the clone owner."""

    dsn: str
    owner_dsn: str
    pool: asyncpg.Pool

    def repository(self) -> PostgresAccountRepository:
        return PostgresAccountRepository.archive(self.dsn)

    async def count(self, table: str, account_id: str) -> int:
        """Rows for ``account_id`` in ``table``, read as the owner (RLS does not apply)."""

        connection = await asyncpg.connect(self.owner_dsn)
        try:
            return int(
                await connection.fetchval(
                    f"SELECT count(*) FROM {table} WHERE account_id = $1", account_id
                )
            )
        finally:
            await connection.close()


@pytest.fixture
async def archive_db(postgres_database: TestDatabase) -> AsyncIterator[ArchiveDatabase]:
    dsn = postgres_database.role_dsn("memoria_app")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10, command_timeout=15)
    try:
        yield ArchiveDatabase(dsn=dsn, owner_dsn=postgres_database.owner_dsn(), pool=pool)
    finally:
        await pool.close()


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


class SkillToolStub:
    async def invoke(
        self,
        *,
        account_id: str,
        run_id: str,
        step_id: str,
        tool_name: str,
        arguments: Mapping[str, object],
        compensation: bool,
    ) -> object:
        del account_id, run_id, step_id, tool_name, arguments, compensation
        return {"ok": True}


async def _seed_skill(
    db: ArchiveDatabase, archive: PostgresLifeArchive, account_id: str
) -> None:
    instruction_id = "governance-skill-instruction"
    await archive.record(
        EvidenceEvent(
            event_id=instruction_id,
            account_id=account_id,
            event_type="speech.utterance_finalized",
            occurred_at=datetime.now(UTC),
            speaker_class="owner",
            source="governance-skill-test",
            payload={"text": "以后我说晚安，就执行睡前流程。"},
        )
    )
    catalog = PostgresSkillCatalog(db.dsn, pool=db.pool)
    candidate = await catalog.propose(
        SkillProposal(
            account_id=account_id,
            name="睡前流程",
            description="关闭床头灯。",
            trigger_phrases=("晚安",),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            output_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            output_template={"ok": "$steps.light.output.ok"},
            allowed_tools=("set_light",),
            steps=(
                SkillStepDefinition(
                    step_id="light",
                    tool_name="set_light",
                    arguments={"brightness": 0},
                ),
            ),
            source_kind="explicit_instruction",
            source_event_ids=(instruction_id,),
        )
    )
    approval_id = "governance-skill-approval"
    await archive.record(
        EvidenceEvent(
            event_id=approval_id,
            account_id=account_id,
            event_type="skill.approved",
            occurred_at=datetime.now(UTC),
            speaker_class="owner",
            source="governance-skill-test",
            payload={"skill_id": candidate.skill_id, "version": candidate.version},
        )
    )
    await catalog.approve(
        SkillApproval(
            account_id=account_id,
            skill_id=candidate.skill_id,
            version=candidate.version,
            approval_event_id=approval_id,
        )
    )
    inputs: dict[str, object] = {}
    confirmation_id = "governance-skill-confirmation"
    await archive.record(
        EvidenceEvent(
            event_id=confirmation_id,
            account_id=account_id,
            event_type="skill.run_confirmed",
            occurred_at=datetime.now(UTC),
            speaker_class="owner",
            source="governance-skill-test",
            payload={
                "skill_id": candidate.skill_id,
                "version": candidate.version,
                "input_sha256": skill_input_sha256(inputs),
            },
        )
    )
    await SkillExecutor(catalog=catalog, tools=SkillToolStub()).execute(
        SkillRunRequest(
            account_id=account_id,
            skill_id=candidate.skill_id,
            version=candidate.version,
            confirmation_event_id=confirmation_id,
            inputs=inputs,
        )
    )


class LateArchiveWriteRepository:
    def __init__(
        self, delegate: PostgresAccountRepository, archive: PostgresLifeArchive
    ) -> None:
        self._delegate = delegate
        self._archive = archive
        self.delete_calls = 0

    async def export_account(self, account_id: str):  # type: ignore[no-untyped-def]
        return await self._delegate.export_account(account_id)

    async def object_references(self, account_id: str):  # type: ignore[no-untyped-def]
        return await self._delegate.object_references(account_id)

    async def mark_account_deleting(self, *, account_id: str, started_at: str) -> None:
        await self._delegate.mark_account_deleting(account_id=account_id, started_at=started_at)

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


class FailingEvolutionDeleteRepository:
    """Leave evolution rows pending once, then allow the saga to finish."""

    def __init__(self, delegate: SqliteEvolutionAccountRepository) -> None:
        self._delegate = delegate
        self.delete_calls = 0

    async def export_account(self, account_id: str):  # type: ignore[no-untyped-def]
        return await self._delegate.export_account(account_id)

    async def object_references(self, account_id: str):  # type: ignore[no-untyped-def]
        return await self._delegate.object_references(account_id)

    async def delete_account(self, account_id: str) -> dict[str, int]:
        self.delete_calls += 1
        if self.delete_calls == 1:
            raise RuntimeError("evolution backend temporarily unavailable")
        return await self._delegate.delete_account(account_id)

    async def remaining_account_rows(self, account_id: str) -> dict[str, int]:
        return await self._delegate.remaining_account_rows(account_id)


def _seed_evolution_signal(path: Path, account_id: str) -> None:
    now = datetime.now(UTC)
    speaker = SpeakerSnapshot(
        classification="owner",
        reason_code="formal_owner",
        history_eligible=True,
        owner_projection_eligible=True,
    )
    store = EvolutionStore(path)
    store.append_signal(
        LearningSignal(
            signal_id="governance-evolution-signal",
            task_family="account-deletion",
            scope="owner_private",
            account_id=account_id,
            fence=FenceSnapshot("governance-session", 1, 1, 0),
            speaker=speaker,
            source_event_ids=("governance-evolution-event",),
            result=LayerVerdict("fail", reason_codes=("test_failure",)),
            process=LayerVerdict("pass"),
            quality=LayerVerdict("fail", reason_codes=("test_quality",)),
            environment_version="governance-test-v1",
            failure_code="test_failure",
            diagnosis="account deletion retry fixture",
            created_at=now,
        )
    )


class LegacyDeleteRetryProbe:
    """Fail once, then leave rows once so both retry and final verification are exercised."""

    def __init__(self, delegate: PostgresLegacyRegistry) -> None:
        self._delegate = delegate
        self.delete_calls = 0

    async def export_for_account(self, *, account_id: str) -> LegacyAccountExport:
        return await self._delegate.export_for_account(account_id=account_id)

    async def delete_for_account(self, *, account_id: str) -> None:
        self.delete_calls += 1
        if self.delete_calls == 1:
            raise RuntimeError("legacy registry temporarily unavailable")
        if self.delete_calls == 2:
            return
        await self._delegate.delete_for_account(account_id=account_id)


async def _seed_legacy(
    db: ArchiveDatabase,
    *,
    owner_account_id: str = "account-governance",
    grantee_account_id: str = "legacy-grantee",
) -> tuple[PostgresLegacyRegistry, LegacyGrant, str, DigitalSelfVersion]:
    relationship_entry = RelationshipProfileManifestEntry(
        profile_id="11111111-1111-4111-8111-111111111111",
        version_number=3,
        person_id="legacy-person",
        relationship_id="22222222-2222-4222-8222-222222222222",
        salutation="小梅",
        tone="warm",
        advice_style="listen-first",
        sharing_scope="family",
        boundaries=("不替代专业意见",),
        support_source_event_ids=("governance-evidence",),
        counterexample_source_event_ids=(),
    )
    memory_entry = MemoryClaimManifestEntry(
        claim_id="legacy-memory",
        category="life_story",
        subject_key="owner",
        predicate="lived_in",
        value="杭州",
        confidence=0.95,
        sensitive_domain="family",
        extractor_version="v1",
        source_event_id="governance-evidence",
        valid_at=_LEGACY_NOW.isoformat(),
    )
    manifest, manifest_bytes, manifest_sha256 = build_manifest(
        (memory_entry, relationship_entry),
        compiler_version="digital-self-compiler-v3",
        policy_version="digital-self-policy-v3",
        persona_version_id=None,
        parent_version_id=None,
    )
    version = DigitalSelfVersion(
        version_id="33333333-3333-4333-8333-333333333333",
        account_id=owner_account_id,
        version_number=7,
        status="frozen",
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        created_at=_LEGACY_NOW,
    )
    # No store API writes a hand-built frozen version: seed it as the owner.
    connection = await asyncpg.connect(db.owner_dsn)
    try:
        await connection.execute(
            """
            INSERT INTO digital_self_versions (
                version_id, account_id, version_number, status, manifest_json,
                manifest_sha256, source_summary_sha256, parent_version_id,
                rollback_target_version_id, created_at
            ) VALUES ($1, $2, $3, 'frozen', $4, $5, $6, NULL, NULL, $7)
            """,
            version.version_id,
            owner_account_id,
            version.version_number,
            manifest_bytes.decode("utf-8"),
            manifest_sha256,
            manifest.source_summary.source_summary_sha256,
            _LEGACY_NOW,
        )
    finally:
        await connection.close()
    relationship = RelationshipProfile(
        profile_id=relationship_entry.profile_id,
        account_id=owner_account_id,
        version_number=relationship_entry.version_number,
        person_id=relationship_entry.person_id,
        relationship_id=relationship_entry.relationship_id,
        salutation=relationship_entry.salutation,
        tone=relationship_entry.tone,
        advice_style=relationship_entry.advice_style,
        sharing_scope=relationship_entry.sharing_scope,
        boundaries=relationship_entry.boundaries,
        status="approved",
        unresolved_conflict=False,
        sources=(),
        owner_reviewed_at=_LEGACY_NOW,
        step_up_verified=True,
        created_at=_LEGACY_NOW,
    )
    registry = PostgresLegacyRegistry(db.dsn, pool=db.pool)
    grant = await registry.issue(
        owner_account_id=owner_account_id,
        grantee=RegisteredGranteeSnapshot(grantee_account_id, _LEGACY_NOW),
        version=version,
        relationship_profile=relationship,
        allowed_items=(LegacyManifestItemRef("memory_claim", memory_entry.claim_id),),
        visibility="family",
        voice_allowed=True,
        expires_at=_LEGACY_NOW + timedelta(days=30),
        idempotency_key="governance-legacy-issue",
        now=_LEGACY_NOW,
    )
    grant = await registry.activate(
        actor_account_id=owner_account_id,
        grant_id=grant.grant_id,
        expected_grant_snapshot_sha256=grant.grant_snapshot_sha256,
        idempotency_key="governance-legacy-activate",
        now=_LEGACY_NOW + timedelta(minutes=1),
    )
    access = await registry.resolve_access(
        actor_account_id=grantee_account_id,
        grant_id=grant.grant_id,
        purpose="grantee_session",
        now=_LEGACY_NOW + timedelta(minutes=2),
    )
    assert access.shell_id is not None
    await registry.append_shell_turn(
        actor_account_id=grantee_account_id,
        shell_id=access.shell_id,
        actor_role="digital_self",
        actual_heard_text="我实际听到了这段传承回答。",
        fence=LegacyFence("legacy-session", "turn-1", "generation-1", 2),
        idempotency_key="governance-legacy-turn",
        now=_LEGACY_NOW + timedelta(minutes=3),
    )
    return registry, grant, access.shell_id, version


async def _fixture(
    tmp_path: Path,
    db: ArchiveDatabase,
) -> tuple[
    AccountDataGovernance,
    MemoryStore,
    PostgresLifeArchive,
    SpeakerAuthority,
    PostgresVoiceProfileManager,
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
    archive = PostgresLifeArchive(db.dsn, pool=db.pool)
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
    # A retained source-audio blob needs the raw voice consent it cites.
    raw_voice = await archive.grant_raw_voice_consent(
        account_id=account_id,
        policy_version="raw-voice-v1",
        retention_policy="account_lifetime",
        granted_at=datetime.now(UTC),
    )
    event = EvidenceEvent(
        event_id="governance-evidence",
        account_id=account_id,
        event_type="speech.utterance_finalized",
        occurred_at=datetime.now(UTC),
        speaker_class="owner",
        source="test",
        consent_grant_id=raw_voice.consent_grant_id,
        payload={
            "text": "删除传播测试。",
            "interaction_mode": "companion",
            "simulated_output": False,
            "owner_projection_eligible": True,
        },
    )
    await archive.record_with_blob(
        event, archive_reference, retention_policy="account_lifetime"
    )
    self_model = PostgresSelfModelRegistry(db.dsn, pool=db.pool)
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
    # A confirmed person and relationship for the profile below; only the
    # memory compiler writes these, so seed them as the owner.
    connection = await asyncpg.connect(db.owner_dsn)
    try:
        await connection.execute(
            """
            INSERT INTO person_entities (
                person_id, account_id, canonical_key, display_name,
                relationship_to_owner, status, source_event_id, created_at
            ) VALUES ($1, $2, $3, '家人', 'family', 'confirmed', $4, now())
            """,
            _PERSON_ID,
            account_id,
            f"family:{_PERSON_ID}",
            event.event_id,
        )
        await connection.execute(
            """
            INSERT INTO relationships (
                relationship_id, account_id, person_id, relationship_type,
                status, source_event_id, valid_at
            ) VALUES ($1, $2, $3, 'family', 'confirmed', $4, now())
            """,
            _RELATIONSHIP_ID,
            account_id,
            _PERSON_ID,
            event.event_id,
        )
    finally:
        await connection.close()
    relationship_profile = await self_model.create_relationship_profile(
        account_id=account_id,
        person_id=_PERSON_ID,
        relationship_id=_RELATIONSHIP_ID,
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
    voice = PostgresVoiceProfileManager(
        db.dsn,
        object_store=voice_objects,
        provider=provider,
        sample_url_factory=lambda sample_id: f"https://control.test/samples/{sample_id}",
        provider_region="cn-beijing",
        target_model="cosyvoice-v3.5-flash",
        pool=db.pool,
    )
    await voice.grant_consent(account_id=account_id, policy_version="voice-clone-v1")
    await voice.enroll(
        VoiceEnrollmentRequest(
            account_id=account_id,
            audio=voice_sample_wav(),
            media_type="audio/wav",
            duration_ms=12_000,
            sample_rate=24_000,
        )
    )
    # The same placement main.py uses: preview tables live with the store.
    preview = (
        SelfPreviewRegistry.in_control_store(store)
        if store.is_postgres
        else SelfPreviewRegistry.sqlite(database_path)
    )
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
        archive_repository=db.repository(),
        speaker_repository=SqliteAccountRepository.speaker(speaker_path),
        evolution_repository=SqliteEvolutionAccountRepository(tmp_path / "evolution.sqlite3"),
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
async def test_export_fails_closed_when_deletion_has_begun(
    tmp_path: Path,
    archive_db: ArchiveDatabase,
) -> None:
    governance, *_ = await _fixture(tmp_path, archive_db)
    evolution_path = tmp_path / "evolution.sqlite3"
    EvolutionStore(evolution_path).mark_account_deleting("account-governance")

    with pytest.raises(AccountWriteBlockedError, match="account deletion is in progress"):
        await governance.export_account("account-governance")


@pytest.mark.asyncio
async def test_account_deletion_propagates_to_objects_provider_and_biometrics(
    tmp_path: Path,
    archive_db: ArchiveDatabase,
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
    ) = await _fixture(tmp_path, archive_db)
    await _seed_skill(archive_db, archive, "account-governance")

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
    for table in _SKILL_TABLES:
        assert exported["sections"]["archive"][table]
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
    for table in _SKILL_TABLES:
        assert await archive_db.count(table, "account-governance") == 0
    # The preview tables live with the control store (see main.py).
    with store.connection() as connection:
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
async def test_account_export_and_owner_deletion_cover_complete_legacy_lifecycle(
    tmp_path: Path,
    archive_db: ArchiveDatabase,
) -> None:
    (
        _governance,
        store,
        _archive,
        _speaker,
        voice,
        _provider,
        terminator,
        archive_objects,
        _archive_reference,
    ) = await _fixture(tmp_path, archive_db)
    legacy, grant, shell_id, _version = await _seed_legacy(archive_db)
    governance = AccountDataGovernance(
        memory_store=store,
        archive_repository=archive_db.repository(),
        speaker_repository=SqliteAccountRepository.speaker(tmp_path / "speakers.sqlite3"),
        evolution_repository=SqliteEvolutionAccountRepository(tmp_path / "evolution.sqlite3"),
        voice_profiles=voice,
        legacy_registry=legacy,
        archive_object_store=archive_objects,
        session_terminator=terminator,
    )

    exported = await governance.export_account("account-governance")
    json.dumps(exported, ensure_ascii=False, sort_keys=True)
    legacy_section = exported["sections"]["legacy"]

    assert [item["grant_id"] for item in legacy_section["grants"]] == [grant.grant_id]
    assert [item["shell_id"] for item in legacy_section["shells"]] == [shell_id]
    assert [item["actual_heard_text"] for item in legacy_section["shell_turns"]] == [
        "我实际听到了这段传承回答。"
    ]
    assert {event["action"] for event in legacy_section["audit_events"]} >= {
        "issue",
        "activate",
        "resolve_access",
        "append_shell_turn",
    }
    assert isinstance(legacy_section["grants"][0]["expires_at"], str)
    assert isinstance(legacy_section["shell_turns"][0]["occurred_at"], str)

    result = await governance.delete_account("account-governance")

    assert result["status"] == "completed"
    assert await legacy.export_for_account(account_id="account-governance") == LegacyAccountExport(
        (), (), (), ()
    )
    assert await legacy.export_for_account(account_id="legacy-grantee") == LegacyAccountExport(
        (), (), (), ()
    )


@pytest.mark.asyncio
async def test_grantee_deletion_removes_shared_legacy_data_without_owner_core(
    tmp_path: Path,
    archive_db: ArchiveDatabase,
) -> None:
    (
        _governance,
        store,
        _archive,
        _speaker,
        voice,
        _provider,
        terminator,
        archive_objects,
        _archive_reference,
    ) = await _fixture(tmp_path, archive_db)
    store.register_account(
        user_id="legacy-grantee",
        username="legacy-grantee",
        username_normalized="legacy-grantee",
        password_hash=hash_password("safe-passphrase"),
        now=_LEGACY_NOW.isoformat(),
    )
    legacy, _grant, _shell_id, version = await _seed_legacy(archive_db)
    digital_self = PostgresDigitalSelfRegistry(archive_db.dsn, pool=archive_db.pool)
    self_model = PostgresSelfModelRegistry(archive_db.dsn, pool=archive_db.pool)
    owner_claims = await self_model.cognitive_claims(account_id="account-governance")
    governance = AccountDataGovernance(
        memory_store=store,
        archive_repository=archive_db.repository(),
        speaker_repository=SqliteAccountRepository.speaker(tmp_path / "speakers.sqlite3"),
        evolution_repository=SqliteEvolutionAccountRepository(tmp_path / "evolution.sqlite3"),
        voice_profiles=voice,
        legacy_registry=legacy,
        archive_object_store=archive_objects,
        session_terminator=terminator,
    )

    result = await governance.delete_account("legacy-grantee")

    assert result["status"] == "completed"
    assert await legacy.export_for_account(account_id="account-governance") == LegacyAccountExport(
        (), (), (), ()
    )
    assert await legacy.export_for_account(account_id="legacy-grantee") == LegacyAccountExport(
        (), (), (), ()
    )
    assert await digital_self.get(
        account_id="account-governance", version_id=version.version_id
    ) == version
    assert await self_model.cognitive_claims(
        account_id="account-governance"
    ) == owner_claims
    assert store.get_account(user_id="account-governance") is not None


@pytest.mark.asyncio
async def test_legacy_deletion_is_retried_and_rechecked_before_verified_empty(
    tmp_path: Path,
    archive_db: ArchiveDatabase,
) -> None:
    (
        _governance,
        store,
        _archive,
        _speaker,
        voice,
        _provider,
        terminator,
        archive_objects,
        _archive_reference,
    ) = await _fixture(tmp_path, archive_db)
    store.register_account(
        user_id="legacy-grantee",
        username="legacy-grantee",
        username_normalized="legacy-grantee",
        password_hash=hash_password("safe-passphrase"),
        now=_LEGACY_NOW.isoformat(),
    )
    legacy, _grant, _shell_id, _version = await _seed_legacy(archive_db)
    probe = LegacyDeleteRetryProbe(legacy)
    governance = AccountDataGovernance(
        memory_store=store,
        archive_repository=archive_db.repository(),
        speaker_repository=SqliteAccountRepository.speaker(tmp_path / "speakers.sqlite3"),
        evolution_repository=SqliteEvolutionAccountRepository(tmp_path / "evolution.sqlite3"),
        voice_profiles=voice,
        legacy_registry=probe,  # type: ignore[arg-type]
        archive_object_store=archive_objects,
        session_terminator=terminator,
    )

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await governance.delete_account("legacy-grantee")

    pending = store.get_account_deletion(user_id="legacy-grantee")
    assert pending is not None
    assert pending["step"] == "sessions_terminated"
    assert pending["last_error"] == "RuntimeError"

    assert await governance.retry_pending_deletions() == 1
    assert probe.delete_calls == 3
    assert await legacy.export_for_account(account_id="account-governance") == LegacyAccountExport(
        (), (), (), ()
    )
    assert store.is_account_deleted(user_id="legacy-grantee") is True


@pytest.mark.asyncio
async def test_provider_failure_keeps_account_retryable_until_external_asset_is_deleted(
    tmp_path: Path,
    archive_db: ArchiveDatabase,
) -> None:
    governance, store, archive, _speaker, _voice, provider, terminator, *_ = await _fixture(tmp_path, archive_db)
    evolution_path = tmp_path / "evolution.sqlite3"
    _seed_evolution_signal(evolution_path, "account-governance")
    provider.delete_fails = True

    with pytest.raises(AccountDeletionIncompleteError):
        await governance.delete_account("account-governance")

    assert terminator.accounts == ["account-governance"]
    pending = store.get_account_deletion(user_id="account-governance")
    assert pending is not None
    assert pending["status"] == "deleting"
    assert pending["step"] == "legacy_rows_deleted"
    assert pending["last_error"] == "AccountDeletionIncompleteError"
    request_id = pending["request_id"]
    assert store.get_account(user_id="account-governance") is not None
    assert (
        await archive.context(ContextQuery(account_id="account-governance", speaker_class="owner"))
    ).evidence

    # The evolution tombstone is durable before the saga reaches this failed
    # provider step. A fresh control-plane instance must reject private writes
    # while a pending deletion remains, regardless of its in-memory gate.
    restarted_evolution = EvolutionStore(evolution_path)
    restarted_plane = EvolutionControlPlane(
        restarted_evolution,
        trusted_root_sha256="f" * 64,
    )
    existing_signal = restarted_evolution.get_signal("governance-evolution-signal")
    with pytest.raises(AccountWriteBlockedError, match="account deletion"):
        restarted_plane.append_signal(
            replace(
                existing_signal,
                signal_id="late-private",
                account_id="account-governance",
            )
        )
    global_signal = replace(
        existing_signal,
        signal_id="late-global",
        scope="global_redacted",
        account_id=None,
    )
    assert restarted_plane.append_signal(global_signal).scope == "global_redacted"

    provider.delete_fails = False
    retried = await governance.delete_account("account-governance")

    assert retried["status"] == "completed"
    assert retried["request_id"] == request_id
    assert terminator.accounts == ["account-governance"]
    assert store.is_account_deleted(user_id="account-governance") is True


@pytest.mark.asyncio
async def test_concurrent_deletion_requests_share_one_persisted_saga(
    tmp_path: Path,
    archive_db: ArchiveDatabase,
) -> None:
    governance, _store, _archive, _speaker, _voice, provider, terminator, *_ = (
        await _fixture(tmp_path, archive_db)
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
    archive_db: ArchiveDatabase,
) -> None:
    governance, store, _archive, _speaker, voice, provider, terminator, objects, _ = (
        await _fixture(tmp_path, archive_db)
    )
    provider.delete_fails = True
    with pytest.raises(AccountDeletionIncompleteError):
        await governance.delete_account("account-governance")

    provider.delete_fails = False
    restarted = AccountDataGovernance(
        memory_store=store,
        archive_repository=archive_db.repository(),
        speaker_repository=SqliteAccountRepository.speaker(tmp_path / "speakers.sqlite3"),
        evolution_repository=SqliteEvolutionAccountRepository(tmp_path / "evolution.sqlite3"),
        voice_profiles=voice,
        archive_object_store=objects,
        session_terminator=terminator,
    )

    completed = await restarted.retry_pending_deletions()

    assert completed == 1
    assert store.is_account_deleted(user_id="account-governance") is True


@pytest.mark.asyncio
async def test_evolution_backend_failure_is_checkpointed_and_retried(
    tmp_path: Path,
    archive_db: ArchiveDatabase,
) -> None:
    (
        _governance,
        store,
        _archive,
        _speaker,
        voice,
        _provider,
        terminator,
        objects,
        _archive_reference,
    ) = await _fixture(tmp_path, archive_db)
    evolution_path = tmp_path / "evolution.sqlite3"
    _seed_evolution_signal(evolution_path, "account-governance")
    evolution = FailingEvolutionDeleteRepository(SqliteEvolutionAccountRepository(evolution_path))
    governance = AccountDataGovernance(
        memory_store=store,
        archive_repository=archive_db.repository(),
        speaker_repository=SqliteAccountRepository.speaker(tmp_path / "speakers.sqlite3"),
        evolution_repository=evolution,  # type: ignore[arg-type]
        voice_profiles=voice,
        archive_object_store=objects,
        session_terminator=terminator,
    )

    with pytest.raises(RuntimeError, match="evolution backend temporarily unavailable"):
        await governance.delete_account("account-governance")

    pending = store.get_account_deletion(user_id="account-governance")
    assert pending is not None
    assert pending["step"] == "speaker_rows_deleted"
    assert pending["last_error"] == "RuntimeError"
    assert await evolution._delegate.remaining_account_rows("account-governance")  # noqa: SLF001

    assert await governance.retry_pending_deletions() == 1
    assert evolution.delete_calls == 2
    assert await evolution._delegate.remaining_account_rows("account-governance") == {}  # noqa: SLF001
    assert store.is_account_deleted(user_id="account-governance") is True


@pytest.mark.asyncio
async def test_deletion_rechecks_every_projection_and_removes_a_late_archive_write(
    tmp_path: Path,
    archive_db: ArchiveDatabase,
) -> None:
    _, store, archive, _speaker, voice, _provider, terminator, objects, _ = await _fixture(tmp_path, archive_db)
    delegate = archive_db.repository()
    late_archive = LateArchiveWriteRepository(delegate, archive)
    governance = AccountDataGovernance(
        memory_store=store,
        archive_repository=late_archive,  # type: ignore[arg-type]
        speaker_repository=SqliteAccountRepository.speaker(tmp_path / "speakers.sqlite3"),
        evolution_repository=SqliteEvolutionAccountRepository(tmp_path / "evolution.sqlite3"),
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


@pytest.mark.asyncio
async def test_export_makes_postgres_guardian_rows_portable(
    tmp_path: Path,
    archive_db: ArchiveDatabase,
) -> None:
    """PostgreSQL guardian rows carry UUID and datetime values; the export is JSON."""
    import uuid as uuid_module

    governance, *_ = await _fixture(tmp_path, archive_db)
    consent_id = uuid_module.uuid4()
    granted_at = datetime(2026, 9, 28, 8, 0, tzinfo=UTC)

    class _PostgresShapedGuardian:
        async def export_for_account(self, *, account_id: str) -> dict[str, object]:
            return {
                "person_consents": [
                    {"consent_id": consent_id, "subject_user_id": account_id, "granted_at": granted_at}
                ]
            }

    governance._guardian_repository = _PostgresShapedGuardian()  # type: ignore[assignment]
    exported = await governance.export_account("account-governance")

    assert exported["manifest_sha256"]
    assert exported["sections"]["guardian"]["person_consents"] == [
        {
            "consent_id": str(consent_id),
            "subject_user_id": "account-governance",
            "granted_at": granted_at.isoformat(),
        }
    ]
