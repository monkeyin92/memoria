"""Fail-closed, public-only Qwen resolver for fresh information."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_MAX_QUERY_CHARS = 1000
_MAX_TOKENS = 240
_SYSTEM_PROMPT = (
    "你负责回答一条当前公开的实时信息问题。只依据联网检索得到的可信结果回答；"
    "不得使用、推断、要求或泄露用户身份、过往对话、记忆、资料、偏好、会话标识或系统实现。"
    "无法确认时直接说明无法确认，不要编造。"
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
    """One public query per forced-search request; provider failures return ``None``."""

    def __init__(
        self,
        config: QwenRealtimeSearchConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient(timeout=config.timeout_s)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def resolve(self, *, query: str) -> str | None:
        normalized_query = query.strip() if isinstance(query, str) else ""
        if not normalized_query or len(normalized_query) > _MAX_QUERY_CHARS:
            return None

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
                            {"role": "system", "content": _SYSTEM_PROMPT},
                            {"role": "user", "content": normalized_query},
                        ],
                        "stream": False,
                        "max_tokens": _MAX_TOKENS,
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
            if content is None:
                logger.warning(
                    "qwen realtime search returned unusable response model=%s elapsed_ms=%s",
                    self._config.model,
                    elapsed_ms,
                )
            else:
                logger.info(
                    "qwen realtime search completed model=%s response_chars=%s elapsed_ms=%s",
                    self._config.model,
                    len(content),
                    elapsed_ms,
                )
            return content
        except (TimeoutError, httpx.TimeoutException):
            logger.warning(
                "qwen realtime search timed out model=%s timeout_s=%s",
                self._config.model,
                self._config.timeout_s,
            )
            return None
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "qwen realtime search failed model=%s status=%s",
                self._config.model,
                exc.response.status_code,
            )
            return None
        except (httpx.HTTPError, TypeError, ValueError, IndexError) as exc:
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
        if not isinstance(content, str):
            return None
        return content.strip() or None
