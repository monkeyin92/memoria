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
        # Round 12 (2026-10-03), first sentence after wake: a 7-character final nobody said to the robot
        # was merged into the turn, and the question mark at its end made the whole sentence "a question".
        "帮我记住我最喜欢绿色。 你告诉我哪个？",
        "帮我记住我最喜欢绿色。 你告诉我哪个",
        "帮我记住我最喜欢绿色。你记住了吗？",
        "帮我记住我最喜欢绿色。好不好？你说呢。",
        "帮我记住我最喜欢绿色。嗯。",
        # A greeting echo ahead of the command and a stray sentence after it.
        "晚上好，你在？ 帮我记住，我最喜欢绿色。 你记住了吗？",
    ],
)
def test_one_or_two_short_sentences_after_the_command_do_not_hide_a_self_fact(text: str) -> None:
    assert explicit_remember_content(text) == "我最喜欢绿色。"
    for minor in (True, False):
        assert explicit_remember_will_save(text, minor=minor) is True


@pytest.mark.parametrize(
    "text",
    [
        # The command sentence itself is the question.
        "帮我记住我最喜欢蓝色吗？你说呢。",
        # Only a closed low-risk self fact may have a stray sentence after it.
        "帮我记住我喜欢恐龙。你告诉我哪个？",
        "帮我记住明天要交作业。你告诉我哪个？",
        # Three, or one longer than a stray remark, are a conversation, not a remark.
        "帮我记住我最喜欢蓝色。好。嗯。啊。",
        "帮我记住我最喜欢蓝色。然后我们明天要去动物园玩好不好呀可以吗？",
        # A retraction after the command takes the request back.
        "帮我记住我最喜欢蓝色。算了不用了。",
        "帮我记住我最喜欢蓝色。不对，是绿色。",
        "帮我记住我最喜欢蓝色。你别记了。",
        "帮我记住我最喜欢蓝色。其实是绿色。",
        "帮我记住我最喜欢蓝色。换成绿色吧。",
        # Family details are sensitive wherever they sit in the sentence.
        "帮我记住我最喜欢蓝色。我爸爸叫小明。",
    ],
)
def test_the_trailing_sentence_path_is_narrow(text: str) -> None:
    for minor in (True, False):
        assert explicit_remember_will_save(text, minor=minor) is False


@pytest.mark.parametrize(
    "text",
    [
        # Round 13 (2026-10-04), first sentence after wake: FunASR glued the two breaths of
        # 「帮我记住我最喜欢绿色。你记住了吗？」 with a comma, so the final question mark hid the command.
        "帮我记住我最喜欢绿色，你记住了吗？",
        "帮我记住我最喜欢绿色,你记住了吗?",
        "帮我记住我最喜欢绿色，你记住了吗",
        "帮我记住我最喜欢绿色，记住了没有？",
        "帮我记住我最喜欢绿色，你告诉我哪个？",
        "帮我记住我最喜欢绿色，好吗？",
        "帮我记住我最喜欢绿色，行不行？",
        # The comma may stand inside the command as well, with a greeting echo ahead of it.
        "帮我记住，我最喜欢绿色，你记住了吗？",
        "晚上好，你在？ 帮我记住，我最喜欢绿色，你记住了吗？",
        # A stray sentence ahead of the question goes with it, and a comma with nothing before it is no clause.
        "帮我记住我最喜欢绿色。好的，你记住了吗？",
        "帮我记住我最喜欢绿色。，你记住了吗？",
    ],
)
def test_a_question_joined_to_the_command_by_a_comma_does_not_hide_a_self_fact(text: str) -> None:
    assert explicit_remember_content(text) == "我最喜欢绿色。"
    for minor in (True, False):
        assert explicit_remember_will_save(text, minor=minor) is True


