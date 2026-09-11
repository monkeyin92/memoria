"""Control API FastAPI application."""

from __future__ import annotations

import base64
import binascii
import hashlib
from asyncio import Lock, to_thread
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.parse import quote, unquote, urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.agent.src.providers.crisis_semantic_classifier import (
    CrisisSemanticClassifier,
    CrisisSemanticClassifierConfig,
)
from services.archive.compiler_worker import MemoryCompilerWorker
from services.archive.domain import EvidenceEvent, LifeArchivePort
from services.archive.life_archive import LifeArchive
from services.archive.memory_catalog import MemoryCatalog
from services.archive.memory_domain import (
    AccountWriteGuard,
    AccountWriteRejectedError,
    MemoryCatalogPort,
)
from services.archive.object_store import (
    EncryptedLocalObjectStore,
    EncryptedS3ObjectStore,
    ObjectStore,
)
from services.archive.postgres_archive import PostgresLifeArchive
from services.archive.postgres_memory_catalog import PostgresMemoryCatalog
from services.archive.postgres_skill_catalog import PostgresSkillCatalog
from services.archive.skill_catalog import SkillCatalog
from services.archive.skill_domain import SkillCatalogPort
from services.consent.binding_snapshot import (
    BindingConsentAuthority,
    BindingConsentStorePort,
    PostgresBindingConsentStore,
    RejectingBindingConsentAuthority,
    SqliteBindingConsentStore,
)
from services.control_api.app.account_gate import AccountDeletingError, AccountOperationGate
from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.device_registry import DeviceRegistry
from services.control_api.app.media_runtime import mint_streamcore_token
from services.control_api.app.media_slo import MediaSLOGate
from services.control_api.app.memory_components import build_memory_embedder, build_memory_extractor
from services.control_api.app.memory_scope_authority import PostgresMemoryAuthority
from services.control_api.app.multi_subject_runtime import (
    MultiSubjectRuntimeControl,
    PostgresMultiSubjectRuntimeControl,
)
from services.control_api.app.routes import account_insights as account_insights_routes
from services.control_api.app.routes import archive as archive_routes
from services.control_api.app.routes import auth as auth_routes
from services.control_api.app.routes import custom_personas as custom_personas_routes
from services.control_api.app.routes import device_control as device_control_routes
from services.control_api.app.routes import device_onboarding as device_onboarding_routes
from services.control_api.app.routes import digital_self as digital_self_routes
from services.control_api.app.routes import evolution as evolution_routes
from services.control_api.app.routes import growth as growth_routes
from services.control_api.app.routes import guardian as guardian_routes
from services.control_api.app.routes import identity_lifecycle as identity_lifecycle_routes
from services.control_api.app.routes import interaction as interaction_routes
from services.control_api.app.routes import legacy as legacy_routes
from services.control_api.app.routes import media as media_routes
from services.control_api.app.routes import memory as memory_routes
from services.control_api.app.routes import multi_subject as multi_subject_routes
from services.control_api.app.routes import persona as persona_routes
from services.control_api.app.routes import (
    persona_assignment as persona_assignment_routes,
)
from services.control_api.app.routes import readiness as readiness_routes
from services.control_api.app.routes import self_model as self_model_routes
from services.control_api.app.routes import self_preview as self_preview_routes
from services.control_api.app.routes import session as session_routes
from services.control_api.app.routes import skills as skill_routes
from services.control_api.app.routes import speaker as speaker_routes
from services.control_api.app.routes import tutor as tutor_routes
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
from services.device_fleet.bootstrap_postgres_store import PostgresBootstrapStore
from services.device_fleet.bootstrap_service import (
    DeviceOnboardingService,
    create_device_onboarding_service,
)
from services.digital_self.domain import RegistryPort
from services.digital_self.postgres_registry import PostgresDigitalSelfRegistry
from services.digital_self.preview import SelfPreviewRegistry
from services.digital_self.registry import DigitalSelfRegistry
from services.evolution.account_fence import AccountWriteGuard as EvolutionAccountWriteGuard
from services.evolution.account_fence import require_account_evolution_subject
from services.evolution.account_repository import (
    PostgresEvolutionAccountRepository,
    SqliteEvolutionAccountRepository,
)
from services.evolution.curation import EvolutionControlPlane, SleepLearningPolicy
from services.evolution.postgres_store import PostgresEvolutionStore
from services.evolution.release_policy import (
    EvolutionReleasePolicy,
    parse_runtime_prompt_families,
)
from services.evolution.resolver import EvolutionResolver
from services.evolution.runtime import EvolutionRuntimeCapture
from services.evolution.store import EvolutionStore
from services.evolution.worker import EvolutionSleepWorker
from services.governance.account_data import (
    AccountDataGovernance,
    AccountDeletionWorker,
    AccountRepository,
    PostgresAccountRepository,
    SqliteAccountRepository,
)
from services.growth.postgres_reader import PostgresGrowthReader
from services.growth.reader import GrowthReader
from services.guardian.consent import ConsentRevocationHook, GuardianConsentService
from services.guardian.corpus import (
    CorpusRetentionService,
    CorpusRetentionWorker,
    CorpusSampleStorePort,
)
from services.guardian.crisis import CrisisNotificationService, CrisisNotificationStorePort
from services.guardian.domain import ConsentKind, GuardianStorePort
from services.guardian.postgres_store import PostgresGuardianStore
from services.guardian.sqlite_store import SqliteGuardianStore
from services.identity.authority import (
    ConsentSnapshotResolver,
    RejectingTransferEvidenceVerifier,
)
from services.identity.postgres_store import PostgresIdentityStore
from services.identity.repository import IdentityStore
from services.identity.service import IdentityService
from services.identity.sqlite_store import SqliteIdentityStore
from services.legacy.domain import LegacyRegistryPort
from services.legacy.postgres_registry import PostgresLegacyRegistry
from services.legacy.registry import LegacyRegistry
from services.memory_scope.capture_policy import (
    build_memory_capture_policy_assembly,
)
from services.memory_scope.production import (
    MemoryProductionSettings,
)
from services.memory_scope.redis_outbox import RedisMemoryOutboxDispatcher
from services.memory_scope.relationship_grants import (
    IdentityRelationshipGrantResolver,
)
from services.memory_scope.shared_actions import PostgresFamilySharedActionExecutor
from services.memory_scope.wiring import (
    MemoryProductionWiring,
    build_memory_router,
    install_memory_production,
)
from services.persona.custom_persona_structurer import QwenCustomPersonaStructurer
from services.persona.domain import PersonaEnginePort
from services.persona.engine import PersonaEngine
from services.persona.postgres_engine import PostgresPersonaEngine
from services.persona.qwen_extractor import FallbackPersonaExtractor, QwenPersonaExtractor
from services.persona.rules import PersonaExtractor, RuleBasedPersonaExtractor
from services.policy.engine import PolicyEngine
from services.policy.receipt_store import InMemoryPolicyReceiptWriter
from services.self_model.domain import SelfModelRegistryPort
from services.self_model.postgres_registry import PostgresSelfModelRegistry
from services.self_model.registry import SelfModelRegistry
from services.session_runtime.postgres_store import PostgresSessionRuntimeStore
from services.session_runtime.service import build_postgres_session_runtime_service
from services.speaker.authority import SpeakerAuthority
from services.speaker.campplus_http import (
    CampPlusHTTPEmbeddingAdapter,
    UnavailableSpeakerEmbeddingAdapter,
)
from services.speaker.domain import SpeakerAuthorityPort, SpeakerEmbeddingAdapter
from services.speaker.postgres_authority import PostgresSpeakerAuthority
from services.tutor.authority import (
    TutorEvidenceGate,
    TutorFenceSnapshot,
    TutorScoringRubric,
)
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


