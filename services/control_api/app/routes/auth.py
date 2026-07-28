"""Account and legacy anonymous identity issuance for the H5 client."""

from __future__ import annotations

import base64
import binascii
import hashlib
import secrets
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, field_validator

from services.control_api.app.database import (
    AUTH_REFRESH_CONCURRENT_RETRY_AFTER_S,
    AuthSessionRotationStatus,
    ExternalIdentityConflictError,
    MemoryStore,
)
from services.control_api.app.security import (
    AuthenticatedUser,
    create_account_user_id,
    create_anonymous_user_id,
    create_legacy_upgrade_refresh_token,
    create_refresh_token,
    create_session_id,
    decode_legacy_access_token,
    hash_password,
    mint_memoria_access_token,
    optional_authenticated_user,
    refresh_token_hash,
    require_authenticated_user,
    verify_password,
)
from services.control_api.app.wechat_auth import (
    WechatAuthError,
    code_to_phone,
    code_to_session,
    mask_phone_number,
    openid_hash,
    phone_subject_hash,
    wechat_user_id,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])
_DUMMY_PASSWORD_HASH = (
    "scrypt$16384$8$1$bWVtb3JpYS1kdW1teS12MQ==$"
    "MHsqulx1-3pOADlIqMs1tWnYbSsj0q2zw2xmMP2ZF14="
)


class AuthTokenResponse(BaseModel):
    user_id: str
    username: str | None = None
    account_type: Literal["registered", "anonymous"]
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    display_name: str | None = None
    phone_number_masked: str | None = None
    avatar_url: str | None = None


class CurrentUserResponse(BaseModel):
    user_id: str
    username: str | None = None
    account_type: Literal["registered", "anonymous"]


