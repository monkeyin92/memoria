"""Which TTS engine speaks a minor refusal, and which one speaks a crisis line."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.mode_policy_client import ModePolicy
from services.agent.src.orchestration.handlers import LanguageModelRequest
from services.agent.src.orchestration.state_machine import (
    ConversationState,
    InteractionPhase,
)
from services.agent.src.prompts import (
    MINOR_DAILY_LIMIT_PHRASE,
    MINOR_QUIET_HOURS_PHRASE,
)
from services.agent.src.providers.tts_failover import (
    StepFunPrimaryTTS,
    TTSFailoverBreaker,
)
from services.agent.src.runtime_profile import VerifiedRuntimeProfile
from services.agent.src.voice_core.aigc_notice import AigcNoticeConfig
from services.agent.src.voice_core.media_protocol import (
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.agent.tests.unit.media_session_support import _CapturingGenerationBridge
from services.agent.tests.unit.test_minor_session_limits import _ledger, _profile
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT

_FRAME = 480
_CHILD_CRISIS_TEXT = "我不想活了"


class _EngineStream:
    """One provider sentence stream that records the engine that received it."""

    def __init__(self, engine: str, heard: list[tuple[str, str]]) -> None:
        self._engine = engine
        self._heard = heard
        self._yielded = False

    def push_text(self, text: str) -> None:
        self._heard.append((self._engine, text))

    def end_input(self) -> None:
        return None

    def __aiter__(self) -> _EngineStream:
        return self

    async def __anext__(self) -> Any:
        if self._yielded:
            raise StopAsyncIteration
        self._yielded = True
        return SimpleNamespace(frame=SimpleNamespace(data=b"\x01\x00" * _FRAME, sample_rate=24_000))

    async def aclose(self) -> None:
        return None


class _Engine:
    """A vendor engine that only records text and yields one PCM frame."""

    def __init__(self, provider: str, model: str) -> None:
        self.provider = provider
        self.current_model = model
        self.model = model
        self.current_voice = f"{provider}-voice"
        self.heard: list[tuple[str, str]] = []

    def bind_fence(self, _fence: GenerationFence) -> None:
        return None

    def apply_speech_plan(self, **_kwargs: Any) -> None:
        return None

    def stream(self, *, conn_options: Any = None) -> _EngineStream:
        del conn_options
        return _EngineStream(self.provider, self.heard)

    async def aclose(self) -> None:
        return None

    @property
    def texts(self) -> list[str]:
        return [text for _engine, text in self.heard]


class _CrisisReply:
    def stream(self, _request: LanguageModelRequest) -> AsyncIterator[Any]:
        async def items() -> AsyncIterator[Any]:
            yield CRISIS_SELF_SCRIPT

        return items()


def _device_identity(session_id: str = "ses-limits") -> SessionIdentity:
    return SessionIdentity(
        session_id,
        account_id="account",
        device_id="dev-limits",
        client_type="device",
        subject_id="person-child",
        binding_id="bind-limits",
        binding_version=1,
        runtime_profile_version=1,
        audio_mode="interrupt_assist",
    )


def _mode_policy(profile: VerifiedRuntimeProfile) -> ModePolicy:
    return ModePolicy(
        mode="companion",
        policy_version="policy-v2",
        companion_style_id=None,
        style_version=None,
        references=(),
        capabilities=(("conversation", True),),
        companion_style=None,
        runtime_profile=profile,
    )


def _provider(speech: StepFunPrimaryTTS, llm: Any) -> ExistingVoiceProviderAdapter:
    return ExistingVoiceProviderAdapter(
        asr_session_factory=cast(Any, lambda: None),
        language_model=cast(Any, llm),
        speech_synthesis=cast(Any, speech),
    )


def _speech() -> tuple[StepFunPrimaryTTS, _Engine, _Engine]:
    primary = _Engine("stepfun", "stepaudio-3-tts")
    backup = _Engine("volcengine_doubao", "seed-tts-2.0")
    wrapper = StepFunPrimaryTTS(
        primary=primary,
        backup=backup,
        breaker=TTSFailoverBreaker(failures=3, open_s=30),
    )
    return wrapper, primary, backup


async def _wait_until(predicate: Any, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition was not reached before the timeout")


async def _finish_owner_playback(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    bridge: _CapturingGenerationBridge,
    session: Any,
) -> None:
    context = registry.session_state(identity.session_id)
    owner = context.output.output_owner
    assert owner is not None
    frame = bridge.frames[-1]
    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=owner.fence.generation_id,
            received_sequence=frame.sequence,
            rendered_sample_end=frame.source_start_sample + frame.frame_samples,
            client_monotonic_ms=1,
            approximate=False,
            turn_id=owner.fence.turn_id,
            tool_epoch=owner.fence.tool_epoch,
            session_epoch=owner.fence.session_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )


async def _refuse_on_a_real_session(
    trigger: Any,
    *,
    profile: VerifiedRuntimeProfile,
    monkeypatch: pytest.MonkeyPatch,
    ledger: httpx.Response | None = None,
) -> tuple[StepFunPrimaryTTS, _Engine, _Engine, Any]:
    """Run one refusal end to end and return the engines that actually spoke."""

    speech, primary, backup = _speech()
    provider = _provider(speech, object())
    bridge = _CapturingGenerationBridge()
    registry = MediaVoiceCoreRegistry(
        bridge=bridge,
        provider_factory=lambda _identity: provider,
        daily_chat_client=None if ledger is None else _ledger(ledger, []),
        aigc_notice_config=AigcNoticeConfig(enabled=False),
    )
    identity = _device_identity()

    async def no_wake_ack(_registry: Any, _context: Any) -> None:
        return None

    # open_session also speaks the wake greeting; this test is about refusals.
    monkeypatch.setattr(MediaVoiceCoreRegistry, "_speak_device_wake_ack", no_wake_ack)
    session = bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    context.runtime.set_mode_policy(_mode_policy(profile))
    await context.runtime.settle_bootstrap_identity_epoch()
    if context.runtime.orchestrator.state is ConversationState.CONNECTING:
        await context.runtime.orchestrator.ready()
        context.runtime.set_interaction_phase(InteractionPhase.LISTENING, cause="test_ready")
    try:
        refused = asyncio.create_task(trigger(registry, context))
        await _wait_until(lambda: context.output.provider_complete and bool(bridge.frames))
        await _finish_owner_playback(registry, identity, bridge, session)
        assert await asyncio.wait_for(refused, timeout=5) is True
        assert context.standby_requested
        assert context.closed
        assert context.runtime.orchestrator.state is not ConversationState.SPEAKING
        return speech, primary, backup, context
    finally:
        await registry.finalize_session(identity.session_id)


@pytest.mark.asyncio
async def test_quiet_hours_refusal_uses_bridge_acknowledgement_and_starts_on_stepfun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.agent.src.voice_core import media_session_projection

    monkeypatch.setattr(
        media_session_projection,
        "current_local_time",
        lambda _tz: datetime(2026, 9, 26, 22, 0),
    )

    _speech_wrapper, primary, backup, _context = await _refuse_on_a_real_session(
        lambda registry, context: registry._refuse_minor_quiet_hours(context),
        profile=_profile(),
        monkeypatch=monkeypatch,
    )

    # bridge_acknowledgement carries the phrase; _speak_allowlisted_bridge_phrase
    # renders it through speech_synthesis.stream(), which is tts_failover here.
    assert primary.texts == [MINOR_QUIET_HOURS_PHRASE]
    assert backup.texts == []


@pytest.mark.asyncio
async def test_daily_limit_refusal_uses_bridge_acknowledgement_and_starts_on_stepfun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _speech_wrapper, primary, backup, _context = await _refuse_on_a_real_session(
        lambda registry, context: registry._refuse_minor_daily_limit(context),
        profile=_profile(daily_chat_seconds="2700"),
        monkeypatch=monkeypatch,
        ledger=httpx.Response(200, json={"used_seconds": 2700}),
    )

    assert primary.texts == [MINOR_DAILY_LIMIT_PHRASE]
    assert backup.texts == []


@pytest.mark.asyncio
async def test_an_approved_crisis_script_still_skips_stepfun_for_doubao() -> None:
    speech, primary, backup = _speech()
    adapter = _provider(speech, _CrisisReply())
    identity = _device_identity("crisis-tts-route")
    fence = GenerationFence(identity.session_id, 1, 1, 0)

    chunks = [
        chunk
        async for chunk in adapter.generate_reply(identity, _CHILD_CRISIS_TEXT, fence)
    ]

    # An approved crisis line must never spend its opening budget on StepFun
    # moderation, so this is the one fixed copy that starts on Doubao.
    assert primary.texts == []
    assert backup.texts == [CRISIS_SELF_SCRIPT.spoken]
    assert any(chunk.pcm_s16le for chunk in chunks)
    assert "".join(chunk.assistant_text_delta for chunk in chunks) == CRISIS_SELF_SCRIPT.display
