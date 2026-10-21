"""PR #378: a crisis plan that arrives in the background still wins the turn."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from services.agent.src.contracts.ids import CancellationContext, GenerationFence
from services.agent.src.duplex_runtime import DuplexRuntime
from services.agent.src.orchestration.handlers import LanguageModelRequest
from services.agent.src.providers.fixed_safety_audio import (
    DEFAULT_FIXED_SAFETY_AUDIO_DIR,
    FixedSafetyAudioCatalog,
)
from services.agent.src.reply_pipeline import ReplyPipeline
from services.agent.src.response_planner_client import (
    ResponsePlan,
    ResponsePlanFetch,
    ResponseProvenance,
    ResponseVoiceTarget,
)
from services.agent.tests.unit.runtime_profile_test_helpers import bind_owner_policy
from services.agent.tests.unit.runtime_state_helpers import bind_owner_speaker
from services.common.crisis_policy import route_crisis
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT, FixedSafetyScript

_REPO_ROOT = Path(__file__).resolve().parents[4]
_AUDIO_DIR = _REPO_ROOT / DEFAULT_FIXED_SAFETY_AUDIO_DIR
_LOCAL_MISS = "我撑不下去了"
_CRISIS_PCM_SHA256 = "f28af42f21ab1efc8c641c58bc4aba86cf9d7353812af6d3e90eac039f1fb545"


def _runtime(name: str) -> DuplexRuntime:
    runtime = DuplexRuntime.create(session_id=name)
    bind_owner_policy(
        runtime,
        policy_version="test-policy",
        private_context=True,
        owner_evidence=False,
        tools=False,
        voice_profile=False,
        memory_recall_grant=True,
    )
    bind_owner_speaker(runtime)
    return runtime


def _chat_plan(fence: GenerationFence) -> ResponsePlan:
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
            speaker_reason_code="owner_match",
            speaker_profile_id="owner",
            speaker_model_version="test",
            speaker_template_version=1,
            source_refs=(),
            epistemic_status="not_applicable",
            epistemic_reason_codes=("test",),
            disclosures=(),
        ),
    )


class _DelayedCrisisPlanner:
    """The legacy sidecar client: the plan is sent in the background."""

    echoes_session_epoch = False

    def __init__(
        self,
        *,
        delay_s: float,
        script: FixedSafetyScript | None = None,
        direct_text: str | None = None,
        start_after: asyncio.Event | None = None,
    ) -> None:
        self._delay_s = delay_s
        self._direct_text = script.display if script is not None else direct_text
        self._start_after = start_after
        self.calls = 0
        self.finished = asyncio.Event()

    async def fetch(self, **kwargs: Any) -> ResponsePlanFetch:
        self.calls += 1
        try:
            if self._start_after is not None:
                await self._start_after.wait()
            await asyncio.sleep(self._delay_s)
            plan = replace(_chat_plan(kwargs["fence"]), direct_text=self._direct_text)
            return ResponsePlanFetch(plan, "ok")
        finally:
            self.finished.set()


class _SlowChatModel:
    """One short normal sentence every ``delay_s``; closes when the pipeline stops."""

    def __init__(self, *, delay_s: float) -> None:
        self._delay_s = delay_s
        self.closed = asyncio.Event()
        self.first_emitted = asyncio.Event()
        self.emitted = 0

    @contextlib.asynccontextmanager
    async def chat(self, *, chat_ctx: Any, tools: list[Any]) -> AsyncIterator[Any]:
        del chat_ctx, tools

        async def tokens() -> AsyncIterator[str]:
            try:
                while True:
                    await asyncio.sleep(self._delay_s)
                    self.emitted += 1
                    yield f"普通句子{self.emitted}。"
                    if self.emitted == 1:
                        self.first_emitted.set()
            finally:
                self.closed.set()

        stream = tokens()
        try:
            yield stream
        finally:
            await stream.aclose()


def _pipeline(runtime: DuplexRuntime, planner: _DelayedCrisisPlanner) -> ReplyPipeline:
    pipeline = ReplyPipeline(
        instructions="系统提示",
        runtime=runtime,
        response_planner_client=planner,  # type: ignore[arg-type]
    )
    return pipeline


def _catalog() -> FixedSafetyAudioCatalog:
    return FixedSafetyAudioCatalog.load(_AUDIO_DIR)


@pytest.mark.asyncio
async def test_a_late_control_plan_stops_a_started_normal_reply_and_plays_the_script(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Local misses, the normal reply starts, then the crisis plan arrives after >80 ms."""

    caplog.set_level(logging.INFO)
    assert route_crisis(_LOCAL_MISS).script is None
    runtime = _runtime("pr378-late-crisis-mid-reply")
    model = _SlowChatModel(delay_s=0.02)
    planner = _DelayedCrisisPlanner(
        delay_s=0.12,
        script=CRISIS_SELF_SCRIPT,
        start_after=model.first_emitted,
    )
    pipeline = _pipeline(runtime, planner)
    pipeline.language_model = model

    fence = await pipeline.prepare_committed_turn(_LOCAL_MISS)
    cached = pipeline.response_plans.get(fence)
    assert cached is not None and cached.fixed_safety_script is None

    items = [
        item
        async for item in pipeline.stream(
            LanguageModelRequest(
                user_text=_LOCAL_MISS,
                cancellation=CancellationContext.capture(fence),
            )
        )
    ]

    normal = [item for item in items if isinstance(item, str)]
    scripts = [item for item in items if isinstance(item, FixedSafetyScript)]
    assert normal, "the ordinary reply must have started before the plan arrived"
    assert scripts == [CRISIS_SELF_SCRIPT]
    assert isinstance(items[-1], FixedSafetyScript)
    assert items[-1] is CRISIS_SELF_SCRIPT
    assert model.closed.is_set()
    assert runtime.fixed_safety_echo_text == CRISIS_SELF_SCRIPT.display

    await asyncio.wait_for(planner.finished.wait(), timeout=1.0)
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "late_crisis_plan hit=true category=crisis-self-v1" in message
        for message in messages
    )
    assert any("late_crisis_preempted category=crisis-self-v1" in message for message in messages)
    assert not any(_LOCAL_MISS in message for message in messages)

    clip = _catalog().match(CRISIS_SELF_SCRIPT.spoken)
    assert clip is not None and clip.playable
    assert clip.pcm_sha256 == _CRISIS_PCM_SHA256
    await runtime.close()


