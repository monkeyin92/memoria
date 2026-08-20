"""Fail-closed, public-only Qwen resolver for fresh information."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from services.common.response_depth import ResponseDepth, response_depth_for

logger = logging.getLogger(__name__)

_MAX_QUERY_CHARS = 1000
_SYSTEM_PROMPT = (
    "你负责回答一条当前公开的实时信息问题。只依据联网检索得到的可信结果回答；"
    "不得使用、推断、要求或泄露用户身份、过往对话、记忆、资料、偏好、会话标识或系统实现。"
    "无法确认时直接说明无法确认，不要编造。"
    "联网结果必须由你先整理后再回答用户，不要朗读搜索过程、检索来源、Markdown 标记、"
    "重复的候选项或无关背景。出行规划默认只给推荐方式、关键耗时和一条注意事项，"
    "用几句短而完整的中文说清楚；只有用户明确要求详细方案时才展开。"
)


@dataclass(frozen=True, slots=True)
class QwenRealtimeSearchConfig:
    api_key: str
    base_url: str
    model: str
    timeout_s: float = 12.0

    def __post_init__(self) -> None:
        url = httpx.URL(self.base_url)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("Qwen realtime search base URL must be HTTP(S)")
        if not self.api_key.strip() or not self.model.strip() or self.timeout_s <= 0:
            raise ValueError("Qwen realtime search requires credentials, model, and timeout")


class QwenRealtimeSearch:
    """Resolve one public query with forced search; provider failures return ``None``."""

    def __init__(
        self,
        config: QwenRealtimeSearchConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_s)
        self._owns_client = client is None

    @property
    def model(self) -> str:
        return self._config.model

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def resolve(self, *, query: str) -> str | None:
        query = query.strip() if isinstance(query, str) else ""
        if not query or len(query) > _MAX_QUERY_CHARS:
            return None
        depth = response_depth_for(query, realtime=True)
        max_tokens = 320 if depth.depth is ResponseDepth.EXTENDED else 160
        started_at = time.monotonic()
        try:
            response = await asyncio.wait_for(
                self._client.post(
                    f"{self._config.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._config.model,
                        "messages": [
                            {
                                "role": "system",
                                "content": f"{_SYSTEM_PROMPT}\n{depth.instruction}",
                            },
                            {"role": "user", "content": query},
                        ],
                        "stream": False,
                        "max_tokens": max_tokens,
                        "thinking": {"type": "disabled"},
                        "enable_search": True,
                        "search_options": {
                            "forced_search": True,
                            "search_strategy": "turbo",
                        },
                    },
                    timeout=self._config.timeout_s,
                ),
                timeout=self._config.timeout_s,
            )
            response.raise_for_status()
            content = self._content(response.json())
            elapsed_ms = round((time.monotonic() - started_at) * 1000)
            
            if elapsed_ms > 5000:
                logger.warning(
                    "slow qwen search detected model=%s response_chars=%s elapsed_ms=%s",
                    self._config.model,
                    len(content or ""),
                    elapsed_ms,
                )
            
            logger.info(
                "qwen realtime search completed model=%s response_chars=%s elapsed_ms=%s",
                self._config.model,
                len(content or ""),
                elapsed_ms,
            )
            return content
        except (TimeoutError, httpx.HTTPError, TypeError, ValueError, IndexError) as exc:
            logger.warning(
                "qwen realtime search failed model=%s reason=%s",
                self._config.model,
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _content(payload: Any) -> str | None:
        if not isinstance(payload, dict):
            return None
        choices = payload.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        return content.strip() if isinstance(content, str) and content.strip() else None
