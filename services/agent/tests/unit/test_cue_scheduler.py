from __future__ import annotations

from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.cue_scheduler import CueScheduler

_FENCE = GenerationFence(
    session_id="ses_cue_test",
    turn_id=1,
    generation_id=1,
    tool_epoch=0,
    session_epoch=1,
)


def test_listener_cues_are_bounded_cooled_down_and_history_free() -> None:
    scheduler = CueScheduler(
        min_speech_ms=1_800,
        cooldown_ms=4_000,
        max_per_turn=2,
    )
    scheduler.start_turn(user_turn_id=7, now_ns=1_000_000_000)

    assert scheduler.observe_partial("我最近想了很多", fence=_FENCE, now_ns=2_700_000_000) is None
    first = scheduler.observe_partial("我最近想了很多事情", fence=_FENCE, now_ns=2_900_000_000)
    assert first is not None
    assert (first.text, first.user_turn_id, first.cue_epoch) == ("嗯", 7, 1)
    assert scheduler.observe_partial("然后呢", fence=_FENCE, now_ns=4_000_000_000) is None

    second = scheduler.observe_partial("我还想继续说", fence=_FENCE, now_ns=7_000_000_000)
    assert second is not None and second.text == "我在听"
    assert scheduler.observe_partial("还有最后一点", fence=_FENCE, now_ns=12_000_000_000) is None


def test_listener_cues_fail_closed_for_sensitive_or_unhealthy_audio() -> None:
    scheduler = CueScheduler(min_speech_ms=1_000)
    scheduler.start_turn(user_turn_id=2, now_ns=0)

    assert (
        scheduler.observe_partial("验证码是 123456", fence=_FENCE, now_ns=2_000_000_000)
        is None
    )
    assert scheduler.observe_partial("我想自尽", fence=_FENCE, now_ns=2_050_000_000) is None
    assert scheduler.observe_partial("我不想再活下去了", fence=_FENCE, now_ns=2_075_000_000) is None
    assert scheduler.observe_partial("我刚被性侵了", fence=_FENCE, now_ns=2_090_000_000) is None
    assert (
        scheduler.observe_partial(
            "我继续讲",
            fence=_FENCE, now_ns=2_100_000_000,
            aec_healthy=False,
        )
        is None
    )
    assert (
        scheduler.observe_partial(
            "我继续讲",
            fence=_FENCE, now_ns=2_200_000_000,
            main_response_active=True,
        )
        is None
    )


def test_new_turn_invalidates_old_cue_epoch() -> None:
    scheduler = CueScheduler(min_speech_ms=0, cooldown_ms=0)
    scheduler.start_turn(user_turn_id=1, now_ns=0)
    first = scheduler.observe_partial("第一轮", fence=_FENCE, now_ns=1)
    assert first is not None

    invalidated_epoch = scheduler.cancel_turn()
    scheduler.start_turn(user_turn_id=2, now_ns=2)
    second = scheduler.observe_partial("第二轮", fence=_FENCE, now_ns=3)

    assert second is not None
    assert invalidated_epoch > first.cue_epoch
    assert second.cue_epoch > invalidated_epoch
