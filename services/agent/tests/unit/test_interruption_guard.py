from __future__ import annotations

import pytest
from services.agent.src.orchestration.interruption_guard import (
    ChineseInterruptionGuard,
    InterruptDecision,
    PlaybackInputDecision,
    PlaybackInputGuard,
    interrupt_ack_phrase,
    is_backchannel,
    is_completion_ack_only,
    is_conversation_close_only,
    is_explicit_farewell_text,
    is_explicit_interrupt,
    is_interrupt_command_only,
    is_primarily_non_chinese_script,
    is_resume_command_only,
    is_short_assistant_farewell_reply,
    is_stop_command_only,
    user_turn_suggests_conversation_close,
)
from services.common.fixed_safety_scripts import CRISIS_SELF_SCRIPT


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
    assert is_completion_ack_only("好啦。")
    assert is_completion_ack_only("好啦好啦。")
    assert is_completion_ack_only("好啦，好啦。")
    assert is_interrupt_command_only("好啦别说了")
    assert is_stop_command_only("好啦别说了")
    assert not is_stop_command_only("好啦")
    assert not is_stop_command_only("好啦好啦")
    assert is_interrupt_command_only("你，好啦，闭嘴")
    assert is_stop_command_only("你，好啦，闭嘴")
    assert is_interrupt_command_only("你，好了，闭嘴")
    assert is_stop_command_only("你，好了，闭嘴")
    assert is_interrupt_command_only("好啦，你闭嘴")
    assert is_stop_command_only("好啦，你闭嘴")
    assert is_interrupt_command_only("好啦，你别说了")
    assert is_stop_command_only("好啦，你别说了")
    assert is_interrupt_command_only("你别说了，暂停")
    assert is_stop_command_only("你别说了，暂停")
    assert is_interrupt_command_only("麻烦别说了")
    assert is_stop_command_only("麻烦别说了")
    for colloquial in (
        "不要说。",
        "不要说啦。",
        "不用说了。",
        "你不用说了。",
        "不用说了好吗？",
        "不用再说了。",
        "别说啦。",
        "不要讲啦。",
        "别讲啦。",
    ):
        assert is_interrupt_command_only(colloquial), colloquial
        assert is_stop_command_only(colloquial), colloquial
    for non_stop in (
        "不要说谎",
        "不要说这个",
        "不要说啦，我想问个问题",
        "妈妈说不要说脏话",
        "你不要说吗？",
        "不要说吗？",
        "你不要说吗",
        "你不用说了吗？",
        "你不用说了吗",
        "不用说了吗？",
        "你不用说了？",
        "不用说了？",
        "你别说啦？",
    ):
        assert not is_interrupt_command_only(non_stop), non_stop
        assert not is_stop_command_only(non_stop), non_stop
    assert is_interrupt_command_only("拜托安静")
    assert is_stop_command_only("拜托安静")
    assert is_interrupt_command_only("帮我暂停")
    assert is_stop_command_only("帮我暂停")
    assert not is_interrupt_command_only("等一下，怎么暂停？")
    assert not is_stop_command_only("等一下，怎么暂停？")
    assert not is_interrupt_command_only("别说了，怎么暂停？")
    assert not is_stop_command_only("别说了，怎么暂停？")
    assert not is_interrupt_command_only("你好了吗")
    assert not is_interrupt_command_only("我想好了")
    assert not is_interrupt_command_only("你不是")
    assert not is_interrupt_command_only("你不对")
    assert not is_interrupt_command_only("好啦，我换个话题")
    assert not is_stop_command_only("好啦，我换个话题")
    assert not is_interrupt_command_only("修好了暂停键")
    assert not is_stop_command_only("修好了暂停键")
    assert not is_completion_ack_only("好。")
    assert not is_completion_ack_only("好的。")
    assert not is_completion_ack_only("太好啦！")
    assert not is_explicit_interrupt("太好啦！")
    assert not is_explicit_interrupt("太好了！")
    assert not is_interrupt_command_only("太好啦！")
    assert not is_interrupt_command_only("太好了！")
    assert not is_stop_command_only("太好啦！")
    assert not is_stop_command_only("太好了！")
    assert is_backchannel("好。", duration_ms=400)
    assert is_backchannel("好的。", duration_ms=500)


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
    assert is_conversation_close_only("你说的好多呀，好的，我知道了，再见！")
    assert is_conversation_close_only("你说的好多呀好的我知道了再见")
    assert is_conversation_close_only("行了，先这样吧，拜拜")
    assert is_conversation_close_only("行，好的，知道了，你退下。")


