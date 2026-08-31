"""Small-model evidence for whether a turn needs a fresh public lookup."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 512
_SYSTEM_PROMPT = """\
你只判断当前一句话是否需要联网查询实时或时效性信息，不执行查询也不生成回答。
- NEEDS_LIVE_LOOKUP：需要最新、会变化或依赖当前时间地点的公开信息，例如天气、新闻、股价、汇率、航班/火车/动车时刻、路况、政策、赛事比分等。
- NO_LIVE_LOOKUP：闲聊、故事、常识、观点、回忆、固定知识或不需要联网即可回答的问题。
- UNSURE：无法可靠判断。
只输出且必须只输出一个枚举词：NEEDS_LIVE_LOOKUP、NO_LIVE_LOOKUP 或 UNSURE。"""


class LiveLookupSemanticVerdict(StrEnum):
    NEEDS_LIVE_LOOKUP = "NEEDS_LIVE_LOOKUP"
    NO_LIVE_LOOKUP = "NO_LIVE_LOOKUP"
    UNSURE = "UNSURE"


@dataclass(frozen=True, slots=True)
class LiveLookupSemanticClassifierConfig:
    api_key: str
    base_url: str
    model: str = "deepseek-v4-flash"
    timeout_s: float = 0.8

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("live lookup semantic classifier requires an API key")
        if not self.base_url.strip():
            raise ValueError("live lookup semantic classifier requires a base URL")
        if not self.model.strip():
            raise ValueError("live lookup semantic classifier requires a model")
        if self.timeout_s <= 0:
            raise ValueError("live lookup semantic classifier timeout must be positive")


def parse_live_lookup_semantic_verdict(raw: str) -> LiveLookupSemanticVerdict:
    try:
        return LiveLookupSemanticVerdict(raw.strip())
    except ValueError:
        return LiveLookupSemanticVerdict.UNSURE


class LiveLookupSemanticClassifier:
    def __init__(
        self,
        config: LiveLookupSemanticClassifierConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def classify(self, *, current_text: str) -> LiveLookupSemanticVerdict:
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": (current_text or "").strip()[-_MAX_TEXT_CHARS:]},
            ],
            "temperature": 0,
            "stream": False,
            "enable_thinking": False,
            "max_tokens": 16,
        }
        try:
            response = await asyncio.wait_for(
                self._client.post(
                    f"{self._config.base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                ),
                timeout=self._config.timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
            choices = payload.get("choices") if isinstance(payload, dict) else None
            first = choices[0] if isinstance(choices, list) and choices else None
            message = first.get("message") if isinstance(first, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            return (
                parse_live_lookup_semantic_verdict(content)
                if isinstance(content, str)
                else LiveLookupSemanticVerdict.UNSURE
            )
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError, IndexError):
            logger.warning("live lookup semantic classifier unavailable", exc_info=True)
            return LiveLookupSemanticVerdict.UNSURE
