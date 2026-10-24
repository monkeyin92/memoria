"""Behavior tests for session-end child recap drafting."""

from __future__ import annotations

import pytest
from services.guardian.session_recap import (
    SessionTurn,
    accept_parent_summary,
    draft_session_recap,
    extract_interest_facts,
    redact_name_and_health_terms,
    session_hits_crisis,
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
        ("user", "我最喜欢霸王龙"),
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
    assert texts == {"喜欢霸王龙"}


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
        _turns(("user", "我最喜欢恐龙"), ("user", "我真的最喜欢恐龙了")),
    )
    labels = [fact.text for fact in facts]
    assert labels == ["喜欢恐龙"]


def _facts(*pairs: tuple[str, str]) -> list[str]:
    return [f.text for f in extract_interest_facts("child-1", _turns(*pairs))]


def test_requests_and_fears_about_a_topic_write_no_preference() -> None:
    # G-5: only the child's own statement about themselves is an archive fact.
    assert _facts(("user", "给我讲个恐龙故事"), ("assistant", "好呀")) == []
    assert (
        _facts(
            ("user", "给我讲个恐龙故事"),
            ("assistant", "好呀"),
            ("user", "再讲一个吧"),
            ("assistant", "好的"),
        )
        == []
    )
    assert _facts(("user", "我怕小狗"), ("assistant", "别怕")) == []
    assert _facts(("user", "我讨厌小狗"), ("assistant", "嗯")) == []
    assert _facts(("user", "我害怕小狗"), ("assistant", "嗯")) == []
    assert _facts(("user", "我不喜欢恐龙"), ("assistant", "嗯")) == []
    assert _facts(("user", "我想学画画"), ("assistant", "好呀")) == []


def test_a_self_statement_makes_the_preference() -> None:
    assert _facts(("user", "我最喜欢恐龙"), ("assistant", "好呀")) == ["喜欢恐龙"]
    # With its own second statement, 「恐龙」 also becomes a preference.
    assert _facts(
        ("user", "其实我最喜欢霸王龙了"),
        ("assistant", "好呀"),
        ("user", "恐龙恐龙"),
        ("assistant", "嗯"),
    ) == ["喜欢霸王龙"]
    # A plain mention outside a self-statement is not a preference.
    assert _facts(("user", "恐龙恐龙"), ("assistant", "嗯")) == []


def test_someone_elses_preference_is_not_the_childs() -> None:
    assert _facts(("user", "我妈妈喜欢恐龙"), ("assistant", "嗯")) == []
    assert _facts(("user", "你喜欢恐龙吗"), ("assistant", "喜欢")) == []
    # A question about whether the robot got it is not a statement either.
    assert _facts(("user", "我现在在恐龙博物馆"), ("assistant", "哇")) == []


def test_a_sentence_is_read_whole_once_it_states_the_childs_liking() -> None:
    # The sentence itself states the liking, so its topics are read together;
    # 「我不喜欢」 on its own sentence is still a dislike.
    assert _facts(("user", "我喜欢小狗，也喜欢小猫"), ("assistant", "好")) == [
        "喜欢小狗",
        "喜欢小猫",
    ]
    assert _facts(("user", "我最喜欢小狗"), ("assistant", "好")) == ["喜欢小狗"]
    assert _facts(("user", "我最喜欢小狗。我也喜欢小猫"), ("assistant", "好")) == [
        "喜欢小狗",
        "喜欢小猫",
    ]
    assert _facts(("user", "小狗很可爱，我不喜欢它"), ("assistant", "嗯")) == []


@pytest.mark.parametrize(
    "turn",
    [
        "我不想活了",
        "我想死",
        "有人打我了",
        "我想结束生命",
    ],
)
def test_any_crisis_turn_suppresses_the_archive_facts(turn: str) -> None:
    turns = _turns(
        ("user", "我最喜欢恐龙"),
        ("assistant", "恐龙很大"),
        ("user", turn),
        ("assistant", "我在这儿陪你。"),
    )
    assert session_hits_crisis(turns) is True
    draft = draft_session_recap(
        subject_id="child-1",
        session_id="sess-1",
        source_event_id="end:sess-1",
        turns=turns,
        archive_consent=True,
        summary_text="聊了恐龙",
    )
    assert draft.archive_facts == ()
    assert draft.summary is None
    assert draft.skipped_reason == "crisis_in_session"


def test_an_ordinary_session_is_not_a_crisis() -> None:
    assert session_hits_crisis(_turns(("user", "我最喜欢恐龙"), ("assistant", "好呀"))) is False
