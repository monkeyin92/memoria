from __future__ import annotations

from typing import Any

import pytest
from services.agent.src.voice_core.asr_stream_supervisor import (
    ASRDecisionReason,
    ASRStreamSupervisor,
)
from services.agent.src.voice_core.speech_timeline import ASRResult
from services.agent.tests.unit.media_session_support import _accept_media_asr_final
from services.common.companion_response_safety import companion_safety_decision

_VICTIM = "救命，有人打我"
_VICTIM_PERIOD = "救命，有人打我。"
_OTHER = "我同学被别人打了"
_SELF = "我不想活了。"
_ESC_DAO = "我想拿刀划自己"
_ESC_HUA = "我想拿刀画自己"


@pytest.mark.asyncio
async def test_media_commits_do_not_replay_a_consumed_asr_tail(
    device_media_session: Any,
) -> None:
    window = await device_media_session("media-consumed-asr-tail", during_playback=False)
    registry = window.registry
    registry.turn_endpoint_grace_s = 60
    registry.turn_endpoint_min_grace_s = 60
    registry.turn_endpoint_max_grace_s = 60
    queries = ("我想伤害自己。", "我爸爸打我，好疼。")

    for turn_id, text in enumerate(queries, start=1):
        start = 40_000 + (turn_id - 1) * 3_200
        await _accept_media_asr_final(
            registry,
            window.identity,
            sentence_id=f"turn-{turn_id}",
            task_epoch=turn_id + 2,
            start_sample=start,
            end_sample=start + 1_600,
            text=text,
        )
        fence, reason = await registry.commit_user_turn(
            window.identity.session_id,
            stream_epoch=window.identity.stream_epoch,
            start_sample=start,
            end_sample=start + 800,
            retire_sample=start + 800,
        )
        assert fence is not None, reason
        assert reason is None
        assert window.context.projection.provisional is None
        assert window.context.runtime.speech_timeline.committed_sample == start + 800

    assert window.provider.prepared == list(queries)
    assert companion_safety_decision(window.provider.prepared[-1]) == "support_victim"


def test_post_watermark_final_strips_replayed_victim_prefix() -> None:
    """#527 residual: start>=committed still carried victim text into other.

    Dense victim→other finals can clear the sample watermark while ``.text``
    still prepends the committed victim clause. Classifier input must be the
    other line alone so local crisis stays support_relay / support-other-v1.
    """

    supervisor = ASRStreamSupervisor()
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=9_600)
    victim = ASRResult(1, "victim", 1, 0, 1_600, _VICTIM, True)
    assert supervisor.accept_result(victim, session_id="session")
    supervisor.mark_committed(1_600)

    merged = ASRResult(1, "victim-other", 1, 1_600, 4_800, _VICTIM + _OTHER, True)
    decision = supervisor.accept_result(merged, session_id="session")

    assert decision.reason is ASRDecisionReason.ACCEPTED
    assert decision.accepted is not None
    assert decision.accepted.text == _OTHER
    assert decision.accepted.capture_start_sample == 1_600
    assert len(decision.accepted.text) == len(_OTHER)
    assert companion_safety_decision(decision.accepted.text) == "support_relay"
    assert companion_safety_decision(_VICTIM + _OTHER) == "support_victim"


def test_post_watermark_exact_repeat_of_committed_text_is_kept() -> None:
    """Same words on a new turn must not peel to empty (warmup same-words)."""

    supervisor = ASRStreamSupervisor()
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=9_600)
    first = ASRResult(1, "same-1", 1, 0, 1_600, _OTHER, True)
    assert supervisor.accept_result(first, session_id="session")
    supervisor.mark_committed(1_600)

    repeat = ASRResult(1, "same-2", 1, 1_600, 3_200, _OTHER, True)
    decision = supervisor.accept_result(repeat, session_id="session")

    assert decision.reason is ASRDecisionReason.ACCEPTED
    assert decision.accepted is not None
    assert decision.accepted.text == _OTHER


