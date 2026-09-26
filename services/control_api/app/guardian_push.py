"""WeChat subscribe-message sender and wiring for guardian crisis pushes.

The sender turns the fixed :class:`CrisisPushContent` into one
``subscribe/send`` request.  It reuses the Mini Program access-token cache in
:mod:`wechat_auth` and never logs the openid, the token or the message data.
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI

from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore
from services.control_api.app.wechat_auth import (
    WechatAuthError,
    invalidate_access_token,
    mini_program_access_token,
)
from services.guardian.push import (
    CrisisPushContent,
    CrisisPushSender,
    CrisisPushStorePort,
    CrisisPushWorker,
    DisplayNameResolver,
    PushSendResult,
)

logger = logging.getLogger(__name__)

# errcodes that mean the cached access token must be refreshed once.
_ACCESS_TOKEN_REJECTED = frozenset({40001, 40014, 42001})
# 43101: the user holds no acceptance for this template (refused or used up).
_NO_SUBSCRIPTION = frozenset({43101})
# System busy / rate limited: the same send may succeed later.
_TRANSIENT = frozenset({-1, 45009, 45011})
# WeChat keyword value limits (characters) by keyword type.
_KEYWORD_LIMITS = {
    "thing": 20,
    "name": 10,
    "phrase": 5,
    "short_thing": 5,
    "symbol": 5,
    "character_string": 32,
    "letter": 32,
}
_DEFAULT_KEYWORD_LIMIT = 20
_DEFAULT_NAME = "TA"


def _keyword_kind(keyword: str) -> str:
    return keyword.rstrip("0123456789")


def fit_keyword_value(keyword: str, value: str) -> str:
    """Fit a value to the WeChat rules of the keyword type it is sent in."""

    kind = _keyword_kind(keyword)
    if kind in {"time", "date"}:
        return value
    clean = value.strip()
    if kind == "name":
        # ``name`` accepts only letters (including CJK); anything else is
        # rejected by WeChat with 47003, so it is dropped here.
        clean = "".join(
            character
            for character in clean
            if unicodedata.category(character).startswith("L")
        )
        clean = clean or _DEFAULT_NAME
    limit = _KEYWORD_LIMITS.get(kind, _DEFAULT_KEYWORD_LIMIT)
    return clean[:limit]


def _remote_code(response: httpx.Response) -> int | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    raw = payload.get("errcode")
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _classify(response: httpx.Response, remote_code: int | None) -> PushSendResult:
    if remote_code is None:
        if response.status_code >= 500 or response.status_code == 429:
            return PushSendResult("retry", f"wechat_http_{response.status_code}")
        if response.is_error:
            return PushSendResult("failed", f"wechat_http_{response.status_code}")
        return PushSendResult("retry", "wechat_subscribe_invalid_response")
    if response.status_code >= 500 or response.status_code == 429:
        return PushSendResult("retry", f"wechat_http_{response.status_code}")
    if remote_code == 0 and not response.is_error:
        return PushSendResult("delivered")
    if remote_code in _NO_SUBSCRIPTION:
        return PushSendResult("no_subscription", f"wechat_{remote_code}")
    if remote_code in _TRANSIENT:
        return PushSendResult("retry", f"wechat_{remote_code}")
    return PushSendResult("failed", f"wechat_{remote_code}")


class WechatSubscribeSender:
    """Send the fixed crisis alert through ``cgi-bin/message/subscribe/send``."""

    def __init__(self, settings: ControlSettings) -> None:
        self._settings = settings
        self._fields = settings.guardian_push_crisis_fields()
        self._zone = ZoneInfo(settings.memoria_timezone)

    def message_data(self, content: CrisisPushContent) -> dict[str, dict[str, str]]:
        values = {
            "title": content.title,
            "child": content.child_display_name,
            "time": content.occurred_at.astimezone(self._zone).strftime("%Y-%m-%d %H:%M"),
            "tip": content.tip,
        }
        return {
            keyword: {"value": fit_keyword_value(keyword, values[slot])}
            for slot, keyword in self._fields.items()
        }

    def request_body(
        self,
        *,
        openid: str,
        template_id: str,
        content: CrisisPushContent,
    ) -> dict[str, Any]:
        return {
            "touser": openid,
            "template_id": template_id,
            "page": self._settings.wechat_subscribe_crisis_page,
            "miniprogram_state": self._settings.wechat_miniprogram_state,
            "lang": "zh_CN",
            "data": self.message_data(content),
        }

    async def send_crisis_alert(
        self,
        *,
        openid: str,
        template_id: str,
        content: CrisisPushContent,
    ) -> PushSendResult:
        body = self.request_body(openid=openid, template_id=template_id, content=content)
        for attempt in range(2):
            try:
                token, cache_key = await mini_program_access_token(self._settings)
            except WechatAuthError as exc:
                return PushSendResult("retry", exc.code)
            try:
                async with httpx.AsyncClient(
                    timeout=self._settings.wechat_auth_timeout_s
                ) as client:
                    response = await client.post(
                        self._settings.wechat_subscribe_send_endpoint,
                        params={"access_token": token},
                        json=body,
                    )
            except httpx.HTTPError:
                return PushSendResult("retry", "wechat_subscribe_unreachable")
            remote_code = _remote_code(response)
            if remote_code in _ACCESS_TOKEN_REJECTED:
                invalidate_access_token(cache_key)
                if attempt == 0:
                    continue
                return PushSendResult("retry", f"wechat_{remote_code}")
            return _classify(response, remote_code)
        raise AssertionError("unreachable")  # pragma: no cover


def resolve_minor_display_name(
    profiles: MemoryStore,
    identity_service: Any,
) -> DisplayNameResolver:
    """Resolve the child's name exactly as the guardian page already shows it."""

    async def resolve(guardian_user_id: str, minor_user_id: str) -> str | None:
        profile = profiles.get_subject_profile(user_id=minor_user_id)
        name = (profile or {}).get("display_name")
        if (not name or name == "朋友") and identity_service is not None:
            try:
                person = await identity_service.get_person(
                    minor_user_id, actor_person_id=guardian_user_id
                )
                name = person.display_name
            except Exception:
                pass
        return str(name) if name else None

    return resolve


def app_display_name_resolver(app: FastAPI) -> DisplayNameResolver:
    async def resolve(guardian_user_id: str, minor_user_id: str) -> str | None:
        resolver = resolve_minor_display_name(
            cast(MemoryStore, app.state.memory_store),
            getattr(app.state, "identity_service", None),
        )
        return await resolver(guardian_user_id, minor_user_id)

    return resolve


def build_crisis_push_worker(
    settings: ControlSettings,
    store: CrisisPushStorePort,
    *,
    display_name: DisplayNameResolver,
    sender: CrisisPushSender | None = None,
) -> CrisisPushWorker | None:
    """Return the delivery worker, or ``None`` when push is disabled."""

    if not settings.guardian_push_enabled:
        return None
    settings.validate_guardian_push()
    logger.info("guardian crisis push delivery enabled")
    return CrisisPushWorker(
        store,
        sender or WechatSubscribeSender(settings),
        template_id=settings.wechat_subscribe_crisis_template_id.strip(),
        display_name=display_name,
        interval_s=settings.guardian_push_interval_s,
        max_attempts=settings.guardian_push_max_attempts,
    )


__all__ = [
    "WechatSubscribeSender",
    "app_display_name_resolver",
    "build_crisis_push_worker",
    "fit_keyword_value",
    "resolve_minor_display_name",
]
