from __future__ import annotations

import asyncio

import pytest
from services.agent.src.contracts.events import TimedWord
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.context_manager import ContextManager, redact_pii
from services.agent.src.orchestration.heard_text_tracker import HeardTextTracker
from services.agent.src.orchestration.interruption_guard import (
    ChineseInterruptionGuard,
    InterruptDecision,
    count_cjk_chars,
    is_backchannel,
)
from services.agent.src.orchestration.orchestrator import (
    OfflinePipeline,
    Orchestrator,
    PlaybackController,
    cancel_and_wait,
)
from services.agent.src.orchestration.phrase_segmenter import (
    PhraseSegmenter,
    _unbalanced_quotes_or_brackets,
    normalize_for_tts,
)
from services.agent.src.orchestration.prosody import (
    ProsodyController,
    ProsodyFeatures,
    SpeakingStyle,
)
from services.agent.src.orchestration.stable_prefix import (
    StablePrefixTracker,
    has_complete_english_word_delta,
    longest_common_prefix,
    snap_to_boundary,
)
from services.agent.src.orchestration.state_machine import ConversationState
from services.agent.src.orchestration.task_manager import (
    TaskManager,
    ToolSpec,
    new_idempotency_key,
    spoken_result_summarizer,
)


def _fence() -> GenerationFence:
    return GenerationFence("s", 1, 1, 0)


def test_context_redaction_summary_trim_and_asr_items() -> None:
    text = (
        "13800138000 11010519491231002X 6222021234567890123 a@b.com "
        "sk_abcdefghijklmnop 北京市朝阳区建国路88号"
    )
    assert all(
        marker in redact_pii(text)
        for marker in ("[手机号]", "[身份证]", "[银行卡]", "[邮箱]", "[密钥]", "[地址]")
    )
    ctx = ContextManager(system_prompt="system", business_summary="业" * 700, max_turns=2)
    ctx.commit_assistant_heard("  ")
    ctx.add_user("旧消息")
    ctx.commit_interrupted_assistant_text("旧回复")
    ctx.add_user("新消息")
    ctx.commit_assistant_heard("新回复")
    messages = ctx.build_messages(current_user_final="13800138000", tools=[{}])
    assert messages[1]["content"] == "当前业务状态摘要：" + "业" * 600
    assert [message["content"] for message in messages[-3:]] == [
        "新消息",
        "新回复",
        "[手机号]",
    ]
    assert ctx.asr_context_items(max_each=1, max_chars=1) == [
        {"role": "user", "text": "新"},
        {"role": "assistant", "text": "新"},
    ]


def test_heard_tracker_edge_fallbacks() -> None:
    tracker = HeardTextTracker()
    assert tracker.heard_audio_ms() == 0
    tracker.mark_playback_started(2_000_000_000)
    assert tracker.heard_audio_ms() == 0
    assert tracker.snapshot() == ""
    tracker.add_words([TimedWord(text="甲", begin_ms=0, end_ms=100)])
    tracker.alignment_degraded = True
    assert tracker.snapshot() == ""
    tracker.set_full_text("甲乙，丙丁")
    tracker.mark_playback_stopped(2_400_000_000)
    assert tracker.snapshot() == "甲乙，"
    tracker.full_text = "没有标点"
    tracker.words.clear()
    tracker.mark_playback_stopped(2_240_000_000)
    assert tracker.snapshot() == "没有"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"elapsed_ms": 40, "asr_text": "", "has_speech_energy": True}, InterruptDecision.DUCK),
        (
            {
                "elapsed_ms": 100,
                "asr_text": "",
                "has_speech_energy": False,
                "looks_like_noise_only": True,
            },
            InterruptDecision.RESUME,
        ),
        (
            {"elapsed_ms": 200, "asr_text": "这是完整问题", "has_speech_energy": True},
            InterruptDecision.CONFIRM_INTERRUPT,
        ),
        (
            {"elapsed_ms": 200, "asr_text": "什么？", "has_speech_energy": True},
            InterruptDecision.CONFIRM_INTERRUPT,
        ),
        (
            {"elapsed_ms": 1200, "asr_text": "", "has_speech_energy": False},
            InterruptDecision.FALSE_INTERRUPTION,
        ),
        (
            {"elapsed_ms": 500, "asr_text": "也许", "has_speech_energy": True},
            InterruptDecision.UNCERTAIN,
        ),
    ],
)
def test_interruption_guard_edges(kwargs: dict[str, object], expected: InterruptDecision) -> None:
    guard = ChineseInterruptionGuard()
    assert guard.evaluate(**kwargs) is expected  # type: ignore[arg-type]


