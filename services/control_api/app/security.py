"""Account authentication and session token helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import jwt
from fastapi import Header, HTTPException, Request

from services.control_api.app.config import ControlSettings
from services.control_api.app.database import MemoryStore


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
    session_id: str | None
    jti: str | None


def mint_memoria_access_token(
    settings: ControlSettings,
    *,
    user_id: str,
    session_id: str,
    ttl_s: int | None = None,
) -> tuple[str, int]:
    ttl = ttl_s if ttl_s is not None else settings.memoria_auth_token_ttl_s
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": settings.memoria_auth_issuer,
            "aud": settings.memoria_auth_audience,
            "sub": user_id,
            "sid": session_id,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "nbf": now,
            "exp": now + ttl,
            "typ": "memoria_access",
        },
        settings.memoria_auth_secret.get_secret_value(),
        algorithm="HS256",
    )
    return (token.decode("utf-8") if isinstance(token, bytes) else str(token), ttl)


def create_refresh_token(*, session_id: str) -> str:
    return f"{session_id}.{secrets.token_urlsafe(48)}"


def create_legacy_upgrade_refresh_token(
    settings: ControlSettings,
    *,
    session_id: str,
    legacy_access_token: str,
) -> str:
    """Derive a retryable migration refresh token without persisting token plaintext."""
    digest = hmac.new(
        settings.memoria_auth_secret.get_secret_value().encode("utf-8"),
        b"memoria-legacy-upgrade-refresh-v1\0" + legacy_access_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"{session_id}.{digest}"


def refresh_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def decode_legacy_access_token(settings: ControlSettings, token: str) -> str | None:
    try:
        claims = jwt.decode(
            token,
            settings.memoria_auth_secret.get_secret_value(),
            algorithms=["HS256"],
            audience=settings.memoria_auth_audience,
            issuer=settings.memoria_auth_issuer,
            options={"require": ["exp", "iat", "nbf", "sub", "aud", "iss"]},
        )
    except jwt.PyJWTError:
        return None
    user_id = claims.get("sub")
    if (
        claims.get("typ") != "memoria_access"
        or "sid" in claims
        or not isinstance(user_id, str)
        or not user_id
    ):
        return None
    return user_id


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
    session_id = claims.get("sid")
    jti = claims.get("jti")
    valid_session = (
        isinstance(session_id, str)
        and bool(session_id)
        and isinstance(jti, str)
        and bool(jti)
    )
    if (
        claims.get("typ") != "memoria_access"
        or not isinstance(user_id, str)
        or not user_id
        or not valid_session
    ):
        raise HTTPException(
            status_code=401,
            detail="invalid access token claims",
            headers={"WWW-Authenticate": "Bearer"},
        )
    store = getattr(request.app.state, "memory_store", None)
    if store is not None:
        unavailable = store.is_account_unavailable(user_id=user_id)
        inactive_session = valid_session and not store.auth_session_active(
            session_id=session_id,
            user_id=user_id,
            now=datetime.now(UTC).isoformat(),
        )
        if unavailable or inactive_session:
            raise HTTPException(
                status_code=401,
                detail="access session is unavailable",
                headers={"WWW-Authenticate": "Bearer"},
            )
    return AuthenticatedUser(
        user_id=user_id,
        session_id=session_id if valid_session else None,
        jti=jti if valid_session else None,
    )


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
    account_ids = {
        str(value)
        for value in (
            session["user_id"],
            session.get("resource_owner_account_id"),
            session.get("legacy_grantee_account_id"),
        )
        if isinstance(value, str) and value
    }
    if any(store.is_account_unavailable(user_id=account_id) for account_id in account_ids):
        raise HTTPException(status_code=410, detail="voice session was deleted")
    return session
