"""Anonymous device identity issuance for the H5 client."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    create_anonymous_user_id,
    mint_memoria_access_token,
    require_authenticated_user,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class AuthTokenResponse(BaseModel):
    user_id: str
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class CurrentUserResponse(BaseModel):
    user_id: str


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@router.post("/anonymous", response_model=AuthTokenResponse)
def create_anonymous_identity(request: Request) -> AuthTokenResponse:
    settings = request.app.state.settings
    user_id = create_anonymous_user_id()
    store = cast(MemoryStore, request.app.state.memory_store)
    store.get_profile(user_id=user_id, now=_utc_now())
    token, ttl = mint_memoria_access_token(settings, user_id=user_id)
    return AuthTokenResponse(user_id=user_id, access_token=token, expires_in=ttl)


@router.get("/me", response_model=CurrentUserResponse)
def current_user(
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> CurrentUserResponse:
    return CurrentUserResponse(user_id=user.user_id)