def test_post_watermark_final_peels_committed_clauses_beyond_short_history() -> None:
    """Long sessions must peel an older clause from a cumulative provider final."""

    supervisor = ASRStreamSupervisor()
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=32_000)
    committed = tuple(f"历史句{i}" for i in range(10))

    for index, text in enumerate(committed):
        result = ASRResult(
            1,
            f"history-{index}",
            index + 1,
            index * 1_600,
            (index + 1) * 1_600,
            text,
            True,
        )
        decision = supervisor.accept_result(result, session_id="session")
        assert decision.accepted is not None
        supervisor.mark_committed((index + 1) * 1_600)

    cumulative = ASRResult(
        1,
        "current",
        99,
        16_000,
        19_200,
        f"{committed[0]}{committed[-1]}当前句",
        True,
    )
    decision = supervisor.accept_result(cumulative, session_id="session")

    assert decision.accepted is not None
    assert decision.accepted.text == "当前句"


@pytest.mark.asyncio
async def test_dense_victim_then_other_commit_does_not_inherit_victim_text(
    device_media_session: Any,
) -> None:
    """Card5 other=3/5: victim→other handoff must not inflate other text_len."""

    window = await device_media_session("media-victim-other-bleed", during_playback=False)
    registry = window.registry
    registry.turn_endpoint_grace_s = 60
    registry.turn_endpoint_min_grace_s = 60
    registry.turn_endpoint_max_grace_s = 60

    victim_start = 40_000
    await _accept_media_asr_final(
        registry,
        window.identity,
        sentence_id="victim-1",
        task_epoch=3,
        start_sample=victim_start,
        end_sample=victim_start + 1_600,
        text=_VICTIM,
    )
    fence, reason = await registry.commit_user_turn(
        window.identity.session_id,
        stream_epoch=window.identity.stream_epoch,
        start_sample=victim_start,
        end_sample=victim_start + 800,
        retire_sample=victim_start + 800,
    )
    assert fence is not None, reason
    assert window.provider.prepared == [_VICTIM]
    assert companion_safety_decision(window.provider.prepared[-1]) == "support_victim"

    # Post-watermark cumulative final: sample range clears the committed
    # watermark, but provider text still prepends the victim clause.
    other_start = window.context.runtime.speech_timeline.committed_sample
    assert other_start == victim_start + 800
    await _accept_media_asr_final(
        registry,
        window.identity,
        sentence_id="other-1",
        task_epoch=4,
        start_sample=other_start,
        end_sample=other_start + 1_600,
        text=_VICTIM + _OTHER,
    )
    fence, reason = await registry.commit_user_turn(
        window.identity.session_id,
        stream_epoch=window.identity.stream_epoch,
        start_sample=other_start,
        end_sample=other_start + 800,
        retire_sample=other_start + 800,
    )
    assert fence is not None, reason
    assert reason is None
    assert window.provider.prepared == [_VICTIM, _OTHER]
    assert len(window.provider.prepared[-1]) == len(_OTHER)
    assert companion_safety_decision(window.provider.prepared[-1]) == "support_relay"


def test_post_watermark_final_strips_knife_hua_slip_before_victim() -> None:
    """Committed 划 must peel a post-watermark 画+victim final to victim only."""

    supervisor = ASRStreamSupervisor()
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=9_600)
    knife = ASRResult(1, "knife", 1, 0, 1_600, _ESC_DAO, True)
    assert supervisor.accept_result(knife, session_id="session")
    supervisor.mark_committed(1_600)

    merged = ASRResult(1, "knife-victim", 1, 1_600, 4_800, _ESC_HUA + _VICTIM_PERIOD, True)
    decision = supervisor.accept_result(merged, session_id="session")

    assert decision.reason is ASRDecisionReason.ACCEPTED
    assert decision.accepted is not None
    assert decision.accepted.text == _VICTIM_PERIOD
    assert len(decision.accepted.text) == len(_VICTIM_PERIOD)
    assert companion_safety_decision(decision.accepted.text) == "support_victim"
    assert companion_safety_decision(_ESC_HUA + _VICTIM_PERIOD) == "crisis_self"


