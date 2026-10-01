"""Where the per-turn semantic classifiers send their requests.

They follow ``LLM_PROVIDER`` like the reply model.  Qwen and Bailian keep the
DashScope key and each classifier's own model; the official DeepSeek API only
serves DeepSeek models, so there every classifier uses ``DEEPSEEK_FAST_MODEL``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.common.llm_thinking import ThinkingMode, thinking_mode_for

_DASHSCOPE_COMPATIBLE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


@dataclass(frozen=True, slots=True)
class SemanticEndpoint:
    api_key: str
    base_url: str
    model: str
    thinking_mode: ThinkingMode


def semantic_endpoint(settings: Any, *, dashscope_model: str) -> SemanticEndpoint | None:
    """The endpoint for one classifier, or ``None`` when its provider has no key."""

    thinking_mode = thinking_mode_for(str(getattr(settings, "llm_provider", "qwen")))
    if thinking_mode == "deepseek":
        api_key = str(getattr(settings, "deepseek_api_key", "") or "").strip()
        base_url = str(getattr(settings, "deepseek_base_url", "https://api.deepseek.com"))
        model = str(getattr(settings, "deepseek_fast_model", "deepseek-flash"))
    else:
        api_key = str(getattr(settings, "dashscope_api_key", "") or "").strip()
        base_url = str(
            getattr(settings, "dashscope_compatible_base_url", _DASHSCOPE_COMPATIBLE_BASE_URL)
        )
        model = dashscope_model
    if not api_key:
        return None
    return SemanticEndpoint(
        api_key=api_key,
        base_url=base_url,
        model=model,
        thinking_mode=thinking_mode,
    )
