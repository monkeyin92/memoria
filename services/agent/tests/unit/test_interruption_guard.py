from __future__ import annotations

from services.agent.src.orchestration.interruption_guard import (
    ChineseInterruptionGuard,
    InterruptDecision,
    PlaybackInputDecision,
    PlaybackInputGuard,
    is_backchannel,
    is_completion_ack_only,
    is_conversation_close_only,
    is_explicit_interrupt,
    is_primarily_non_chinese_script,
    is_short_assistant_farewell_reply,
    user_turn_suggests_conversation_close,
)


def test_backchannel_whitelist() -> None:
    assert is_backchannel("嗯嗯", duration_ms=400)
    assert is_backchannel("好的", duration_ms=500)
    assert not is_backchannel("好的，那我们下周见", duration_ms=1200)


def test_explicit_interrupt_prefixes() -> None:
    assert is_explicit_interrupt("等等，不是这个意思")
    assert is_explicit_interrupt("停一下")
    assert is_explicit_interrupt("我问的是下周三")
    assert is_explicit_interrupt("别说了")
    assert is_explicit_interrupt("好了，知道了")
    assert is_completion_ack_only("好了，知道了。")


def test_conversation_close_phrases_are_exact_control_only_matches() -> None:
    for phrase in ("再见", "拜拜", "知道了", "我知道了", "退下吧", "先这样吧"):
        assert is_conversation_close_only(phrase), phrase
    for phrase in (
        "知道了，再见",
        "好的，再见",
        "嗯，拜拜",
        "行，拜拜",
        "就这样吧，拜拜",
    ):
        assert is_conversation_close_only(phrase), phrase
    for phrase in ("goodbye", "Good bye", "BYE", "see you"):
        assert is_conversation_close_only(phrase), phrase

    for sentence in (
        "再见是什么意思",
        "下次见到小明要说再见",
        "我知道了怎么做",
        "茉莉花茶怎么做",
    ):
        assert not is_conversation_close_only(sentence), sentence


def test_assistant_farewell_reply_pairs_with_user_close_intent() -> None:
    assert user_turn_suggests_conversation_close("知道了，再见")
    assert is_short_assistant_farewell_reply("祝你上海玩得开心，再见")
    assert not user_turn_suggests_conversation_close("下次见到小明要说再见")


def test_primarily_non_chinese_script_detects_garbled_rescue() -> None:
    assert is_primarily_non_chinese_script("안녕하세요")
    assert not is_primarily_non_chinese_script("今天星期几")


def test_interrupt_ack_phrase_by_semantics() -> None:
    from services.agent.src.orchestration.interruption_guard import (
        interrupt_ack_phrase,
        is_interrupt_command_only,
    )

    assert interrupt_ack_phrase("停一下") == "嗯，你说。"
    assert interrupt_ack_phrase("等等") == "嗯，你说。"
    assert interrupt_ack_phrase("等下。") == "嗯，你说。"
    assert interrupt_ack_phrase("等一下我问你") == "嗯，你说。"
    assert interrupt_ack_phrase("听我说") == "嗯，你说。"
    assert interrupt_ack_phrase("别说了") == "好的。"
    assert interrupt_ack_phrase("暂停") == "好的。"
    assert interrupt_ack_phrase("停下") == "好的。"
    assert interrupt_ack_phrase("不要说了") == "好的。"
    assert interrupt_ack_phrase("好了，知道了") == "好的。"
    assert interrupt_ack_phrase("") == "嗯，你说。"

    assert is_interrupt_command_only("等等") is True
    assert is_interrupt_command_only("嗯，等等，等等。") is True
    assert is_interrupt_command_only("等一下") is True
    assert is_interrupt_command_only("等下。") is True
    assert is_interrupt_command_only("别说了") is True
    assert is_interrupt_command_only("好了，知道了") is True
    assert is_interrupt_command_only("等一下我想问下周三") is False
    assert is_interrupt_command_only("等下我想问下周三") is False
    assert is_interrupt_command_only("我等下") is False
    assert is_interrupt_command_only("等下我") is False
    assert is_interrupt_command_only("我们等下") is False
    assert is_interrupt_command_only("今天天气怎么样") is False


def test_guard_decisions() -> None:
    g = ChineseInterruptionGuard()
    assert g.on_user_voice_while_speaking() is InterruptDecision.DUCK
    assert (
        g.evaluate(elapsed_ms=100, asr_text="停一下", has_speech_energy=True)
        is InterruptDecision.CONFIRM_INTERRUPT
    )
    assert (
        g.evaluate(elapsed_ms=200, asr_text="嗯嗯", has_speech_energy=True)
        is InterruptDecision.RESUME
    )
    assert (
        g.evaluate(
            elapsed_ms=150,
            asr_text="",
            has_speech_energy=False,
            looks_like_noise_only=True,
        )
        is InterruptDecision.RESUME
    )


