from __future__ import annotations

import pytest
from services.agent.src.voice_core.semantic_completeness import (
    SemanticCompleteness,
    classify_semantic_completeness,
    is_semantically_complete,
    is_semantically_incomplete,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Short commands & interruptions
        ("停", SemanticCompleteness.COMPLETE),
        ("别说了", SemanticCompleteness.COMPLETE),
        ("闭嘴", SemanticCompleteness.COMPLETE),
        ("暂停", SemanticCompleteness.COMPLETE),
        ("安静", SemanticCompleteness.COMPLETE),
        ("好了", SemanticCompleteness.COMPLETE),
        # Story & content requests
        ("讲个故事", SemanticCompleteness.COMPLETE),
        ("讲故事", SemanticCompleteness.COMPLETE),
        ("讲个故事吧", SemanticCompleteness.COMPLETE),
        ("唱首歌", SemanticCompleteness.COMPLETE),
        ("再讲一个", SemanticCompleteness.COMPLETE),
        ("换一个", SemanticCompleteness.COMPLETE),
        ("继续", SemanticCompleteness.COMPLETE),
        ("接着讲", SemanticCompleteness.COMPLETE),
        # Assistance & memory
        ("帮我", SemanticCompleteness.COMPLETE),
        ("帮我记一下", SemanticCompleteness.COMPLETE),
        ("帮我查一下", SemanticCompleteness.COMPLETE),
        ("记住这个", SemanticCompleteness.COMPLETE),
        ("提醒我明天去跑步", SemanticCompleteness.COMPLETE),
        # Greetings & confirmations
        ("你好", SemanticCompleteness.COMPLETE),
        ("早上好", SemanticCompleteness.COMPLETE),
        ("晚安", SemanticCompleteness.COMPLETE),
        ("拜拜", SemanticCompleteness.COMPLETE),
        ("好的", SemanticCompleteness.COMPLETE),
        ("对的", SemanticCompleteness.COMPLETE),
        ("是的", SemanticCompleteness.COMPLETE),
        ("不用了", SemanticCompleteness.COMPLETE),
        ("可以", SemanticCompleteness.COMPLETE),
        ("没问题", SemanticCompleteness.COMPLETE),
        ("谢谢你", SemanticCompleteness.COMPLETE),
        # Interrogatives & questions
        ("你是谁呀", SemanticCompleteness.COMPLETE),
        ("今天星期几", SemanticCompleteness.COMPLETE),
        ("为什么会下雨", SemanticCompleteness.COMPLETE),
        ("明天去公园好不好", SemanticCompleteness.COMPLETE),
        ("真的吗？", SemanticCompleteness.COMPLETE),
        ("然后呢", SemanticCompleteness.COMPLETE),
        ("还有谁呢？", SemanticCompleteness.COMPLETE),
        ("这个可以吗", SemanticCompleteness.COMPLETE),
        # Short self-contained declarative clauses
        ("我想听白雪公主的故事", SemanticCompleteness.COMPLETE),
        ("今天天气真好", SemanticCompleteness.COMPLETE),
        ("我喜欢小猫咪", SemanticCompleteness.COMPLETE),
        ("这是我的梦想", SemanticCompleteness.COMPLETE),
        ("小狗在草地上跑", SemanticCompleteness.COMPLETE),
        # Incomplete conjunctions / transitional connectors
        ("然后", SemanticCompleteness.INCOMPLETE),
        ("因为", SemanticCompleteness.INCOMPLETE),
        ("但是", SemanticCompleteness.INCOMPLETE),
        ("还有就是", SemanticCompleteness.INCOMPLETE),
        ("不过", SemanticCompleteness.INCOMPLETE),
        ("如果", SemanticCompleteness.INCOMPLETE),
        ("所以", SemanticCompleteness.INCOMPLETE),
        ("或者", SemanticCompleteness.INCOMPLETE),
        # Incomplete fillers & hesitations
        ("让我想想", SemanticCompleteness.INCOMPLETE),
        ("我想想", SemanticCompleteness.INCOMPLETE),
        ("想一想", SemanticCompleteness.INCOMPLETE),
        ("那个", SemanticCompleteness.INCOMPLETE),
        ("就是", SemanticCompleteness.INCOMPLETE),
        ("呃", SemanticCompleteness.INCOMPLETE),
        ("啊这个", SemanticCompleteness.INCOMPLETE),
        ("emm", SemanticCompleteness.INCOMPLETE),
        # Incomplete prepositions & dangling verbs
        ("小狗在", SemanticCompleteness.INCOMPLETE),
        ("我和", SemanticCompleteness.INCOMPLETE),
        ("我想", SemanticCompleteness.INCOMPLETE),
        ("想要", SemanticCompleteness.INCOMPLETE),
        ("打算", SemanticCompleteness.INCOMPLETE),
        ("准备", SemanticCompleteness.INCOMPLETE),
        ("我想听", SemanticCompleteness.INCOMPLETE),
        ("我想把", SemanticCompleteness.INCOMPLETE),
        ("把小猫抱走", SemanticCompleteness.COMPLETE),
        # Trailing punctuation preserving incompleteness
        ("因为。", SemanticCompleteness.INCOMPLETE),
        ("然后，", SemanticCompleteness.INCOMPLETE),
        ("让我想想…", SemanticCompleteness.INCOMPLETE),
        # Neutral / long unclassified utterances
        ("我今天在学校吃了一个特别特别甜而且味道很香的大苹果", SemanticCompleteness.NEUTRAL),
        ("", SemanticCompleteness.NEUTRAL),
        ("   ", SemanticCompleteness.NEUTRAL),
        ("。。", SemanticCompleteness.NEUTRAL),
    ],
)
def test_semantic_completeness_classification(text: str, expected: SemanticCompleteness) -> None:
    assert classify_semantic_completeness(text) == expected


def test_helper_predicates() -> None:
    assert is_semantically_complete("讲个故事吧") is True
    assert is_semantically_complete("然后") is False
    assert is_semantically_incomplete("然后") is True
    assert is_semantically_incomplete("讲个故事吧") is False