class _PostgresRuntimeProfileFencePort:
    """Adapt the persistent Session Runtime profile to Tutor's fence port."""

    def __init__(self, runtime: PostgresMultiSubjectRuntimeControl) -> None:
        self._runtime = runtime

    async def resolve_fence(
        self,
        *,
        actor_id: str,
        voice_session_id: str,
        now: datetime,
    ) -> TutorFenceSnapshot | None:
        profile = await self._runtime.tutor_profile(
            actor_id=actor_id,
            session_id=voice_session_id,
            now=now,
        )
        if profile is None or profile.active_subject_id is None:
            return None
        return TutorFenceSnapshot(
            voice_session_id=voice_session_id,
            actor_id=profile.actor_id,
            active_subject_id=profile.active_subject_id,
            device_id=profile.device_id,
            binding_id=profile.binding_id,
            binding_version=profile.binding_version,
            subject_revision=profile.subject_revision,
            session_epoch=profile.session_epoch,
            runtime_profile_id=profile.runtime_profile_id,
            policy_receipt_ids=tuple(profile.policy_receipt_ids),
            expires_at=profile.expires_at,
            resolved_at=now,
        )


def _install_tutor_authority(app: FastAPI) -> None:
    """PR-13: wire server-owned tutor subject fence and evidence gate.

    The assessment authority (voice-agent seam) is intentionally not wired:
    practice scoring fails closed with 503 until the agent-side evidence
    producer lands.  The action-receipt verifier (Policy transaction-bound
    seam) is also not wired yet: practice writes fail closed with 503 until
    the Policy port lands.  The fence and signing gate are real.
    """

    runtime = app.state.multi_subject_runtime
    if isinstance(runtime, PostgresMultiSubjectRuntimeControl):
        app.state.tutor_session_fence = _PostgresRuntimeProfileFencePort(runtime)
    else:
        from services.control_api.app.routes.tutor import RuntimeProfileFencePort

        app.state.tutor_session_fence = RuntimeProfileFencePort(runtime)
    app.state.tutor_receipt_verifier = None
    app.state.tutor_evidence_gate = TutorEvidenceGate(
        signing_key=app.state.settings.runtime_profile_signing_key()
    )
    app.state.tutor_scoring_rubric = TutorScoringRubric()
    app.state.tutor_assessment_authority = None
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