def test_interruption_helpers_and_resolution() -> None:
    guard = ChineseInterruptionGuard()
    assert guard.on_user_voice_while_speaking() is InterruptDecision.DUCK
    assert guard.evaluate(elapsed_ms=10, asr_text="停一下", has_speech_energy=True) is (
        InterruptDecision.CONFIRM_INTERRUPT
    )
    assert is_backchannel("嗯", duration_ms=901) is False
    assert is_backchannel("是。", duration_ms=300) is True
    assert count_cjk_chars("a中文") == 2
    assert guard.resolve_uncertain_prefer_yield(InterruptDecision.UNCERTAIN) is (
        InterruptDecision.CONFIRM_INTERRUPT
    )
    assert (
        guard.resolve_uncertain_prefer_yield(InterruptDecision.RESUME) is InterruptDecision.RESUME
    )


def test_phrase_segmenter_normalization_and_edge_cuts() -> None:
    normalized, canonical = normalize_for_tts(
        "# 标题\n- [说明](https://example.com)  😀  `code`\n\n下一行"
    )
    assert canonical.startswith("# 标题")
    assert "https://" not in normalized and "😀" not in normalized and "`" not in normalized
    assert _unbalanced_quotes_or_brackets("他说“你好") is True
    assert _unbalanced_quotes_or_brackets('say "hello') is True
    assert _unbalanced_quotes_or_brackets("（完整）") is False

    seg = PhraseSegmenter(_fence())
    assert seg.push_token("") == []
    assert seg._find_cut(force=False, now_ns=0) is None
    seg.buffer = "   。"
    assert seg._find_cut(force=False, now_ns=0) == 3
    seg.buffer = "他说“这是很长的内容，仍然没有结束”"
    assert seg._find_cut(force=False, now_ns=0) is None
    seg.buffer = "这是足够长的一段内容，需要立即切开后面还有内容"
    assert seg._find_cut(force=False, now_ns=0) == seg.buffer.index("，")
    seg.buffer = "甲" * 43
    assert seg._find_cut(force=False, now_ns=0) is not None
    seg.buffer = "abcdefghijklmnopqrstuvwxyz abcdefghijklmnopqrstuvwxyz"
    assert seg._last_word_boundary(seg.buffer) == 26


def test_phrase_segmenter_timing_and_reset() -> None:
    seg = PhraseSegmenter(_fence())
    first = 1_000_000_000
    seg.push_token("甲乙丙丁戊己庚辛，后续", now_ns=first)
    assert seg.push_token("", now_ns=first + 300_000_000) == []
    assert seg._find_cut(force=False, now_ns=first + 300_000_000) is not None
    seg.reset(_fence().bump_generation())
    seg.push_token("没有任何边界但是字数足够", now_ns=first)
    assert seg._find_cut(force=False, now_ns=first + 500_000_000) == len(seg.buffer) - 1
    flushed = seg.flush(end_of_stream=True)
    assert flushed and seg.stream_ended is True and seg.buffer == ""


@pytest.mark.parametrize(
    ("features", "style"),
    [
        (ProsodyFeatures(explicit_request="慢一点"), SpeakingStyle.CALM),
        (ProsodyFeatures(explicit_request="快一点"), SpeakingStyle.URGENT),
        (ProsodyFeatures(recent_interrupt_count=2), SpeakingStyle.CALM),
        (ProsodyFeatures(speech_rate_cps=7, pause_ratio=0.1), SpeakingStyle.URGENT),
        (ProsodyFeatures(speech_rate_cps=2), SpeakingStyle.HESITANT),
        (ProsodyFeatures(rms_dbfs=-10), SpeakingStyle.NEUTRAL),
        (ProsodyFeatures(), SpeakingStyle.NEUTRAL),
    ],
)
def test_prosody_all_inference_routes(features: ProsodyFeatures, style: SpeakingStyle) -> None:
    controller = ProsodyController()
    assert controller.update(features).style is style
    assert 0.9 <= controller.state.rate <= 1.1
    assert controller.instruction_for_cosyvoice() in {
        "你正在进行闲聊互动，你说话的情感是neutral。",
        "你正在进行闲聊互动，你说话的情感是happy。",
    }