class AccountCredentials(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        username = unicodedata.normalize("NFKC", value).strip()
        if not 2 <= len(username) <= 32:
            raise ValueError("用户名需要 2–32 个字符")
        if any(not (character.isalnum() or character in "._-") for character in username):
            raise ValueError("用户名只能包含文字、数字、点、下划线或短横线")
        return username


class WechatLoginRequest(BaseModel):
    login_code: str = Field(min_length=1, max_length=256)
    phone_code: str | None = Field(default=None, min_length=1, max_length=256)
    display_name: str | None = Field(default=None, max_length=64)


class WechatAvatarRequest(BaseModel):
    file_base64: str = Field(min_length=1, max_length=6_000_000)
    content_type: Literal["image/png", "image/jpeg", "image/webp"]


class WechatAvatarResponse(BaseModel):
    avatar_url: str
    size: int
    sha256: str


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _account_username(account: dict[str, object] | None) -> str | None:
    if account is None:
        return None
    username = account.get("username")
    return username if isinstance(username, str) and username else None


def _wechat_error(exc: WechatAuthError) -> HTTPException:
    status_code = (
        status.HTTP_503_SERVICE_UNAVAILABLE
        if exc.code == "wechat_credentials_missing"
        else status.HTTP_502_BAD_GATEWAY
    )
    return HTTPException(status_code=status_code, detail={"code": exc.code})


def _valid_avatar_content(content_type: str, content: bytes) -> bool:
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return content.startswith(b"\xff\xd8\xff")
    return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP"


def _refresh_cookie_path(request: Request) -> str:
    prefix = urlsplit(request.app.state.settings.public_base_url).path.rstrip("/")
    return f"{prefix}/v1/auth" if prefix else "/v1/auth"


def _set_refresh_cookie(response: Response, request: Request, token: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        key=settings.memoria_refresh_cookie_name,
        value=token,
        max_age=settings.memoria_auth_refresh_ttl_s,
        httponly=True,
        samesite="strict",
        secure=settings.environment == "production",
        path=_refresh_cookie_path(request),
    )


def _clear_refresh_cookie(response: Response, request: Request) -> None:
    settings = request.app.state.settings
    response.delete_cookie(
        key=settings.memoria_refresh_cookie_name,
        httponly=True,
        samesite="strict",
        secure=settings.environment == "production",
        path=_refresh_cookie_path(request),
    )


def _issue_session(
    *,
    request: Request,
    response: Response,
    user_id: str,
    replace_existing_sessions: bool = False,
) -> tuple[str, int] | None:
    settings = request.app.state.settings
    store = cast(MemoryStore, request.app.state.memory_store)
    session_id = create_session_id()
    refresh = create_refresh_token(session_id=session_id)
    now = datetime.now(UTC)
    created = store.create_auth_session(
        session_id=session_id,
        user_id=user_id,
        refresh_hash=refresh_token_hash(refresh),
        expires_at=(now + timedelta(seconds=settings.memoria_auth_refresh_ttl_s)).isoformat(),
        now=now.isoformat(),
        replace_existing_sessions=replace_existing_sessions,
    )
    if not created:
        return None
    token, ttl = mint_memoria_access_token(
        settings,
        user_id=user_id,
        session_id=session_id,
    )
    _set_refresh_cookie(response, request, refresh)
    return token, ttl


@router.post("/anonymous", response_model=AuthTokenResponse)
def create_anonymous_identity(request: Request, response: Response) -> AuthTokenResponse:
    user_id = create_anonymous_user_id()
    store = cast(MemoryStore, request.app.state.memory_store)
    store.get_profile(user_id=user_id, now=_utc_now())
    issued = _issue_session(request=request, response=response, user_id=user_id)
    assert issued is not None
    token, ttl = issued
    return AuthTokenResponse(
        user_id=user_id,
        account_type="anonymous",
        access_token=token,
        expires_in=ttl,
    )


@router.post(
    "/register",
    response_model=AuthTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
def register_account(
    body: AccountCredentials,
    request: Request,
    response: Response,
    current_user: Annotated[
        AuthenticatedUser | None,
        Depends(optional_authenticated_user),
    ],
) -> AuthTokenResponse:
    store = cast(MemoryStore, request.app.state.memory_store)
    user_id = current_user.user_id if current_user is not None else create_account_user_id()
    try:
        account = store.register_account(
            user_id=user_id,
            username=body.username,
            username_normalized=body.username.casefold(),
            password_hash=hash_password(body.password),
            now=_utc_now(),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="用户名已存在，请换一个") from exc
    issued = _issue_session(
        request=request,
        response=response,
        user_id=user_id,
        replace_existing_sessions=current_user is not None,
    )
    assert issued is not None
    token, ttl = issued
    return AuthTokenResponse(
        user_id=user_id,
        username=str(account["username"]),
        account_type="registered",
        access_token=token,
        expires_in=ttl,
    )


@router.post("/login", response_model=AuthTokenResponse)
def login_account(body: AccountCredentials, request: Request, response: Response) -> AuthTokenResponse:
    store = cast(MemoryStore, request.app.state.memory_store)
    account = store.get_account_by_username(username_normalized=body.username.casefold())
    password_hash = str(account["password_hash"]) if account is not None else _DUMMY_PASSWORD_HASH
    password_matches = verify_password(body.password, password_hash)
    if (
        account is None
        or not password_matches
        or store.is_account_unavailable(user_id=str(account["user_id"]))
    ):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    user_id = str(account["user_id"])
    issued = _issue_session(request=request, response=response, user_id=user_id)
    assert issued is not None
    token, ttl = issued
    return AuthTokenResponse(
        user_id=user_id,
        username=str(account["username"]),
        account_type="registered",
        access_token=token,
        expires_in=ttl,
    )


@router.post("/wechat-login", response_model=AuthTokenResponse)
async def login_wechat(
    body: WechatLoginRequest,
    request: Request,
    response: Response,
) -> AuthTokenResponse:
    settings = request.app.state.settings
    store = cast(MemoryStore, request.app.state.memory_store)
    try:
        session = await code_to_session(settings, body.login_code)
    except WechatAuthError as exc:
        raise _wechat_error(exc) from exc
    openid_subject = openid_hash(session.openid)
    existing_user_id = store.external_identity_user(
        provider="wechat_openid",
        subject_hash=openid_subject,
    )
    if existing_user_id is not None and store.is_account_unavailable(
        user_id=existing_user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "account_deletion_in_progress"},
        )
    if existing_user_id is None and body.phone_code is None:
        raise HTTPException(
            status_code=status.HTTP_428_PRECONDITION_REQUIRED,
            detail={"code": "phone_authorization_required"},
        )

    identities = {"wechat_openid": openid_subject}
    masked_phone: str | None = None
    if body.phone_code is not None:
        try:
            phone = await code_to_phone(settings, body.phone_code)
        except WechatAuthError as exc:
            raise _wechat_error(exc) from exc
        identities["wechat_phone"] = phone_subject_hash(settings, phone)
        try:
            masked_phone = mask_phone_number(phone.phone_number)
        except WechatAuthError as exc:
            raise _wechat_error(exc) from exc
    preferred_user_id = existing_user_id or wechat_user_id(session.openid)
    if existing_user_id is None and store.is_account_unavailable(user_id=preferred_user_id):
        preferred_user_id = f"{preferred_user_id}-{secrets.token_hex(6)}"
    try:
        user_id, _ = store.bind_external_identities(
            preferred_user_id=preferred_user_id,
            identities=identities,
            now=_utc_now(),
        )
    except ExternalIdentityConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "wechat_identity_conflict"},
        ) from exc
    profile = store.update_external_profile(
        user_id=user_id,
        display_name=body.display_name,
        phone_number_masked=masked_phone,
        now=_utc_now(),
    )
    issued = _issue_session(request=request, response=response, user_id=user_id)
    assert issued is not None
    token, ttl = issued
    return AuthTokenResponse(
        user_id=user_id,
        account_type="registered",
        access_token=token,
        expires_in=ttl,
        display_name=str(profile["display_name"]),
        phone_number_masked=str(profile["phone_number_masked"]),
        avatar_url=str(profile["avatar_url"]),
    )


