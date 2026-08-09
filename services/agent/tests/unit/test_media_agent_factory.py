from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.media_agent_factory import build_production_media_session_factory
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.orchestration.handlers import SpeechSynthesisRequest
from services.agent.src.response_planner_client import ResponsePlanFetch
from services.agent.src.voice_core.media_protocol import SessionIdentity


@pytest.mark.asyncio
async def test_production_media_factory_builds_one_policy_bound_agent_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent.src import media_agent_factory as factory_module

    closed: list[str] = []

    class PolicyClient:
        def __init__(self, _config: object) -> None:
            pass

        async def fetch(self, *, session_id: str) -> ModePolicy:
            assert session_id == "production-media"
            return ModePolicy.companion_for_test(
                policy_version="media-policy",
                private_context=False,
                owner_evidence=False,
                tools=False,
                voice_profile=False,
                shadow_low_sensitivity_persona=False,
            )

        async def aclose(self) -> None:
            closed.append("policy")

    class PlannerClient:
        def __init__(self, _config: object) -> None:
            pass

        async def fetch(self, **_kwargs: object) -> ResponsePlanFetch:
            return ResponsePlanFetch(None, "test_safe_fallback")

        async def aclose(self) -> None:
            closed.append("planner")

    class LanguageModelStream:
        async def __aenter__(self) -> LanguageModelStream:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        def __aiter__(self) -> AsyncIterator[str]:
            async def tokens() -> AsyncIterator[str]:
                yield "已按安全计划回答。"

            return tokens()

    class LanguageModel:
        def chat(self, **_kwargs: object) -> LanguageModelStream:
            return LanguageModelStream()

        async def aclose(self) -> None:
            closed.append("llm")

    class Resolver:
        async def resolve(self, *, query: str) -> str:
            return query

        async def aclose(self) -> None:
            closed.append("search")

    class TTS:
        current_voice_profile_id = "warm_companion"
        current_model = "seed-tts-2.0"
        current_voice = "zh_female_wanwanxiaohe_moon_bigtts"
        current_voice_kind = "designed"

        def __init__(self) -> None:
            self.pool = SimpleNamespace(warm=self.warm)

        async def warm(self) -> None:
            return None

        def bind_fence(self, _fence: GenerationFence) -> None:
            return None

        def apply_speech_plan(self, **_kwargs: object) -> None:
            return None

        async def synthesize(self, request: SpeechSynthesisRequest) -> Any:
            assert request.phrases
            return SimpleNamespace(pcm=b"\x01\x00" * 480)

        async def aclose(self) -> None:
            closed.append("tts")

    monkeypatch.setattr(factory_module, "ModePolicyClient", PolicyClient)
    monkeypatch.setattr(factory_module, "ResponsePlannerClient", PlannerClient)
    monkeypatch.setattr(
        factory_module,
        "build_language_model_handler",
        lambda **_kwargs: LanguageModel(),
    )
    monkeypatch.setattr(
        factory_module,
        "build_realtime_search_resolver",
        lambda **_kwargs: Resolver(),
    )
    monkeypatch.setattr(factory_module.DoubaoTTS, "from_env", classmethod(lambda cls: TTS()))
    monkeypatch.setattr(
        factory_module.FunASRConfig,
        "from_env",
        classmethod(
            lambda cls: factory_module.FunASRConfig(
                api_key="test",
                ws_url="wss://asr.example/ws",
            )
        ),
    )
    settings = SimpleNamespace(
        environment="production",
        interaction_policy_url="http://control-api:8000/v1/internal/interaction-policy",
        interaction_policy_timeout_s=0.4,
        response_plan_url="http://control-api:8000/v1/internal/response-plan",
        response_plan_timeout_s=0.8,
        speaker_verify_enabled=False,
        speaker_authority_enabled=False,
        speaker_enroll_speech_ms=4000,
        speaker_enroll_timeout_ms=12000,
        speaker_accept_threshold=0.78,
        speaker_min_verify_speech_ms=1200,
        listener_cues_enabled=True,
        listener_cue_playback="main_track",
        listener_cue_aec_validated=False,
        listener_cue_min_speech_ms=1800,
        listener_cue_pause_ms=250,
        listener_cue_cooldown_ms=5000,
        listener_cue_max_per_turn=2,
        archive_sink_enabled=False,
        archive_spool_key=SimpleNamespace(get_secret_value=lambda: ""),
        voice_profile_enabled=False,
        funasr_sample_rate=16000,
        llm_provider="bailian_deepseek",
        llm_fast_model="deepseek-v4-flash",
        llm_api_key="secret",
        llm_base_url="https://llm.example/v1",
        doubao_tts_resource_id="seed-tts-2.0",
        doubao_tts_sample_rate=24000,
        internal_token=lambda capability: {
            "interaction_policy": "p" * 32,
            "response_plan": "r" * 32,
            "archive_write": "",
            "voice_resolution": "",
        }[capability],
    )

    session_factory = build_production_media_session_factory(settings)
    identity = SessionIdentity("production-media", stream_epoch=1)
    resources = await session_factory(identity)

    assert resources.runtime.session_id == identity.session_id
    assert resources.runtime.mode_policy.available
    assert resources.runtime.mode_policy_enforced
    assert resources.provider.supports_delegation  # type: ignore[attr-defined]
    assert resources.provider.language_model.supports_delegation  # type: ignore[attr-defined]
    resources.runtime.set_delegation_starter(None)
    fence = await resources.provider.prepare_committed_turn(identity, "你好")  # type: ignore[attr-defined]
    chunks = [chunk async for chunk in resources.provider.generate_reply(identity, "你好", fence)]
    assert chunks and chunks[-1].final

    await resources.runtime.close()
    await resources.provider.close(identity)
    await session_factory.aclose()  # type: ignore[attr-defined]
    assert sorted(closed) == ["llm", "planner", "policy", "search", "tts"]