def test_prosody_history_is_bounded_and_smoothed() -> None:
    controller = ProsodyController()
    for _ in range(4):
        controller.update(ProsodyFeatures(recent_interrupt_count=2))
    assert controller.history == [SpeakingStyle.CALM] * 3


def test_stable_prefix_helpers_and_rewrite_paths() -> None:
    assert longest_common_prefix([]) == ""
    assert longest_common_prefix(["abc", "xyz"]) == ""
    assert snap_to_boundary("") == ""
    assert snap_to_boundary("english") == "english"
    assert snap_to_boundary("中文word") == "中文"
    assert has_complete_english_word_delta("x", "other") is False
    assert has_complete_english_word_delta("中", "中word") is True

    tracker = StablePrefixTracker(stability_ms=1)
    assert tracker.observe(1, "   ", now_ns=0) is None
    for text in ("甲乙", "甲丙", "甲丁"):
        assert tracker.observe(1, text, now_ns=0) is None
    assert tracker.candidate == ""
    tracker.reset(2)
    for text in ("号码138", "号码1380", "号码13800"):
        assert tracker.observe(2, text, now_ns=0) is None
    tracker.on_final(999)
    assert tracker.sentence_id == 2
    tracker.on_final(2)
    assert tracker.sentence_id is None


def test_stable_prefix_empty_incomplete_and_rewrite_rejection() -> None:
    empty = StablePrefixTracker(stability_ms=0)
    for text in ("abc", "xyz", "other"):
        assert empty.observe(1, text, now_ns=0) is None
    assert empty.candidate == ""

    incomplete = StablePrefixTracker(stability_ms=0)
    for text in ("138", "138", "138"):
        assert incomplete.observe(1, text, now_ns=0) is None
    assert incomplete.last_published == ""

    rewritten = StablePrefixTracker(
        sentence_id=1,
        stability_ms=0,
        last_published="旧内容",
    )
    for text in ("新内容甲", "新内容乙", "新内容丙"):
        assert rewritten.observe(1, text, now_ns=0) is None
    assert rewritten.candidate == ""


@pytest.mark.asyncio
async def test_task_manager_errors_timeout_cancel_and_summaries() -> None:
    tm = TaskManager()
    with pytest.raises(KeyError):
        await tm.start("missing", {}, _fence())

    async def timeout_handler(args: dict[str, object], event: asyncio.Event) -> None:
        _ = args, event
        await asyncio.sleep(1)

    tm.register(ToolSpec("timeout", "", {}, True, True, 0.001), timeout_handler)
    timeout_rec = await tm.start("timeout", {}, _fence())
    assert await tm.wait_result(timeout_rec.tool_task_id, _fence()) == {
        "error": "tool_timeout",
        "tool": "timeout",
    }
    assert tm.accept_result("missing", _fence()) is None

    async def stubborn(args: dict[str, object], event: asyncio.Event) -> None:
        _ = args, event
        await asyncio.sleep(1)

    tm.register(ToolSpec("stubborn", "", {}, True, True, 2), stubborn)
    rec = await tm.start("stubborn", {}, _fence())
    assert tm.active_count() == 1
    await tm.cancel_cancellable(_fence())
    assert rec.cancelled is True
    assert await tm.wait_result(rec.tool_task_id, _fence()) is None

    assert spoken_result_summarizer({"error": "x"}).startswith("刚才")
    assert spoken_result_summarizer({"spoken": "一句"}) == "一句。"
    assert spoken_result_summarizer({"a": 1, "b": "二"}) == "1，二。"
    assert spoken_result_summarizer({}) == "结果已经出来了。"
    assert spoken_result_summarizer({"summary": "一。二。三。四。"}) == "一。二。三。"
    assert new_idempotency_key() != new_idempotency_key()


