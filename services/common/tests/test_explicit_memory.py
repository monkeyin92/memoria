import pytest
from services.common.explicit_memory import (
    asks_to_remember,
    explicit_remember_content,
    explicit_remember_will_save,
    low_risk_self_fact_predicate,
    minor_sensitive_context,
)


@pytest.mark.parametrize(
    ("text", "content"),
    [
        ("帮我记住我最喜欢蓝色。", "我最喜欢蓝色。"),
        ("请帮我记住：我平时习惯早起。", "我平时习惯早起。"),
        ("请记住一下这件事，我喜欢音乐", "我喜欢音乐"),
        ("帮我记住  我喜欢绿色", "我喜欢绿色"),
    ],
)
def test_a_sentence_initial_command_yields_what_follows_it(text: str, content: str) -> None:
    assert explicit_remember_content(text) == content


@pytest.mark.parametrize(
    "text",
    [
        "",
        "我最喜欢蓝色。",
        "帮我记住",
        "帮我记住。",
        "帮我记住我喜欢蓝色吗？",
        "帮我记住我喜欢蓝色吗",
        "我想你帮我记住我最喜欢蓝色。",
        "记住我最喜欢蓝色。",
    ],
)
def test_anything_but_a_strict_command_yields_nothing(text: str) -> None:
    assert explicit_remember_content(text) is None


@pytest.mark.parametrize(
    "text",
    [
        # Round 10, first sentence after wake: the greeting echo came back as ASR text.
        "晚上好，你在？ 帮我记住，我最喜欢蓝色。",
        "哎，你好！ 帮我记住我最喜欢蓝色。",
        "晚上好，你在？帮我记住我最喜欢蓝色。",
        "哎，你好！ 晚上好，你在？ 请帮我记住我最喜欢蓝色。",
    ],
)
def test_one_or_two_lead_in_sentences_do_not_hide_a_self_fact_command(text: str) -> None:
    assert explicit_remember_content(text) in {"我最喜欢蓝色。", "我最喜欢蓝色。".strip()}


@pytest.mark.parametrize(
    "text",
    [
        # The lead-in belongs to the content: the extractor needs the whole sentence.
        "我明天考试。帮我记住这件事。",
        "今天学了乘法口诀。帮我记住今天学了乘法口诀。",
        # Not a closed low-risk self fact, so the lead-in path stays closed.
        "晚上好，你在？ 帮我记住我喜欢恐龙。",
        "晚上好，你在？ 帮我记住我爸爸叫小明。",
        # Three lead-ins, or one longer than a greeting, are not an echo.
        "好。嗯。啊。帮我记住我最喜欢蓝色。",
        "这是一个特别特别特别特别长的前一句话。帮我记住我最喜欢蓝色。",
        # No sentence end before the command: there is no telling where the lead-in stops.
        "晚上好你在帮我记住我最喜欢蓝色。",
    ],
)
def test_the_lead_in_path_is_narrow(text: str) -> None:
    assert explicit_remember_content(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "帮我记住我最喜欢蓝色",
        "请你记住，我喜欢音乐。",
        "你要记住我喜欢绿色",
        "晚上好，你在？ 帮我记住我最喜欢蓝色。",
        "给我记一下我喜欢红色",
        "别忘了我喜欢蓝色",
        "记住啦，帮我记一下我早睡。",
        "你记住我喜欢蓝色",
    ],
)
def test_asks_to_remember_finds_the_request_anywhere(text: str) -> None:
    assert asks_to_remember(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "我最喜欢蓝色",
        "我记住了",
        "你记住了吗",
        "你还记得我喜欢什么颜色吗",
        "你记住了吗？",
        "我会记住的",
        "老师让我记住这首诗",
    ],
)
def test_asks_to_remember_ignores_statements_and_questions_about_memory(text: str) -> None:
    assert asks_to_remember(text) is False


@pytest.mark.parametrize("minor", [True, False])
def test_the_closed_vocabulary_is_what_the_policy_will_save(minor: bool) -> None:
    assert explicit_remember_will_save("帮我记住我最喜欢蓝色。", minor=minor) is True
    assert explicit_remember_will_save("晚上好，你在？ 帮我记住我最喜欢蓝色。", minor=minor) is True
    # Outside the closed vocabulary, not a self preference, or not a command at all.
    assert explicit_remember_will_save("帮我记住我喜欢恐龙。", minor=minor) is False
    assert explicit_remember_will_save("帮我记住明天要交作业。", minor=minor) is False
    assert explicit_remember_will_save("我最喜欢蓝色。", minor=minor) is False
    assert explicit_remember_will_save("帮我记住我最喜欢蓝色吗？", minor=minor) is False
    # Family and contact details are sensitive for everyone, wherever in the sentence they sit.
    assert explicit_remember_will_save("帮我记住我爸爸喜欢蓝色。", minor=minor) is False
    assert explicit_remember_will_save("妈妈说得对。帮我记住我最喜欢蓝色。", minor=minor) is False
    assert explicit_remember_will_save("帮我记住我的手机号是13800138000。", minor=minor) is False


def test_a_minor_sentence_with_a_hard_context_is_never_saved_but_an_adult_one_is() -> None:
    text = "我今天被欺负了。帮我记住我最喜欢蓝色。"

    assert explicit_remember_will_save(text, minor=True) is False
    # The lead-in sentence is outside the command, so it only matters on the minor path.
    assert explicit_remember_will_save(text, minor=False) is True


def test_the_moved_vocabulary_checks_behave_as_they_did_in_the_archive() -> None:
    assert low_risk_self_fact_predicate("我最喜欢蓝色。") == "preference"
    assert low_risk_self_fact_predicate("我平时习惯早起") == "habit"
    assert low_risk_self_fact_predicate("我喜欢恐龙") is None
    assert low_risk_self_fact_predicate("我喜欢妈妈做的饭") is None
    assert low_risk_self_fact_predicate("我今年6岁，喜欢蓝色") is None
    assert minor_sensitive_context("老师批评了我") is True
    assert minor_sensitive_context("我不用紧张") is False