def _subject_category_resolver(store: MemoryStore) -> Callable[[str], str | None]:
    def resolve(account_id: str) -> str | None:
        profile = store.get_subject_profile(user_id=account_id)
        value = profile.get("subject_category") if profile is not None else None
        return str(value) if value in {"adult", "minor"} else None

    return resolve


def _device_onboarding_database_path(settings: ControlSettings) -> str:
    memory_path = Path(settings.memoria_db_path)
    return str(memory_path.with_name(f"{memory_path.stem}-device-onboarding.sqlite3"))


def _device_onboarding_service(
    settings: ControlSettings,
) -> DeviceOnboardingService | None:
    """Build the explicit local or PostgreSQL/RLS onboarding authority."""

    if settings.environment == "production":
        if not settings.device_media_gateway_url.strip():
            return None
        database_url = settings.device_onboarding_database_url.get_secret_value().strip()
        encoded_seed = (
            settings.device_activation_signing_seed_b64.get_secret_value().strip()
        )
        try:
            seed = base64.b64decode(encoded_seed, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise RuntimeError("invalid production Device Activation signing seed") from exc
        if len(seed) != 32:
            raise RuntimeError("production Device Activation signing seed must be 32 bytes")
        store = PostgresBootstrapStore(database_url)
        try:
            store.initialize()
        except BaseException:
            store.close()
            raise
        return DeviceOnboardingService(
            store,
            server_signing_key=Ed25519PrivateKey.from_private_bytes(seed),
            offline_mock=False,
        )
    seed = hashlib.sha256(
        b"memoria-device-activation-v1\0"
        + settings.memoria_auth_secret.get_secret_value().encode("utf-8")
    ).digest()
    return create_device_onboarding_service(
        database_path=_device_onboarding_database_path(settings),
        server_signing_key=Ed25519PrivateKey.from_private_bytes(seed),
        offline_mock=settings.offline_mock,
    )


def _replace_device_onboarding_service(
    app: FastAPI,
    settings: ControlSettings,
) -> None:
    current = getattr(app.state, "device_onboarding_service", None)
    if isinstance(current, DeviceOnboardingService):
        current.close()
    app.state.device_onboarding_service = _device_onboarding_service(settings)


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


def _persona_structurer(
    settings: ControlSettings,
) -> QwenCustomPersonaStructurer | None:
    """Build the custom-persona structurer, or ``None`` when unmocked/offline.

    Without authority the endpoint returns ``503 persona_structuring_
    unavailable`` and the client hand-fills the same controlled fields
    (PRD P1-2), so no free-text ever reaches a prompt.
    """

    api_key = settings.dashscope_api_key.get_secret_value()
    if settings.offline_mock or not api_key:
        return None
    return QwenCustomPersonaStructurer(
        api_key=api_key,
        base_url=settings.dashscope_base_url,
        model=settings.persona_structuring_model,
        timeout_s=settings.persona_structuring_timeout_s,
        workspace_id=settings.dashscope_workspace_id,
    )


def _crisis_semantic_classifier(
    settings: ControlSettings,
) -> CrisisSemanticClassifier | None:
    api_key = settings.dashscope_api_key.get_secret_value()
    if settings.offline_mock or not settings.crisis_semantic_enabled or not api_key:
        return None
    return CrisisSemanticClassifier(
        CrisisSemanticClassifierConfig(
            api_key=api_key,
            base_url=settings.dashscope_base_url,
            model=settings.crisis_semantic_model,
            timeout_s=settings.crisis_semantic_timeout_s,
        )
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
    ) -> dict[str, object]:
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
        idempotency_key = str(event.get("idempotency_key") or "").strip()
        if not idempotency_key:
            raise ValueError("media stop dispatch requires an idempotency key")
        body = {key: value for key, value in event.items() if key != "idempotency_key"}
        async with httpx.AsyncClient(timeout=settings.media_edge_control_timeout_s) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Idempotency-Key": idempotency_key,
                },
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("media edge stop response must be an object")
        return payload

    return dispatch


