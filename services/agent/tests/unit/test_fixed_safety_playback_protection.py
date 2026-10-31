"""A fixed safety script plays to completion even when the child says 「停」.

Field 2026-10-21 H01 cut crisis-self-v1 after 5.4 s, before 12356. Product and
compliance decided on 2026-10-22 00:40 that the fixed safety script is not
interruptible by voice, exactly like a head pat or screen tap (#355 changes
the earlier 2026-10-21 stop rule). A stop must not cut the line, and it must
not raise the parent's unfinished-script reminder; that reminder is reserved
for power loss, network loss and playback failure.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import (
    ResponsePlan,
    ResponsePlanFetch,
    ResponseProvenance,
    ResponseVoiceTarget,
)
from services.agent.src.voice_core.generated.memoria.media.v1 import media_pb2
from services.agent.src.voice_core.grpc_bridge import MediaBridgeGrpcServer
from services.agent.src.voice_core.media_protocol import SessionIdentity
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import (
    ScriptedChatModel,
    bind_owner_speaker,
)
from services.agent.tests.unit.test_media_session_golden import (
    _h5_identity,
    _Harness,
    _ScriptedProvider,
)
from services.agent.tests.unit.test_media_session_playback_stop import (
    _device_identity,
    _run_story,
    _StoryHarness,
)
from services.common.fixed_safety_scripts import (
    CRISIS_SELF_SCRIPT,
    SUPPORT_RELAY_SCRIPT,
    SUPPORT_VICTIM_SCRIPT,
    FixedSafetyScript,
)

_SCRIPTS = pytest.mark.parametrize(
    "script",
    [CRISIS_SELF_SCRIPT, SUPPORT_VICTIM_SCRIPT, SUPPORT_RELAY_SCRIPT],
    ids=lambda script: script.template_id,
)


async def _finish_script(harness: _StoryHarness) -> None:
    harness.story.release.set()
    await harness.settle()
    await harness.playback_ended()
    await harness.wait_for(lambda: not harness.runtime.assistant_speaking)


@_SCRIPTS
@pytest.mark.asyncio
async def test_device_stop_word_does_not_cut_the_crisis_script(
    script: FixedSafetyScript,
) -> None:
    """「停」 is ignored while the fixed safety script is audible."""

    async def scenario(harness: _StoryHarness) -> None:
        published: list[str] = []
        harness.registry.crisis_script_interrupt_publisher = published.append
        playing = await harness.start_story()
        assert harness.runtime.floor.fixed_safety_audible
        await harness.audio_frames(15, final_text="停")
        await harness.assert_story_still_playing(playing)
        assert published == []
        assert not harness.closed()

        await _finish_script(harness)
        await harness.settle()
        assert harness.cancel_effects() == []
        assert published == []

    await _run_story(
        _device_identity(f"safety-stop-{script.template_id}"),
        scenario,
        opening=script.display,
    )


@pytest.mark.asyncio
async def test_device_hard_stop_keyword_does_not_cut_the_crisis_script() -> None:
    """The device already flushed locally; the Edge generation must stay playable."""

    async def scenario(harness: _StoryHarness) -> None:
        playing = await harness.start_story()
        await harness.send(
            media_pb2.MediaToCore(
                keyword=media_pb2.KeywordEvent(
                    identity=harness.wire_identity,
                    keyword="停",
                    confidence=0.95,
                    start_sample=harness.sample,
                    end_sample=harness.sample + 320,
                    hard_stop=True,
                )
            )
        )
        await harness.assert_story_still_playing(playing)
        bridge_session = harness.bridge.bridge.get(harness.identity.session_id)
        assert bridge_session is not None
        assert bridge_session.generation_active
        assert bridge_session.fence.matches(playing)

        frames_before_release = harness.count("audio")
        await _finish_script(harness)
        await harness.settle()
        assert harness.count("audio") > frames_before_release
        assert harness.cancel_effects() == []

    await _run_story(
        _device_identity("safety-hard-stop"),
        scenario,
        opening=CRISIS_SELF_SCRIPT.display,
    )


@_SCRIPTS
@pytest.mark.asyncio
async def test_device_non_stop_turns_do_not_cut_a_fixed_safety_script(
    script: FixedSafetyScript,
) -> None:
    """Wait/completion turns and new questions keep #346 protection."""

    async def scenario(harness: _StoryHarness) -> None:
        playing = await harness.start_story()
        assert harness.runtime.floor.fixed_safety_audible
        for text in ("等一下", "好了", "我们下午几点出发"):
            await harness.audio_frames(15, final_text=text)
            await harness.assert_story_still_playing(playing)
        await _finish_script(harness)
        await harness.settle()
        assert harness.cancel_effects() == []
        assert harness.story.reply_calls == 1
        assert not harness.closed()

    await _run_story(
        _device_identity(f"safety-nonstop-{script.template_id}"),
        scenario,
        opening=script.display,
    )