def test_assistant_farewell_reply_pairs_with_user_close_intent() -> None:
    assert user_turn_suggests_conversation_close("知道了，再见")
    assert is_short_assistant_farewell_reply("祝你上海玩得开心，再见")
    assert not user_turn_suggests_conversation_close("下次见到小明要说再见")


def test_only_goodbye_close_phrases_play_the_spoken_farewell() -> None:
    for phrase in ("再见", "好的，再见", "嗯，拜拜", "下次见", "回头见", "bye"):
        assert is_explicit_farewell_text(phrase), phrase
    for phrase in ("退下吧", "知道了", "先这样吧", "就这样吧", "休息吧"):
        assert not is_explicit_farewell_text(phrase), phrase


def test_primarily_non_chinese_script_detects_garbled_rescue() -> None:
    assert is_primarily_non_chinese_script("안녕하세요")
    assert not is_primarily_non_chinese_script("今天星期几")


def test_interrupt_ack_phrase_by_semantics() -> None:
    from services.agent.src.orchestration.interruption_guard import (
        interrupt_ack_phrase,
        is_interrupt_command_only,
    )

    assert interrupt_ack_phrase("停一下") is None
    assert interrupt_ack_phrase("别说了") is None
    assert interrupt_ack_phrase("暂停") is None
    assert interrupt_ack_phrase("停下") is None
    assert interrupt_ack_phrase("不要说了") is None
    assert interrupt_ack_phrase("等等") == "嗯，你说。"
    assert interrupt_ack_phrase("等下。") == "嗯，你说。"
    assert interrupt_ack_phrase("等一下我问你") == "嗯，你说。"
    assert interrupt_ack_phrase("听我说") == "嗯，你说。"
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


def test_speaker_echo_of_a_read_out_helpline_is_guarded_mid_script() -> None:
    guard = PlaybackInputGuard(enabled=True)
    announced = "".join(phrase.display for phrase in CRISIS_SELF_SCRIPT.phrases[:2])
    echo = "你现在一定特别难受，我就在这里陪着你，哪儿也不去"

    for assistant_text in (announced, CRISIS_SELF_SCRIPT.display):
        assert (
            guard.guarded_reason(echo, duration_ms=2400, assistant_text=assistant_text)
            == "assistant_echo"
        )
    assert guard.guarded_reason("我现在就去找妈妈", duration_ms=1200, assistant_text=announced) is None


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


def test_repeated_bare_stop_is_a_command_but_stop_words_in_nouns_are_not() -> None:
    for text in ("停", "停停", "停停停", "停，停"):
        assert is_interrupt_command_only(text), text
    for text in ("停车", "停电了", "停停车"):
        assert not is_interrupt_command_only(text), text


@pytest.mark.parametrize(
    "text",
    [
        "停",
        "停下",
        "停停",
        "停一下",
        "暂停",
        "别说了",
        "不要说了",
        "别讲了",
        "不要讲了",
        "不讲了",
        "不听了",
        "我不想听了",
        "安静",
        "闭嘴",
        "嘘",
        "stop",
        "STOP",
        "Stop.",
        "停！",
        "停，停",
        "停下来",
        "请暂停",
        "我想停一下",
    ],
)
def test_bare_stop_phrase_is_a_command_with_no_spoken_reply(text: str) -> None:
    assert is_explicit_interrupt(text)
    assert is_interrupt_command_only(text)
    assert interrupt_ack_phrase(text) is None


def test_completion_ack_still_speaks_its_quiet_reply() -> None:
    assert is_interrupt_command_only("够了")
    assert interrupt_ack_phrase("够了") == "好的。"


@pytest.mark.parametrize(
    "text",
    [
        "停车",
        "别停",
        "不要停",
        "停电",
        "雨停了",
        "停在哪",
        "小兔子停下来看了看",
        "别停下",
        "不要暂停",
        "等下我",
        "我们等下",
    ],
)
def test_stop_character_without_a_stop_command_is_not_an_interrupt(text: str) -> None:
    assert not is_explicit_interrupt(text)
    assert not is_interrupt_command_only(text)


def test_stop_phrase_with_a_request_interrupts_without_dropping_the_request() -> None:
    assert is_explicit_interrupt("我不想听了你讲个别的")
    assert not is_interrupt_command_only("我不想听了你讲个别的")


