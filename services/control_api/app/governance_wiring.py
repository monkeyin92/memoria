"""Control API wiring for account export/deletion and bound-subject deletion.

Moved out of ``main.py`` (TODOLIST P2-08: split ``_wire_services`` by domain).
"""

from __future__ import annotations

from typing import cast

from fastapi import FastAPI

from services.archive.object_store import (
    ObjectStore,
)
from services.control_api.app.account_gate import AccountOperationGate
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.session_termination import (
    AccountSessionTerminator,
    RealtimeConnectionRegistry,
)
from services.governance.account_data import (
    AccountDataGovernance,
    AccountRepository,
    PostgresAccountRepository,
    SqliteAccountRepository,
)
from services.governance.subject_archive import PostgresSubjectArchive, SqliteSubjectArchive
from services.governance.subject_deletion import SubjectDeletionLedger, SubjectDeletionService
from services.governance.subject_ports import SubjectGuardianPort
from services.guardian.corpus import (
    CorpusRetentionService,
)
from services.guardian.domain import GuardianStorePort
from services.identity.service import IdentityService
from services.legacy.domain import LegacyRegistryPort
from services.voice_profile.domain import (
    VoiceProfilePort,
)


def subject_archive(settings: ControlSettings) -> PostgresSubjectArchive | SqliteSubjectArchive:
    # One repository serves account deletion and a bound subject's deletion.
    archive_url = settings.archive_database_url.get_secret_value()
    if archive_url:
        return PostgresSubjectArchive(archive_url)
    return SqliteSubjectArchive(settings.memoria_db_path)


def install_subject_deletion(
    app: FastAPI,
    settings: ControlSettings,
    *,
    guardian_store: GuardianStorePort,
    archive_object_store: ObjectStore,
    corpus_retention_service: CorpusRetentionService,
    session_terminator: AccountSessionTerminator,
) -> SubjectDeletionService:
    """Erase one bound subject inside their owner's account (not account-wide)."""

    ledger = SubjectDeletionLedger.beside(app.state.memory_store, settings.memoria_db_path)
    ledger.initialize()
    identity = cast(IdentityService, app.state.identity_service)

    async def redact(subject_id: str, account_id: str) -> None:
        await identity.redact_bound_subject(person_id=subject_id, actor_person_id=account_id)

    async def purge_corpus(subject_id: str) -> int:
        return await corpus_retention_service.purge_minor(minor_user_id=subject_id)
    service = SubjectDeletionService(
        ledger=ledger,
        archive=subject_archive(settings),
        object_store=archive_object_store,
        guardian=cast(SubjectGuardianPort, guardian_store),
        memory_scope=getattr(app.state, "subject_memory_scope", None),
        terminate_sessions=session_terminator.terminate_subject,
        purge_corpus=purge_corpus,
        redact_identity=redact,
        persona=app.state.persona_engine,
    )
    app.state.subject_deletion = service
    return service


def account_data_governance(
    settings: ControlSettings,
    *,
    store: MemoryStore,
    voice_profiles: VoiceProfilePort,
    archive_object_store: ObjectStore,
    realtime_connections: RealtimeConnectionRegistry,
    account_operations: AccountOperationGate,
    legacy_registry: LegacyRegistryPort,
    evolution_repository: AccountRepository,
    guardian_repository: GuardianStorePort,
    corpus_retention_service: CorpusRetentionService,
    session_terminator: AccountSessionTerminator,
) -> AccountDataGovernance:
    archive_url = settings.archive_database_url.get_secret_value()
    archive_repository = subject_archive(settings)
    speaker_url = settings.speaker_database_url.get_secret_value() or archive_url
    speaker_repository = (
        PostgresAccountRepository.speaker(speaker_url)
        if speaker_url
        else SqliteAccountRepository.speaker(settings.speaker_database_path)
    )
    return AccountDataGovernance(
        memory_store=store,
        archive_repository=archive_repository,
        speaker_repository=speaker_repository,
        evolution_repository=evolution_repository,
        guardian_repository=guardian_repository,
        corpus_retention_service=corpus_retention_service,
        voice_profiles=voice_profiles,
        legacy_registry=legacy_registry,
        archive_object_store=archive_object_store,
        session_terminator=session_terminator,
        operation_blocker=account_operations,
        account_read_guard=account_operations.sync_read,
    )
