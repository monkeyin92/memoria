"""Control API FastAPI application."""

from __future__ import annotations

import base64
import hashlib
from asyncio import Lock, to_thread
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.archive.compiler_worker import MemoryCompilerWorker
from services.archive.domain import LifeArchivePort
from services.archive.life_archive import LifeArchive
from services.archive.memory_catalog import MemoryCatalog
from services.archive.memory_domain import (
    AccountWriteGuard,
    AccountWriteRejectedError,
    MemoryCatalogPort,
    MemoryEmbedder,
    MemoryExtractor,
)
from services.archive.memory_extractor import RuleBasedMemoryExtractor
from services.archive.object_store import (
    EncryptedLocalObjectStore,
    EncryptedS3ObjectStore,
    ObjectStore,
)
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog, QwenMemoryEmbedder
from services.archive.postgres_skill_catalog import PostgresSkillCatalog
from services.archive.qwen_memory_extractor import FallbackMemoryExtractor, QwenMemoryExtractor
from services.archive.skill_catalog import SkillCatalog
from services.archive.skill_domain import SkillCatalogPort
from services.control_api.app.account_gate import AccountDeletingError, AccountOperationGate
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.device_registry import DeviceRegistry
from services.control_api.app.media_runtime import mint_streamcore_token
from services.control_api.app.media_slo import MediaSLOGate
from services.control_api.app.routes import archive as archive_routes
from services.control_api.app.routes import auth as auth_routes
from services.control_api.app.routes import digital_self as digital_self_routes
from services.control_api.app.routes import growth as growth_routes
from services.control_api.app.routes import interaction as interaction_routes
from services.control_api.app.routes import legacy as legacy_routes
from services.control_api.app.routes import media as media_routes
from services.control_api.app.routes import memory as memory_routes
from services.control_api.app.routes import persona as persona_routes
from services.control_api.app.routes import readiness as readiness_routes
from services.control_api.app.routes import self_model as self_model_routes
from services.control_api.app.routes import self_preview as self_preview_routes
from services.control_api.app.routes import session as session_routes
from services.control_api.app.routes import skills as skill_routes
from services.control_api.app.routes import speaker as speaker_routes
from services.control_api.app.routes import voice as voice_routes
from services.control_api.app.session_directory import (
    InMemorySessionDirectory,
    RedisSessionDirectory,
    SessionDirectory,
    SessionRoute,
)
from services.control_api.app.session_termination import (
    AccountSessionTerminator,
    LiveKitRoomCloser,
    RealtimeConnectionRegistry,
)
from services.digital_self.domain import RegistryPort
from services.digital_self.postgres_registry import PostgresDigitalSelfRegistry
from services.digital_self.preview import SelfPreviewRegistry
from services.digital_self.registry import DigitalSelfRegistry
from services.governance.account_data import (
    AccountDataGovernance,
    AccountDeletionWorker,
    PostgresAccountRepository,
    SqliteAccountRepository,
)
from services.growth.postgres_reader import PostgresGrowthReader
from services.growth.reader import GrowthReader
from services.legacy.domain import LegacyRegistryPort
from services.legacy.postgres_registry import PostgresLegacyRegistry
from services.legacy.registry import LegacyRegistry
from services.persona.domain import PersonaEnginePort
from services.persona.engine import PersonaEngine
from services.persona.postgres_engine import PostgresPersonaEngine
from services.persona.qwen_extractor import FallbackPersonaExtractor, QwenPersonaExtractor
from services.persona.rules import PersonaExtractor, RuleBasedPersonaExtractor
from services.self_model.domain import SelfModelRegistryPort
from services.self_model.postgres_registry import PostgresSelfModelRegistry
from services.self_model.registry import SelfModelRegistry
from services.speaker.authority import SpeakerAuthority
from services.speaker.campplus_http import (
    CampPlusHTTPEmbeddingAdapter,
    UnavailableSpeakerEmbeddingAdapter,
)
from services.speaker.domain import SpeakerAuthorityPort, SpeakerEmbeddingAdapter
from services.speaker.postgres_authority import PostgresSpeakerAuthority
from services.voice_profile.cosyvoice_enrollment import (
    CosyVoiceEnrollmentClient,
    CosyVoiceEnrollmentConfig,
    UnavailableVoiceEnrollmentProvider,
)
from services.voice_profile.cosyvoice_preview import (
    CosyVoicePreviewRenderer,
    UnavailableVoicePreviewRenderer,
)
from services.voice_profile.domain import (
    VoiceEnrollmentProvider,
    VoicePreviewRenderer,
    VoiceProfilePort,
)
from services.voice_profile.doubao_voice_clone import (
    DoubaoVoiceCloneClient,
    DoubaoVoiceCloneConfig,
)
from services.voice_profile.manager import VoiceProfileManager
from services.voice_profile.postgres_manager import PostgresVoiceProfileManager
from services.voice_profile.sample_url import VoiceSampleURLSigner


