"""The request field that keeps a chat model out of its thinking mode.

DashScope's compatible mode reads ``enable_thinking``.  DeepSeek's official API
ignores that field and thinks by default, so a short classifier or JSON call
spends its whole token budget on reasoning and returns empty content unless it
sends ``thinking: {"type": "disabled"}``.
"""

from __future__ import annotations

from typing import Literal

ThinkingMode = Literal["dashscope", "deepseek"]


def thinking_disabled(mode: ThinkingMode) -> dict[str, object]:
    """Request fields that turn thinking off for this provider's API."""

    if mode == "deepseek":
        return {"thinking": {"type": "disabled"}}
    return {"enable_thinking": False}


def thinking_mode_for(llm_provider: str) -> ThinkingMode:
    """Only the official DeepSeek API needs DeepSeek's switch; Bailian is DashScope."""

    return "deepseek" if llm_provider == "deepseek" else "dashscope"
