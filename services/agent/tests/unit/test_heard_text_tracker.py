from __future__ import annotations

from services.agent.src.contracts.events import TimedWord
from services.agent.src.contracts.ids import GenerationFence
from services.agent.src.orchestration.heard_text_tracker import HeardTextTracker


def test_heard_text_truncates_to_played_words() -> None:
    tr = HeardTextTracker()
    full = "明天下午可能有雨，建议你带一把伞，晚上温度会更低。"
    tr.set_full_text(full)
    # First phrase words ~ 1320ms ending after "，"
    words = []
    t = 0
    phrase = "明天下午可能有雨，"
    for ch in phrase:
        words.append(TimedWord(text=ch, begin_ms=t, end_ms=t + 120))
        t += 120
    for ch in "建议你带一把伞，晚上温度会更低。":
        words.append(TimedWord(text=ch, begin_ms=t, end_ms=t + 120))
        t += 120
    tr.add_words(words)
    # playback 1.32s - 80ms margin = 1240ms → ends within first phrase
    start = 1_000_000_000
    tr.mark_playback_started(start)
    tr.mark_playback_stopped(start + 1_320_000_000)
    heard = tr.snapshot()
    assert heard.startswith("明天")
    assert "建议" not in heard
    assert "伞" not in heard


def test_prefer_under_count_when_degraded() -> None:
    tr = HeardTextTracker(alignment_degraded=True)
    tr.set_full_text("明天下午可能有雨，建议带伞。")
    start = 0
    tr.mark_playback_started(start)
    tr.mark_playback_stopped(start + 400_000_000)  # 400ms
    heard = tr.snapshot()
    assert "建议" not in heard or heard.endswith("，") or len(heard) < len(tr.full_text)


def test_alignment_status_is_fenced_to_current_generation_and_utterance() -> None:
    tracker = HeardTextTracker()
    current = GenerationFence("s", 2, 3, 0)
    stale = GenerationFence("s", 1, 2, 0)

    tracker.expect_utterance(current)
    assert tracker.observe_alignment(stale, "old-task", "started") is False
    assert tracker.observe_alignment(stale, "old-task", "degraded") is False
    assert tracker.alignment_degraded is False

    assert tracker.observe_alignment(current, "first-task", "started") is True
    assert tracker.observe_alignment(current, "retry-task", "started") is True
    assert tracker.observe_alignment(current, "first-task", "degraded") is False
    assert tracker.alignment_degraded is False

    assert tracker.observe_alignment(current, "retry-task", "degraded") is True
    assert tracker.alignment_degraded is True

    tracker.expect_utterance(current.bump_turn())
    assert tracker.alignment_degraded is False
    assert tracker.observe_alignment(current, "retry-task", "degraded") is False


def test_degraded_alignment_never_invents_a_precise_unpunctuated_suffix() -> None:
    tracker = HeardTextTracker(alignment_degraded=True)
    tracker.set_full_text("没有自然标点的回答")
    tracker.add_words([TimedWord(text="回答", begin_ms=800, end_ms=1000)])
    tracker.mark_playback_started(1_000_000_000)
    tracker.mark_playback_stopped(1_480_000_000)

    assert tracker.snapshot() == ""