def _memory_account_guard(
    gate: AccountOperationGate,
    store: MemoryStore,
) -> AccountWriteGuard:
    @asynccontextmanager
    async def guard(account_id: str) -> AsyncIterator[None]:
        try:
            async with gate.write(account_id):
                if store.is_account_unavailable(user_id=account_id):
                    raise AccountWriteRejectedError("account deletion is in progress")
                yield
        except AccountDeletingError as exc:
            raise AccountWriteRejectedError(str(exc)) from exc

    return guard


def _memory_embedder(settings: ControlSettings) -> MemoryEmbedder | None:
    api_key = settings.memory_embedding_api_key.get_secret_value()
    if not settings.memory_embedding_url or not api_key or not settings.memory_embedding_model:
        return None
    return QwenMemoryEmbedder(
        endpoint=settings.memory_embedding_url,
        api_key=api_key,
        model=settings.memory_embedding_model,
        dimensions=settings.memory_embedding_dimensions,
        timeout_s=settings.memory_embedding_timeout_s,
    )


def _speaker_authority(settings: ControlSettings) -> SpeakerAuthorityPort:
    configured_key = settings.speaker_template_key.get_secret_value()
    template_key = configured_key or base64.urlsafe_b64encode(
        hashlib.sha256(b"memoria-development-speaker-template-key").digest()
    ).decode("ascii")
    embedding_token = settings.speaker_embedding_token.get_secret_value()
    if settings.speaker_embedding_url and embedding_token:
        adapter: SpeakerEmbeddingAdapter = CampPlusHTTPEmbeddingAdapter(
            endpoint=settings.speaker_embedding_url,
            token=embedding_token,
            model_version=settings.speaker_embedding_model,
            timeout_s=settings.speaker_embedding_timeout_s,
        )
    else:
        adapter = UnavailableSpeakerEmbeddingAdapter(settings.speaker_embedding_model)
    speaker_database_url = (
        settings.speaker_database_url.get_secret_value()
        or settings.archive_database_url.get_secret_value()
    )
    if speaker_database_url:
        return PostgresSpeakerAuthority(
            speaker_database_url,
            template_key=template_key,
            adapter=adapter,
            owner_threshold=settings.speaker_owner_threshold,
            guest_threshold=settings.speaker_guest_threshold,
            classify_timeout_s=settings.speaker_embedding_timeout_s,
        )
    return SpeakerAuthority.sqlite(
        settings.speaker_database_path,
        template_key=template_key,
        adapter=adapter,
        owner_threshold=settings.speaker_owner_threshold,
        guest_threshold=settings.speaker_guest_threshold,
        classify_timeout_s=settings.speaker_embedding_timeout_s,
    )


def _memory_extractor(settings: ControlSettings) -> MemoryExtractor:
    fallback = RuleBasedMemoryExtractor()
    api_key = settings.dashscope_api_key.get_secret_value()
    if settings.offline_mock or not api_key:
        return fallback
    return FallbackMemoryExtractor(
        QwenMemoryExtractor(
            api_key=api_key,
            base_url=settings.dashscope_base_url,
            model=settings.memory_extraction_model,
            timeout_s=settings.memory_extraction_timeout_s,
            workspace_id=settings.dashscope_workspace_id,
        ),
        fallback,
    )