def _voice_preview_renderer(settings: ControlSettings) -> VoicePreviewRenderer:
    if (
        settings.offline_mock
        or not settings.dashscope_api_key.get_secret_value()
        or not settings.dashscope_ws_url
    ):
        return UnavailableVoicePreviewRenderer()
    return CosyVoicePreviewRenderer()


def _guardian_revocation_hook(
    terminator: AccountSessionTerminator,
) -> ConsentRevocationHook:
    async def on_revoked(account_id: str, kind: ConsentKind) -> None:
        if kind in {"minor_voice_session", "memory_retention"}:
            await terminator.terminate_account(account_id)

    return on_revoked


def _account_data_governance(
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


def _evolution_release_policy(settings: ControlSettings) -> EvolutionReleasePolicy:
    return EvolutionReleasePolicy(
        parse_runtime_prompt_families(settings.evolution_runtime_prompt_families)
    )


def _evolution_plane(
    settings: ControlSettings,
    store: EvolutionStore,
    *,
    profile_store: MemoryStore,
    account_write_guard: EvolutionAccountWriteGuard | None = None,
) -> EvolutionControlPlane:
    return EvolutionControlPlane(
        store,
        trusted_root_sha256=settings.evolution_trusted_root(),
        policy=SleepLearningPolicy(
            min_new_signals=settings.evolution_min_new_signals,
            min_failure_support=settings.evolution_min_failure_support,
            stale_after_days=settings.evolution_stale_after_days,
        ),
        release_policy=_evolution_release_policy(settings),
        account_write_guard=account_write_guard,
        account_subject_guard=lambda account_id: require_account_evolution_subject(
            account_id,
            resolve_subject_category=_subject_category_resolver(profile_store),
        ),
    )


async def _install_session_runtime(
    app: FastAPI,
    settings: ControlSettings,
) -> PostgresSessionRuntimeStore | None:
    """Install the authoritative Session Runtime for the current profile."""

    app.state.session_runtime_store = None
    app.state.session_runtime_service = None
    if settings.environment == "production":
        session_runtime_store = PostgresSessionRuntimeStore(
            dsn=settings.session_runtime_database_url.get_secret_value().strip(),
            action_dsn=settings.action_executor_database_url.get_secret_value().strip(),
            bootstrap_dsn=(
                settings.session_runtime_bootstrap_database_url.get_secret_value().strip()
                or None
            ),
        )
        app.state.session_runtime_store = session_runtime_store
        await session_runtime_store.initialize()
        session_runtime_service = build_postgres_session_runtime_service(
            store=session_runtime_store,
            signing_key=settings.runtime_profile_signing_key(),
            policy=PolicyEngine(),
        )
        app.state.session_runtime_service = session_runtime_service
        app.state.policy_receipt_writer = None
        app.state.multi_subject_runtime = PostgresMultiSubjectRuntimeControl(
            identity=app.state.identity_service,
            sessions=session_runtime_service,
        )
        return session_runtime_store

    app.state.policy_receipt_writer = InMemoryPolicyReceiptWriter()
    app.state.multi_subject_runtime = MultiSubjectRuntimeControl(
        identity=app.state.identity_service,
        policy=PolicyEngine(receipt_writer=app.state.policy_receipt_writer),
        signing_key=settings.runtime_profile_signing_key(),
    )
    return None


async def _install_memory_scope(
    app: FastAPI,
    settings: ControlSettings,
) -> None:
    """Install MemoryScope only from dedicated production PostgreSQL roles."""

    app.state.memory_wiring = None
    if settings.environment != "production":
        return
    api_dsn = settings.memory_api_database_url.get_secret_value().strip()
    worker_dsn = settings.memory_worker_database_url.get_secret_value().strip()
    if not api_dsn or not worker_dsn:
        return
    runtime = app.state.multi_subject_runtime
    if not isinstance(runtime, PostgresMultiSubjectRuntimeControl):
        raise RuntimeError("MemoryScope requires the production Session runtime")
    bootstrap_dsn = (
        settings.memory_bootstrap_database_url.get_secret_value().strip() or None
    )
    api_password = urlsplit(api_dsn).password
    worker_password = urlsplit(worker_dsn).password
    if bootstrap_dsn and api_password != worker_password:
        raise RuntimeError(
            "MemoryScope bootstrap requires matching API and worker role passwords"
        )
    production_settings = MemoryProductionSettings(
        api_dsn=api_dsn,
        worker_dsn=worker_dsn,
        action_executor_dsn=(
            settings.action_executor_database_url.get_secret_value().strip()
        ),
        bootstrap_dsn=bootstrap_dsn,
        app_role_password=(
            unquote(api_password) if bootstrap_dsn and api_password else None
        ),
        schema_managed_externally=settings.memory_schema_managed_externally,
    )
    capture_policy = build_memory_capture_policy_assembly()
    relationship_grants = IdentityRelationshipGrantResolver(
        app.state.identity_service
    )
    dispatcher = (
        RedisMemoryOutboxDispatcher.from_url(settings.redis_url)
        if settings.redis_url.strip()
        else None
    )
    shared_actions = PostgresFamilySharedActionExecutor(
        production_settings.action_executor_dsn
    )
    wiring = install_memory_production(
        app,
        production_settings,
        receipt_verifier=None,
        family_membership_verifier=None,
        consent_verifier=None,
        grant_resolver=relationship_grants,
        authority=PostgresMemoryAuthority(runtime),
        sensitive_write=capture_policy.sensitive_write,
        context_builder=capture_policy.context_builder,
        outbox_dispatcher=dispatcher,
        shared_action_executor=shared_actions,
        include_router=False,
    )
    await wiring.start()


@asynccontextmanager
async def _lifespan_impl(app: FastAPI) -> AsyncIterator[None]:
    settings = ControlSettings()
    try:
        settings.validate_production()
    except ValueError as exc:
        if settings.environment == "production":
            raise
        app.state.config_warning = str(exc)
    app.state.settings = settings
    crisis_semantic_classifier = _crisis_semantic_classifier(settings)
    app.state.crisis_semantic_classifier = crisis_semantic_classifier
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
    _replace_device_onboarding_service(app, settings)
    consent_url = settings.consent_database_url.get_secret_value().strip()
    if consent_url:
        binding_consent_store: BindingConsentStorePort = (
            PostgresBindingConsentStore(consent_url)
        )
    else:
        binding_consent_store = SqliteBindingConsentStore(
            settings.consent_sqlite_path()
        )
    await binding_consent_store.initialize()
    binding_consent_authority = BindingConsentAuthority(binding_consent_store)
    app.state.binding_consent_store = binding_consent_store
    app.state.binding_consent_authority = binding_consent_authority

    identity_url = settings.identity_database_url.get_secret_value().strip()
    if identity_url:
        identity_store: IdentityStore = PostgresIdentityStore(
            identity_url,
            registration_dsn=(
                settings.identity_registration_database_url.get_secret_value().strip()
                or None
            ),
        )
        await identity_store.initialize()
    else:
        sqlite_identity_store = SqliteIdentityStore(settings.identity_sqlite_path())
        await to_thread(sqlite_identity_store.initialize)
        identity_store = cast(IdentityStore, sqlite_identity_store)
    app.state.identity_store = identity_store
    app.state.identity_service = IdentityService(
        identity_store,
        # Transfer evidence authorities are NOT wired yet: the composite
        # verifier fails closed until the policy receipt / step-up
        # integration lands.
        transfer_verifier=RejectingTransferEvidenceVerifier(),
        consent_resolver=binding_consent_authority,
    )
    app.state.multi_subject_binding_manifests = {}
    await _install_session_runtime(app, settings)
    await _install_memory_scope(app, settings)
    _install_tutor_authority(app)
    app.state.device_registry = DeviceRegistry(
        store,
        challenge_ttl_ms=settings.device_challenge_ttl_ms,
    )
    session_terminator = AccountSessionTerminator(
        store=store,
        connections=app.state.realtime_connections,
        close_room=LiveKitRoomCloser(settings),
    )
    app.state.session_terminator = session_terminator
    guardian_url = settings.guardian_database_url.get_secret_value().strip()
    guardian_maintenance_url = (
        settings.guardian_maintenance_database_url.get_secret_value().strip() or None
    )
    guardian_worker_url = (
        settings.guardian_worker_database_url.get_secret_value().strip() or None
    )
    postgres_guardian: PostgresGuardianStore | None = None
    if guardian_url:
        postgres_guardian = PostgresGuardianStore(
            guardian_url,
            maintenance_dsn=guardian_maintenance_url,
            worker_dsn=guardian_worker_url,
            initialize_schema=settings.environment != "production",
        )
        await postgres_guardian.initialize()
        guardian_store: GuardianStorePort = postgres_guardian
    else:
        sqlite_guardian = SqliteGuardianStore(settings.memoria_db_path)
        await to_thread(sqlite_guardian.initialize)
        guardian_store = sqlite_guardian
    app.state.guardian_store = guardian_store
    app.state.tutor_store = guardian_store
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
    postgres_evolution: PostgresEvolutionStore | None = None
    archive: LifeArchivePort
    memory_catalog: MemoryCatalogPort
    skill_catalog: SkillCatalogPort
    persona_engine: PersonaEnginePort
    extractor = build_memory_extractor(settings)
    persona_extractor = _persona_extractor(settings)
    embedder = build_memory_embedder(settings)
    account_guard = _memory_account_guard(app.state.account_operations, store)
    if archive_url:
        postgres_archive = PostgresLifeArchive(
            archive_url,
            outbox_max_attempts=settings.archive_compile_max_attempts,
        )
        await postgres_archive.initialize()
        archive = postgres_archive
        memory_wiring = cast(
            MemoryProductionWiring | None,
            getattr(app.state, "memory_wiring", None),
        )

        async def project_capture_evidence(event: EvidenceEvent) -> bool:
            candidate = event.payload.get("memory_capture_candidate_v1")
            if not isinstance(candidate, dict) or memory_wiring is None:
                return False
            return await memory_wiring.project_capture_evidence(
                event_id=event.event_id,
                content_sha256=event.content_sha256,
                occurred_at=event.occurred_at,
                candidate=cast(dict[str, object], candidate),
            )

        postgres_catalog = PostgresMemoryCatalog(
            archive_url,
            extractor=extractor,
            compiler_dsn=compiler_url or None,
            compiler_role=settings.archive_compiler_role or None,
            account_guard=account_guard,
            subject_category_resolver=_subject_category_resolver(store),
            capture_evidence_projector=(
                project_capture_evidence
                if settings.environment == "production"
                else None
            ),
            embedder=embedder,
            require_vector=settings.environment == "production",
            outbox_lease_s=settings.archive_compile_lease_s,
            outbox_retry_base_s=settings.archive_compile_retry_base_s,
            outbox_retry_max_s=settings.archive_compile_retry_max_s,
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
            subject_category_resolver=_subject_category_resolver(store),
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
    app.state.crisis_notification_service = CrisisNotificationService(
        cast(CrisisNotificationStorePort, guardian_store),
        archive,
    )
    app.state.guardian_consent_service = GuardianConsentService(
        guardian_store,
        archive,
        on_revoked=_guardian_revocation_hook(session_terminator),
    )
    app.state.memory_catalog = memory_catalog
    app.state.skill_catalog = skill_catalog
    app.state.persona_engine = persona_engine
    app.state.persona_structurer = _persona_structurer(settings)
    configured_evolution_url = settings.evolution_database_url.get_secret_value().strip()
    if settings.environment == "production" and not configured_evolution_url:
        # ``validate_production`` normally catches this first; keep the
        # lifecycle fail-closed if a caller constructs settings directly.
        raise ValueError("production requires MEMORIA_EVOLUTION_DATABASE_URL")
    evolution_url = configured_evolution_url or archive_url
    if evolution_url:
        postgres_evolution = PostgresEvolutionStore(
            evolution_url,
            initialize_schema=settings.environment != "production",
        )
        await to_thread(postgres_evolution.initialize)
        evolution_store: EvolutionStore = postgres_evolution
    else:
        evolution_store = EvolutionStore(settings.evolution_sqlite_path())
    evolution_control_plane = _evolution_plane(
        settings,
        evolution_store,
        profile_store=store,
        account_write_guard=app.state.account_operations.sync_write,
    )
    app.state.evolution_store = evolution_store
    app.state.evolution_control_plane = evolution_control_plane
    app.state.evolution_runtime_capture = EvolutionRuntimeCapture(evolution_control_plane)
    app.state.evolution_resolver = EvolutionResolver(
        evolution_store,
        trusted_root_sha256=settings.evolution_trusted_root(),
        canary_percent=settings.evolution_canary_percent,
        release_policy=_evolution_release_policy(settings),
        account_subject_guard=lambda account_id: require_account_evolution_subject(
            account_id,
            resolve_subject_category=_subject_category_resolver(store),
        ),
        account_read_guard=app.state.account_operations.sync_read,
    )
    evolution_sleep_worker = EvolutionSleepWorker(
        evolution_control_plane,
        interval_s=settings.evolution_sleep_interval_s,
    )
    evolution_sleep_worker.start()
    app.state.evolution_sleep_worker = evolution_sleep_worker
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
    corpus_retention_service = CorpusRetentionService(
        cast(CorpusSampleStorePort, guardian_store),
        archive_object_store,
    )
    corpus_retention_worker = CorpusRetentionWorker(
        corpus_retention_service,
        interval_s=settings.corpus_retention_interval_s,
    )
    corpus_retention_worker.start()
    app.state.corpus_retention_service = corpus_retention_service
    app.state.corpus_retention_worker = corpus_retention_worker
    app.state.account_data_governance = _account_data_governance(
        settings,
        store=store,
        voice_profiles=voice_profile_manager,
        archive_object_store=archive_object_store,
        realtime_connections=app.state.realtime_connections,
        account_operations=app.state.account_operations,
        legacy_registry=legacy_registry,
        evolution_repository=(
            PostgresEvolutionAccountRepository(evolution_url)
            if evolution_url
            else SqliteEvolutionAccountRepository(settings.evolution_sqlite_path())
        ),
        guardian_repository=guardian_store,
        corpus_retention_service=corpus_retention_service,
        session_terminator=session_terminator,
    )
    deletion_worker = AccountDeletionWorker(app.state.account_data_governance)
    deletion_worker.start()
    app.state.account_deletion_worker = deletion_worker
    try:
        yield
    finally:
        await corpus_retention_worker.stop()
        if crisis_semantic_classifier is not None:
            await crisis_semantic_classifier.aclose()
        await evolution_sleep_worker.stop()
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
        if postgres_evolution is not None:
            await to_thread(postgres_evolution.close)
        if postgres_archive is not None:
            await postgres_archive.close()
        if postgres_speaker is not None:
            await postgres_speaker.close()
        if postgres_voice is not None:
            await postgres_voice.close()
        if postgres_guardian is not None:
            await postgres_guardian.close()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    try:
        async with _lifespan_impl(app):
            yield
    finally:
        memory_wiring = getattr(app.state, "memory_wiring", None)
        if memory_wiring is not None:
            await memory_wiring.close()
        session_runtime_store = getattr(
            app.state,
            "session_runtime_store",
            None,
        )
        if session_runtime_store is not None:
            await session_runtime_store.close()
        device_onboarding_service = getattr(
            app.state,
            "device_onboarding_service",
            None,
        )
        if isinstance(device_onboarding_service, DeviceOnboardingService):
            device_onboarding_service.close()
        identity_store = getattr(
            app.state,
            "identity_store",
            None,
        )
        if identity_store is not None:
            await identity_store.close()
        binding_consent_store = getattr(
            app.state,
            "binding_consent_store",
            None,
        )
        if binding_consent_store is not None:
            await binding_consent_store.close()


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
    # Independent Edge-to-Control close-report secret (MEDIA_EDGE_DEVICE_
    # CLOSE_REPORT_TOKEN on the Edge side). Deliberately distinct from the
    # Control-to-Edge runtime-control token; the internal device-close
    # endpoint validates it before touching any private state.
    app.state.device_close_report_token = settings.media_edge_device_close_report_token
    app.state.crisis_semantic_classifier = _crisis_semantic_classifier(settings)
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
    if production:
        app.state.device_onboarding_service = None
    else:
        _replace_device_onboarding_service(app, settings)
    identity_store = SqliteIdentityStore(settings.identity_sqlite_path())
    identity_store.initialize()
    app.state.identity_store = identity_store
    if production:
        binding_consent_store = None
        binding_consent_authority: ConsentSnapshotResolver = (
            RejectingBindingConsentAuthority()
        )
    else:
        sqlite_binding_consent_store = SqliteBindingConsentStore(
            settings.consent_sqlite_path()
        )
        sqlite_binding_consent_store.initialize_sync()
        binding_consent_store = sqlite_binding_consent_store
        binding_consent_authority = BindingConsentAuthority(
            sqlite_binding_consent_store
        )
    app.state.binding_consent_store = binding_consent_store
    app.state.binding_consent_authority = binding_consent_authority
    app.state.identity_service = IdentityService(
        cast(IdentityStore, identity_store),
        transfer_verifier=RejectingTransferEvidenceVerifier(),
        consent_resolver=binding_consent_authority,
    )
    app.state.multi_subject_binding_manifests = {}
    app.state.session_runtime_store = None
    app.state.session_runtime_service = None
    app.state.memory_wiring = None
    if production:
        # Production Session Runtime is installed by the async lifespan after
        # schema/RLS/action-role initialization; never provide an in-memory
        # policy receipt fallback in the eager app state.
        app.state.policy_receipt_writer = None
        app.state.multi_subject_runtime = None
        app.state.tutor_session_fence = None
    else:
        app.state.policy_receipt_writer = InMemoryPolicyReceiptWriter()
        app.state.multi_subject_runtime = MultiSubjectRuntimeControl(
            identity=app.state.identity_service,
            policy=PolicyEngine(receipt_writer=app.state.policy_receipt_writer),
            signing_key=settings.runtime_profile_signing_key(),
        )
        _install_tutor_authority(app)
    app.state.device_registry = DeviceRegistry(
        app.state.memory_store,
        challenge_ttl_ms=settings.device_challenge_ttl_ms,
    )
    guardian_store = SqliteGuardianStore(settings.memoria_db_path)
    guardian_store.initialize()
    app.state.guardian_store = guardian_store
    app.state.tutor_store = guardian_store
    session_terminator = AccountSessionTerminator(
        store=app.state.memory_store,
        connections=app.state.realtime_connections,
        close_room=LiveKitRoomCloser(settings),
    )
    app.state.session_terminator = session_terminator
    app.state.life_archive = LifeArchive.sqlite(settings.memoria_db_path)
    app.state.crisis_notification_service = CrisisNotificationService(
        guardian_store,
        app.state.life_archive,
    )
    app.state.guardian_consent_service = GuardianConsentService(
        guardian_store,
        app.state.life_archive,
        on_revoked=_guardian_revocation_hook(session_terminator),
    )
    app.state.memory_catalog = MemoryCatalog.sqlite(
        settings.memoria_db_path,
        extractor=build_memory_extractor(settings),
        account_guard=_memory_account_guard(
            app.state.account_operations,
            app.state.memory_store,
        ),
        subject_category_resolver=_subject_category_resolver(app.state.memory_store),
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
    evolution_store = EvolutionStore(settings.evolution_sqlite_path())
    evolution_control_plane = _evolution_plane(
        settings,
        evolution_store,
        profile_store=app.state.memory_store,
        account_write_guard=app.state.account_operations.sync_write,
    )
    app.state.evolution_store = evolution_store
    app.state.evolution_control_plane = evolution_control_plane
    app.state.evolution_runtime_capture = EvolutionRuntimeCapture(evolution_control_plane)
    app.state.evolution_resolver = EvolutionResolver(
        evolution_store,
        trusted_root_sha256=settings.evolution_trusted_root(),
        canary_percent=settings.evolution_canary_percent,
        release_policy=_evolution_release_policy(settings),
        account_subject_guard=lambda account_id: require_account_evolution_subject(
            account_id,
            resolve_subject_category=_subject_category_resolver(app.state.memory_store),
        ),
        account_read_guard=app.state.account_operations.sync_read,
    )
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
    corpus_retention_service = CorpusRetentionService(
        guardian_store,
        archive_object_store,
    )
    app.state.corpus_retention_service = corpus_retention_service
    app.state.account_data_governance = _account_data_governance(
        settings,
        store=app.state.memory_store,
        voice_profiles=voice_profile_manager,
        archive_object_store=archive_object_store,
        realtime_connections=app.state.realtime_connections,
        account_operations=app.state.account_operations,
        legacy_registry=app.state.legacy_registry,
        evolution_repository=SqliteEvolutionAccountRepository(settings.evolution_sqlite_path()),
        guardian_repository=guardian_store,
        corpus_retention_service=corpus_retention_service,
        session_terminator=session_terminator,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins_list(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(auth_routes.router)
    app.include_router(account_insights_routes.router)
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
    app.include_router(build_memory_router())
    app.include_router(multi_subject_routes.router)
    app.include_router(persona_assignment_routes.router)
    app.include_router(device_onboarding_routes.router)
    app.include_router(device_control_routes.router)
    app.include_router(device_control_routes.internal_router)
    app.include_router(identity_lifecycle_routes.router)
    app.include_router(persona_routes.router)
    app.include_router(custom_personas_routes.router)
    app.include_router(digital_self_routes.router)
    app.include_router(evolution_routes.router)
    app.include_router(self_preview_routes.router)
    app.include_router(self_model_routes.router)
    app.include_router(growth_routes.router)
    app.include_router(guardian_routes.router)
    app.include_router(tutor_routes.router)
    app.include_router(voice_routes.router)
    app.include_router(readiness_routes.router)

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
