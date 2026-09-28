"""Sessions, account auth, WeChat and guardian push."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from services.common.security_constants import (
    DEV_AUTH_SECRET,
    DEV_MESSAGE_IDEMPOTENCY_SECRET,
)


class AuthFields(BaseModel):
    session_token_ttl_s: int = Field(default=300, alias="SESSION_TOKEN_TTL_S")
    jwt_issuer: str = Field(default="voice-agent", alias="JWT_ISSUER")

    memoria_auth_secret: SecretStr = Field(
        default=SecretStr(DEV_AUTH_SECRET),
        alias="MEMORIA_AUTH_SECRET",
    )
    memoria_message_idempotency_secret: SecretStr = Field(
        default=SecretStr(DEV_MESSAGE_IDEMPOTENCY_SECRET),
        alias="MEMORIA_MESSAGE_IDEMPOTENCY_SECRET",
    )
    memoria_auth_issuer: str = Field(
        default="memoria-control-api",
        alias="MEMORIA_AUTH_ISSUER",
    )
    memoria_auth_audience: str = Field(
        default="memoria-h5",
        alias="MEMORIA_AUTH_AUDIENCE",
    )
    memoria_auth_token_ttl_s: int = Field(
        default=900,
        ge=600,
        le=900,
        alias="MEMORIA_AUTH_TOKEN_TTL_S",
    )
    memoria_auth_refresh_ttl_s: int = Field(
        default=2_592_000,
        ge=86_400,
        le=2_592_000,
        alias="MEMORIA_AUTH_REFRESH_TTL_S",
    )
    memoria_refresh_cookie_name: str = Field(
        default="memoria_refresh",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        alias="MEMORIA_REFRESH_COOKIE_NAME",
    )
    wechat_miniprogram_appid: str = Field(
        default="",
        alias="WECHAT_MINIPROGRAM_APPID",
    )
    wechat_miniprogram_appsecret: SecretStr = Field(
        default=SecretStr(""),
        alias="WECHAT_MINIPROGRAM_APPSECRET",
    )
    memoria_wechat_identity_secret: SecretStr = Field(
        default=SecretStr(""),
        alias="MEMORIA_WECHAT_IDENTITY_SECRET",
    )
    wechat_jscode2session_endpoint: str = Field(
        default="https://api.weixin.qq.com/sns/jscode2session",
        alias="WECHAT_JSCODE2SESSION_ENDPOINT",
    )
    wechat_access_token_endpoint: str = Field(
        default="https://api.weixin.qq.com/cgi-bin/token",
        alias="WECHAT_ACCESS_TOKEN_ENDPOINT",
    )
    wechat_phone_number_endpoint: str = Field(
        default="https://api.weixin.qq.com/wxa/business/getuserphonenumber",
        alias="WECHAT_PHONE_NUMBER_ENDPOINT",
    )
    wechat_auth_timeout_s: float = Field(
        default=8.0,
        ge=1.0,
        le=30.0,
        alias="WECHAT_AUTH_TIMEOUT_S",
    )
    wechat_avatar_max_bytes: int = Field(
        default=2 * 1024 * 1024,
        ge=1024,
        le=4 * 1024 * 1024,
        alias="WECHAT_AVATAR_MAX_BYTES",
    )
    wechat_avatar_public_base_url: str = Field(
        default="",
        alias="WECHAT_AVATAR_PUBLIC_BASE_URL",
    )
    # Guardian crisis alerts via Mini Program one-time subscribe messages.
    # Disabled by default: alerts then stay queued and visible on the
    # guardian page only, exactly as before delivery existed.
    guardian_push_enabled: bool = Field(
        default=False,
        alias="MEMORIA_GUARDIAN_PUSH_ENABLED",
    )
    wechat_subscribe_crisis_template_id: str = Field(
        default="",
        max_length=128,
        alias="MEMORIA_WECHAT_SUBSCRIBE_CRISIS_TEMPLATE_ID",
    )
    # slot=keyword pairs mapping the fixed alert onto the chosen template's
    # keywords.  Slots: title, child, time, tip.
    wechat_subscribe_crisis_fields: str = Field(
        default="title=thing1,child=name2,time=time3,tip=thing4",
        alias="MEMORIA_WECHAT_SUBSCRIBE_CRISIS_FIELDS",
    )
    wechat_subscribe_send_endpoint: str = Field(
        default="https://api.weixin.qq.com/cgi-bin/message/subscribe/send",
        alias="WECHAT_SUBSCRIBE_SEND_ENDPOINT",
    )
    wechat_subscribe_crisis_page: str = Field(
        default="pages/guardian/index",
        alias="MEMORIA_WECHAT_SUBSCRIBE_CRISIS_PAGE",
    )
    wechat_miniprogram_state: Literal["formal", "trial", "developer"] = Field(
        default="formal",
        alias="WECHAT_MINIPROGRAM_STATE",
    )
    guardian_push_interval_s: float = Field(
        default=10.0,
        ge=1.0,
        le=300.0,
        alias="MEMORIA_GUARDIAN_PUSH_INTERVAL_S",
    )
    guardian_push_max_attempts: int = Field(
        default=5,
        ge=1,
        le=10,
        alias="MEMORIA_GUARDIAN_PUSH_MAX_ATTEMPTS",
    )
    legacy_auth_compat_until: datetime | None = Field(
        default=None,
        alias="MEMORIA_LEGACY_AUTH_COMPAT_UNTIL",
    )