def test_resume_strips_a_stop_phrase_but_not_a_noun_that_starts_with_stop() -> None:
    assert is_resume_command_only("暂停。继续")
    assert is_resume_command_only("暂停一下，继续")
    assert is_resume_command_only("继续好吗？")
    assert is_resume_command_only("继续可以吗？")
    assert is_resume_command_only("暂停一下，继续好吗？")
    assert is_resume_command_only("嗯？继续。")
    assert not is_resume_command_only("停车，继续")
    assert not is_resume_command_only("暂停一下，继续吗？")
    assert not is_resume_command_only("暂停一下，继续吗")
    assert not is_resume_command_only("继续？")
    assert not is_resume_command_only("继续吗？好吗？")
    assert not is_resume_command_only("别继续好吗？")
    assert not is_resume_command_only("不要继续好吗？")
    assert not is_resume_command_only("不继续")


@pytest.mark.parametrize(
    "text",
    [
        "不要再说了",
        "不要再说啦",
        "不用说啦",
        "不用再说啦",
        "不要再讲了",
        "不要再讲啦",
        "不用讲了",
        "不用讲啦",
        "不用再讲了",
        "不用再讲啦",
        "别再说了",
        "别再说啦",
        "别再讲了",
        "别再讲啦",
        "先停一下",
        "你先停一下",
        "你先停一下！",
        "先停下来",
        "你先停下来",
        "先别说了",
        "你先别说了",
        "暂停一下",
        "停一停",
        "停一停，停一停！",
        "停一停，停下来！",
        "停下来，停一停！",
        "你先停一停",
        "你先，嗯？停一下！",
        "不用讲了，嗯？停一停！",
        "不用再讲了！嗯？",
        "暂停一下！啊？",
        "暂停一下. 嗯？",
        "停，别说了",
        "先停，别说了",
        "啊？别说啦！",
        "啊？别再 说啦！",
        "嗯？不要再说了！",
        "啊？暂停一下！",
        "不用再说了好吗？",
        "不要说啦，好 吗？",
        "不要说啦，好吗……",
        "暂停一下好吗？",
        "你先停一下，可以吗？",
    ],
)
def test_symmetric_colloquial_stops_and_filler_qmark_commands_stop_cleanly(text: str) -> None:
    assert is_interrupt_command_only(text), text
    assert is_stop_command_only(text), text
    assert interrupt_ack_phrase(text) is None, text


@pytest.mark.parametrize(
    "text",
    [
        "你先停一下？",
        "你先停下来？",
        "你不要再说了？",
        "你不要再说了吗？",
        "你不要再说了吗",
        "不用讲了吗？",
        "不用讲了吗，好吧",
        "不用讲了吗？可以吗？",
        "不用讲了？可以吗？",
        "不用讲了，好吧？",
        "不用再讲了？嗯！",
        "去İstanbul和İzmir好吗？",
        "不用再讲啦？",
        "别再说了吗",
        "别再讲了？",
        "暂停一下？",
        "暂停一下啊？",
        "你想停一停？",
        "啊？别说啦？",
        "小兔子停一停",
        "停下来看了看",
        "先停车",
        "你先停电了",
    ],
)
def test_question_colloquial_stops_and_embedded_nouns_stay_in_chat(text: str) -> None:
    assert not is_interrupt_command_only(text), text
    assert not is_stop_command_only(text), text


def test_embedded_colloquial_stop_in_question_does_not_get_explicit_interrupt() -> None:
    for text in (
        "谁不用讲了？",
        "谁不要再说了？",
        "谁别再讲了？",
        "不用讲了吗？",
        "不用讲了，吗？",
        "不用讲了？",
        "你不要再说了？",
        "你不要再说了吗？",
        "不用讲了吗，好吧",
        "不用讲了，好吧？",
        "不用讲了吗？可以吗？",
        "暂停一下？",
        "暂停一下啊？",
        "“不用讲了”是什么意思？",
        "「不要再说了」是什么词？",
        "小兔子停一停",
        "停下来看了看",
        "猫停一停",
    ):
        assert not is_explicit_interrupt(text), text
    for text in (
        "别再讲了，我想问恐龙吃什么",
        "别再讲了，我想问恐龙吃什么？",
        "暂停一下！你听得见吗？",
        "不用再说了好吗？",
        "不用再讲了！嗯？",
    ):
        assert is_explicit_interrupt(text), text
    assert not is_interrupt_command_only("别再讲了，我想问恐龙吃什么")
    assert not is_interrupt_command_only("别再讲了，我想问恐龙吃什么？")
    assert not is_interrupt_command_only("暂停一下！你听得见吗？")

