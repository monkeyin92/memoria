from __future__ import annotations

from typing import Final

_FAMILY_BY_PROVIDER: Final = {
    "deepseek": "deepseek",
    "bailian_deepseek": "deepseek",
    "qwen": "qwen",
}
_FAMILY_ORDER: Final = ("deepseek", "qwen")


def chat_llm_providers(*, llm_provider: str, llm_failover_enabled: bool) -> list[str]:
    families = {_FAMILY_BY_PROVIDER[llm_provider]}
    if llm_failover_enabled:
        families.add("qwen")
    return [family for family in _FAMILY_ORDER if family in families]