@pytest.mark.asyncio
async def test_orchestrator_interruption_decision_routes() -> None:
    orch = Orchestrator()
    await orch.ready()
    await orch.on_vad_start()
    await orch.commit_turn("问题")
    await orch.begin_speaking([], "回答")
    await orch.on_vad_start()
    assert orch.state is ConversationState.INTERRUPTION_PENDING
    assert await orch.evaluate_interruption("嗯", 200) is InterruptDecision.RESUME
    await orch.on_vad_start()
    assert await orch.evaluate_interruption("", 1200) is InterruptDecision.FALSE_INTERRUPTION
    await orch.on_vad_start()
    assert await orch.evaluate_interruption("也许", 500) is InterruptDecision.CONFIRM_INTERRUPT
    assert orch.state is ConversationState.USER_SPEAKING

    await orch.commit_turn("再问")
    assert await orch.evaluate_interruption("停一下", 10) is InterruptDecision.CONFIRM_INTERRUPT
    assert orch.state is ConversationState.USER_SPEAKING


@pytest.mark.asyncio
async def test_commit_turn_recovers_from_interruption_pending() -> None:
    """Regression: barge-in left INTERRUPTION_PENDING and blackholed replies."""
    orch = Orchestrator()
    await orch.ready()
    await orch.on_vad_start()
    await orch.commit_turn("问题")
    await orch.begin_speaking([], "回答")
    await orch.on_vad_start()
    assert orch.state is ConversationState.INTERRUPTION_PENDING
    fence = await orch.commit_turn("旁边人说的或主人继续说的内容")
    assert fence.turn_id == 2
    assert orch.state is ConversationState.THINKING


@pytest.mark.asyncio
async def test_orchestrator_background_result_and_actual_playback_edges() -> None:
    orch = Orchestrator()
    await orch.ready()
    await orch.on_vad_start()
    fence = await orch.commit_turn("问题")
    assert await orch.accept_background_result(fence.bump_generation(), {"x": 1}) is None
    assert await orch.accept_background_result(fence, {"x": 1}) == {"x": 1}

    assert orch.state_machine is not None
    orch.state_machine.state = ConversationState.SPEAKING
    orch.heard_tracker.reset()
    heard = await orch.finish_livekit_playback(
        playback_position_s=0.2,
        synchronized_transcript="听到",
        tools_active=True,
    )
    assert heard == "听到"
    assert orch.state is ConversationState.TOOL_WAITING
    accepted = await orch.accept_background_result(fence, {"summary": "完成"})
    assert accepted == {"summary": "完成"}
    assert orch.state is ConversationState.THINKING


@pytest.mark.asyncio
async def test_orchestrator_small_helpers_and_offline_fallback() -> None:
    await cancel_and_wait(None)
    done = asyncio.create_task(asyncio.sleep(0))
    await done
    await cancel_and_wait(done)
    playback = PlaybackController()
    playback.push_pcm(b"ignored")
    await playback.stop_and_flush()
    assert playback.pcm_played == b""

    orch = Orchestrator()
    result = await OfflinePipeline(orchestrator=orch).run_turn("离线问题")
    assert result["pcm_bytes"] > 0
    assert result["full_generated"].startswith("收到")
    await orch.close()


@pytest.mark.asyncio
async def test_orchestrator_clear_task_identity_and_else_fence_path() -> None:
    orch = Orchestrator()
    await orch.ready()
    task = asyncio.create_task(asyncio.sleep(10))
    other = asyncio.create_task(asyncio.sleep(10))
    orch.set_active_llm_task(task)
    orch.set_active_tts_task(task)
    orch.clear_active_llm_task(other)
    orch.clear_active_tts_task(other)
    assert orch.active_llm_task is task and orch.active_tts_task is task
    task.cancel()
    other.cancel()
    await asyncio.gather(task, other, return_exceptions=True)
    orch.clear_active_llm_task()
    orch.clear_active_tts_task()

    assert orch.state_machine is not None
    orch.state_machine.state = ConversationState.TOOL_WAITING
    old = orch.fence
    new = await orch.confirm_interruption(create_user_turn=True)
    assert new.generation_id == old.generation_id + 1
    assert orch.state_machine.fence == new
