import pytest
from services.common.chat_llm_providers import chat_llm_providers


@pytest.mark.parametrize(
    ("llm_provider", "llm_failover_enabled", "expected"),
    [
        ("deepseek", False, ["deepseek"]),
        ("qwen", False, ["qwen"]),
        ("deepseek", True, ["deepseek", "qwen"]),
        ("bailian_deepseek", False, ["deepseek"]),
        ("qwen", True, ["qwen"]),
        ("bailian_deepseek", True, ["deepseek", "qwen"]),
    ],
)
def test_chat_llm_providers_orders_filing_families(
    llm_provider: str,
    llm_failover_enabled: bool,
    expected: list[str],
) -> None:
    assert (
        chat_llm_providers(
            llm_provider=llm_provider,
            llm_failover_enabled=llm_failover_enabled,
        )
        == expected
    )