def test_playback_input_guard_opens_feedback_circuit_on_third_rapid_turn() -> None:
    guard = PlaybackInputGuard(enabled=True, max_feedback_turns=2, feedback_window_s=15)

    for index in range(2):
        now = (index + 1) * 1_000_000_000
        guard.start(during_playback=True, now_ns=now)
        assert (
            guard.observe(
                "嗯哈",
                final=True,
                assistant_text="当前回答。",
                now_ns=now + 100_000_000,
            )
            is PlaybackInputDecision.ACCEPT
        )
        assert guard.accept_turn("嗯哈", now_ns=now)[0] is True

    guard.start(during_playback=True, now_ns=3_000_000_000)
    guard.observe(
        "嗯哈",
        final=True,
        assistant_text="当前回答。",
        now_ns=3_100_000_000,
    )
    assert guard.accept_turn("嗯哈", now_ns=3_000_000_000) == (
        False,
        "feedback_circuit_open",
    )


def test_unanchored_playback_transcript_cannot_bypass_echo_guard_as_interrupt() -> None:
    guard = PlaybackInputGuard(enabled=True)

    decision = guard.observe(
        "不是一座只有历史的城市",
        final=True,
        assistant_text="南京不是一座只有历史的城市，也是一座现代都市。",
        during_playback_if_unstarted=True,
    )

    assert decision is PlaybackInputDecision.IGNORE
    assert guard.candidate_reason == "unanchored_playback_transcript"


def test_final_content_replaces_a_short_interim_backchannel_decision() -> None:
    guard = PlaybackInputGuard(enabled=True)
    guard.start(during_playback=True, now_ns=1_000_000_000)

    assert (
        guard.observe(
            "好的",
            final=False,
            assistant_text="我还在继续回答。",
            now_ns=1_100_000_000,
        )
        is PlaybackInputDecision.WAIT
    )
    assert guard.candidate_reason == "backchannel"

    assert guard.accept_turn(
        "好的，我想问一下明天上海的天气",
        now_ns=2_500_000_000,
        assistant_text="我还在继续回答。",
    ) == (True, None)


def test_wait_alias_cannot_bypass_assistant_echo_guard() -> None:
    guard = PlaybackInputGuard(enabled=True)

    for text, assistant_text in (
        ("等下。", "你先等一下，我马上说完。"),
        ("等 下", "你先等下，我马上说完。"),
        ("等下", "请等一下。"),
    ):
        assert (
            guard.guarded_reason(
                text,
                duration_ms=900,
                assistant_text=assistant_text,
            )
            == "assistant_echo"
        )


def test_short_weekday_echo_cannot_bypass_assistant_echo_guard() -> None:
    guard = PlaybackInputGuard(enabled=True)
    assistant_text = "今天是2026年8月6日，星期四。"

    for text in ("星期四", "今天是星期四", "周四"):
        assert (
            guard.guarded_reason(
                text,
                duration_ms=900,
                assistant_text=assistant_text,
            )
            == "assistant_echo"
        )
    assert (
        guard.guarded_reason(
            "星期四我有安排",
            duration_ms=900,
            assistant_text=assistant_text,
        )
        is None
    )


def test_playback_guard_quarantines_low_information_decoder_fragments() -> None:
    guard = PlaybackInputGuard(enabled=True)

    assert (
        guard.guarded_reason(
            "其。",
            duration_ms=900,
            assistant_text="我还在继续回答。",
        )
        == "low_information_fragment"
    )
    assert (
        guard.guarded_reason(
            "对谢ght.",
            duration_ms=900,
            assistant_text="我还在继续回答。",
        )
        == "low_information_fragment"
    )
    assert guard.guarded_reason("我爱GPT", duration_ms=900, assistant_text="") is None
    assert guard.guarded_reason("我用BERT", duration_ms=900, assistant_text="") is None
    assert guard.guarded_reason("谁？", duration_ms=900, assistant_text="") is None
    assert guard.guarded_reason("停", duration_ms=900, assistant_text="") is None


def test_meaningful_follow_up_does_not_consume_feedback_circuit() -> None:
    guard = PlaybackInputGuard(enabled=True, max_feedback_turns=1)
    guard.start(during_playback=True, now_ns=1_000_000_000)
    assert (
        guard.observe(
            "这是一个完整的新问题？",
            final=True,
            assistant_text="当前回答。",
            now_ns=1_100_000_000,
        )
        is PlaybackInputDecision.ACCEPT
    )
    assert guard.accept_turn("这是一个完整的新问题？", now_ns=1_100_000_000)[0] is True

    guard.start(during_playback=True, now_ns=2_000_000_000)
    assert (
        guard.observe(
            "这是另一个完整的新问题？",
            final=True,
            assistant_text="当前回答。",
            now_ns=2_100_000_000,
        )
        is PlaybackInputDecision.ACCEPT
    )
    assert guard.accept_turn("这是另一个完整的新问题？", now_ns=2_100_000_000)[0] is True


def test_barge_in_disabled_revalidated_after_playback_ends() -> None:
    guard = PlaybackInputGuard(enabled=True)
    guard.start(during_playback=True, now_ns=1_000_000_000)
    guard.candidate_reason = "barge_in_disabled"
    guard.candidate_decision = PlaybackInputDecision.IGNORE

    accepted, reason = guard.accept_turn("今天是星期几", now_ns=5_000_000_000)

    assert accepted is True
    assert reason is None


def test_primarily_non_chinese_script_detects_short_hangul_rescue() -> None:
    from services.agent.src.orchestration.interruption_guard import (
        is_primarily_non_chinese_script,
    )

    assert is_primarily_non_chinese_script("한국어요") is True
    assert is_primarily_non_chinese_script("今天星期几") is False
