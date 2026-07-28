"""Small server-side adapter for WeChat Mini Program identity APIs."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Any

import httpx

from services.control_api.app.config import ControlSettings

_ACCESS_TOKEN_CACHE: dict[tuple[str, str, str], tuple[str, float]] = {}


@dataclass(frozen=True, slots=True)
class WechatSession:
    openid: str


@dataclass(frozen=True, slots=True)
class WechatPhone:
    phone_number: str
    country_code: str


class WechatAuthError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        remote_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.remote_code = remote_code


def openid_hash(openid: str) -> str:
    """Keep EchoLife's stable 24-character identity rule."""
    return hashlib.sha256(openid.encode("utf-8")).hexdigest()[:24]


def wechat_user_id(openid: str) -> str:
    return f"wx_{openid_hash(openid)}"


def phone_subject_hash(settings: ControlSettings, phone: WechatPhone) -> str:
    canonical = f"{phone.country_code}:{phone.phone_number}".encode()
    return hmac.new(
        settings.wechat_identity_secret().encode(),
        b"memoria-wechat-phone-v1\0" + canonical,
        hashlib.sha256,
    ).hexdigest()


def mask_phone_number(phone_number: str) -> str:
    digits = "".join(character for character in phone_number if character.isdigit())
    if len(digits) < 7:
        raise WechatAuthError("wechat_phone_invalid", "微信返回的手机号格式无效")
    return f"{digits[:3]}****{digits[-4:]}"


def _dev_code(value: str) -> bool:
    return value.startswith(("dev-", "mock-"))


def _payload(response: httpx.Response, *, code: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise WechatAuthError(code, "微信服务返回了无效响应") from exc
    if not isinstance(payload, dict):
        raise WechatAuthError(code, "微信服务返回了无效响应")
    raw_remote_code = payload.get("errcode")
    try:
        remote_code = int(raw_remote_code) if raw_remote_code is not None else None
    except (TypeError, ValueError):
        remote_code = None
    if response.is_error or remote_code:
        raise WechatAuthError(
            code,
            str(payload.get("errmsg") or "微信服务请求失败"),
            remote_code=remote_code,
        )
    return payload


def _require_credentials(settings: ControlSettings) -> tuple[str, str]:
    appid = settings.wechat_miniprogram_appid.strip()
    secret = settings.wechat_miniprogram_appsecret.get_secret_value().strip()
    if not appid or not secret:
        raise WechatAuthError(
            "wechat_credentials_missing",
            "微信登录服务尚未配置",
        )
    return appid, secret


async def _access_token(
    settings: ControlSettings,
    *,
    appid: str,
    secret: str,
) -> tuple[str, tuple[str, str, str]]:
    cache_key = (
        appid,
        settings.wechat_access_token_endpoint,
        hashlib.sha256(secret.encode()).hexdigest()[:16],
    )
    cached = _ACCESS_TOKEN_CACHE.get(cache_key)
    if cached is not None and cached[1] > time.monotonic() + 60:
        return cached[0], cache_key
    try:
        async with httpx.AsyncClient(timeout=settings.wechat_auth_timeout_s) as client:
            response = await client.get(
                settings.wechat_access_token_endpoint,
                params={
                    "grant_type": "client_credential",
                    "appid": appid,
                    "secret": secret,
                },
            )
    except httpx.HTTPError as exc:
        raise WechatAuthError("wechat_access_token_failed", "微信 access_token 获取失败") from exc
    payload = _payload(response, code="wechat_access_token_failed")
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise WechatAuthError(
            "wechat_access_token_missing",
            "微信 access_token 响应无效",
        )
    try:
        expires_in = float(payload.get("expires_in") or 7200)
    except (TypeError, ValueError):
        expires_in = 7200.0
    expires_in = max(0.0, expires_in - 300.0)
    _ACCESS_TOKEN_CACHE[cache_key] = token, time.monotonic() + expires_in
    return token, cache_key


async def code_to_session(settings: ControlSettings, login_code: str) -> WechatSession:
    code = login_code.strip()
    if not code:
        raise WechatAuthError("wechat_login_code_required", "缺少微信登录凭证")
    if settings.offline_mock and _dev_code(code):
        return WechatSession(openid=f"dev-openid-{code[:64]}")
    appid, secret = _require_credentials(settings)
    try:
        async with httpx.AsyncClient(timeout=settings.wechat_auth_timeout_s) as client:
            response = await client.get(
                settings.wechat_jscode2session_endpoint,
                params={
                    "appid": appid,
                    "secret": secret,
                    "js_code": code,
                    "grant_type": "authorization_code",
                },
            )
    except httpx.HTTPError as exc:
        raise WechatAuthError("wechat_jscode2session_failed", "微信登录校验失败") from exc
    payload = _payload(response, code="wechat_jscode2session_failed")
    openid = str(payload.get("openid") or "").strip()
    if not openid:
        raise WechatAuthError("wechat_openid_missing", "微信登录响应缺少 openid")
    return WechatSession(openid=openid)


async def code_to_phone(settings: ControlSettings, phone_code: str) -> WechatPhone:
    code = phone_code.strip()
    if not code:
        raise WechatAuthError("wechat_phone_code_required", "缺少微信手机号授权凭证")
    if settings.offline_mock and _dev_code(code):
        return WechatPhone(phone_number="18100008880", country_code="86")
    appid, secret = _require_credentials(settings)
    phone_payload: dict[str, Any] | None = None
    for attempt in range(2):
        access_token, cache_key = await _access_token(settings, appid=appid, secret=secret)
        try:
            async with httpx.AsyncClient(timeout=settings.wechat_auth_timeout_s) as client:
                phone_response = await client.post(
                    settings.wechat_phone_number_endpoint,
                    params={"access_token": access_token},
                    json={"code": code},
                )
        except httpx.HTTPError as exc:
            raise WechatAuthError(
                "wechat_phone_number_failed",
                "微信手机号校验失败",
            ) from exc
        try:
            phone_payload = _payload(
                phone_response,
                code="wechat_phone_number_failed",
            )
            break
        except WechatAuthError as exc:
            _ACCESS_TOKEN_CACHE.pop(cache_key, None)
            if attempt == 0 and exc.remote_code in {40014, 42001}:
                continue
            raise
    assert phone_payload is not None
    phone_info = phone_payload.get("phone_info")
    if not isinstance(phone_info, dict):
        raise WechatAuthError("wechat_phone_number_missing", "微信手机号响应无效")
    number = str(
        phone_info.get("purePhoneNumber") or phone_info.get("phoneNumber") or ""
    ).strip()
    country_code = str(phone_info.get("countryCode") or "86").strip()
    if not number:
        raise WechatAuthError("wechat_phone_number_missing", "微信手机号响应无效")
    return WechatPhone(phone_number=number, country_code=country_code)