@pytest.mark.parametrize(
    "text",
    [
        # Only a closed low-risk self fact may have a question cut off after it.
        "帮我记住我喜欢恐龙，你记住了吗？",
        "帮我记住明天要交作业，你记住了吗？",
        # The head has to be the command, and the question the last clause of the sentence.
        "你记住了吗，帮我记住我最喜欢绿色？",
        "我最喜欢绿色，你记住了吗？",
        "帮我记住我最喜欢绿色吗，你说呢？",
        # A clause that takes the request back, or one too long to be a remark, is not cut off.
        "帮我记住我最喜欢绿色，算了吧？",
        "帮我记住我最喜欢绿色，不对吗？",
        "帮我记住我最喜欢绿色，你到底有没有听清楚我刚才说的这件事情呢？",
        # Without a question nothing tells a remark from the rest of the content.
        "帮我记住我最喜欢绿色，谢谢你。",
        # A comma also separates the items of the content, and a clause that names a value is no remark.
        "帮我记住我喜欢红色，蓝色，绿色？",
        "帮我记住我最喜欢绿色，还是红色？",
        "帮我记住我最喜欢绿色，是红色吗？",
        # Family details stay sensitive wherever they sit in the sentence.
        "帮我记住我最喜欢绿色，你记住我妈妈了吗？",
    ],
)
def test_the_comma_question_path_is_narrow(text: str) -> None:
    for minor in (True, False):
        assert explicit_remember_will_save(text, minor=minor) is False


def test_a_comma_between_the_items_of_the_content_is_still_content() -> None:
    # The sentence is NFKC-normalized first, so the full-width comma comes back as an ASCII one.
    assert explicit_remember_content("帮我记住我喜欢红色，蓝色。") == "我喜欢红色,蓝色。"
    assert explicit_remember_content("帮我记住我喜欢红色，蓝色") == "我喜欢红色,蓝色"
    assert explicit_remember_will_save("帮我记住我喜欢红色，蓝色。", minor=True) is True


def test_what_is_not_a_closed_fact_keeps_its_whole_sentence() -> None:
    # The extractor needs the whole sentence when the fact is not one the archive confirms on its own.
    assert explicit_remember_content("帮我记住明天要交作业。你说呢。") == "明天要交作业。你说呢。"
    assert explicit_remember_content("帮我记住我喜欢恐龙。你告诉我哪个？") is None
    assert (
        explicit_remember_content("帮我记住我最喜欢蓝色。算了不用了。")
        == "我最喜欢蓝色。算了不用了。"
    )


def test_a_stray_trailing_question_alone_does_not_make_a_command() -> None:
    assert explicit_remember_content("我最喜欢绿色。 你告诉我哪个？") is None
    assert explicit_remember_content("帮我记住我最喜欢蓝色吗？你说呢。") != "我最喜欢蓝色。"
    assert (
        explicit_remember_content("帮我记住我最喜欢蓝色。然后我们明天要去动物园玩好不好呀可以吗？")
        is None
    )


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
        # A question mark ends only its own sentence: a later question does not take the command back.
        "帮我记住我最喜欢绿色。 你告诉我哪个？",
        "帮我记住我最喜欢蓝色。你记住了吗？",
        # Round 12: the ASR dropped 「帮」 and put a stray word ahead of the request.
        "滚蛋！ 我记住我最喜欢绿色。",
        "我记住，我最喜欢绿色。",
        # Round 13: a question that rides on the command's sentence after a comma does not take it back.
        "帮我记住我最喜欢绿色，你记住了吗？",
        "帮我记住我的新书包,你记住了吗?",
        "帮我记住我最喜欢绿色，你记住了吗",
        "帮我记住，我最喜欢绿色，你记住了吗，好不好？",
        "你记住了吗，帮我记住我最喜欢绿色，好吗？",
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
        "我记住了。",
        "我记住啦！",
        "好，我记住这首诗了",
        "帮我记住我最喜欢蓝色吗？",
        "你能帮我记住我最喜欢蓝色吗？你说呢？",
        # A comma does not turn a question about memory into a request.
        "你记住了吗，好吗？",
        "你能记住我的名字吗，好不好？",
        "我最喜欢什么颜色，你记住了吗？",
        "你还记得我喜欢什么颜色吗，告诉我？",
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