@pytest.mark.asyncio
async def test_device_farewell_waits_for_the_crisis_script_to_end() -> None:
    async def scenario(harness: _StoryHarness) -> None:
        playing = await harness.start_story()
        await harness.audio_frames(15, final_text="再见")
        await harness.assert_story_still_playing(playing)
        assert not harness.closed()

        await _finish_script(harness)
        assert harness.cancel_effects() == []

    await _run_story(
        _device_identity("safety-farewell"),
        scenario,
        opening=CRISIS_SELF_SCRIPT.display,
    )


@pytest.mark.asyncio
async def test_h5_spoken_stop_does_not_cut_the_crisis_script() -> None:
    async def scenario(harness: _StoryHarness) -> None:
        playing = await harness.start_story()
        await harness.utterance("停一下", frames=15)
        await harness.assert_story_still_playing(playing)

        await _finish_script(harness)
        await harness.settle()
        assert harness.cancel_effects() == []
        assert harness.story.reply_calls == 1

    await _run_story(
        _h5_identity("h5-safety-stop"),
        scenario,
        opening=CRISIS_SELF_SCRIPT.display,
    )


@pytest.mark.asyncio
async def test_device_stop_word_still_stops_an_ordinary_story() -> None:
    async def scenario(harness: _StoryHarness) -> None:
        playing = await harness.start_story()
        assert not harness.runtime.floor.fixed_safety_audible
        await harness.audio_frames(15, final_text="停")
        await harness.wait_for(lambda: bool(harness.cancel_effects()))
        harness.assert_story_stopped_session_open(playing)

    await _run_story(_device_identity("ordinary-story-stop"), scenario)


def _speaking_runtime(text: str) -> DuplexRuntime:
    runtime = DuplexRuntime.create(session_id="safety-floor")
    runtime._voice_floor.update(pending_assistant_text=text, assistant_speaking=True)
    return runtime


@_SCRIPTS
def test_fixed_safety_playback_blocks_barge_in_while_audible(script: FixedSafetyScript) -> None:
    runtime = _speaking_runtime(script.display)

    assert runtime.floor.fixed_safety_audible
    assert runtime.playback_overlap_input_blocked()


def test_first_phrase_of_the_crisis_script_is_already_protected() -> None:
    runtime = _speaking_runtime(CRISIS_SELF_SCRIPT.phrases[0].display)

    assert runtime.floor.fixed_safety_audible


def test_ordinary_reply_keeps_barge_in() -> None:
    runtime = _speaking_runtime("从前有一座山，山里有一座庙。")

    assert not runtime.floor.fixed_safety_audible
    assert not runtime.playback_overlap_input_blocked()


def test_protection_ends_when_the_script_is_no_longer_audible() -> None:
    runtime = _speaking_runtime(CRISIS_SELF_SCRIPT.display)
    runtime._voice_floor.update(pending_assistant_text="", assistant_speaking=False)

    assert not runtime.floor.fixed_safety_audible
    assert not runtime.playback_overlap_input_blocked()


_PLAYBACK_TEXT = {
    CRISIS_SELF_SCRIPT.template_id: "我不想活了",
    SUPPORT_VICTIM_SCRIPT.template_id: "有人正在打我",
    SUPPORT_RELAY_SCRIPT.template_id: "我朋友说他不想活了",
}
_ORDINARY_TEXT = "你好呀"
_ORDINARY_REPLY = "好的，我记住了。"


class _CompanionPlanner:
    """An ordinary plan; the deterministic crisis route decides a script locally."""

    echoes_session_epoch = True

    async def fetch(self, **kwargs: Any) -> ResponsePlanFetch:
        return ResponsePlanFetch(_companion_plan(kwargs["fence"]), "ok")


def _companion_plan(fence: GenerationFence) -> ResponsePlan:
    return ResponsePlan(
        fence=fence,
        instructions="系统提示",
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
            speaker_reason_code="owner_match",
            speaker_profile_id="owner-profile",
            speaker_model_version="test",
            speaker_template_version=None,
            source_refs=(),
            epistemic_status="not_applicable",
            epistemic_reason_codes=("test",),
            disclosures=(),
        ),
    )


class _PipelineLanguageModel:
    """The adapter seam that drives the real pipeline for commit and stream."""

    supports_delegation = False

    def __init__(self, pipeline: ReplyPipeline) -> None:
        self.pipeline = pipeline

    async def prepare_committed_turn(self, text: str) -> GenerationFence:
        return await self.pipeline.prepare_committed_turn(text)

    def stream(self, request: Any) -> AsyncIterator[Any]:
        return self.pipeline.stream(request)

    def fixed_safety_planned(self, fence: GenerationFence) -> bool:
        return self.pipeline.fixed_safety_planned(fence)

    @staticmethod
    def accept_output_intent(intent: Any) -> Any:
        return intent


