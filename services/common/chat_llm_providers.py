"""Which chat model families the voice agent builds, for About's model filing lines."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal, assert_never, get_args

ChatLlmProvider = Literal["qwen", "bailian_deepseek", "deepseek"]
# Declaration order is the order About lists the filings in.
ChatLlmFamily = Literal["deepseek", "qwen"]
CHAT_LLM_FAMILIES: Final[tuple[ChatLlmFamily, ...]] = get_args(ChatLlmFamily)


def _family(provider: ChatLlmProvider) -> ChatLlmFamily:
    match provider:
        case "deepseek" | "bailian_deepseek":
            return "deepseek"
        case "qwen":
            return "qwen"
        case _:
            assert_never(provider)


@dataclass(frozen=True, slots=True)
class ChatLlmPlan:
    """The chat models the voice agent builds for every session.

    ``build_language_model_handler`` builds exactly this plan and the heartbeat
    reports its families, so About discloses what the agent runs.
    """

    primary: ChatLlmProvider
    backup: ChatLlmProvider | None

    @property
    def families(self) -> tuple[ChatLlmFamily, ...]:
        built = {_family(self.primary)}
        if self.backup is not None:
            built.add(_family(self.backup))
        return tuple(family for family in CHAT_LLM_FAMILIES if family in built)


def chat_llm_plan(*, llm_provider: ChatLlmProvider, llm_failover_enabled: bool) -> ChatLlmPlan:
    """Failover adds the Beijing DashScope qwen backup. Nothing else adds a model."""
    return ChatLlmPlan(primary=llm_provider, backup="qwen" if llm_failover_enabled else None)