def test_post_watermark_hua_exact_repeat_of_dao_commit_is_kept() -> None:
    """Same-words 画 after a 划 commit must not peel to empty."""

    supervisor = ASRStreamSupervisor()
    supervisor.start_task()
    supervisor.record_audio(start_sample=0, frame_samples=9_600)
    first = ASRResult(1, "dao", 1, 0, 1_600, _ESC_DAO, True)
    assert supervisor.accept_result(first, session_id="session")
    supervisor.mark_committed(1_600)

    repeat = ASRResult(1, "hua", 1, 1_600, 3_200, _ESC_HUA, True)
    decision = supervisor.accept_result(repeat, session_id="session")

    assert decision.reason is ASRDecisionReason.ACCEPTED
    assert decision.accepted is not None
    assert decision.accepted.text == _ESC_HUA
    assert companion_safety_decision(decision.accepted.text) == "crisis_self"


@pytest.mark.asyncio
async def test_dense_self_then_victim_commit_does_not_inherit_escalation_text(
    device_media_session: Any,
) -> None:
    """Self then victim dense handoff must commit victim-only text."""

    window = await device_media_session("media-self-victim-bleed", during_playback=False)
    registry = window.registry
    registry.turn_endpoint_grace_s = 60
    registry.turn_endpoint_min_grace_s = 60
    registry.turn_endpoint_max_grace_s = 60

    self_start = 40_000
    await _accept_media_asr_final(
        registry,
        window.identity,
        sentence_id="self-1",
        task_epoch=3,
        start_sample=self_start,
        end_sample=self_start + 1_600,
        text=_SELF,
    )
    fence, reason = await registry.commit_user_turn(
        window.identity.session_id,
        stream_epoch=window.identity.stream_epoch,
        start_sample=self_start,
        end_sample=self_start + 800,
        retire_sample=self_start + 800,
    )
    assert fence is not None, reason
    assert window.provider.prepared == [_SELF]
    assert companion_safety_decision(window.provider.prepared[-1]) == "crisis_self"

    esc_start = window.context.runtime.speech_timeline.committed_sample
    await _accept_media_asr_final(
        registry,
        window.identity,
        sentence_id="esc-1",
        task_epoch=4,
        start_sample=esc_start,
        end_sample=esc_start + 1_600,
        text=_ESC_DAO,
    )
    fence, reason = await registry.commit_user_turn(
        window.identity.session_id,
        stream_epoch=window.identity.stream_epoch,
        start_sample=esc_start,
        end_sample=esc_start + 800,
        retire_sample=esc_start + 800,
    )
    assert fence is not None, reason
    assert window.provider.prepared == [_SELF, _ESC_DAO]

    victim_start = window.context.runtime.speech_timeline.committed_sample
    await _accept_media_asr_final(
        registry,
        window.identity,
        sentence_id="victim-1",
        task_epoch=5,
        start_sample=victim_start,
        end_sample=victim_start + 1_600,
        text=_ESC_HUA + _VICTIM_PERIOD,
    )
    fence, reason = await registry.commit_user_turn(
        window.identity.session_id,
        stream_epoch=window.identity.stream_epoch,
        start_sample=victim_start,
        end_sample=victim_start + 800,
        retire_sample=victim_start + 800,
    )
    assert fence is not None, reason
    assert reason is None
    assert window.provider.prepared == [_SELF, _ESC_DAO, _VICTIM_PERIOD]
    assert len(window.provider.prepared[-1]) == len(_VICTIM_PERIOD)
    assert companion_safety_decision(window.provider.prepared[-1]) == "support_victim"
