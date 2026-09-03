"""Small-model evidence for whether a turn should end the device conversation."""

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
你只判断当前一句话是否在请求结束与语音助手的对话并进入待命/休息，不执行回复或任何动作。
- END_SESSION：明确告别、让人退下、先不聊了、不用陪了、可以休息了、dismiss/goodbye 等结束会话请求。
- CONTINUE：普通聊天、提问、讲故事、继续当前话题，或只是在讨论「再见/拜拜」这个词本身而非结束对话。
- UNSURE：上下文不足，不能可靠判断。
只输出且必须只输出一个枚举词：END_SESSION、CONTINUE 或 UNSURE。"""


class CloseIntentSemanticVerdict(StrEnum):
    END_SESSION = "END_SESSION"
    CONTINUE = "CONTINUE"
    UNSURE = "UNSURE"


@dataclass(frozen=True, slots=True)
class CloseIntentSemanticClassifierConfig:
    api_key: str
    base_url: str
    model: str = "qwen-flash"
    timeout_s: float = 0.8

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("close intent semantic classifier requires an API key")
        if not self.base_url.strip():
            raise ValueError("close intent semantic classifier requires a base URL")
        if not self.model.strip():
            raise ValueError("close intent semantic classifier requires a model")
        if self.timeout_s <= 0:
            raise ValueError("close intent semantic classifier timeout must be positive")


def parse_close_intent_semantic_verdict(raw: str) -> CloseIntentSemanticVerdict:
    try:
        return CloseIntentSemanticVerdict(raw.strip())
    except ValueError:
        return CloseIntentSemanticVerdict.UNSURE


class CloseIntentSemanticClassifier:
    def __init__(
        self,
        config: CloseIntentSemanticClassifierConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def classify(self, *, current_text: str) -> CloseIntentSemanticVerdict:
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
                parse_close_intent_semantic_verdict(content)
                if isinstance(content, str)
                else CloseIntentSemanticVerdict.UNSURE
            )
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError, IndexError):
            logger.warning("close intent semantic classifier unavailable", exc_info=True)
            return CloseIntentSemanticVerdict.UNSURE
