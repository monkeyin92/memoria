"""Shared LLM failover constants and backup reply copy gates.

Beijing DashScope is the only allowed backup base URL. Backup chat text is
checked against the product-dept forbidden-copy list before it is spoken.
"""

from __future__ import annotations

from typing import Final

from services.common.companion_response_safety import SAFE_UNKNOWN_REPLY

LLM_UNAVAILABLE_LINE_ID: Final = "llm_unavailable"
LLM_UNAVAILABLE_TEXT: Final = "我现在有点卡住了，等一会儿再来找我聊吧。"
LLM_UNAVAILABLE_STANDBY_REASON: Final = "llm_unavailable"

DASHSCOPE_CN_COMPATIBLE_BASE_URL: Final = (
    "https://dashscope.aliyuncs.com/compatible-mode/v1"
)
_BLOCKED_DASHSCOPE_HOST_MARKERS: Final = (
    "dashscope-intl",
    "dashscope-us",
    "dashscope-eu",
)
DASHSCOPE_LLM_PROVIDERS: Final = frozenset({"qwen", "bailian_deepseek"})


def dashscope_endpoint_in_use(
    *,
    llm_provider: str,
    llm_failover_enabled: bool = False,
) -> bool:
    """True when a configured path will call the DashScope compatible endpoint."""

    return llm_failover_enabled or llm_provider in DASHSCOPE_LLM_PROVIDERS

# Same words as apps/miniprogram forbidden-ui-copy (product dept).
PRODUCT_FORBIDDEN_COPY: Final = frozenset(
    ("危机", "伙伴", "好朋友", "家人", "推送", "通知")
)

LLM_UNAVAILABLE_AIGC: Final = {
    "content_id": LLM_UNAVAILABLE_LINE_ID,
    "service_provider": "Memoria",
    "ai_generated": True,
}


def normalize_dashscope_compatible_base_url(value: str) -> str:
    """Return a validated China North 2 DashScope OpenAI-compatible base URL."""

    cleaned = value.strip().rstrip("/")
    if not cleaned:
        cleaned = DASHSCOPE_CN_COMPATIBLE_BASE_URL.rstrip("/")
    lowered = cleaned.lower()
    if any(marker in lowered for marker in _BLOCKED_DASHSCOPE_HOST_MARKERS):
        raise ValueError(
            "DASHSCOPE_COMPATIBLE_BASE_URL must be the China North 2 (Beijing) "
            "endpoint, not an intl/us region URL"
        )
    if lowered != DASHSCOPE_CN_COMPATIBLE_BASE_URL.rstrip("/").lower():
        raise ValueError(
            "DASHSCOPE_COMPATIBLE_BASE_URL must be "
            f"{DASHSCOPE_CN_COMPATIBLE_BASE_URL}"
        )
    return DASHSCOPE_CN_COMPATIBLE_BASE_URL


def backup_reply_passes_forbidden_copy(text: str) -> bool:
    """True when backup model text contains none of the product forbidden words."""

    return not any(word in text for word in PRODUCT_FORBIDDEN_COPY)


def gate_backup_reply_copy(text: str) -> str:
    """Replace backup replies that hit product forbidden copy with the safe line."""

    if backup_reply_passes_forbidden_copy(text):
        return text
    return SAFE_UNKNOWN_REPLY


__all__ = [
    "DASHSCOPE_CN_COMPATIBLE_BASE_URL",
    "DASHSCOPE_LLM_PROVIDERS",
    "LLM_UNAVAILABLE_AIGC",
    "LLM_UNAVAILABLE_LINE_ID",
    "LLM_UNAVAILABLE_STANDBY_REASON",
    "LLM_UNAVAILABLE_TEXT",
    "PRODUCT_FORBIDDEN_COPY",
    "backup_reply_passes_forbidden_copy",
    "dashscope_endpoint_in_use",
    "gate_backup_reply_copy",
    "normalize_dashscope_compatible_base_url",
]
