"""Session token helpers. Permanent LiveKit secrets stay server-side only."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, cast

import jwt
from fastapi import Header, HTTPException, Request

from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore


def create_room_name() -> str:
    return f"voice-{uuid.uuid4()}"


def create_session_id() -> str:
    return str(uuid.uuid4())


def create_anonymous_user_id() -> str:
    return f"anon-{uuid.uuid4()}"


def create_account_user_id() -> str:
    return f"account-{uuid.uuid4()}"


_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    salt_text = base64.urlsafe_b64encode(salt).decode("ascii")
    digest_text = base64.urlsafe_b64encode(digest).decode("ascii")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_text}${digest_text}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, expected_text = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        parameters = (int(n), int(r), int(p))
        if parameters != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_text.encode("ascii"))
        if len(salt) < 16 or len(expected) != _SCRYPT_DKLEN:
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=parameters[0],
            r=parameters[1],
            p=parameters[2],
            dklen=len(expected),
        )
    except (binascii.Error, ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: str


def mint_memoria_access_token(
    settings: ControlSettings,
    *,
    user_id: str,
    ttl_s: int | None = None,
) -> tuple[str, int]:
    ttl = ttl_s if ttl_s is not None else settings.memoria_auth_token_ttl_s
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": settings.memoria_auth_issuer,
            "aud": settings.memoria_auth_audience,
            "sub": user_id,
            "iat": now,
            "nbf": now,
            "exp": now + ttl,
            "typ": "memoria_access",
        },
        settings.memoria_auth_secret.get_secret_value(),
        algorithm="HS256",
    )
    return (token.decode("utf-8") if isinstance(token, bytes) else str(token), ttl)


def require_authenticated_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthenticatedUser:
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Bearer authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(
            status_code=401,
            detail="invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    settings: ControlSettings = request.app.state.settings
    try:
        claims = jwt.decode(
            token.strip(),
            settings.memoria_auth_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=settings.memoria_auth_audience,
            issuer=settings.memoria_auth_issuer,
            options={"require": ["exp", "iat", "nbf", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=401,
            detail="invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user_id = claims.get("sub")
    if claims.get("typ") != "memoria_access" or not isinstance(user_id, str) or not user_id:
        raise HTTPException(
            status_code=401,
            detail="invalid access token claims",
            headers={"WWW-Authenticate": "Bearer"},
        )
    store = getattr(request.app.state, "memory_store", None)
    if store is not None and store.is_account_unavailable(user_id=user_id):
        raise HTTPException(
            status_code=401,
            detail="account is unavailable",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return AuthenticatedUser(user_id=user_id)


def optional_authenticated_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthenticatedUser | None:
    if authorization is None:
        return None
    return require_authenticated_user(request, authorization)


def require_matching_user(requested_user_id: str, user: AuthenticatedUser) -> str:
    if requested_user_id != user.user_id:
        raise HTTPException(status_code=403, detail="user identity does not match access token")
    return user.user_id


def require_active_voice_session(request: Request, session_id: str) -> dict[str, Any]:
    """Resolve session ownership without reviving data after deletion starts."""
    store = cast(MemoryStore, request.app.state.memory_store)
    if store.is_voice_session_tombstoned(session_id=session_id):
        raise HTTPException(status_code=410, detail="voice session was deleted")
    session = store.get_voice_session_by_id(session_id=session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="voice session not found")
    if store.is_account_unavailable(user_id=str(session["user_id"])):
        raise HTTPException(status_code=410, detail="voice session was deleted")
    return session


def mint_participant_token(
    settings: ControlSettings,
    *,
    room_name: str,
    identity: str,
    agent_name: str | None = None,
    ttl_s: int | None = None,
) -> tuple[str, int]:
    """
    Mint a short-lived LiveKit access token for a single room.
    When LiveKit API key/secret are missing (offline), return a signed local JWT
    so the frontend path can still be exercised.
    """
    ttl = ttl_s if ttl_s is not None else settings.session_token_ttl_s
    now = int(time.time())
    exp = now + ttl

    if settings.livekit_api_key and settings.livekit_api_secret:
        try:
            from datetime import timedelta

            from livekit.api import (
                AccessToken,
                RoomAgentDispatch,
                RoomConfiguration,
                VideoGrants,
            )

            access_token = (
                AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
                .with_identity(identity)
                .with_name(identity)
                .with_grants(
                    VideoGrants(
                        room_join=True,
                        room=room_name,
                        can_publish=True,
                        can_subscribe=True,
                    )
                )
                .with_ttl(timedelta(seconds=ttl))
            )
            if agent_name:
                access_token.with_room_config(
                    RoomConfiguration(agents=[RoomAgentDispatch(agent_name=agent_name)])
                )
            jwt_token = access_token.to_jwt()
            return str(jwt_token), ttl
        except Exception as exc:
            logging.getLogger(__name__).exception("livekit token mint failed")
            raise RuntimeError("failed to mint LiveKit participant token") from exc

    if not settings.offline_mock:
        raise RuntimeError("LiveKit credentials are required outside offline mock mode")

    # Explicit offline mode only. This token must never be returned in production.
    secret = settings.livekit_api_secret or "offline-dev-secret-not-for-production"
    payload: dict[str, Any] = {
        "iss": settings.livekit_api_key or settings.jwt_issuer,
        "sub": identity,
        "iat": now,
        "exp": exp,
        "video": {
            "roomJoin": True,
            "room": room_name,
            "canPublish": True,
            "canSubscribe": True,
        },
    }
    if agent_name:
        payload["roomConfig"] = {"agents": [{"agentName": agent_name}]}
    encoded = jwt.encode(payload, secret, algorithm="HS256")
    if isinstance(encoded, bytes):
        return encoded.decode("utf-8"), ttl
    return encoded, ttl