def _persona_extractor(settings: ControlSettings) -> PersonaExtractor:
    fallback = RuleBasedPersonaExtractor()
    api_key = settings.dashscope_api_key.get_secret_value()
    if settings.offline_mock or not api_key:
        return fallback
    return FallbackPersonaExtractor(
        QwenPersonaExtractor(
            api_key=api_key,
            base_url=settings.dashscope_base_url,
            model=settings.memory_extraction_model,
            timeout_s=settings.memory_extraction_timeout_s,
            workspace_id=settings.dashscope_workspace_id,
        ),
        fallback,
    )


def _voice_profile_services(
    settings: ControlSettings,
) -> tuple[VoiceProfilePort, VoiceSampleURLSigner, ObjectStore]:
    configured_key = settings.voice_sample_encryption_key.get_secret_value()
    object_key = configured_key or base64.urlsafe_b64encode(
        hashlib.sha256(b"memoria-development-voice-sample-key").digest()
    ).decode("ascii")
    object_store: ObjectStore
    if settings.voice_object_bucket:
        object_store = EncryptedS3ObjectStore.from_boto3(
            bucket=settings.voice_object_bucket,
            key=object_key,
            key_version=settings.voice_sample_key_version,
            read_keys=settings.voice_sample_read_key_map(),
            endpoint_url=settings.voice_object_endpoint or None,
            region_name=settings.voice_object_region or None,
            access_key_id=(settings.voice_object_access_key.get_secret_value().strip() or None),
            secret_access_key=(settings.voice_object_secret_key.get_secret_value().strip() or None),
            prefix=settings.voice_object_prefix,
        )
    else:
        object_store = EncryptedLocalObjectStore(
            root=Path(settings.voice_sample_store_path),
            key=object_key,
            key_version=settings.voice_sample_key_version,
            read_keys=settings.voice_sample_read_key_map(),
        )
    configured_signer = settings.voice_sample_url_secret.get_secret_value()
    signer_secret = (
        configured_signer or hashlib.sha256(b"memoria-development-voice-sample-url").hexdigest()
    )
    signer = VoiceSampleURLSigner(
        secret=signer_secret,
        public_base_url=settings.public_base_url,
        ttl_s=settings.voice_sample_url_ttl_s,
    )
    provider: VoiceEnrollmentProvider
    if settings.offline_mock:
        provider = UnavailableVoiceEnrollmentProvider()
    elif settings.voice_clone_provider == "volcengine_doubao":
        api_key = settings.doubao_voice_api_key.get_secret_value()
        if not api_key:
            provider = UnavailableVoiceEnrollmentProvider()
        else:
            provider = DoubaoVoiceCloneClient(
                DoubaoVoiceCloneConfig(
                    endpoint=settings.doubao_voice_clone_url,
                    query_endpoint=settings.doubao_voice_query_url,
                    api_key=api_key,
                    timeout_s=settings.voice_enrollment_timeout_s,
                    poll_interval_s=settings.doubao_voice_clone_poll_interval_s,
                    synth_ready_id_mode=settings.doubao_voice_synth_ready_id_mode,
                    synth_ready_id_field=settings.doubao_voice_synth_ready_id_field,
                    expires_at_field=settings.doubao_voice_expires_at_field,
                    expires_at_format=settings.doubao_voice_expires_at_format,
                )
            )
    else:
        api_key = settings.dashscope_api_key.get_secret_value()
        if not api_key:
            provider = UnavailableVoiceEnrollmentProvider()
        else:
            provider = CosyVoiceEnrollmentClient(
                CosyVoiceEnrollmentConfig(
                    endpoint=settings.voice_enrollment_url,
                    api_key=api_key,
                    timeout_s=settings.voice_enrollment_timeout_s,
                )
            )
    archive_url = settings.archive_database_url.get_secret_value()
    manager: VoiceProfilePort
    if archive_url:
        manager = PostgresVoiceProfileManager(
            archive_url,
            object_store=object_store,
            provider=provider,
            sample_url_factory=signer.url,
            provider_region=settings.voice_provider_region,
            target_model=settings.voice_target_model,
            provider_name=settings.voice_clone_provider,
        )
    else:
        manager = VoiceProfileManager.sqlite(
            settings.memoria_db_path,
            object_store=object_store,
            provider=provider,
            sample_url_factory=signer.url,
            provider_region=settings.voice_provider_region,
            target_model=settings.voice_target_model,
            provider_name=settings.voice_clone_provider,
        )
    return manager, signer, object_store


