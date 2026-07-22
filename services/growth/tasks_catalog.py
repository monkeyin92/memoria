"""Server-owned prompt texts for structured learning tasks."""

from __future__ import annotations

from services.growth.domain import TaskKind

_PROMPTS: dict[TaskKind, tuple[str, str]] = {
    "natural_chat": ("natural-chat-v1", "自然聊聊最近让你在意的一件事。"),
    "life_interview": ("life-interview-v1", "讲讲一段对你影响很深的经历。"),
    "scenario_choice": ("scenario-choice-v1", "遇到重要选择时，你通常会先考虑什么？"),
    "decision_review": ("decision-review-v1", "复盘一次决定：你当时如何取舍，结果怎样？"),
}


def prompt_for(kind: TaskKind, prompt_id: str | None = None) -> tuple[str, str]:
    prompt = _PROMPTS[kind]
    if prompt_id is not None and prompt_id != prompt[0]:
        raise ValueError("unknown_prompt")
    return prompt


def prompt_kind_for(kind: TaskKind) -> str:
    return {
        "natural_chat": "spontaneous",
        "life_interview": "open",
        "scenario_choice": "structured",
        "decision_review": "open",
    }[kind]
