"""Account and legacy anonymous identity issuance for the H5 client."""

from __future__ import annotations

import sqlite3
import unicodedata
from datetime import UTC, datetime
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    create_account_user_id,
    create_anonymous_user_id,
    hash_password,
    mint_memoria_access_token,
    optional_authenticated_user,
    require_authenticated_user,
    verify_password,
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


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@router.post("/anonymous", response_model=AuthTokenResponse)
def create_anonymous_identity(request: Request) -> AuthTokenResponse:
    settings = request.app.state.settings
    user_id = create_anonymous_user_id()
    store = cast(MemoryStore, request.app.state.memory_store)
    store.get_profile(user_id=user_id, now=_utc_now())
    token, ttl = mint_memoria_access_token(settings, user_id=user_id)
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
    current_user: Annotated[
        AuthenticatedUser | None,
        Depends(optional_authenticated_user),
    ],
) -> AuthTokenResponse:
    settings = request.app.state.settings
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
    token, ttl = mint_memoria_access_token(settings, user_id=user_id)
    return AuthTokenResponse(
        user_id=user_id,
        username=str(account["username"]),
        account_type="registered",
        access_token=token,
        expires_in=ttl,
    )


@router.post("/login", response_model=AuthTokenResponse)
def login_account(body: AccountCredentials, request: Request) -> AuthTokenResponse:
    settings = request.app.state.settings
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
    token, ttl = mint_memoria_access_token(settings, user_id=user_id)
    return AuthTokenResponse(
        user_id=user_id,
        username=str(account["username"]),
        account_type="registered",
        access_token=token,
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
        username=str(account["username"]) if account is not None else None,
        account_type="registered" if account is not None else "anonymous",
    )
