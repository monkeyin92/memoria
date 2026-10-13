"""Behavior tests for session-end child recap drafting."""

from __future__ import annotations

from services.guardian.session_recap import (
    SessionTurn,
    draft_session_recap,
    extract_interest_facts,
    redact_parent_summary_text,
    skip_session_recap,
)


def _turns(*pairs: tuple[str, str]) -> list[SessionTurn]:
    return [SessionTurn(role=role, text=text) for role, text in pairs]  # type: ignore[arg-type]


def test_skip_fewer_than_two_user_turns() -> None:
    assert skip_session_recap(_turns(("user", "你好"), ("assistant", "嗨"))) == "fewer_than_two_turns"


def test_skip_story_playback_only() -> None:
    turns = _turns(
        ("user", "讲个故事"),
        ("assistant", "从前有一座山"),
        ("user", "再讲一个故事吧"),
        ("assistant", "好的"),
    )
    assert skip_session_recap(turns) == "story_playback_only"


def test_session_end_draft_summary_and_interest_fact() -> None:
    turns = _turns(
        ("user", "我想听恐龙的故事"),
        ("assistant", "好呀"),
        ("user", "霸王龙好厉害"),
        ("assistant", "是啊"),
    )
    draft = draft_session_recap(
        subject_id="child-1",
        session_id="sess-1",
        source_event_id="end:sess-1",
        turns=turns,
        archive_consent=True,
    )
    assert draft.skipped_reason is None
    assert draft.summary is not None
    assert "聊了" in draft.summary.text
    assert "恐龙" in draft.summary.text or "霸王龙" in draft.summary.text
    assert draft.summary.session_id == "sess-1"
    texts = {fact.text for fact in draft.archive_facts}
    assert "喜欢恐龙" in texts
    assert "喜欢霸王龙" in texts


def test_archive_facts_gated_on_consent() -> None:
    turns = _turns(
        ("user", "我喜欢恐龙"),
        ("assistant", "真棒"),
        ("user", "还有小狗"),
        ("assistant", "好可爱"),
    )
    denied = draft_session_recap(
        subject_id="child-1",
        session_id="sess-1",
        source_event_id="end:sess-1",
        turns=turns,
        archive_consent=False,
    )
    assert denied.summary is not None
    assert denied.archive_facts == ()


def test_redacts_teacher_classmate_and_health() -> None:
    assert "老师" not in redact_parent_summary_text("王老师今天请假了")
    assert "同学" not in redact_parent_summary_text("小明同学来我家玩")
    assert "医院" not in redact_parent_summary_text("去医院打针了")
    draft = draft_session_recap(
        subject_id="child-1",
        session_id="sess-1",
        source_event_id="end:sess-1",
        turns=_turns(
            ("user", "王老师说要去医院"),
            ("assistant", "嗯"),
            ("user", "还有小明同学"),
            ("assistant", "好"),
        ),
        archive_consent=False,
    )
    assert draft.summary is not None
    assert "老师" not in draft.summary.text
    assert "同学" not in draft.summary.text
    assert "医院" not in draft.summary.text


def test_interest_extract_dedupes_same_label() -> None:
    facts = extract_interest_facts(
        "child-1",
        _turns(("user", "恐龙恐龙霸王龙"), ("user", "还是恐龙")),
    )
    labels = [fact.text for fact in facts]
    assert labels.count("喜欢恐龙") == 1
    assert "喜欢霸王龙" in labels
