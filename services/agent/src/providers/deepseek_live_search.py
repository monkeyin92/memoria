"""Fail-closed, public-only DeepSeek web search plus a short spoken answer."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from services.common.redaction import redact_pii
from services.common.response_depth import ResponseDepth, response_depth_for

logger = logging.getLogger(__name__)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
# The Search API rejects queries over 128 characters.
_MAX_SEARCH_QUERY_CHARS = 128
_MAX_RESULT_CHARS = 300
_SYSTEM_PROMPT = (
    "你负责回答一条当前公开的实时信息问题，回答会直接说给 3 到 12 岁的孩子听。"
    "只依据下面给出的联网搜索结果回答，结果里没有或对不上就直接说没有查到，不要编造。"
    "问今天、明天、后天的天气时，只采用日期对得上的结果。"
    "结果日期离现在较远时，说清楚那是哪天的消息，不要说成最近。"
    "不得使用、推断、要求或泄露用户身份、过往对话、记忆、资料、偏好或系统实现。"
    "不要朗读搜索过程、来源、网址或 Markdown 标记。"
    "暴力、灾难、犯罪等不适合孩子的内容不要展开，温和地带过。"
    "数字、日期用口语读法，用一到两句短而完整的中文说清楚。"
)


@dataclass(frozen=True, slots=True)
class DeepSeekLiveSearchConfig:
    api_key: str
    base_url: str
    model: str
    result_count: int = 5
    timeout_s: float = 12.0

    def __post_init__(self) -> None:
        url = httpx.URL(self.base_url)
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("DeepSeek live search base URL must be HTTP(S)")
        if not self.api_key.strip() or not self.model.strip() or self.timeout_s <= 0:
            raise ValueError("DeepSeek live search requires credentials, model, and timeout")
        if not 1 <= self.result_count <= 50:
            raise ValueError("DeepSeek live search result count must be 1 to 50")


class DeepSeekLiveSearch:
    """Search the web once, then answer from the results; failures return ``None``."""

    def __init__(
        self,
        config: DeepSeekLiveSearchConfig,
        *,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(_SHANGHAI),
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_s)
        self._owns_client = client is None
        self._now = now

    @property
    def model(self) -> str:
        return self._config.model

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def resolve(self, *, query: str) -> str | None:
        query = redact_pii(query.strip()) if isinstance(query, str) else ""
        if not query:
            return None
        depth = response_depth_for(query, realtime=True)
        started_at = time.monotonic()
        try:
            async with asyncio.timeout(self._config.timeout_s):
                results = await self._search(query)
                searched_ms = round((time.monotonic() - started_at) * 1000)
                if not results:
                    logger.info("deepseek live search empty elapsed_ms=%s", searched_ms)
                    return None
                content = await self._answer(query, results, depth.depth, depth.instruction)
        except (TimeoutError, httpx.HTTPError, TypeError, ValueError, KeyError, IndexError) as exc:
            logger.warning(
                "deepseek live search failed model=%s reason=%s",
                self._config.model,
                type(exc).__name__,
            )
            return None
        logger.info(
            "deepseek live search completed model=%s results=%s response_chars=%s "
            "search_ms=%s elapsed_ms=%s",
            self._config.model,
            len(results),
            len(content or ""),
            searched_ms,
            round((time.monotonic() - started_at) * 1000),
        )
        return content

    async def _search(self, query: str) -> list[dict[str, Any]]:
        response = await self._client.post(
            f"{self._config.base_url.rstrip('/').removesuffix('/v1')}/v1/tools/search",
            headers=self._headers(),
            json={"query": query[:_MAX_SEARCH_QUERY_CHARS], "count": self._config.result_count},
        )
        response.raise_for_status()
        results = response.json().get("results")
        return [item for item in results if isinstance(item, dict)] if isinstance(results, list) else []

    async def _answer(
        self,
        query: str,
        results: list[dict[str, Any]],
        depth: ResponseDepth,
        instruction: str,
    ) -> str | None:
        response = await self._client.post(
            f"{self._config.base_url.rstrip('/')}/chat/completions",
            headers=self._headers(),
            json={
                "model": self._config.model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            f"{_SYSTEM_PROMPT}\n{instruction}\n"
                            f"现在是北京时间{self._now():%Y年%m月%d日%H点%M分}。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": f"搜索结果：\n{_render(results)}\n\n问题：{query}",
                    },
                ],
                "stream": False,
                "max_tokens": 320 if depth is ResponseDepth.EXTENDED else 160,
                "thinking": {"type": "disabled"},
            },
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return content.strip() if isinstance(content, str) and content.strip() else None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_key}",
            "Content-Type": "application/json",
        }


def _render(results: list[dict[str, Any]]) -> str:
    lines = []
    for index, item in enumerate(results, 1):
        date = str(item.get("date") or "")[:10] or "无日期"
        body = str(item.get("summary") or item.get("snippet") or "").strip()
        lines.append(f"[{index}] {item.get('title') or ''}（{date}）{body}"[:_MAX_RESULT_CHARS])
    return "\n".join(lines)
