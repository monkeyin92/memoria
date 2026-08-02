"""Small-model evidence adapter for ambiguous barge-in transcripts."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from services.agent.src.orchestration.utterance_router import InterruptSemanticVerdict

logger = logging.getLogger(__name__)

_MAX_EVIDENCE_CHARS = 512
_SYSTEM_PROMPT = """\
你只判断一次语音打断话轮，不执行任何动作。
先从 final_transcript 中扣除 assistant_text 的回声、同音改写和交错尾音，再判断剩余内容：
- CONTROL_ONLY：剩余只表达停止、等待、暂停，或没有真实用户内容。
- HAS_USER_CONTENT：剩余包含用户自己的问题、纠正、补充或请求。用户引用助手原话后继续提问也属于此类。
- UNSURE：无法可靠区分。
只输出且必须只输出一个枚举词：CONTROL_ONLY、HAS_USER_CONTENT 或 UNSURE。"""


@dataclass(frozen=True, slots=True)
class InterruptSemanticClassifierConfig:
    api_key: str
    base_url: str
    model: str = "deepseek-v4-flash"
    timeout_s: float = 0.6

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("interrupt semantic classifier requires an API key")
        if not self.base_url.strip():
            raise ValueError("interrupt semantic classifier requires a base URL")
        if not self.model.strip():
            raise ValueError("interrupt semantic classifier requires a model")
        if self.timeout_s <= 0:
            raise ValueError("interrupt semantic classifier timeout must be positive")


def parse_interrupt_semantic_verdict(raw: str) -> InterruptSemanticVerdict:
    """Accept one exact enum token; explanations and JSON are invalid."""

    try:
        return InterruptSemanticVerdict(raw.strip())
    except ValueError:
        return InterruptSemanticVerdict.UNSURE


def _bounded(text: str) -> str:
    return (text or "").strip()[-_MAX_EVIDENCE_CHARS:]


class InterruptSemanticClassifier:
    """Reusable OpenAI-compatible client with a strict three-state output."""

    def __init__(
        self,
        config: InterruptSemanticClassifierConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def classify(
        self,
        *,
        final_text: str,
        sticky_text: str,
        assistant_text: str,
    ) -> InterruptSemanticVerdict:
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"sticky_transcript: {_bounded(sticky_text)}\n"
                        f"assistant_text: {_bounded(assistant_text)}\n"
                        f"final_transcript: {_bounded(final_text)}"
                    ),
                },
            ],
            "temperature": 0,
            "stream": False,
            "enable_thinking": False,
            "max_tokens": 12,
        }
        endpoint = f"{self._config.base_url.rstrip('/')}/chat/completions"
        try:
            response = await asyncio.wait_for(
                self._client.post(
                    endpoint,
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
            if not isinstance(content, str):
                return InterruptSemanticVerdict.UNSURE
            return parse_interrupt_semantic_verdict(content)
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError, IndexError):
            logger.warning("interrupt semantic classifier unavailable", exc_info=True)
            return InterruptSemanticVerdict.UNSURE