class _LiveSpeech:
    """A live voice that yields silence; it never speaks a fixed safety script."""

    fixed_safety_catalog = None

    def __init__(self) -> None:
        self.spoken: list[str] = []

    def stream(self, **kwargs: Any) -> Any:
        self.spoken.append(str(kwargs.get("text", "")))

        async def pcm() -> AsyncIterator[bytes]:
            yield b"\x00\x00" * 480

        return pcm()


class _PlannedScriptProvider(_ScriptedProvider):
    """A wire harness whose turn preparation and plan come from the real pipeline."""

    def __init__(self) -> None:
        super().__init__()
        self.pipeline: ReplyPipeline | None = None

    def attach(self, runtime: DuplexRuntime) -> ReplyPipeline:
        bind_owner_policy(runtime)
        bind_owner_speaker(runtime)
        pipeline = ReplyPipeline(
            instructions="系统提示",
            runtime=runtime,
            response_planner_client=cast(Any, _CompanionPlanner()),
            tts_model="seed-tts-2.0",
        )

        async def tokens(_ctx: Any, _tools: list[Any]) -> AsyncIterator[str]:
            yield _ORDINARY_REPLY

        pipeline.language_model = ScriptedChatModel(lambda ctx, tools: tokens(ctx, tools))
        runtime.set_response_plan_rebinder(pipeline.rebind_response_plan)
        self.pipeline = pipeline
        return pipeline

    async def prepare_committed_turn(
        self,
        _identity: SessionIdentity,
        text: str,
    ) -> GenerationFence:
        assert self.pipeline is not None
        return await self.pipeline.prepare_committed_turn(text)

    def fixed_safety_planned(self, fence: GenerationFence) -> bool:
        assert self.pipeline is not None
        return self.pipeline.fixed_safety_planned(fence)


class _StartBridge(MediaBridgeGrpcServer):
    """Records each generation start's reason and protected flag."""

    def __init__(self) -> None:
        super().__init__()
        self.starts: list[tuple[str, bool]] = []

    async def emit_generation(
        self,
        session_id: str,
        fence: object,
        *,
        action: int,
        reason: str = "",
        protected_playback: bool = False,
        **kwargs: object,
    ) -> bool:
        if action == media_pb2.GENERATION_ACTION_START:
            self.starts.append((reason, protected_playback))
        return await super().emit_generation(
            session_id,
            cast(GenerationFence, fence),
            action=action,
            reason=reason,
            protected_playback=protected_playback,
            **kwargs,
        )


def _wire_session(identity: SessionIdentity) -> tuple[_Harness, _StartBridge]:
    provider = _PlannedScriptProvider()
    harness = _Harness(identity, provider)
    provider.attach(harness.runtime)
    harness.registry.bridge = _StartBridge()
    return harness, harness.registry.bridge


def _commit_starts(bridge: _StartBridge) -> list[tuple[str, bool]]:
    return [start for start in bridge.starts if start[0] == "user_turn_committed"]


@_SCRIPTS
@pytest.mark.asyncio
async def test_committed_fixed_safety_turn_starts_protected_playback(
    script: FixedSafetyScript,
) -> None:
    """The device learns the reply must not be cut before any of its audio."""

    identity = _device_identity(f"protected-start-{script.template_id}")
    harness, bridge = _wire_session(identity)
    await harness.start()
    try:
        await harness.utterance(_PLAYBACK_TEXT[script.template_id], frames=25)
        await harness.wait_for(lambda: bool(_commit_starts(bridge)), timeout=4.0)
        assert _commit_starts(bridge) == [("user_turn_committed", True)]
    finally:
        await harness.close()


@pytest.mark.asyncio
async def test_ordinary_committed_turn_starts_interruptible() -> None:
    """Every reply that is not a fixed safety script keeps today's behaviour."""

    identity = _device_identity("ordinary-start")
    harness, bridge = _wire_session(identity)
    await harness.start()
    try:
        await harness.utterance(_ORDINARY_TEXT, frames=25)
        await harness.wait_for(lambda: bool(_commit_starts(bridge)), timeout=4.0)
        assert _commit_starts(bridge) == [("user_turn_committed", False)]
    finally:
        await harness.close()

@_SCRIPTS
@pytest.mark.asyncio
async def test_output_generation_start_carries_the_planned_protection(
    script: FixedSafetyScript,
) -> None:
    """A late transport start for the same turn is protected too.

    The speech lane starts the generation itself when the turn commit's START
    lost the race to its first frame, so that path must carry the same flag.
    """

    identity = _device_identity(f"late-start-{script.template_id}")
    harness, bridge = _wire_session(identity)
    await harness.start()
    try:
        context = await harness.registry.open_session(identity)
        provider = cast(Any, context.provider)
        fence = await provider.prepare_committed_turn(identity, _PLAYBACK_TEXT[script.template_id])
        connection = bridge._open_connection(identity)
        connection.session.generation_active = False

        assert await harness.registry._ensure_transport_generation_started(context, fence)

        assert [start for start in bridge.starts if start[0] == "output_generation_start"] == [
            ("output_generation_start", True)
        ]
    finally:
        await harness.close()
