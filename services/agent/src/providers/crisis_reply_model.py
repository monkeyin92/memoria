"""One fast-model pass that drafts a crisis line and another that reviews it.

The endpoint is the same one the crisis classifier already uses. A failure
here is not a spoken line. The caller plays the short fallback script.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from services.common.llm_thinking import ThinkingMode, thinking_disabled
from services.common.redaction import redact_pii

logger = logging.getLogger(__name__)

_MAX_QUERY_CHARS = 512
_GENERATE_SYSTEM = """\
你在给孩子写危机回复。只输出要说的话，不要解释。
先点名孩子刚才说的事，再安慰。自称小助手。最多三句。
不要答应保密。不要讲自伤或伤害的方法。
{tier_rule}"""
_TIER_RULE = {
    "crisis_self": "必须请孩子马上去找身边的大人，请大人拨幺二三五六，已经受伤要请大人打幺二零。",
    "support_victim": "必须请孩子马上去找身边的大人，有危险要请大人打幺幺零。",
    "support_relay": "必须请孩子去告诉身边的大人或老师。不要报电话号码。",
}
_REVIEW_SYSTEM = """\
你只检查这句给孩子的回复是否同时满足：点名了孩子说的事、有安慰、找了身边的大人或老师、热线和档位一致（自伤要有幺二三五六和幺二零，受害者要有幺幺零，帮朋友只找大人或老师）、没有保密承诺、没有自伤方法。
只输出 PASS 或 FAIL。"""
_FAILURE_TEXT = {
    "adult": "没有请孩子找身边的大人、爸爸妈妈或老师",
    "hotline_12356": "没有请大人拨幺二三五六",
    "hotline_120": "没有请大人打幺二零",
    "hotline_110": "没有请大人打幺幺零",
    "confidentiality": "出现了保密承诺",
    "method": "出现了伤害方法",
    "too_long": "超过三句",
    "empty": "没有内容",
    "semantic_reject": "没有点名孩子说的事，或没有安慰",
}


@dataclass(frozen=True, slots=True)
class CrisisReplyModelConfig:
    api_key: str
    base_url: str
    model: str
    generate_timeout_s: float = 1.6
    review_timeout_s: float = 1.0
    thinking_mode: ThinkingMode = "dashscope"

    def __post_init__(self) -> None:
        if not self.api_key.strip():
            raise ValueError("crisis reply model requires an API key")
        if not self.base_url.strip():
            raise ValueError("crisis reply model requires a base URL")
        if not self.model.strip():
            raise ValueError("crisis reply model requires a model")
        if self.generate_timeout_s <= 0 or self.review_timeout_s <= 0:
            raise ValueError("crisis reply model timeout must be positive")


class CrisisReplyModel:
    def __init__(
        self,
        config: CrisisReplyModelConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def generate(
        self,
        *,
        query: str,
        tier: str,
        failures: tuple[str, ...],
    ) -> str | None:
        rule = _TIER_RULE.get(tier, _TIER_RULE["support_relay"])
        gaps = "；".join(_FAILURE_TEXT.get(item, item) for item in failures)
        user = f"档位：{tier}\n孩子说：{_child(query)}"
        if gaps:
            user = f"{user}\n上一句没有通过：{gaps}。只改这些缺口。"
        return await self._complete(
            system=_GENERATE_SYSTEM.format(tier_rule=rule),
            user=user,
            max_tokens=180,
            timeout_s=self._config.generate_timeout_s,
        )

    async def accept(self, *, query: str, tier: str, reply: str) -> bool:
        content = await self._complete(
            system=_REVIEW_SYSTEM,
            user=f"档位：{tier}\n孩子说：{_child(query)}\n回复：{reply}",
            max_tokens=8,
            timeout_s=self._config.review_timeout_s,
        )
        if content is None:
            return False
        token = content.strip().split()[0].rstrip("。.")
        return token == "PASS"

    async def _complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        timeout_s: float,
    ) -> str | None:
        body: dict[str, Any] = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "stream": False,
            **thinking_disabled(self._config.thinking_mode),
            "max_tokens": max_tokens,
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
                timeout=timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
            choices = payload.get("choices") if isinstance(payload, dict) else None
            first = choices[0] if isinstance(choices, list) and choices else None
            message = first.get("message") if isinstance(first, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            return content if isinstance(content, str) and content.strip() else None
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError, IndexError):
            logger.warning("crisis reply model unavailable", exc_info=True)
            return None


def _child(query: str) -> str:
    return redact_pii((query or "").strip())[-_MAX_QUERY_CHARS:]
