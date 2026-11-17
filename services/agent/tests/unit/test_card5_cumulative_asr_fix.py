"""Card5 regression: one untimed Volc sentence held open across four turns.

asrdebug-20261105-163904, stream_epoch=3116, task_epoch=4, sentence_id=4 never
rotated. Each revision keeps begin_sample=1583216 and appends the next turn:

    turn 6  t005-o3  text_len=9   support-other-v1
    turn 7  t006-v1  text_len=8   victim-support-v1
    turn 8  t007-v2  text_len=16  victim-support-v1  (right label, replayed text)
    turn 9  t008-o1  text_len=25  victim-support-v1  (the misclassification)

Only the last revision's text was logged; the earlier ones are inferred from it
and reproduce all four logged lengths on the unfixed supervisor.
"""

from __future__ import annotations

from services.agent.src.voice_core.asr_stream_supervisor import (
    ASRAcceptDecision,
    ASRDecisionReason,
    ASRStreamSupervisor,
)
from services.agent.src.voice_core.speech_timeline import ASRResult

_SESSION = "card5-test-session"
_STREAM = 3116
_SENTENCE_START = 1_583_216
_OTHER = "我同学被别人打了。"
_VICTIM = "救命，有人打我。"
_VICTIM_FINAL_PUNCT = "救命！有人打我！"
_LOGGED_34_CHAR_FINAL = _OTHER + _VICTIM_FINAL_PUNCT + _VICTIM_FINAL_PUNCT + _OTHER

# (cumulative provider text, capture end = the turn's logged endpoint).
_CARD5_REVISIONS = (
    (_OTHER, 1_623_552),
    (_OTHER + _VICTIM, 1_978_112),
    (_OTHER + _VICTIM + _VICTIM, 2_359_072),
    (_LOGGED_34_CHAR_FINAL, 2_743_552),
)


def _final(
    sentence_id: str,
    revision: int,
    start: int,
    end: int,
    text: str,
) -> ASRResult:
    return ASRResult(
        task_epoch=4,
        sentence_id=sentence_id,
        revision=revision,
        capture_start_sample=start,
        capture_end_sample=end,
        text=text,
        is_final=True,
        stream_epoch=_STREAM,
    )


def _supervisor() -> ASRStreamSupervisor:
    supervisor = ASRStreamSupervisor(stream_epoch=_STREAM, task_epoch=4)
    supervisor.mark_committed(1_273_600)
    return supervisor


def _replay_card5(supervisor: ASRStreamSupervisor) -> list[ASRAcceptDecision]:
    decisions = []
    for revision, (text, end) in enumerate(_CARD5_REVISIONS, start=1):
        decision = supervisor.accept_result(
            _final("4", revision, _SENTENCE_START, end, text),
            session_id=_SESSION,
        )
        decisions.append(decision)
        supervisor.mark_committed(end)
    return decisions


def test_cumulative_final_does_not_commit_prior_victim_text() -> None:
    """t008-o1 must reach the classifier as the other-turn clause alone."""

    decision = _replay_card5(_supervisor())[-1]

    assert decision.reason is ASRDecisionReason.ACCEPTED
    assert decision.accepted is not None
    assert decision.accepted.text == _OTHER
    assert decision.accepted.capture_start_sample == 2_359_072
    assert decision.accepted.capture_end_sample == 2_743_552


def test_new_turn_text_commits_intact() -> None:
    supervisor = _supervisor()
    assert supervisor.accept_result(
        _final("4", 1, 1_583_216, 1_623_000, _VICTIM),
        session_id=_SESSION,
    ).accepted is not None
    supervisor.mark_committed(1_978_112)

    fresh = _final("5", 1, 2_000_000, 2_080_000, _OTHER)
    decision = supervisor.accept_result(fresh, session_id=_SESSION)

    assert decision.accepted == fresh


def test_legitimate_exact_repeat_preserved() -> None:
    """The same words in a new post-watermark sentence are that turn's speech."""

    supervisor = _supervisor()
    assert supervisor.accept_result(
        _final("4", 1, 1_583_216, 1_623_000, _VICTIM),
        session_id=_SESSION,
    ).accepted is not None
    supervisor.mark_committed(1_623_552)

    repeat = _final("5", 1, 1_700_000, 1_780_000, _VICTIM)
    decision = supervisor.accept_result(repeat, session_id=_SESSION)

    assert decision.accepted == repeat


def test_playback_followup_endpoint_path_with_victim_then_other() -> None:
    """Every turn keeps its own clause, including the repeated cry for help.

    Rejecting t007-v2 as "already committed" would drop a real safety utterance:
    ``_recover_rejected_semantic_final`` only rescues clock, close and live-lookup
    text, so a rejected crisis final has no other way into a turn.
    """

    decisions = _replay_card5(_supervisor())

    assert [decision.reason for decision in decisions] == [ASRDecisionReason.ACCEPTED] * 4
    assert [decision.accepted.text if decision.accepted else None for decision in decisions] == [
        _OTHER,
        _VICTIM,
        _VICTIM,
        _OTHER,
    ]
    assert [
        decision.accepted.capture_start_sample if decision.accepted else None
        for decision in decisions
    ] == [_SENTENCE_START, 1_623_552, 1_978_112, 2_359_072]


def test_tail_with_only_a_repeat_followed_by_new_clause_keeps_the_new_clause() -> None:
    supervisor = _supervisor()
    for revision, (text, end) in enumerate(_CARD5_REVISIONS[:2], start=1):
        supervisor.accept_result(
            _final("4", revision, _SENTENCE_START, end, text),
            session_id=_SESSION,
        )
        supervisor.mark_committed(end)

    decision = supervisor.accept_result(
        _final("4", 3, _SENTENCE_START, 2_359_072, _OTHER + _VICTIM + "我想回家。"),
        session_id=_SESSION,
    )

    assert decision.accepted is not None
    assert decision.accepted.text == "我想回家。"