@pytest.mark.asyncio
async def test_a_crisis_plan_before_the_first_token_yields_only_the_prerecorded_script() -> None:
    """The late plan can arrive before any normal token; the clip is then the first item."""

    runtime = _runtime("pr378-late-crisis-first-item")
    planner = _DelayedCrisisPlanner(delay_s=0.01, script=CRISIS_SELF_SCRIPT)
    model = _SlowChatModel(delay_s=0.2)
    pipeline = _pipeline(runtime, planner)
    pipeline.language_model = model

    fence = await pipeline.prepare_committed_turn(_LOCAL_MISS)
    items = [
        item
        async for item in pipeline.stream(
            LanguageModelRequest(
                user_text=_LOCAL_MISS,
                cancellation=CancellationContext.capture(fence),
            )
        )
    ]

    assert items == [CRISIS_SELF_SCRIPT]
    assert model.emitted <= 1
    assert model.closed.is_set()
    await runtime.close()


@pytest.mark.asyncio
async def test_late_plan_that_is_not_crisis_keeps_the_normal_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A late ordinary plan is a miss and must not truncate the reply."""

    caplog.set_level(logging.INFO)
    runtime = _runtime("pr378-late-plan-miss")
    planner = _DelayedCrisisPlanner(delay_s=0.02, direct_text="普通回答。")
    model = _SlowChatModel(delay_s=0.01)
    pipeline = _pipeline(runtime, planner)
    pipeline.language_model = model
    fence = await pipeline.prepare_committed_turn(_LOCAL_MISS)
    items = [
        item
        async for item in pipeline.stream(
            LanguageModelRequest(
                user_text=_LOCAL_MISS,
                cancellation=CancellationContext.capture(fence),
            )
        )
    ]
    assert all(isinstance(item, str) for item in items)
    assert items
    assert any(
        "late_crisis_plan hit=false category=none" in record.getMessage()
        for record in caplog.records
    )
    await runtime.close()
