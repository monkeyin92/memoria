from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Final, Literal
from urllib.parse import quote
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.agent.src.runtime_profile import VerifiedRuntimeProfile
from services.common.daily_chat_limit import DailyChatBudget

logger = logging.getLogger(__name__)

CHAT_USAGE_PATH: Final = "/v1/internal/chat-usage"

type DailyChatRefusal = Literal["exhausted", "unavailable"]


@dataclass(frozen=True, slots=True)
class GovernedDailyChat:
    subject_id: str
    budget: DailyChatBudget


@dataclass(frozen=True, slots=True)
class DailyChatAccrual:
    subject_id: str
    accrual_id: str


def governed_daily_chat(profile: VerifiedRuntimeProfile | None) -> GovernedDailyChat | None:
    if profile is None:
        return None
    signed = profile.profile
    budget, subject_id = signed.daily_chat_budget, signed.active_subject_id
    if signed.subject_category != "minor" or budget is None or subject_id is None:
        return None
    return GovernedDailyChat(subject_id=subject_id, budget=budget)


class _UsedToday(BaseModel):
    model_config = ConfigDict(strict=True)

    used_seconds: int = Field(ge=0)


class _Recorded(BaseModel):
    status: Literal["recorded"]


@dataclass(frozen=True, slots=True)
class DailyChatClientConfig:
    endpoint: str
    internal_token: str
    read_timeout_s: float
    write_timeout_s: float = 3.0
    write_attempts: int = 2
    retry_delay_s: float = 1.0


class DailyChatClient:
    def __init__(
        self,
        config: DailyChatClientConfig,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._headers = {"X-Memoria-Internal-Token": config.internal_token}

    async def admit(self, governed: GovernedDailyChat) -> DailyChatAccrual | DailyChatRefusal:
        used = await self._used_today(governed.subject_id)
        if used is None:
            return "unavailable"
        if not governed.budget.admits(used):
            return "exhausted"
        return DailyChatAccrual(subject_id=governed.subject_id, accrual_id=uuid4().hex)

    async def record(self, accrual: DailyChatAccrual, *, elapsed_seconds: int) -> None:
        body = {
            "accrual_id": accrual.accrual_id,
            "subject_id": accrual.subject_id,
            "elapsed_seconds": elapsed_seconds,
        }
        error = ""
        for attempt in range(self._config.write_attempts):
            if attempt:
                await asyncio.sleep(self._config.retry_delay_s)
            try:
                response = await self._client.post(
                    self._config.endpoint,
                    headers=self._headers,
                    json=body,
                    timeout=self._config.write_timeout_s,
                )
                _Recorded.model_validate_json(response.raise_for_status().content)
                return
            except Exception as exc:
                error = type(exc).__name__
        logger.warning(
            "daily chat usage report lost accrual=%s error=%s", accrual.accrual_id, error
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _used_today(self, subject_id: str) -> int | None:
        try:
            response = await self._client.get(
                f"{self._config.endpoint}/{quote(subject_id, safe='')}/today",
                headers=self._headers,
                timeout=self._config.read_timeout_s,
            )
            return _UsedToday.model_validate_json(response.raise_for_status().content).used_seconds
        except (httpx.HTTPError, ValidationError) as exc:
            logger.warning("daily chat usage read failed error=%s", type(exc).__name__)
            return None


def chat_usage_endpoint(interaction_policy_url: str) -> str:
    return str(httpx.URL(interaction_policy_url).join(CHAT_USAGE_PATH))


def build_daily_chat_client(settings: Any) -> DailyChatClient | None:
    token = settings.internal_token("interaction_policy")
    if not token:
        return None
    return DailyChatClient(
        DailyChatClientConfig(
            endpoint=chat_usage_endpoint(settings.interaction_policy_url),
            internal_token=token,
            read_timeout_s=settings.interaction_policy_timeout_s,
        )
    )


__all__ = [
    "CHAT_USAGE_PATH",
    "DailyChatAccrual",
    "DailyChatClient",
    "DailyChatClientConfig",
    "DailyChatRefusal",
    "GovernedDailyChat",
    "build_daily_chat_client",
    "chat_usage_endpoint",
    "governed_daily_chat",
]
