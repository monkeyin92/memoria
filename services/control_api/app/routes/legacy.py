"""Explicit Legacy grants, owner rehearsal, revocation, and grantee shells."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.control_api.app.account_gate import (
    require_capability_for_account_id,
    require_capability_for_subject,
    require_writable_account,
)
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
    verify_password,
)
from services.digital_self.domain import RegistryPort, VersionNotFoundError
from services.legacy.domain import (
    LegacyAccessDeniedError,
    LegacyGrant,
    LegacyGrantConflictError,
    LegacyIdempotencyConflictError,
    LegacyManifestItemKind,
    LegacyManifestItemRef,
    LegacyNotFoundError,
    LegacyRegistryPort,
    LegacyRelationshipShell,
    RegisteredGranteeSnapshot,
)
from services.self_model.domain import (
    SelfModelNotFoundError,
    SelfModelRegistryPort,
)

router = APIRouter(prefix="/v1/legacy", tags=["legacy"])


def _legacy(request: Request) -> LegacyRegistryPort:
    return cast(LegacyRegistryPort, request.app.state.legacy_registry)


def _versions(request: Request) -> RegistryPort:
    return cast(RegistryPort, request.app.state.digital_self_registry)


def _self_model(request: Request) -> SelfModelRegistryPort:
    return cast(SelfModelRegistryPort, request.app.state.self_model_registry)


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


def _registered(request: Request, user: AuthenticatedUser) -> dict[str, Any]:
    account = _store(request).get_account(user_id=user.user_id)
    if account is None:
        raise _error(status.HTTP_403_FORBIDDEN, "account_not_registered")
    return account


def _step_up(account: dict[str, Any], password: str) -> None:
    if not verify_password(password, str(account["password_hash"])):
        raise _error(status.HTTP_403_FORBIDDEN, "step_up_failed")


def _raise_legacy_error(exc: Exception) -> NoReturn:
    if isinstance(exc, (LegacyNotFoundError, VersionNotFoundError, SelfModelNotFoundError)):
        raise _error(status.HTTP_404_NOT_FOUND, "legacy_resource_not_found") from exc
    if isinstance(exc, LegacyAccessDeniedError):
        raise _error(status.HTTP_403_FORBIDDEN, "legacy_access_denied") from exc
    if isinstance(exc, LegacyGrantConflictError):
        raise _error(status.HTTP_409_CONFLICT, "legacy_snapshot_conflict") from exc
    if isinstance(exc, LegacyIdempotencyConflictError):
        raise _error(status.HTTP_409_CONFLICT, "legacy_idempotency_conflict") from exc
    if isinstance(exc, ValueError):
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "legacy_contract_invalid") from exc
    raise exc


def _account_username(store: MemoryStore, account_id: str) -> str:
    account = store.get_account(user_id=account_id)
    return str(account["username"]) if account is not None else "不可用账户"


def _grant_payload(request: Request, grant: LegacyGrant, *, now: datetime) -> dict[str, Any]:
    store = _store(request)
    return {
        "grant_id": grant.grant_id,
        "owner_account_id": grant.owner_account_id,
        "owner_username": _account_username(store, grant.owner_account_id),
        "grantee_account_id": grant.grantee_account_id,
        "grantee_username": _account_username(store, grant.grantee_account_id),
        "version_id": grant.version_id,
        "version_number": grant.version_number,
        "manifest_sha256": grant.manifest_sha256,
        "relationship_profile_id": grant.relationship.profile_id,
        "relationship_profile_version": grant.relationship.version_number,
        "relationship_id": grant.relationship.relationship_id,
        "allowed_items": [
            {"kind": item.kind, "item_id": item.item_id}
            for item in grant.allowed_items
        ],
        "visibility": grant.visibility,
        "scope_sha256": grant.scope_sha256,
        "voice_allowed": grant.voice_allowed,
        "status": grant.status_at(now),
        "expires_at": grant.expires_at.isoformat(),
        "activated_at": (
            grant.activated_at.isoformat() if grant.activated_at is not None else None
        ),
        "revoked_at": (
            grant.revoked_at.isoformat() if grant.revoked_at is not None else None
        ),
        "grant_snapshot_sha256": grant.grant_snapshot_sha256,
        "revision": grant.revision,
        "created_at": grant.created_at.isoformat(),
        "digital_identity_disclosure": (
            "这是基于冻结资料生成的数字分身，不是本人。"
        ),
    }


def _shell_payload(shell: LegacyRelationshipShell) -> dict[str, Any]:
    return {
        "shell_id": shell.shell_id,
        "grant_id": shell.grant_id,
        "owner_account_id": shell.owner_account_id,
        "grantee_account_id": shell.grantee_account_id,
        "preferences": dict(shell.preferences),
        "revision": shell.revision,
        "created_at": shell.created_at.isoformat(),
        "updated_at": shell.updated_at.isoformat(),
    }


class LegacyItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: LegacyManifestItemKind
    item_id: str = Field(min_length=1, max_length=128)


class LegacyGrantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    grantee_username: str = Field(min_length=3, max_length=64)
    version_id: str = Field(min_length=1, max_length=128)
    relationship_profile_id: str = Field(min_length=1, max_length=128)
    allowed_items: tuple[LegacyItemCreate, ...] = Field(min_length=1, max_length=128)
    voice_allowed: bool = False
    expires_at: datetime
    password: str = Field(min_length=8, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("expires_at")
    @classmethod
    def require_utc_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("legacy expiry must be timezone aware")
        return value.astimezone(UTC)


class LegacyGrantTransition(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_grant_snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    password: str = Field(min_length=8, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=128)


class LegacyShellPreferencesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=1)
    preferred_response_length: Literal["brief", "balanced", "detailed"]
    question_frequency: Literal["rare", "occasional"]
    idempotency_key: str = Field(min_length=1, max_length=128)


@router.post("/grants", status_code=status.HTTP_201_CREATED)
async def issue_grant(
    body: LegacyGrantCreate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "legacy_grant", store=_store(request))
    owner = _registered(request, user)
    _step_up(owner, body.password)
    store = _store(request)
    grantee = store.get_account_by_username(
        username_normalized=body.grantee_username.casefold()
    )
    if (
        grantee is None
        or str(grantee["user_id"]) == user.user_id
        or store.is_account_unavailable(user_id=str(grantee["user_id"]))
    ):
        raise _error(status.HTTP_404_NOT_FOUND, "legacy_grantee_not_found")
    require_capability_for_account_id(
        str(grantee["user_id"]),
        "legacy_receive",
        store=store,
    )
    try:
        version = await _versions(request).get(
            account_id=user.user_id,
            version_id=body.version_id,
        )
        relationship = await _self_model(request).get_relationship_profile(
            account_id=user.user_id,
            profile_id=body.relationship_profile_id,
        )
        if version.status != "frozen":
            raise ValueError("legacy grant requires a frozen Digital Self version")
        if relationship.status != "approved" or not relationship.step_up_verified:
            raise ValueError("legacy grant requires an approved relationship profile")
        grant = await _legacy(request).issue(
            owner_account_id=user.user_id,
            grantee=RegisteredGranteeSnapshot(
                account_id=str(grantee["user_id"]),
                registered_at=datetime.fromisoformat(str(grantee["created_at"])),
            ),
            version=version,
            relationship_profile=relationship,
            allowed_items=tuple(
                LegacyManifestItemRef(kind=item.kind, item_id=item.item_id)
                for item in body.allowed_items
            ),
            visibility="family",
            voice_allowed=body.voice_allowed,
            expires_at=body.expires_at,
            idempotency_key=body.idempotency_key,
            now=datetime.now(UTC),
        )
    except Exception as exc:
        _raise_legacy_error(exc)
    return _grant_payload(request, grant, now=datetime.now(UTC))


@router.get("/grants")
async def list_grants(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    role: Annotated[Literal["owner", "grantee"], Query()],
) -> dict[str, Any]:
    require_capability_for_subject(
        user,
        "legacy_grant" if role == "owner" else "legacy_receive",
        store=_store(request),
    )
    _registered(request, user)
    try:
        grants = await _legacy(request).list_grants(
            actor_account_id=user.user_id,
            role=role,
        )
    except Exception as exc:
        _raise_legacy_error(exc)
    now = datetime.now(UTC)
    items: list[dict[str, Any]] = []
    for grant in grants:
        item = _grant_payload(request, grant, now=now)
        item["shell"] = None
        if role == "grantee" and grant.status_at(now) == "active":
            shell = await _legacy(request).get_shell_for_grant(
                actor_account_id=user.user_id,
                grant_id=grant.grant_id,
                now=now,
            )
            item["shell"] = _shell_payload(shell) if shell is not None else None
        items.append(item)
    return {
        "role": role,
        "items": items,
    }


async def _transition(
    *,
    action: Literal["activate", "revoke"],
    grant_id: str,
    body: LegacyGrantTransition,
    request: Request,
    user: AuthenticatedUser,
) -> dict[str, Any]:
    require_capability_for_subject(
        user,
        "legacy_grant",
        store=_store(request),
    )
    account = _registered(request, user)
    _step_up(account, body.password)
    try:
        registry = _legacy(request)
        if action == "activate":
            visible = await registry.list_grants(
                actor_account_id=user.user_id,
                role="owner",
            )
            pending = next((item for item in visible if item.grant_id == grant_id), None)
            if pending is None:
                raise LegacyNotFoundError("legacy grant not found")
            require_capability_for_account_id(
                pending.grantee_account_id,
                "legacy_receive",
                store=_store(request),
            )
        transition = registry.activate if action == "activate" else registry.revoke
        grant = await transition(
            actor_account_id=user.user_id,
            grant_id=grant_id,
            expected_grant_snapshot_sha256=body.expected_grant_snapshot_sha256,
            idempotency_key=body.idempotency_key,
            now=datetime.now(UTC),
        )
    except Exception as exc:
        _raise_legacy_error(exc)
    return _grant_payload(request, grant, now=datetime.now(UTC))


@router.post("/grants/{grant_id}/activate")
async def activate_grant(
    grant_id: str,
    body: LegacyGrantTransition,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    return await _transition(
        action="activate", grant_id=grant_id, body=body, request=request, user=user
    )


@router.post("/grants/{grant_id}/revoke")
async def revoke_grant(
    grant_id: str,
    body: LegacyGrantTransition,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    return await _transition(
        action="revoke", grant_id=grant_id, body=body, request=request, user=user
    )


@router.get("/shells/{shell_id}/preferences")
async def get_shell_preferences(
    shell_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "legacy_receive", store=_store(request))
    _registered(request, user)
    try:
        shell = await _legacy(request).get_shell(
            actor_account_id=user.user_id,
            shell_id=shell_id,
            now=datetime.now(UTC),
        )
    except Exception as exc:
        _raise_legacy_error(exc)
    return _shell_payload(shell)


@router.patch("/shells/{shell_id}/preferences")
async def update_shell_preferences(
    shell_id: str,
    body: LegacyShellPreferencesUpdate,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, Any]:
    require_capability_for_subject(user, "legacy_receive", store=_store(request))
    _registered(request, user)
    try:
        shell = await _legacy(request).update_shell_preferences(
            actor_account_id=user.user_id,
            shell_id=shell_id,
            preferences=(
                ("preferred_response_length", body.preferred_response_length),
                ("question_frequency", body.question_frequency),
            ),
            expected_shell_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
            now=datetime.now(UTC),
        )
    except Exception as exc:
        _raise_legacy_error(exc)
    return _shell_payload(shell)