def _archive_object_store(settings: ControlSettings) -> ObjectStore:
    configured_key = settings.archive_object_encryption_key.get_secret_value()
    object_key = configured_key or base64.urlsafe_b64encode(
        hashlib.sha256(b"memoria-development-archive-object-key").digest()
    ).decode("ascii")
    if settings.archive_object_bucket:
        return EncryptedS3ObjectStore.from_boto3(
            bucket=settings.archive_object_bucket,
            key=object_key,
            key_version=settings.archive_object_key_version,
            read_keys=settings.archive_object_read_key_map(),
            endpoint_url=settings.archive_object_endpoint or None,
            region_name=settings.archive_object_region or None,
            access_key_id=(settings.archive_object_access_key.get_secret_value().strip() or None),
            secret_access_key=(
                settings.archive_object_secret_key.get_secret_value().strip() or None
            ),
            prefix=settings.archive_object_prefix,
        )
    return EncryptedLocalObjectStore(
        root=Path(settings.archive_object_store_path),
        key=object_key,
        key_version=settings.archive_object_key_version,
        read_keys=settings.archive_object_read_key_map(),
    )


def _build_media_stop_dispatcher(settings: ControlSettings) -> object | None:
    """Build the production StreamCore stop path when Media Edge is present."""

    base_url = settings.media_edge_control_url.strip().rstrip("/")
    if not base_url:
        return None

    async def dispatch(
        *, session_id: str, route: SessionRoute, event: dict[str, object]
    ) -> None:
        stream_epoch = route.stream_epoch
        account_id = route.account_id
        device_id = route.device_id
        client_type = "h5" if device_id == "h5" else "device"
        token, _ = mint_streamcore_token(
            settings,
            session_id=session_id,
            user_id=account_id,
            client_platform=client_type,
            device_id=device_id,
            stream_epoch=stream_epoch,
        )
        url = f"{base_url}/v1/media/sessions/{quote(session_id, safe='')}/stop"
        idempotency_key = f"{session_id}:{route.generation}"
        async with httpx.AsyncClient(timeout=settings.media_edge_control_timeout_s) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": idempotency_key,
                },
                json=event,
            )
            response.raise_for_status()

    return dispatch


def _voice_preview_renderer(settings: ControlSettings) -> VoicePreviewRenderer:
    if (
        settings.offline_mock
        or not settings.dashscope_api_key.get_secret_value()
        or not settings.dashscope_ws_url
    ):
        return UnavailableVoicePreviewRenderer()
    return CosyVoicePreviewRenderer()


