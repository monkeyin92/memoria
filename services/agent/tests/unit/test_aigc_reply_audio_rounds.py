"""Consecutive device reply rounds through the real pipeline and plan cache.

The regression is the 2026-10-22 field trace: the opening AIGC cue was admitted
as its own output intent on the reply's fence, took the output owner, skipped
the reply as ``output_intent_not_selected``, and then promoted the reply to a
new generation whose response plan did not exist. The child heard only tones.

These rounds use the real ``MediaVoiceCoreRegistry``, ``ReplyPipeline`` and
``ResponsePlanCache``. Only the network providers are doubled: the StepFun and
Doubao TTS engines and the chat model.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.providers.fixed_safety_audio import (
    load_fixed_safety_audio_catalog,
)
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import (
    ResponsePlan,
    ResponsePlanFetch,
    ResponseProvenance,
    ResponseVoiceTarget,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.media_protocol import (
    PlaybackEventType,
    PlaybackProgress,
    SessionIdentity,
)
from services.agent.src.voice_core.media_session import MediaVoiceCoreRegistry
from services.agent.src.voice_core.provider_adapter import ExistingVoiceProviderAdapter
from services.agent.tests.unit.media_session_support import (
    _CapturingGenerationBridge,
    _device_identity,
    _wait_until,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import (
    ScriptedChatModel,
    bind_owner_speaker,
)
from services.agent.tests.unit.test_minor_session_limits import _ledger
from services.agent.tests.unit.test_tts_failover_aigc_rounds import (
    _NORMAL_REPLY,
    _notice_config,
    _speech,
)
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT

_USER_TEXT = "你好呀"
_CRISIS_TEXT = "我想伤害自己。"
_DEFAULT_FIXED_SAFETY_DIR = "infra/voices/fixed_safety"


def _companion_plan(fence: GenerationFence) -> ResponsePlan:
    return ResponsePlan(
        fence=fence,
        instructions="按当前控制计划自然回答。",
        direct_text=None,
        epistemic_status="not_applicable",
        epistemic_reason_codes=("test",),
        grounded_items=(),
        disclosures=(),
        voice_target=ResponseVoiceTarget(
            kind="companion",
            profile_id="warm_companion",
            model="seed-tts-2.0",
        ),
        provenance=ResponseProvenance(
            planner_policy_version="digital-self-response-planner-v2",
            interaction_mode="companion",
            mode_policy_version="test-policy",
            digital_self_version_id=None,
            manifest_sha256=None,
            persona_version_id=None,
            persona_version_number=None,
            persona_style_only=False,
            relationship_profile_id=None,
            relationship_profile_version=None,
            speaker_class="owner",
            speaker_reason_code="authenticated_text_input",
            speaker_profile_id=None,
            speaker_model_version="account-auth-v1",
            speaker_template_version=None,
            source_refs=(),
            epistemic_status="not_applicable",
            epistemic_reason_codes=("test",),
            disclosures=(),
        ),
    )


class _PlanPlanner:
    echoes_session_epoch = True

    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self, **kwargs: Any) -> ResponsePlanFetch:
        self.calls += 1
        return ResponsePlanFetch(_companion_plan(kwargs["fence"]), "ok")


class _PipelineLanguageModel:
    """Adapter seam that drives the real ReplyPipeline for commit and stream."""

    supports_delegation = False

    def __init__(self, pipeline: ReplyPipeline) -> None:
        self.pipeline = pipeline

    async def prepare_committed_turn(self, text: str) -> GenerationFence:
        return await self.pipeline.prepare_committed_turn(text)

    def stream(self, request: Any) -> AsyncIterator[Any]:
        return self.pipeline.stream(request)

    @staticmethod
    def accept_output_intent(intent: Any) -> Any:
        return intent


class _RoundBridge(_CapturingGenerationBridge):
    pass


def _audio_bytes(frames: list[Any]) -> bytes:
    return b"".join(bytes(frame.pcm_s16le) for frame in frames)


def _frames_between(
    registry: MediaVoiceCoreRegistry,
    *,
    start: int,
    end: int,
) -> list[Any]:
    return list(registry.bridge.frames[start:end])


async def _playback(
    registry: MediaVoiceCoreRegistry,
    identity: SessionIdentity,
    session: Any,
    fence: GenerationFence,
) -> None:
    frame = next(
        frame
        for frame in reversed(registry.bridge.frames)
        if frame.generation_id == fence.generation_id and frame.turn_id == fence.turn_id
    )
    await registry.on_playback_progress(
        session,
        PlaybackProgress(
            identity=identity,
            generation_id=fence.generation_id,
            received_sequence=frame.sequence,
            rendered_sample_end=frame.source_start_sample + frame.frame_samples,
            client_monotonic_ms=1,
            approximate=False,
            turn_id=fence.turn_id,
            tool_epoch=fence.tool_epoch,
            session_epoch=fence.session_epoch,
            event_type=PlaybackEventType.ENDED,
        ),
    )


@pytest.mark.asyncio
async def test_four_consecutive_rounds_keep_reply_audio_on_its_own_plan(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    notice = _notice_config()
    speech, primary, backup = _speech("audio", "audio")
    catalog = load_fixed_safety_audio_catalog(_DEFAULT_FIXED_SAFETY_DIR)
    assert catalog.get("crisis-self-v1") is not None
    # The real approved catalog is the crisis source; no live TTS may speak it.
    cast(Any, speech).fixed_safety_catalog = catalog

    planner = _PlanPlanner()
    runtimes: dict[str, DuplexRuntime] = {}

    def runtime_factory(session_id: str) -> DuplexRuntime:
        runtime = DuplexRuntime.create(session_id=session_id)
        bind_owner_policy(runtime)
        bind_owner_speaker(runtime)
        runtimes[session_id] = runtime
        return runtime

    def provider_factory(identity: SessionIdentity) -> ExistingVoiceProviderAdapter:
        runtime = runtimes[identity.session_id]
        pipeline = ReplyPipeline(
            instructions="系统提示",
            runtime=runtime,
            response_planner_client=cast(Any, planner),
            tts_model="seed-tts-2.0",
        )

        async def tokens(_ctx: Any, _tools: list[Any]) -> AsyncIterator[str]:
            yield _NORMAL_REPLY

        pipeline.language_model = ScriptedChatModel(lambda ctx, tools: tokens(ctx, tools))
        runtime.set_response_plan_rebinder(pipeline.rebind_response_plan)
        adapters[identity.session_id] = pipeline
        return ExistingVoiceProviderAdapter(
            asr_session_factory=cast(Any, lambda: None),
            language_model=_PipelineLanguageModel(pipeline),
            speech_synthesis=cast(Any, speech),
        )

    adapters: dict[str, ReplyPipeline] = {}
    registry = MediaVoiceCoreRegistry(
        bridge=_RoundBridge(),
        provider_factory=provider_factory,
        runtime_factory=runtime_factory,
        aigc_notice_config=notice,
        aigc_round_logger=None,
        daily_chat_client=_ledger(httpx.Response(200, json={"used_seconds": 0}), []),
        owner_silence_timeout_s=100,
        speaking_stall_timeout_s=0.3,
    )
    registry.install()
    identity = _device_identity("aigc-reply-plan-rounds")

    async def no_wake_ack(_registry: Any, _context: Any) -> None:
        return None

    monkeypatch.setattr(MediaVoiceCoreRegistry, "_speak_device_wake_ack", no_wake_ack)
    session = registry.bridge.bridge.open(identity)
    context = await registry.open_session(identity)
    if context.runtime.orchestrator.state.name == "CONNECTING":
        await context.runtime.orchestrator.ready()
    pipeline = adapters[identity.session_id]

    async def run_round(
        text: str,
        *,
        stop: bool = False,
        expected_engine_count: int = 1,
        spoken: str = _NORMAL_REPLY,
    ) -> tuple[GenerationFence, list[Any], bytes]:
        if context.runtime.orchestrator.state.name == "CONNECTING":
            await context.runtime.orchestrator.ready()
        await registry._wait_for_output_idle(context, timeout_s=4.0)
        start = len(registry.bridge.frames)
        fence = await context.provider.prepare_committed_turn(identity, text)  # type: ignore[attr-defined]
        context.output.begin_turn_output(fence)
        reply_task = asyncio.create_task(
            registry.generate_reply(identity.session_id, text, fence)
        )
        context.output.reply_task = reply_task
        engine_before = len(primary.received)
        if expected_engine_count:
            await _wait_until(
                lambda: len(primary.received) >= engine_before + expected_engine_count,
                timeout=5.0,
            )
        if stop:
            await _wait_until(
                lambda: context.output.output_owner is not None
                and int(context.output.output_owner.intent.kind)
                != int(media_pb2.OUTPUT_INTENT_KIND_FAST_ACKNOWLEDGEMENT),
                timeout=3.0,
            )
            marker_frames = (
                len(notice.tone_pcm())
                + int(getattr(context.provider, "output_frame_samples", 480)) * 2
                - 1
            ) // (int(getattr(context.provider, "output_frame_samples", 480)) * 2)
            await _wait_until(
                lambda: sum(
                    1
                    for frame in registry.bridge.frames
                    if frame.generation_id == fence.generation_id
                    and frame.turn_id == fence.turn_id
                )
                > marker_frames,
                timeout=3.0,
            )
            await registry._cancel_reply_task(context, fence, reason="voice_stop_command")
            registry._schedule_aigc_round_closing(context, close_reason="voice_stop_command")
        else:
            await _wait_until(lambda: context.output.provider_complete, timeout=25.0)
            await _playback(registry, identity, session, fence)
            await _wait_until(
                lambda: context.aigc_closing_marker_state == "done",
                timeout=5.0,
            )
        await asyncio.gather(reply_task, return_exceptions=True)
        await _wait_until(lambda: context.output.output_owner is None, timeout=5.0)
        await _wait_until(lambda: context.aigc_closing_marker_state == "done", timeout=5.0)
        end = len(registry.bridge.frames)
        frames = _frames_between(registry, start=start, end=end)
        turn_frames = [
            frame
            for frame in frames
            if frame.generation_id == fence.generation_id and frame.turn_id == fence.turn_id
        ]
        closing_frames = [
            frame
            for frame in frames
            if frame.generation_id != fence.generation_id or frame.turn_id != fence.turn_id
        ]
        frame_bytes = int(getattr(context.provider, "output_frame_samples", 480)) * 2
        marker_audio = notice.tone_pcm()
        # The cue is padded to a whole provider frame, as every PCM frame is.
        marker_audio += b"\x00" * (-len(marker_audio) % frame_bytes)
        turn_audio = _audio_bytes(turn_frames)
        assert turn_audio.startswith(notice.tone_pcm()), "opening cue must lead the reply"
        if stop:
            # A stopped round cannot append to the cancelled generation, so the
            # cue owns a successor generation and must end it explicitly for
            # the firmware to leave SPEAKING.
            assert closing_frames, "stopped round must still play its closing cue"
            assert _audio_bytes(closing_frames).startswith(notice.tone_pcm())
            first_turn = frames.index(turn_frames[0])
            first_closing = frames.index(closing_frames[0])
            assert first_turn < first_closing, "order must be reply (opening head) then closing"
            assert closing_frames[0].generation_id in {
                complete.generation_id for complete in registry.bridge.generation_completes
            }, "standalone closing generation must send generation.completed"
            reply_audio = turn_audio[len(notice.tone_pcm()) :]
        else:
            # The closing cue rides the round's own generation, so the reply
            # and the cue share one fence and no successor is left open.
            assert not closing_frames, "closing cue must ride the reply's generation"
            assert turn_audio.endswith(marker_audio), "closing cue must follow the reply"
            reply_audio = turn_audio[len(marker_audio) : -len(marker_audio)]
        assert reply_audio
        return fence, frames, reply_audio

    try:
        # The wake cue is its own wire-legal turn; retire it before round 1.
        await _wait_until(lambda: context.aigc_closing_marker_state != "playing", timeout=3.0)
        await registry._close_aigc_round(context, close_reason="wake_ack")
        await registry._wait_for_output_idle(context, timeout_s=4.0)

        # Round 1: normal.
        fence, _frames, reply_audio = await run_round(_USER_TEXT)
        assert pipeline.response_plans.get(fence) is not None
        assert b"\x01\x00" in reply_audio
        assert primary.received.count(_NORMAL_REPLY) >= 1

        # Round 2: stopped after the reply reached the engine.
        primary.behavior = "hold"
        primary.release.clear()
        fence, _frames, reply_audio = await run_round(
            _USER_TEXT,
            stop=True,
        )
        assert b"\x01\x00" in reply_audio
        assert primary.received.count(_NORMAL_REPLY) >= 2
        primary.release.set()
        assert pipeline.response_plans.get(fence) is not None

        # Round 3: StepFun fails before audio, Doubao serves the reply.
        primary.behavior = "connection"
        fence, _frames, reply_audio = await run_round(_USER_TEXT)
        assert b"\x01\x00" in reply_audio
        assert _NORMAL_REPLY in backup.received
        assert pipeline.response_plans.get(fence) is not None

        # Round 4: crisis. The approved prerecorded clip plays, no live voice.
        primary.behavior = "audio"
        primary.received.clear()
        backup.received.clear()
        fence, frames, reply_audio = await run_round(_CRISIS_TEXT, expected_engine_count=0, spoken="")
        crisis_audio = catalog.get("crisis-self-v1")
        assert crisis_audio is not None and crisis_audio.pcm
        assert bytes(crisis_audio.pcm) in reply_audio
        assert primary.received == []

        text = "\n".join(record.message for record in caplog.records)
        assert "output_intent_not_selected" not in text
        assert "blocked without exact response plan" not in text
        assert planner.calls >= 4
    finally:
        await registry.finalize_session(identity.session_id)


def test_rebinding_reply_work_moves_its_response_plan_to_the_new_fence() -> None:
    """The internal handoff must never leave a plan on the previous fence."""

    import time
    from dataclasses import replace as dataclass_replace
    from types import SimpleNamespace

    from services.agent.src.voice_core.media_session_output_owner import (
        MediaOutputOwnerMixin,
    )
    from services.agent.src.voice_core.media_session_types import OutputWork

    old = GenerationFence("plan-rebind", turn_id=2, generation_id=3, tool_epoch=0)
    new = dataclass_replace(old, generation_id=4)
    intent = media_pb2.OutputIntent(
        intent_id="intent-rebind",
        session_id=old.session_id,
        turn_id=old.turn_id,
        generation_id=old.generation_id,
        tool_epoch=old.tool_epoch,
        expires_at_ms=int(time.time() * 1_000) + 10_000,
    )
    calls: list[tuple[GenerationFence, GenerationFence]] = []
    context = SimpleNamespace(
        runtime=SimpleNamespace(
            rebind_response_plan=lambda previous, current: calls.append((previous, current))
        )
    )
    work = OutputWork(
        intent,
        old,
        conversation_text="回答",
        lead_pcm_s16le=b"\x00\x00" * 4,
        tail_pcm_s16le=b"\x04\x00" * 4,
    )

    rebound = MediaOutputOwnerMixin._rebind_output_work(
        context,  # type: ignore[arg-type]
        work,
        new,
        context_version=7,
    )

    assert rebound is not None
    assert calls == [(old, new)]
    assert rebound.fence == new
    assert rebound.lead_pcm_s16le == work.lead_pcm_s16le
    assert rebound.tail_pcm_s16le == work.tail_pcm_s16le
    assert rebound.conversation_text == work.conversation_text


def test_crisis_lookup_follows_the_turn_across_a_generation_bump() -> None:
    """A prerecorded crisis script survives an internal reply-generation move."""

    from dataclasses import replace as dataclass_replace
    from types import SimpleNamespace

    from services.agent.src.response_plan_cache import ResponsePlanCache

    old = GenerationFence("crisis-rebind", turn_id=2, generation_id=3, tool_epoch=0)
    new = dataclass_replace(old, generation_id=4)
    cache = ResponsePlanCache()
    plan = dataclass_replace(
        _companion_plan(old),
        direct_text=CRISIS_SELF_SCRIPT.display,
    )
    cache.store(plan)

    assert cache.get(new) is None
    assert cache.same_turn(new) == (plan,)

    pipeline = SimpleNamespace(
        _response_plans=cache,
        _late_crisis={},
        _runtime=SimpleNamespace(session_id=old.session_id),
    )
    script = ReplyPipeline._fixed_safety_script_for_turn(pipeline, new)  # type: ignore[arg-type]
    assert script == CRISIS_SELF_SCRIPT


def test_same_turn_plans_are_ordered_by_generation_not_insertion() -> None:
    """A plan stored late for an older generation must not read as the latest."""

    from dataclasses import replace as dataclass_replace

    from services.agent.src.response_plan_cache import ResponsePlanCache

    newer_fence = GenerationFence("plan-order", turn_id=2, generation_id=6, tool_epoch=0)
    older_fence = dataclass_replace(newer_fence, generation_id=4)
    newer = _companion_plan(newer_fence)
    older = _companion_plan(older_fence)
    cache = ResponsePlanCache()
    cache.store(newer)
    cache.store(older)

    assert [plan.fence.generation_id for plan in cache.same_turn(older_fence)] == [4, 6]
    assert cache.same_turn(older_fence)[-1] is newer