@router.post("/wechat-avatar", response_model=WechatAvatarResponse)
def upload_wechat_avatar(
    body: WechatAvatarRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> WechatAvatarResponse:
    settings = request.app.state.settings
    try:
        content = base64.b64decode(body.file_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "wechat_avatar_invalid"},
        ) from exc
    if not content or len(content) > settings.wechat_avatar_max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={"code": "wechat_avatar_too_large"},
        )
    if not _valid_avatar_content(body.content_type, content):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "wechat_avatar_content_mismatch"},
        )
    public_id = secrets.token_urlsafe(24)
    avatar_url = f"{settings.wechat_avatar_base_url()}/v1/auth/wechat-avatars/{public_id}"
    digest = hashlib.sha256(content).hexdigest()
    cast(MemoryStore, request.app.state.memory_store).save_profile_avatar(
        user_id=user.user_id,
        public_id=public_id,
        content_type=body.content_type,
        content=content,
        sha256=digest,
        avatar_url=avatar_url,
        now=_utc_now(),
    )
    return WechatAvatarResponse(
        avatar_url=avatar_url,
        size=len(content),
        sha256=digest,
    )


@router.get("/wechat-avatars/{public_id}")
def get_wechat_avatar(public_id: str, request: Request) -> Response:
    if len(public_id) < 20 or len(public_id) > 64:
        raise HTTPException(status_code=404, detail="avatar not found")
    avatar = cast(MemoryStore, request.app.state.memory_store).get_profile_avatar(
        public_id=public_id
    )
    if avatar is None:
        raise HTTPException(status_code=404, detail="avatar not found")
    return Response(
        content=bytes(avatar["content"]),
        media_type=str(avatar["content_type"]),
        headers={
            "Cache-Control": "private, no-store",
            "ETag": f'"{avatar["sha256"]}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/refresh", response_model=AuthTokenResponse)
def refresh_access_token(request: Request, response: Response) -> AuthTokenResponse:
    settings = request.app.state.settings
    refresh = request.cookies.get(settings.memoria_refresh_cookie_name, "")
    if not refresh or "." not in refresh:
        _clear_refresh_cookie(response, request)
        raise HTTPException(status_code=401, detail="invalid refresh session")
    session_id, _, _ = refresh.partition(".")
    next_refresh = create_refresh_token(session_id=session_id)
    rotation = cast(MemoryStore, request.app.state.memory_store).rotate_auth_session(
        refresh_hash=refresh_token_hash(refresh),
        next_refresh_hash=refresh_token_hash(next_refresh),
        now=_utc_now(),
    )
    if rotation.status is AuthSessionRotationStatus.CONCURRENT_RETRY:
        raise HTTPException(
            status_code=409,
            detail="refresh already rotated; retry with the current cookie",
            headers={"Retry-After": str(AUTH_REFRESH_CONCURRENT_RETRY_AFTER_S)},
        )
    if rotation.status is not AuthSessionRotationStatus.ROTATED:
        _clear_refresh_cookie(response, request)
        raise HTTPException(status_code=401, detail="invalid refresh session")
    rotated = rotation.session
    assert rotated is not None
    user_id = str(rotated["user_id"])
    if cast(MemoryStore, request.app.state.memory_store).is_account_unavailable(user_id=user_id):
        _clear_refresh_cookie(response, request)
        raise HTTPException(status_code=401, detail="access session is unavailable")
    token, ttl = mint_memoria_access_token(settings, user_id=user_id, session_id=session_id)
    _set_refresh_cookie(response, request, next_refresh)
    account = cast(MemoryStore, request.app.state.memory_store).get_account(user_id=user_id)
    return AuthTokenResponse(
        user_id=user_id,
        username=_account_username(account),
        account_type="registered" if account is not None else "anonymous",
        access_token=token,
        expires_in=ttl,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> None:
    if user.session_id is not None:
        cast(MemoryStore, request.app.state.memory_store).revoke_auth_session(
            session_id=user.session_id,
            user_id=user.user_id,
            now=_utc_now(),
        )
    _clear_refresh_cookie(response, request)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
def logout_all(
    request: Request,
    response: Response,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> None:
    cast(MemoryStore, request.app.state.memory_store).revoke_all_auth_sessions(
        user_id=user.user_id,
        now=_utc_now(),
    )
    _clear_refresh_cookie(response, request)


@router.post("/upgrade", response_model=AuthTokenResponse)
def upgrade_legacy_access_token(
    request: Request,
    response: Response,
    authorization: str | None = Header(default=None),
) -> AuthTokenResponse:
    settings = request.app.state.settings
    if not settings.legacy_auth_compat_active():
        raise HTTPException(status_code=401, detail="legacy access upgrade rejected")
    scheme, separator, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not separator:
        raise HTTPException(status_code=401, detail="legacy access upgrade rejected")
    user_id = decode_legacy_access_token(settings, token.strip())
    store = cast(MemoryStore, request.app.state.memory_store)
    if user_id is None or store.is_account_unavailable(user_id=user_id):
        raise HTTPException(status_code=401, detail="legacy access upgrade rejected")
    now = datetime.now(UTC)
    session_id = create_session_id()
    refresh = create_legacy_upgrade_refresh_token(
        settings,
        session_id=session_id,
        legacy_access_token=token.strip(),
    )
    upgraded = store.create_or_recover_legacy_upgrade_session(
        legacy_token_hash=refresh_token_hash(token.strip()),
        session_id=session_id,
        user_id=user_id,
        refresh_hash=refresh_token_hash(refresh),
        expires_at=(now + timedelta(seconds=settings.memoria_auth_refresh_ttl_s)).isoformat(),
        recovery_expires_at=settings.legacy_auth_compat_until.isoformat(),
        now=_utc_now(),
    )
    if upgraded is None:
        raise HTTPException(status_code=401, detail="legacy access upgrade rejected")
    recovered_session_id = str(upgraded["session_id"])
    recovered_refresh = create_legacy_upgrade_refresh_token(
        settings,
        session_id=recovered_session_id,
        legacy_access_token=token.strip(),
    )
    access, ttl = mint_memoria_access_token(
        settings,
        user_id=user_id,
        session_id=recovered_session_id,
    )
    _set_refresh_cookie(response, request, recovered_refresh)
    account = store.get_account(user_id=user_id)
    return AuthTokenResponse(
        user_id=user_id,
        username=_account_username(account),
        account_type="registered" if account is not None else "anonymous",
        access_token=access,
        expires_in=ttl,
    )


@router.get("/me", response_model=CurrentUserResponse)
def current_user(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> CurrentUserResponse:
    store = cast(MemoryStore, request.app.state.memory_store)
    account = store.get_account(user_id=user.user_id)
    return CurrentUserResponse(
        user_id=user.user_id,
        username=_account_username(account),
        account_type="registered" if account is not None else "anonymous",
    )
