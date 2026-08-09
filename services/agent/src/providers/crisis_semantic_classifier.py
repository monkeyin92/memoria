"""Bounded small-model evidence for ambiguous crisis language.

This adapter never chooses a response, creates a notification, or receives
history.  The deterministic crisis policy remains the sole execution point.
"""

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
你只为当前一句话提供危机语义证据，不执行回复、通知或分级。
- SELF_CRISIS：说话人本人明确表达自伤、轻生、已采取行动或马上行动。
- SUPPORT_FOR_OTHER：替他人求助、引用他人的话或一般性咨询。
- NO_CRISIS：没有上述语义。
- UNSURE：上下文不足，不能可靠判断。
只输出且必须只输出一个枚举词：SELF_CRISIS、SUPPORT_FOR_OTHER、NO_CRISIS 或 UNSURE。"""


class CrisisSemanticVerdict(StrEnum):
    SELF_CRISIS = "SELF_CRISIS"
    SUPPORT_FOR_OTHER = "SUPPORT_FOR_OTHER"
    NO_CRISIS = "NO_CRISIS"
    UNSURE = "UNSURE"


@dataclass(frozen=True, slots=True)
class CrisisSemanticClassifierConfig:
    api_key: str
    base_url: str
    model: str = "deepseek-v4-flash"
    timeout_s: float = 0.8

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("crisis semantic classifier requires an API key")
        if not self.base_url.strip():
            raise ValueError("crisis semantic classifier requires a base URL")
        if not self.model.strip():
            raise ValueError("crisis semantic classifier requires a model")
        if self.timeout_s <= 0:
            raise ValueError("crisis semantic classifier timeout must be positive")


def parse_crisis_semantic_verdict(raw: str) -> CrisisSemanticVerdict:
    try:
        return CrisisSemanticVerdict(raw.strip())
    except ValueError:
        return CrisisSemanticVerdict.UNSURE


class CrisisSemanticClassifier:
    def __init__(
        self,
        config: CrisisSemanticClassifierConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def classify(self, *, current_text: str) -> CrisisSemanticVerdict:
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": (current_text or "").strip()[-_MAX_TEXT_CHARS:]},
            ],
            "temperature": 0,
            "stream": False,
            "enable_thinking": False,
            "max_tokens": 12,
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
                parse_crisis_semantic_verdict(content)
                if isinstance(content, str)
                else CrisisSemanticVerdict.UNSURE
            )
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError, IndexError):
            logger.warning("crisis semantic classifier unavailable", exc_info=True)
            return CrisisSemanticVerdict.UNSURE