def _account_data_governance(
    settings: ControlSettings,
    *,
    store: MemoryStore,
    voice_profiles: VoiceProfilePort,
    archive_object_store: ObjectStore,
    realtime_connections: RealtimeConnectionRegistry,
    account_operations: AccountOperationGate,
    legacy_registry: LegacyRegistryPort,
) -> AccountDataGovernance:
    archive_url = settings.archive_database_url.get_secret_value()
    archive_repository = (
        PostgresAccountRepository.archive(archive_url)
        if archive_url
        else SqliteAccountRepository.archive(settings.memoria_db_path)
    )
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
        voice_profiles=voice_profiles,
        legacy_registry=legacy_registry,
        archive_object_store=archive_object_store,
        session_terminator=AccountSessionTerminator(
            store=store,
            connections=realtime_connections,
            close_room=LiveKitRoomCloser(settings),
        ),
        operation_blocker=account_operations,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = ControlSettings()
    try:
        settings.validate_production()
    except ValueError as exc:
        if settings.environment == "production":
            raise
        app.state.config_warning = str(exc)
    app.state.settings = settings
    media_stop_dispatcher = _build_media_stop_dispatcher(settings)
    if media_stop_dispatcher is not None:
        app.state.media_stop_dispatcher = media_stop_dispatcher
    session_directory: SessionDirectory = (
        RedisSessionDirectory(settings.redis_url)
        if settings.redis_url.strip()
        else InMemorySessionDirectory()
    )
    app.state.session_directory = session_directory
    media_slo_gate = MediaSLOGate(
        ttl_s=settings.media_slo_snapshot_ttl_s,
        redis_url=settings.redis_url.strip() or None,
    )
    app.state.media_slo_gate = media_slo_gate
    store = MemoryStore(settings.memoria_db_path)
    await to_thread(store.initialize)
    app.state.memory_store = store
    app.state.device_registry = DeviceRegistry(
        store,
        challenge_ttl_ms=settings.device_challenge_ttl_ms,
    )
    archive_url = settings.archive_database_url.get_secret_value()
    compiler_url = settings.archive_compiler_database_url.get_secret_value()
    postgres_archive: PostgresLifeArchive | None = None
    postgres_catalog: PostgresMemoryCatalog | None = None
    postgres_persona: PostgresPersonaEngine | None = None
    postgres_digital_self: PostgresDigitalSelfRegistry | None = None
    postgres_growth: PostgresGrowthReader | None = None
    postgres_self_model: PostgresSelfModelRegistry | None = None
    postgres_legacy: PostgresLegacyRegistry | None = None
    postgres_skills: PostgresSkillCatalog | None = None
    archive: LifeArchivePort
    memory_catalog: MemoryCatalogPort
    skill_catalog: SkillCatalogPort
    persona_engine: PersonaEnginePort
    extractor = _memory_extractor(settings)
    persona_extractor = _persona_extractor(settings)
    embedder = _memory_embedder(settings)
    account_guard = _memory_account_guard(app.state.account_operations, store)
    if archive_url:
        postgres_archive = PostgresLifeArchive(archive_url)
        await postgres_archive.initialize()
        archive = postgres_archive
        postgres_catalog = PostgresMemoryCatalog(
            archive_url,
            extractor=extractor,
            compiler_dsn=compiler_url or None,
            compiler_role=settings.archive_compiler_role or None,
            account_guard=account_guard,
            embedder=embedder,
            require_vector=settings.environment == "production",
        )
        await postgres_catalog.initialize()
        memory_catalog = postgres_catalog
        postgres_skills = PostgresSkillCatalog(archive_url)
        await postgres_skills.initialize()
        skill_catalog = postgres_skills
        postgres_persona = PostgresPersonaEngine(
            archive_url,
            extractor=persona_extractor,
        )
        await postgres_persona.initialize()
        persona_engine = postgres_persona
    else:
        sqlite_archive = LifeArchive.sqlite(settings.memoria_db_path)
        await to_thread(sqlite_archive.initialize)
        archive = sqlite_archive
        sqlite_catalog = MemoryCatalog.sqlite(
            settings.memoria_db_path,
            extractor=extractor,
            account_guard=account_guard,
        )
        await to_thread(sqlite_catalog.initialize)
        memory_catalog = sqlite_catalog
        sqlite_skills = SkillCatalog.sqlite(settings.memoria_db_path)
        await to_thread(sqlite_skills.initialize)
        skill_catalog = sqlite_skills
        sqlite_persona = PersonaEngine.sqlite(
            settings.memoria_db_path,
            extractor=persona_extractor,
        )
        await to_thread(sqlite_persona.initialize)
        persona_engine = sqlite_persona
    app.state.life_archive = archive
    app.state.memory_catalog = memory_catalog
    app.state.skill_catalog = skill_catalog
    app.state.persona_engine = persona_engine
    digital_self_registry: RegistryPort
    if archive_url:
        postgres_digital_self = PostgresDigitalSelfRegistry(archive_url)
        await postgres_digital_self.initialize()
        digital_self_registry = postgres_digital_self
    else:
        sqlite_digital_self = DigitalSelfRegistry.sqlite(settings.memoria_db_path)
        await to_thread(sqlite_digital_self.initialize)
        digital_self_registry = sqlite_digital_self
    app.state.digital_self_registry = digital_self_registry
    app.state.self_preview_registry = SelfPreviewRegistry.sqlite(settings.memoria_db_path)
    await to_thread(app.state.self_preview_registry.initialize)
    self_model_registry: SelfModelRegistryPort
    if archive_url:
        postgres_self_model = PostgresSelfModelRegistry(archive_url)
        await postgres_self_model.initialize()
        self_model_registry = postgres_self_model
    else:
        sqlite_self_model = SelfModelRegistry.sqlite(settings.memoria_db_path)
        await to_thread(sqlite_self_model.initialize)
        self_model_registry = sqlite_self_model
    app.state.self_model_registry = self_model_registry
    legacy_registry: LegacyRegistryPort
    if archive_url:
        postgres_legacy = PostgresLegacyRegistry(archive_url)
        await postgres_legacy.initialize()
        legacy_registry = postgres_legacy
    else:
        sqlite_legacy = LegacyRegistry.sqlite(settings.memoria_db_path)
        await to_thread(sqlite_legacy.initialize)
        legacy_registry = sqlite_legacy
    app.state.legacy_registry = legacy_registry
    # S4 is intentionally read-only over the same ledger; no coverage cache exists.
    if archive_url:
        postgres_growth = PostgresGrowthReader(
            archive_url,
            self_model_registry=self_model_registry,
        )
        await postgres_growth.initialize()
        app.state.growth_reader = postgres_growth
    else:
        app.state.growth_reader = GrowthReader.sqlite(
            settings.memoria_db_path,
            self_model_registry=self_model_registry,
        )
    voice_profile_manager, voice_sample_signer, voice_object_store = _voice_profile_services(
        settings
    )
    postgres_voice = (
        voice_profile_manager
        if isinstance(voice_profile_manager, PostgresVoiceProfileManager)
        else None
    )
    if postgres_voice is not None:
        await postgres_voice.initialize()
    else:
        assert isinstance(voice_profile_manager, VoiceProfileManager)
        await to_thread(voice_profile_manager.initialize)
    app.state.voice_profile_manager = voice_profile_manager
    app.state.voice_sample_signer = voice_sample_signer
    app.state.voice_object_store = voice_object_store
    app.state.voice_preview_renderer = _voice_preview_renderer(settings)
    compiler_worker = MemoryCompilerWorker(
        memory_catalog,
        interval_s=settings.archive_compile_interval_s,
        batch_size=settings.archive_compile_batch_size,
    )
    compiler_worker.start()
    app.state.memory_compiler_worker = compiler_worker
    speaker_authority = _speaker_authority(settings)
    postgres_speaker = (
        speaker_authority if isinstance(speaker_authority, PostgresSpeakerAuthority) else None
    )
    if postgres_speaker is not None:
        await postgres_speaker.initialize()
    else:
        assert isinstance(speaker_authority, SpeakerAuthority)
        await to_thread(speaker_authority.initialize)
    app.state.speaker_authority = speaker_authority
    archive_object_store = _archive_object_store(settings)
    app.state.archive_object_store = archive_object_store
    app.state.account_data_governance = _account_data_governance(
        settings,
        store=store,
        voice_profiles=voice_profile_manager,
        archive_object_store=archive_object_store,
        realtime_connections=app.state.realtime_connections,
        account_operations=app.state.account_operations,
        legacy_registry=legacy_registry,
    )
    deletion_worker = AccountDeletionWorker(app.state.account_data_governance)
    deletion_worker.start()
    app.state.account_deletion_worker = deletion_worker
    try:
        yield
    finally:
        await deletion_worker.stop()
        await compiler_worker.stop()
        await session_directory.close()
        await media_slo_gate.close()
        if postgres_persona is not None:
            await postgres_persona.close()
        if postgres_digital_self is not None:
            await postgres_digital_self.close()
        if postgres_self_model is not None:
            await postgres_self_model.close()
        if postgres_growth is not None:
            await postgres_growth.close()
        if postgres_legacy is not None:
            await postgres_legacy.close()
        if postgres_catalog is not None:
            await postgres_catalog.close()
        if postgres_skills is not None:
            await postgres_skills.close()
        if postgres_archive is not None:
            await postgres_archive.close()
        if postgres_speaker is not None:
            await postgres_speaker.close()
        if postgres_voice is not None:
            await postgres_voice.close()


