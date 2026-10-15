import pytest
from services.common.chat_llm_providers import (
    ChatLlmFamily,
    ChatLlmPlan,
    ChatLlmProvider,
    chat_llm_plan,
)


@pytest.mark.parametrize(
    ("llm_provider", "llm_failover_enabled", "backup", "families"),
    [
        ("deepseek", False, None, ("deepseek",)),
        ("deepseek", True, "qwen", ("deepseek", "qwen")),
        ("bailian_deepseek", False, None, ("deepseek",)),
        ("bailian_deepseek", True, "qwen", ("deepseek", "qwen")),
        ("qwen", False, None, ("qwen",)),
        ("qwen", True, "qwen", ("qwen",)),
    ],
)
def test_the_plan_lists_each_built_family_once_in_filing_order(
    llm_provider: ChatLlmProvider,
    llm_failover_enabled: bool,
    backup: ChatLlmProvider | None,
    families: tuple[ChatLlmFamily, ...],
) -> None:
    plan = chat_llm_plan(llm_provider=llm_provider, llm_failover_enabled=llm_failover_enabled)

    assert plan == ChatLlmPlan(primary=llm_provider, backup=backup)
    assert plan.families == families
