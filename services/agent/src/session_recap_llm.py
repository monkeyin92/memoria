"""Call the summary chat model for a parent-perspective session recap."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

import httpx

from services.common.llm_thinking import ThinkingMode, thinking_disabled, thinking_mode_for
from services.guardian.session_recap import (
    SESSION_SUMMARY_SYSTEM_PROMPT,
    SessionTurn,
    accept_parent_summary,
    session_summary_prompt,
)

logger = logging.getLogger(__name__)


class SessionSummaryModelConfig(Protocol):
    @property
    def api_key(self) -> str: ...

    @property
    def base_url(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def timeout_s(self) -> float: ...

    @property
    def thinking_mode(self) -> ThinkingMode: ...


@dataclass(frozen=True, slots=True)
class HttpSessionSummaryModelConfig:
    api_key: str
    base_url: str
    model: str
    timeout_s: float = 30.0
    thinking_mode: ThinkingMode = "dashscope"


def summary_model_config_from_settings(settings: object) -> HttpSessionSummaryModelConfig | None:
    """Pick DeepSeek or DashScope summary settings the same way Control API does."""

    provider = str(getattr(settings, "llm_provider", "") or "")
    if provider == "deepseek":
        key = str(getattr(settings, "deepseek_api_key", "") or "")
        if not key:
            return None
        return HttpSessionSummaryModelConfig(
            api_key=key,
            base_url=str(getattr(settings, "deepseek_base_url", "https://api.deepseek.com")),
            model=str(
                getattr(settings, "deepseek_summary_model", None)
                or getattr(settings, "deepseek_fast_model", "deepseek-flash")
            ),
            timeout_s=float(getattr(settings, "deepseek_summary_timeout_s", 30.0)),
            thinking_mode=thinking_mode_for("deepseek"),
        )
    if provider in {"qwen", "bailian_deepseek"}:
        key = str(getattr(settings, "dashscope_api_key", "") or "")
        if not key:
            return None
        return HttpSessionSummaryModelConfig(
            api_key=key,
            base_url=str(
                getattr(
                    settings,
                    "dashscope_compatible_base_url",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1",
                )
            ),
            model=str(
                getattr(settings, "dashscope_summary_model", None)
                or getattr(settings, "llm_fast_model", "qwen-flash")
            ),
            timeout_s=float(getattr(settings, "dashscope_summary_timeout_s", 30.0)),
            thinking_mode=thinking_mode_for(provider),
        )
    return None


async def generate_parent_session_summary(
    turns: Sequence[SessionTurn],
    *,
    config: SessionSummaryModelConfig,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Ask the summary model; return accepted text or None on any failure."""

    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=config.timeout_s)
    try:
        payload: dict[str, object] = {
            "model": config.model,
            "messages": [
                {"role": "system", "content": SESSION_SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": session_summary_prompt(turns)},
            ],
            "temperature": 0.2,
            "max_tokens": 400,
            **thinking_disabled(config.thinking_mode),
        }
        response = await http.post(
            f"{config.base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {config.api_key}"},
            json=payload,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            return None
        return accept_parent_summary(content, turns)
    except Exception as exc:
        logger.warning("session summary model failed error=%s", type(exc).__name__)
        return None
    finally:
        if owns_client:
            await http.aclose()
