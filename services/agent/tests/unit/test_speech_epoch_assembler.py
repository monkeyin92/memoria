from __future__ import annotations

from services.agent.src.orchestration.speech_epoch_assembler import SpeechEpochAssembler


def test_one_committed_turn_can_join_multiple_vad_epochs() -> None:
    assembler = SpeechEpochAssembler()
    assembler.start_epoch(1)
    assembler.observe_final("我今天", accepted=True)
    assembler.start_epoch(2)
    assembler.observe_final("在幼儿园玩了积木", accepted=True)

    turn = assembler.consume("我今天在幼儿园玩了积木", fallback_epoch=None)

    assert turn.text == "我今天 在幼儿园玩了积木"
    assert turn.speech_epoch == 2


def test_current_turn_skips_an_unrelated_older_epoch() -> None:
    assembler = SpeechEpochAssembler()
    assembler.start_epoch(1)
    assembler.observe_final("怀孕咋样你是谁呀你是谁呀", accepted=True)
    assembler.start_epoch(2)
    assembler.observe_final("拿", accepted=True)

    turn = assembler.consume("拿。", fallback_epoch=None)

    assert turn.text == "拿"
    assert turn.speech_epoch == 2
    assert turn.discarded_epochs == (1,)


def test_late_final_in_the_current_epoch_cannot_prefix_a_matching_final() -> None:
    assembler = SpeechEpochAssembler()
    assembler.start_epoch(1)
    assembler.start_epoch(2)
    assembler.observe_final("这是迟到的旧长句", accepted=True)
    assembler.observe_final("拿", accepted=True)

    turn = assembler.consume("拿。", fallback_epoch=None)

    assert turn.text == "拿"
    assert turn.speech_epoch == 2


def test_queued_callbacks_consume_only_their_matching_segments() -> None:
    assembler = SpeechEpochAssembler()
    assembler.start_epoch(1)
    assembler.observe_final("第一句", accepted=True)
    assembler.start_epoch(2)
    assembler.observe_final("第二句", accepted=True)

    first = assembler.consume("第一句", fallback_epoch=None)
    second = assembler.consume("第二句", fallback_epoch=None)

    assert first.text == "第一句"
    assert first.speech_epoch == 1
    assert second.text == "第二句"
    assert second.speech_epoch == 2


def test_identical_turns_are_consumed_once_in_callback_order() -> None:
    assembler = SpeechEpochAssembler()
    assembler.start_epoch(1)
    assembler.observe_final("再说一次", accepted=True)
    assembler.start_epoch(2)
    assembler.observe_final("再说一次", accepted=True)

    first = assembler.consume("再说一次", fallback_epoch=None)
    second = assembler.consume("再说一次", fallback_epoch=None)

    assert first.speech_epoch == 1
    assert second.speech_epoch == 2


def test_unanchored_playback_prefix_is_removed_from_the_next_real_final() -> None:
    assembler = SpeechEpochAssembler()
    assembler.mark_contaminated("你好呀很高兴见到你")
    assembler.start_epoch(1)
    assembler.observe_final("你好呀很高兴见到你，介绍一下南京", accepted=True)

    turn = assembler.consume("你好呀很高兴见到你，介绍一下南京", fallback_epoch=None)

    assert turn.text == "介绍一下南京"


def test_accepted_barge_in_final_survives_a_playback_contaminated_endpoint() -> None:
    assembler = SpeechEpochAssembler()
    assembler.start_epoch(1)
    assembler.observe_final("好了，知道了", accepted=True, contaminated=True)

    turn = assembler.consume(
        "根据提供的数据和指示来协助。好了，知道了。",
        fallback_epoch=None,
    )

    assert turn.text == "好了，知道了"
    assert turn.discarded_epochs == ()


def test_unmatched_pending_segment_cannot_leak_into_a_later_callback() -> None:
    assembler = SpeechEpochAssembler()
    assembler.start_epoch(1)
    assembler.observe_final("没有对应回调的旧句子", accepted=True)
    assembler.start_epoch(2)

    stale = assembler.consume("完全不相关", fallback_epoch=None)
    assembler.observe_final("当前问题", accepted=True)
    current = assembler.consume("当前问题", fallback_epoch=None)

    assert stale.text is None
    assert stale.discarded_epochs == (1,)
    assert current.text == "当前问题"
    assert current.speech_epoch == 2


def test_unmatched_active_segment_cannot_leak_into_a_later_callback() -> None:
    assembler = SpeechEpochAssembler()
    assembler.start_epoch(1)
    assembler.observe_final("没有对应回调的旧句子", accepted=True)

    stale = assembler.consume("完全不相关", fallback_epoch=None)
    assert assembler.current_epoch is None

    assembler.start_epoch(2)
    assembler.observe_final("当前问题", accepted=True)
    current = assembler.consume("当前问题", fallback_epoch=None)

    assert stale.text is None
    assert stale.discarded_epochs == (1,)
    assert current.text == "当前问题"
    assert current.speech_epoch == 2
