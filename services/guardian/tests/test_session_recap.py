"""Behavior tests for session-end child recap drafting."""

from __future__ import annotations

from services.guardian.session_recap import (
    SessionTurn,
    accept_parent_summary,
    draft_session_recap,
    extract_interest_facts,
    redact_name_and_health_terms,
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


def test_session_end_draft_uses_llm_summary_and_interest_facts() -> None:
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
        summary_text="聊了恐龙和霸王龙",
    )
    assert draft.skipped_reason is None
    assert draft.summary is not None
    assert draft.summary.text == "聊了恐龙和霸王龙"
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
        summary_text="聊了恐龙和小狗",
    )
    assert denied.summary is not None
    assert denied.archive_facts == ()


def test_rejects_raw_transcript_excerpts_instead_of_stitching() -> None:
    turns = _turns(
        ("user", "我今天在公园看到黄色小狗"),
        ("assistant", "哇"),
        ("user", "它好可爱"),
        ("assistant", "是啊"),
    )
    assert accept_parent_summary("聊了我今天在公园看到黄色", turns) is None
    draft = draft_session_recap(
        subject_id="child-1",
        session_id="sess-1",
        source_event_id="end:sess-1",
        turns=turns,
        archive_consent=False,
        summary_text=None,
        skipped_reason="summary_model_failed",
    )
    assert draft.summary is None
    assert draft.skipped_reason == "summary_model_failed"


def test_name_and_health_post_check_does_not_mangle_unrelated_words() -> None:
    # Bare role words without a name stay; named roles and health terms go.
    assert "同学们" in redact_name_and_health_terms("和同学们玩")
    assert "王老师" not in redact_name_and_health_terms("王老师今天请假了")
    assert "医院" not in redact_name_and_health_terms("去医院打针了")
    turns = _turns(
        ("user", "王老师说要去医院"),
        ("assistant", "嗯"),
        ("user", "还有小明同学"),
        ("assistant", "好"),
    )
    assert accept_parent_summary("聊了王老师和医院", turns) is None
    assert accept_parent_summary("聊了学校和周末安排", turns) == "聊了学校和周末安排"
    assert accept_parent_summary("聊了阳光小学的操场", turns) is None
    assert accept_parent_summary("提到了幸福路12号", turns) is None
    assert accept_parent_summary("聊了被打和被性侵", turns) is None
    assert accept_parent_summary("提到了危机情况", turns) is None
    assert accept_parent_summary("和小明聊了画画", turns) == "和小明聊了画画"
    assert "只写昵称" in __import__(
        "services.guardian.session_recap", fromlist=["SESSION_SUMMARY_USER_PROMPT_PREFIX"]
    ).SESSION_SUMMARY_USER_PROMPT_PREFIX


def test_interest_extract_dedupes_same_label() -> None:
    facts = extract_interest_facts(
        "child-1",
        _turns(("user", "恐龙恐龙霸王龙"), ("user", "还是恐龙")),
    )
    labels = [fact.text for fact in facts]
    assert labels.count("喜欢恐龙") == 1
    assert "喜欢霸王龙" in labels
