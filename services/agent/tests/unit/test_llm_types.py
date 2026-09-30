from __future__ import annotations

import pytest
from services.agent.src.llm_types import ChatContext, ChatMessage


def test_serializes_messages_in_order_with_text_parts_joined() -> None:
    ctx = ChatContext.empty()
    ctx.add_message(role="system", content="规则")
    ctx.add_message(role="user", content=["第一行", "第二行"])
    ctx.add_message(role="assistant", content=[])

    assert ctx.to_openai_messages() == [
        {"role": "system", "content": "规则"},
        {"role": "user", "content": "第一行\n第二行"},
        {"role": "assistant", "content": ""},
    ]


def test_copy_is_shallow_so_edits_reach_the_original_messages() -> None:
    ctx = ChatContext.empty()
    reply = ctx.add_message(role="assistant", content="生成的回复")
    ctx.add_message(role="user", content="问题")

    copied = ctx.copy()
    copied.items[0].content = ["听到的回复"]
    copied.remove(copied.items[1])

    assert copied.items[0] is reply
    assert [item.text_content for item in ctx.items] == ["听到的回复", "问题"]
    assert [item.text_content for item in copied.items] == ["听到的回复"]


def test_remove_requires_a_message_in_the_context() -> None:
    ctx = ChatContext.empty()
    ctx.add_message(role="user", content="问题")

    with pytest.raises(ValueError):
        ctx.remove(ChatMessage(role="user", content=["问题"]))