def create_app() -> FastAPI:
    settings = ControlSettings()
    production = settings.environment == "production"
    app = FastAPI(
        title="voice-agent-control-api",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    # Eager defaults so tests without lifespan still work.
    app.state.settings = settings
    app.state.session_directory = (
        RedisSessionDirectory(settings.redis_url)
        if settings.redis_url.strip()
        else InMemorySessionDirectory()
    )
    app.state.media_slo_gate = MediaSLOGate(
        ttl_s=settings.media_slo_snapshot_ttl_s,
        redis_url=settings.redis_url.strip() or None,
    )
    app.state.account_operations = AccountOperationGate()
    # Single-process CAS fence. A distributed deployment must replace this
    # with an account/task advisory lock or revision projection.
    app.state.growth_task_lock = Lock()
    app.state.realtime_connections = RealtimeConnectionRegistry()
    # The store initializes lazily for ASGI test clients that do not run lifespan.
    app.state.memory_store = MemoryStore(settings.memoria_db_path)
    app.state.device_registry = DeviceRegistry(
        app.state.memory_store,
        challenge_ttl_ms=settings.device_challenge_ttl_ms,
    )
    app.state.life_archive = LifeArchive.sqlite(settings.memoria_db_path)
    app.state.memory_catalog = MemoryCatalog.sqlite(
        settings.memoria_db_path,
        extractor=_memory_extractor(settings),
        account_guard=_memory_account_guard(
            app.state.account_operations,
            app.state.memory_store,
        ),
    )
    app.state.skill_catalog = SkillCatalog.sqlite(settings.memoria_db_path)
    app.state.persona_engine = PersonaEngine.sqlite(
        settings.memoria_db_path,
        extractor=_persona_extractor(settings),
    )
    app.state.digital_self_registry = DigitalSelfRegistry.sqlite(settings.memoria_db_path)
    app.state.self_preview_registry = SelfPreviewRegistry.sqlite(settings.memoria_db_path)
    app.state.self_model_registry = SelfModelRegistry.sqlite(settings.memoria_db_path)
    app.state.legacy_registry = LegacyRegistry.sqlite(settings.memoria_db_path)
    app.state.growth_reader = GrowthReader.sqlite(
        settings.memoria_db_path,
        self_model_registry=app.state.self_model_registry,
    )
    app.state.speaker_authority = _speaker_authority(settings)
    voice_profile_manager, voice_sample_signer, voice_object_store = _voice_profile_services(
        settings
    )
    app.state.voice_profile_manager = voice_profile_manager
    app.state.voice_sample_signer = voice_sample_signer
    app.state.voice_object_store = voice_object_store
    app.state.voice_preview_renderer = _voice_preview_renderer(settings)
    archive_object_store = _archive_object_store(settings)
    app.state.archive_object_store = archive_object_store
    app.state.account_data_governance = _account_data_governance(
        settings,
        store=app.state.memory_store,
        voice_profiles=voice_profile_manager,
        archive_object_store=archive_object_store,
        realtime_connections=app.state.realtime_connections,
        account_operations=app.state.account_operations,
        legacy_registry=app.state.legacy_registry,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth_routes.router)
    app.include_router(interaction_routes.router)
    app.include_router(legacy_routes.router)
    app.include_router(archive_routes.router)
    app.include_router(session_routes.router)
    app.include_router(media_routes.router)
    app.include_router(media_routes.device_router)
    app.include_router(media_routes.internal_router)
    app.include_router(skill_routes.router)
    app.include_router(speaker_routes.router)
    app.include_router(memory_routes.router)
    app.include_router(persona_routes.router)
    app.include_router(digital_self_routes.router)
    app.include_router(self_preview_routes.router)
    app.include_router(self_model_routes.router)
    app.include_router(growth_routes.router)
    app.include_router(voice_routes.router)
    app.include_router(readiness_routes.router)

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
