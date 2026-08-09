"""Account-scoped Digital Self version lifecycle APIs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, NoReturn, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from services.control_api.app.account_gate import (
    AccountDeletingError,
    AccountOperationGate,
    require_capability_for_subject,
)
from services.control_api.app.database import MemoryStore
from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
    verify_password,
)
from services.digital_self.compiler import manifest_dict
from services.digital_self.domain import (
    DigitalSelfVersion,
    EmptyDigitalSelfSourceError,
    InvalidVersionTransitionError,
    ManifestIntegrityError,
    RegistryPort,
    SourceSnapshotConflictError,
    VersionNotFoundError,
)
from services.digital_self.preview import SelfPreviewRegistryPort

router = APIRouter(prefix="/v1/digital-self", tags=["digital-self"])


def _registry(request: Request) -> RegistryPort:
    return cast(RegistryPort, request.app.state.digital_self_registry)


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _preview(request: Request) -> SelfPreviewRegistryPort:
    return cast(SelfPreviewRegistryPort, request.app.state.self_preview_registry)


@asynccontextmanager
async def _account_write(request: Request, account_id: str) -> AsyncIterator[None]:
    gate = cast(AccountOperationGate, request.app.state.account_operations)
    try:
        async with gate.write(account_id):
            yield
    except AccountDeletingError as exc:
        raise _error(status.HTTP_409_CONFLICT, "account_deletion_in_progress") from exc


async def require_digital_self_writable(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> AsyncIterator[AuthenticatedUser]:
    async with _account_write(request, user.user_id):
        yield user


def _error(status_code: int, code: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code})


def _require_registered(request: Request, user: AuthenticatedUser) -> dict[str, Any]:
    require_capability_for_subject(user, "digital_self", store=_store(request))
    if _store(request).is_account_unavailable(user_id=user.user_id):
        raise _error(status.HTTP_409_CONFLICT, "account_deletion_in_progress")
    account = _store(request).get_account(user_id=user.user_id)
    if account is None:
        raise _error(status.HTTP_403_FORBIDDEN, "account_not_registered")
    return account


def _version_payload(version: DigitalSelfVersion) -> dict[str, Any]:
    manifest = manifest_dict(version.manifest)
    return {
        "version_id": version.version_id,
        "version_number": version.version_number,
        "status": version.status,
        "manifest_sha256": version.manifest_sha256,
        "manifest": manifest,
        "source_summary": manifest["source_summary"],
        "parent_version_id": version.manifest.parent_version_id,
        "rollback_target_version_id": version.manifest.rollback_target_version_id,
        "created_at": version.created_at.isoformat(),
    }


def _raise_registry_error(exc: Exception) -> NoReturn:
    if isinstance(exc, VersionNotFoundError):
        raise _error(status.HTTP_404_NOT_FOUND, "version_not_found") from exc
    if isinstance(exc, EmptyDigitalSelfSourceError):
        raise _error(status.HTTP_422_UNPROCESSABLE_CONTENT, "empty_source") from exc
    if isinstance(exc, InvalidVersionTransitionError):
        raise _error(status.HTTP_409_CONFLICT, "invalid_transition") from exc
    if isinstance(exc, ManifestIntegrityError):
        raise _error(status.HTTP_409_CONFLICT, "manifest_integrity") from exc
    if isinstance(exc, SourceSnapshotConflictError):
        raise _error(status.HTTP_409_CONFLICT, "source_snapshot_conflict") from exc
    raise exc


class ExpectedManifestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SensitiveTransitionBody(ExpectedManifestBody):
    password: str = Field(min_length=8, max_length=128)


def _require_step_up(account: dict[str, Any], password: str) -> None:
    if not verify_password(password, str(account["password_hash"])):
        raise _error(status.HTTP_403_FORBIDDEN, "step_up_failed")


@router.post("/versions", status_code=status.HTTP_201_CREATED)
async def build_version(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_digital_self_writable)],
) -> dict[str, Any]:
    _require_registered(request, user)
    try:
        version = await _registry(request).build(account_id=user.user_id)
    except Exception as exc:
        _raise_registry_error(exc)
    return _version_payload(version)


@router.get("/versions")
async def list_versions(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    _require_registered(request, user)
    try:
        versions = await _registry(request).list(account_id=user.user_id)
    except Exception as exc:
        _raise_registry_error(exc)
    return {"items": [_version_payload(version) for version in versions]}


@router.get("/versions/{version_id}")
async def get_version(
    version_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    _require_registered(request, user)
    try:
        version = await _registry(request).get(account_id=user.user_id, version_id=version_id)
    except Exception as exc:
        _raise_registry_error(exc)
    return _version_payload(version)


@router.post("/versions/{version_id}/testing")
async def begin_testing(
    version_id: str,
    body: ExpectedManifestBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_digital_self_writable)],
) -> dict[str, Any]:
    _require_registered(request, user)
    try:
        version = await _registry(request).begin_testing(
            account_id=user.user_id,
            version_id=version_id,
            expected_manifest_sha256=body.expected_manifest_sha256,
        )
    except Exception as exc:
        _raise_registry_error(exc)
    return _version_payload(version)


async def _sensitive_transition(
    *,
    action: str,
    version_id: str,
    body: SensitiveTransitionBody,
    request: Request,
    user: AuthenticatedUser,
) -> dict[str, Any]:
    account = _require_registered(request, user)
    _require_step_up(account, body.password)
    registry = _registry(request)
    try:
        if action == "rollback":
            version = await registry.rollback(
                account_id=user.user_id,
                target_version_id=version_id,
                expected_manifest_sha256=body.expected_manifest_sha256,
            )
        elif action == "approve":
            current = await registry.get(
                account_id=user.user_id,
                version_id=version_id,
            )
            if current.manifest_sha256 != body.expected_manifest_sha256:
                raise SourceSnapshotConflictError(version_id)
            verdict = await _preview(request).completed_verdict(
                account_id=user.user_id,
                version_id=version_id,
                manifest_sha256=body.expected_manifest_sha256,
            )
            if verdict != "approve":
                raise _error(
                    status.HTTP_409_CONFLICT,
                    "fidelity_approval_required",
                )
            version = await registry.approve(
                account_id=user.user_id,
                version_id=version_id,
                expected_manifest_sha256=body.expected_manifest_sha256,
            )
        elif action == "freeze":
            version = await registry.freeze(
                account_id=user.user_id,
                version_id=version_id,
                expected_manifest_sha256=body.expected_manifest_sha256,
            )
        elif action == "revoke":
            version = await registry.revoke(
                account_id=user.user_id,
                version_id=version_id,
                expected_manifest_sha256=body.expected_manifest_sha256,
            )
        else:  # pragma: no cover - internal callers use the fixed action set
            raise RuntimeError(f"unknown digital self transition: {action}")
    except Exception as exc:
        _raise_registry_error(exc)
    return _version_payload(version)


@router.post("/versions/{version_id}/approve")
async def approve_version(
    version_id: str,
    body: SensitiveTransitionBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_digital_self_writable)],
) -> dict[str, Any]:
    return await _sensitive_transition(
        action="approve", version_id=version_id, body=body, request=request, user=user
    )


@router.post("/versions/{version_id}/freeze")
async def freeze_version(
    version_id: str,
    body: SensitiveTransitionBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_digital_self_writable)],
) -> dict[str, Any]:
    return await _sensitive_transition(
        action="freeze", version_id=version_id, body=body, request=request, user=user
    )


@router.post("/versions/{version_id}/revoke")
async def revoke_version(
    version_id: str,
    body: SensitiveTransitionBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_digital_self_writable)],
) -> dict[str, Any]:
    return await _sensitive_transition(
        action="revoke", version_id=version_id, body=body, request=request, user=user
    )


@router.post("/versions/{version_id}/rollback")
async def rollback_version(
    version_id: str,
    body: SensitiveTransitionBody,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_digital_self_writable)],
) -> dict[str, Any]:
    return await _sensitive_transition(
        action="rollback", version_id=version_id, body=body, request=request, user=user
    )
