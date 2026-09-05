"""Production session bootstrap shared by StreamCore runtime and providers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from services.agent.src.agent import DuplexVoiceAgent
from services.agent.src.agent_voice_profile import (
    _apply_cached_voice_profile,
    align_tts_voice_to_policy,
)
from services.agent.src.archive_sink import ArchiveSink, ArchiveSinkConfig
from services.agent.src.conversation_close_wiring import (
    install_conversation_close_semantic_resolver,
)
from services.agent.src.device_vad import DEVICE_POST_PLAYBACK_HOLDOFF_S
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.generation_output_policy import frozen_companion_clone_permitted
from services.agent.src.live_lookup_wiring import install_live_lookup_semantic_resolver
from services.agent.src.mode_policy_client import ModePolicyClient, ModePolicyClientConfig
from services.agent.src.observability.metrics import GLOBAL_METRICS
from services.agent.src.orchestration.handlers import (
    LanguageModelRequest,
    SpeechSynthesisHandler,
)
from services.agent.src.orchestration.speaker_verify import SpeakerVerifier
from services.agent.src.providers.cosyvoice_tts import CosyVoiceTTS
from services.agent.src.providers.doubao_tts import DoubaoTTS
from services.agent.src.providers.funasr_stt import FunASRConfig, FunASRSession
from services.agent.src.providers.handlers import (
    build_language_model_handler,
    build_realtime_search_resolver,
)
from services.agent.src.response_planner_client import (
    ResponsePlannerClient,
    ResponsePlannerClientConfig,
)
from services.agent.src.runtime_profile import VerifiedRuntimeProfile
from services.agent.src.session_entrypoint import should_enable_legacy_speaker_verifier
from services.agent.src.tutor_session import production_system_prompt
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.src.voice_core.media_session import MediaSessionResources
from services.agent.src.voice_core.provider_adapter import (
    ExistingVoiceProviderAdapter,
    ExistingVoiceProviderConfig,
)
from services.agent.src.voice_profile_client import (
    VoiceProfileClient,
    VoiceProfileClientConfig,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _SessionLanguageModel:
    agent: DuplexVoiceAgent
    closeables: tuple[object, ...]
    delegation_enabled: bool = False
    closed: bool = False

    def stream(self, request: LanguageModelRequest) -> Any:
        return self.agent.stream(request)

    async def prepare_committed_turn(self, text: str) -> Any:
        return await self.agent.prepare_committed_turn(text)

    @property
    def supports_delegation(self) -> bool:
        return self.delegation_enabled

    async def start_delegation(self, text: str, fence: Any) -> str | None:
        if not self.delegation_enabled:
            return None
        return await self.agent.resolve_media_delegation(text, fence)

    @staticmethod
    def accept_output_intent(intent: Any) -> Any:
        # MediaSession owns source execution; this seam only exposes admission.
        return intent

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        for component in self.closeables:
            close = getattr(component, "aclose", None) or getattr(component, "close", None)
            if not callable(close):
                continue
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.error(
                    "media Agent component shutdown failed component=%s",
                    type(component).__name__,
                    exc_info=True,
                )


@dataclass(slots=True)
class ProductionMediaSessionFactory:
    settings: Any
    llm_factory: Any
    archive_sink: ArchiveSink | None = None

    async def __call__(self, identity: SessionIdentity) -> MediaSessionResources:
        if not identity.session_id:
            raise ValueError("production media session requires a session id")
        tts = DoubaoTTS.from_env()
        runtime: DuplexRuntime | None = None
        owned: list[object] = []
        try:
            warm = getattr(getattr(tts, "pool", None), "warm", None)
            if callable(warm):
                with contextlib.suppress(Exception):
                    await warm()
            language_model = build_language_model_handler(
                settings=self.settings,
                llm_factory=self.llm_factory,
            )
            owned.append(language_model)
            realtime_search_resolver = build_realtime_search_resolver(settings=self.settings)
            if realtime_search_resolver is not None:
                owned.append(realtime_search_resolver)
            runtime = self._new_runtime(
                identity.session_id,
                tts,
                device_id=identity.device_id or None,
                identity=identity,
            )
            live_lookup_classifier = install_live_lookup_semantic_resolver(runtime, self.settings)
            if live_lookup_classifier is not None:
                owned.append(live_lookup_classifier)
            close_intent_classifier = install_conversation_close_semantic_resolver(
                runtime,
                self.settings,
            )
            if close_intent_classifier is not None:
                owned.append(close_intent_classifier)
            mode_policy_client = await self._bind_mode_policy(runtime)
            owned.append(mode_policy_client)
            tts = await self._maybe_use_cosyvoice_clone_tts(runtime, tts)

            async def _refresh_profile() -> VerifiedRuntimeProfile | None:
                policy = await mode_policy_client.fetch(session_id=runtime.session_id)
                expected_profile_version = (
                    runtime.orchestrator.runtime_profiles.expected_device_profile_version
                )
                if expected_profile_version is not None and (
                    policy.runtime_profile_version != expected_profile_version
                ):
                    raise RuntimeError(
                        "device RuntimeProfile version changed; reconnect at a safe session boundary"
                    )
                return policy.runtime_profile

            runtime.set_runtime_profile_refresher(_refresh_profile)
            response_planner_client = self._response_planner_client()
            owned.append(response_planner_client)
            voice_profile_client = await self._bind_voice_profile(runtime, tts)
            if voice_profile_client is not None:
                owned.append(voice_profile_client)
            align_tts_voice_to_policy(
                tts,
                runtime.mode_policy,
                personal_voice_permitted=runtime.profile_permits(
                    runtime.fence, capability="voice_clone_use"
                )
                or frozen_companion_clone_permitted(runtime.mode_policy),
            )
            self._bind_speaker_authority(runtime)
            await self._bind_archive(runtime)
            self._configure_runtime(runtime, tts)
            await runtime.orchestrator.ready()
            warmer = getattr(language_model, "prewarm", None)
            agent = DuplexVoiceAgent(
                instructions=production_system_prompt(runtime),
                runtime=runtime,
                voice_profile_client=voice_profile_client,
                response_planner_client=response_planner_client,
                realtime_search_resolver=realtime_search_resolver,
                realtime_search_model=(
                    str(
                        getattr(
                            realtime_search_resolver,
                            "model",
                            getattr(self.settings, "qwen_deep_model", "qwen-plus"),
                        )
                    )
                    if realtime_search_resolver is not None
                    else None
                ),
                standalone_llm=language_model,
                fast_model_warmer=warmer if callable(warmer) else None,
                llm_provider=self.settings.llm_provider,
                llm_model=self.settings.llm_fast_model,
                tts_provider="volcengine_doubao",
                tts_model=self.settings.doubao_tts_resource_id,
            )
            handler = _SessionLanguageModel(
                agent,
                tuple(reversed(owned)),
                delegation_enabled=realtime_search_resolver is not None,
            )
            asr_config = FunASRConfig.from_env()
            provider = ExistingVoiceProviderAdapter(
                asr_session_factory=lambda: FunASRSession(
                    replace(asr_config),
                    metrics=GLOBAL_METRICS,
                ),
                language_model=handler,
                speech_synthesis=cast(SpeechSynthesisHandler, tts),
                config=ExistingVoiceProviderConfig(
                    sample_rate=asr_config.sample_rate,
                    output_sample_rate=int(self.settings.doubao_tts_sample_rate),
                ),
                metrics=GLOBAL_METRICS,
                owns_speech_synthesis=True,
                owns_language_model=True,
            )
            return MediaSessionResources(runtime=runtime, provider=provider)
        except Exception:
            if runtime is not None:
                with contextlib.suppress(Exception):
                    await runtime.close()
            close_tts = getattr(tts, "aclose", None)
            if callable(close_tts):
                with contextlib.suppress(Exception):
                    await close_tts()
            for component in reversed(owned):
                close = getattr(component, "aclose", None) or getattr(component, "close", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        result = close()
                        if asyncio.iscoroutine(result):
                            await result
            raise

    def _new_runtime(
        self,
        session_id: str,
        tts: Any,
        *,
        device_id: str | None = None,
        identity: SessionIdentity | None = None,
    ) -> DuplexRuntime:
        settings = self.settings
        device_session = identity is not None and identity.client_type == "device"
        runtime = DuplexRuntime.create(
            session_id=session_id,
            device_id=device_id,
            tts=tts,
            input_guard_enabled=True,
            barge_in_enabled=not device_session,
            capture_release_holdoff_s=(
                DEVICE_POST_PLAYBACK_HOLDOFF_S if device_session else 0.0
            ),
            listener_cues_enabled=bool(settings.listener_cues_enabled),
            use_paralinguistic_tags=False,
            speaker_verifier=SpeakerVerifier(
                enabled=should_enable_legacy_speaker_verifier(settings, offline=False),
                enroll_speech_ms=settings.speaker_enroll_speech_ms,
                enroll_timeout_ms=settings.speaker_enroll_timeout_ms,
                accept_threshold=settings.speaker_accept_threshold,
                min_verify_speech_ms=settings.speaker_min_verify_speech_ms,
            ),
        )
        if identity is not None and identity.client_type == "device":
            gate = runtime.orchestrator.runtime_profiles
            gate.expected_actor_id = identity.account_id
            gate.expected_binding_id = identity.binding_id
            gate.expected_binding_version = identity.binding_version
            gate.expected_active_subject_id = identity.subject_id
            # Device authority always pins the runtime subject: an empty
            # subject_id means the signed profile must carry no subject
            # (unknown_safe), never that subject validation is skipped.
            gate.expected_subject_fence_enabled = True
            gate.expected_device_profile_version = identity.runtime_profile_version
        return runtime

    async def _bind_mode_policy(self, runtime: DuplexRuntime) -> ModePolicyClient:
        settings = self.settings
        token = settings.internal_token("interaction_policy")
        if not token:
            raise RuntimeError("production media Agent requires interaction policy authority")
        client = ModePolicyClient(
            ModePolicyClientConfig(
                endpoint=settings.interaction_policy_url,
                internal_token=token,
                timeout_s=settings.interaction_policy_timeout_s,
            )
        )
        policy = await client.fetch(session_id=runtime.session_id)
        expected_profile_version = (
            runtime.orchestrator.runtime_profiles.expected_device_profile_version
        )
        if expected_profile_version is not None and (
            policy.runtime_profile_version != expected_profile_version
        ):
            await client.aclose()
            raise RuntimeError("device ticket RuntimeProfile version does not match authority")
        runtime.set_mode_policy(policy)
        if not policy.allows_conversation():
            await client.aclose()
            raise RuntimeError("interaction policy does not authorize the media session")
        return client

    def _response_planner_client(self) -> ResponsePlannerClient:
        settings = self.settings
        token = settings.internal_token("response_plan")
        if not token:
            raise RuntimeError("production media Agent requires response plan authority")
        return ResponsePlannerClient(
            ResponsePlannerClientConfig(
                endpoint=settings.response_plan_url,
                internal_token=token,
                timeout_s=settings.response_plan_timeout_s,
            )
        )

    def _bind_speaker_authority(self, runtime: DuplexRuntime) -> None:
        settings = self.settings
        if not settings.speaker_authority_enabled:
            return
        from services.agent.src.speaker_authority_client import (
            SpeakerAuthorityClient,
            SpeakerAuthorityClientConfig,
        )

        authority = SpeakerAuthorityClient(
            SpeakerAuthorityClientConfig(
                endpoint=settings.speaker_authority_url,
                internal_token=settings.speaker_internal_token.get_secret_value(),
                timeout_s=settings.speaker_authority_timeout_s,
            )
        )

        async def classify(pcm: bytes, sample_rate: int) -> Any:
            try:
                return await authority.classify(
                    session_id=runtime.session_id,
                    pcm=pcm,
                    sample_rate=sample_rate,
                )
            finally:
                runtime.set_reject_non_owner_voice(authority.reject_non_owner_voice)

        runtime.set_speaker_classifier(
            classify,
            sample_rate=settings.funasr_sample_rate,
            timeout_s=settings.speaker_authority_timeout_s,
        )
        runtime.set_target_speaker_focus(True)

    async def _bind_voice_profile(
        self,
        runtime: DuplexRuntime,
        tts: Any,
    ) -> VoiceProfileClient | None:
        settings = self.settings
        token = settings.internal_token("voice_resolution")
        if not (settings.voice_profile_enabled and token):
            return None
        client = VoiceProfileClient(
            VoiceProfileClientConfig(
                endpoint=settings.voice_profile_url,
                internal_token=token,
                timeout_s=settings.voice_profile_timeout_s,
            )
        )
        runtime.set_voice_profile_refresher(lambda: client.refresh(session_id=runtime.session_id))
        await client.refresh(session_id=runtime.session_id)
        _apply_cached_voice_profile(
            tts_plugin=tts,
            client=client,
            session_id=runtime.session_id,
            mode=runtime.mode_policy.mode,
            policy=runtime.mode_policy,
        )
        return client

    async def _maybe_use_cosyvoice_clone_tts(self, runtime: DuplexRuntime, tts: Any) -> Any:
        if dict(runtime.mode_policy.references).get("voice_provider") != "alibaba_model_studio":
            return tts
        if not frozen_companion_clone_permitted(runtime.mode_policy):
            return tts
        close_tts = getattr(tts, "aclose", None)
        if callable(close_tts):
            with contextlib.suppress(Exception):
                await close_tts()
        clone_tts = CosyVoiceTTS.from_env()
        runtime.tts = clone_tts
        warm = getattr(getattr(clone_tts, "pool", None), "warm", None)
        if callable(warm):
            with contextlib.suppress(Exception):
                await warm()
        return clone_tts

    async def _bind_archive(self, runtime: DuplexRuntime) -> None:
        sink = self.archive_sink
        if sink is None:
            return

        async def publish(event: dict[str, Any]) -> None:
            await sink.publish(event)

        async def publish_owner_turn(
            event: dict[str, Any],
            pcm: bytes,
            sample_rate: int,
        ) -> None:
            await sink.publish_owner_turn(event, pcm=pcm, sample_rate=sample_rate)

        runtime.set_evidence_publisher(publish)
        runtime.set_owner_turn_publisher(publish_owner_turn)

    def _configure_runtime(self, runtime: DuplexRuntime, tts: Any) -> None:
        settings = self.settings
        runtime.cue_scheduler.min_speech_ms = settings.listener_cue_min_speech_ms
        runtime.cue_scheduler.pause_ms = settings.listener_cue_pause_ms
        runtime.cue_scheduler.cooldown_ms = settings.listener_cue_cooldown_ms
        runtime.cue_scheduler.max_per_turn = settings.listener_cue_max_per_turn
        runtime.set_listener_cue_aec_healthy(
            settings.listener_cue_playback == "main_track" or settings.listener_cue_aec_validated
        )
        trace = getattr(tts, "set_trace_callback", None)
        if callable(trace):
            trace(
                lambda name, status, detail: runtime.mark_audio_event(
                    name,
                    status=status,
                    detail=detail,
                )
            )
        runtime.mark_audio_event("agent_runtime_created")

    async def aclose(self) -> None:
        if self.archive_sink is not None:
            await self.archive_sink.close()


def build_production_media_session_factory(settings: Any) -> ProductionMediaSessionFactory:
    """Build the deployable StreamCore session factory from the established Agent stack."""

    from livekit.plugins import openai

    archive_sink: ArchiveSink | None = None
    if settings.archive_sink_enabled:
        token = settings.internal_token("archive_write")
        spool_key = settings.archive_spool_key.get_secret_value()
        if not token or not spool_key:
            raise RuntimeError("production media Agent requires archive credentials")
        archive_sink = ArchiveSink(
            ArchiveSinkConfig(
                endpoint=settings.archive_session_events_url,
                internal_token=token,
                spool_path=Path(settings.archive_spool_path),
                spool_key=spool_key,
                spool_max_bytes=settings.archive_spool_max_bytes,
            )
        )
    return ProductionMediaSessionFactory(
        settings=settings,
        llm_factory=openai.LLM,
        archive_sink=archive_sink,
    )


__all__ = ["ProductionMediaSessionFactory", "build_production_media_session_factory"]