@pytest.mark.asyncio
async def test_production_media_factory_does_not_replay_archive_during_session_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent.src import media_agent_factory as factory_module

    replay_started = asyncio.Event()

    class Archive:
        async def replay(self) -> int:
            replay_started.set()
            await asyncio.Event().wait()
            return 0

        async def publish(self, _event: dict[str, Any]) -> bool:
            return True

        async def publish_owner_turn(
            self,
            _event: dict[str, Any],
            *,
            pcm: bytes,
            sample_rate: int,
        ) -> bool:
            return bool(pcm) and sample_rate > 0

    class Closeable:
        async def aclose(self) -> None:
            return None

    class Orchestrator:
        async def ready(self) -> None:
            return None

    class Runtime:
        session_id = "archive-session"
        orchestrator = Orchestrator()
        mode_policy = ModePolicy.companion_for_test(
            policy_version="archive-test-policy",
            private_context=False,
            owner_evidence=False,
            tools=False,
            voice_profile=False,
            shadow_low_sensitivity_persona=False,
            session_focus="chat",
        )

        def set_evidence_publisher(self, publisher: object) -> None:
            assert callable(publisher)

        def set_owner_turn_publisher(self, publisher: object) -> None:
            assert callable(publisher)

    class TTS:
        pool = None

        async def aclose(self) -> None:
            return None

    runtime = Runtime()
    monkeypatch.setattr(factory_module.DoubaoTTS, "from_env", classmethod(lambda cls: TTS()))
    monkeypatch.setattr(
        factory_module,
        "build_language_model_handler",
        lambda **_kwargs: Closeable(),
    )
    monkeypatch.setattr(
        factory_module.ProductionMediaSessionFactory,
        "_new_runtime",
        lambda self, session_id, tts: runtime,
    )

    async def bind_mode_policy(self: object, bound_runtime: object) -> Closeable:
        assert bound_runtime is runtime
        return Closeable()

    async def bind_voice_profile(
        self: object,
        bound_runtime: object,
        tts: object,
    ) -> None:
        assert bound_runtime is runtime

    monkeypatch.setattr(
        factory_module.ProductionMediaSessionFactory,
        "_bind_mode_policy",
        bind_mode_policy,
    )
    monkeypatch.setattr(
        factory_module.ProductionMediaSessionFactory,
        "_response_planner_client",
        lambda self: Closeable(),
    )
    monkeypatch.setattr(
        factory_module.ProductionMediaSessionFactory,
        "_bind_voice_profile",
        bind_voice_profile,
    )
    monkeypatch.setattr(
        factory_module.ProductionMediaSessionFactory,
        "_bind_speaker_authority",
        lambda self, bound_runtime: None,
    )
    monkeypatch.setattr(
        factory_module.ProductionMediaSessionFactory,
        "_configure_runtime",
        lambda self, bound_runtime, tts: None,
    )
    monkeypatch.setattr(factory_module, "DuplexVoiceAgent", lambda **_kwargs: object())
    monkeypatch.setattr(
        factory_module.FunASRConfig,
        "from_env",
        classmethod(lambda cls: SimpleNamespace(sample_rate=16000)),
    )
    monkeypatch.setattr(
        factory_module,
        "ExistingVoiceProviderAdapter",
        lambda **_kwargs: object(),
    )
    factory = factory_module.ProductionMediaSessionFactory(
        settings=SimpleNamespace(
            llm_provider="test",
            llm_fast_model="test",
            doubao_tts_resource_id="test",
            doubao_tts_sample_rate=24000,
        ),
        llm_factory=object(),
        archive_sink=Archive(),  # type: ignore[arg-type]
    )

    resources = await asyncio.wait_for(
        factory(SessionIdentity("archive-session", stream_epoch=1)),
        timeout=1.0,
    )

    assert resources.runtime is runtime
    assert not replay_started.is_set()
