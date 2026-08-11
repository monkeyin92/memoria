"""Thin HTTP adapters for the identity lifecycle.

Relationships (invite/accept/suspend/resume/dispute/resolution/revoke),
versioned bindings (supersede/unbind/versions) and the two-party ownership
transfer flow (create/accept/cancel).  Every actor comes exclusively from
``AuthenticatedUser.user_id``; request bodies that try to carry an actor
field are rejected with extra=forbid.  All mutations delegate to
``IdentityService`` — the router never re-implements domain rules.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status
from packages.contracts.generated.python.multi_subject_contracts import (
    BindingRoleValue,
    DeviceDeclaredModeValue,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.identity.domain import (
    ALL_RELATION_TYPES,
    BindingRole,
    IdentityAccessDeniedError,
    IdentityConflictError,
    IdentityNotFoundError,
    Permission,
    RelationType,
    TransferLifecycleError,
    validate_manifest_wire,
)
from services.identity.service import IdentityService

router = APIRouter(tags=["identity-lifecycle"])


def _identity(request: Request) -> IdentityService:
    return cast(IdentityService, request.app.state.identity_service)


def _error(
    exc: Exception,
    *,
    not_found: str,
    forbidden: str,
    conflict: str,
) -> HTTPException:
    if isinstance(exc, IdentityNotFoundError):
        return HTTPException(status_code=404, detail={"code": not_found})
    if isinstance(exc, IdentityAccessDeniedError):
        return HTTPException(status_code=403, detail={"code": forbidden})
    if isinstance(exc, (IdentityConflictError, TransferLifecycleError)):
        return HTTPException(
            status_code=409,
            detail={"code": conflict, "message": str(exc)},
        )
    if isinstance(exc, ValueError):
        return HTTPException(
            status_code=422,
            detail={"code": "invalid_request", "message": str(exc)},
        )
    raise exc


def _aware_datetime(value: str | None, field: str) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must be an RFC3339 timestamp with offset")
    return parsed.astimezone(UTC)


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InviteRelationshipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    target_person_id: str = Field(min_length=1, max_length=128)
    relation_type: str = Field(min_length=1, max_length=32)
    established_evidence_id: str = Field(min_length=1, max_length=128)
    permissions: tuple[str, ...] = ()
    can_delegate: bool = False
    delegated_from_relationship_id: str | None = Field(default=None, max_length=128)
    valid_until: str | None = Field(default=None, max_length=64)

    @field_validator("relation_type")
    @classmethod
    def _relation_type_known(cls, value: str) -> str:
        if value not in ALL_RELATION_TYPES:
            raise ValueError(f"unknown relation_type {value!r}")
        return value

    @model_validator(mode="after")
    def _contract(self) -> InviteRelationshipRequest:
        if self.relation_type == "self":
            raise ValueError("self relationships cannot be invited")
        if self.delegated_from_relationship_id is not None and not self.can_delegate:
            raise ValueError("delegation requires can_delegate=true")
        if len(self.permissions) != len(set(self.permissions)):
            raise ValueError("permissions must be unique")
        return self


class DisputeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(min_length=1, max_length=256)


class RevokeRelationshipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_id: str = Field(min_length=1, max_length=128)


class RoleGrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    person_id: str = Field(min_length=1, max_length=128)
    role: BindingRoleValue
    permissions: tuple[str, ...] = ()


class BindingMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    declared_mode: DeviceDeclaredModeValue
    primary_subject_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    roles: tuple[RoleGrantRequest, ...] = ()
    family_space_id: str | None = Field(default=None, max_length=128)
    valid_until: str | None = Field(default=None, max_length=64)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=64)

    # service_profile_version / policy_bundle_version / consent_snapshot_id /
    # persona_assignment_id are server-authoritative fields: the service
    # derives them (inheritance / consent authority), the client may not
    # inject them (extra=forbid rejects any attempt).

    @model_validator(mode="after")
    def _contract(self) -> BindingMutationRequest:
        if len(self.primary_subject_ids) != len(set(self.primary_subject_ids)):
            raise ValueError("primary_subject_ids must be unique")
        seen: set[tuple[str, str]] = set()
        for grant in self.roles:
            key = (grant.person_id, grant.role)
            if key in seen:
                raise ValueError("role grants must be unique per person+role")
            seen.add(key)
            if len(grant.permissions) != len(set(grant.permissions)):
                raise ValueError("permissions must be unique")
        if self.declared_mode == "family_shared" and not self.family_space_id:
            raise ValueError("family_shared requires family_space_id")
        return self


class CreateTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    to_account_owner_person_id: str = Field(min_length=1, max_length=128)
    # The step-up *submission* is a signed evidence ticket; the persisted
    # intent stores only the authority-resolved short evidence id (<= 128).
    step_up_evidence_id: str = Field(min_length=1, max_length=2048)
    # The policy receipt is referenced by its immutable receipt id, never by
    # a client-crafted ticket.
    policy_receipt_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=64)
    valid_until: str | None = Field(default=None, max_length=64)


class CancelTransferRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(default="cancelled by owner", min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=8, max_length=64)


class UnbindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reason: str = Field(default="unbind", min_length=1, max_length=256)


def _role_permissions(
    grants: tuple[RoleGrantRequest, ...],
) -> tuple[
    tuple[tuple[str, BindingRole], ...],
    dict[tuple[str, BindingRole], frozenset[Permission]],
]:
    roles: list[tuple[str, BindingRole]] = []
    role_permissions: dict[tuple[str, BindingRole], frozenset[Permission]] = {}
    for grant in grants:
        roles.append((grant.person_id, grant.role))
        if grant.permissions:
            role_permissions[(grant.person_id, grant.role)] = frozenset(
                cast(tuple[Permission, ...], grant.permissions)
            )
    return tuple(roles), role_permissions


def _manifest_payload(manifest: Any) -> dict[str, object]:
    validate_manifest_wire(manifest)
    return cast(dict[str, object], manifest.to_dict())


def _manifest_includes(manifest: Any, person_id: str) -> bool:
    roles = {role.person_id for role in manifest.roles}
    return person_id in roles or person_id == manifest.account_owner_id


# ---------------------------------------------------------------------------
# Persons
# ---------------------------------------------------------------------------


@router.get("/v1/persons/me")
async def get_my_person(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    try:
        person = await identity.get_person(
            user.user_id, actor_person_id=user.user_id
        )
    except IdentityNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "person_not_found"},
        ) from exc
    return person.to_dict()


# ---------------------------------------------------------------------------
# Relationships
# ---------------------------------------------------------------------------


@router.post("/v1/relationships/invites", status_code=status.HTTP_201_CREATED)
async def invite_relationship(
    body: InviteRelationshipRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    try:
        relationship = await identity.propose_relationship(
            source_person_id=user.user_id,
            target_person_id=body.target_person_id,
            relation_type=cast(RelationType, body.relation_type),
            established_evidence_id=body.established_evidence_id,
            permissions=frozenset(
                cast(tuple[Permission, ...], body.permissions)
            ),
            can_delegate=body.can_delegate,
            delegated_from_relationship_id=body.delegated_from_relationship_id,
            valid_until=_aware_datetime(body.valid_until, "valid_until"),
            actor_person_id=user.user_id,
        )
    except Exception as exc:
        raise _error(
            exc,
            not_found="relationship_target_not_found",
            forbidden="relationship_forbidden",
            conflict="relationship_conflict",
        ) from exc
    return relationship.to_dict()


@router.get("/v1/relationships")
async def list_my_relationships(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    relationships = await identity.list_relationships(
        person_id=user.user_id,
        actor_person_id=user.user_id,
    )
    return {"relationships": [item.to_dict() for item in relationships]}


@router.post("/v1/relationships/{relationship_id}/accept")
async def accept_relationship(
    relationship_id: str,
    body: EmptyBody | None,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    try:
        relationship = await identity.confirm_relationship(
            relationship_id=relationship_id,
            person_id=user.user_id,
        )
    except Exception as exc:
        raise _error(
            exc,
            not_found="relationship_not_found",
            forbidden="relationship_forbidden",
            conflict="relationship_conflict",
        ) from exc
    return relationship.to_dict()


@router.post("/v1/relationships/{relationship_id}/suspend")
async def suspend_relationship(
    relationship_id: str,
    body: EmptyBody | None,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    try:
        relationship = await identity.suspend_relationship(
            relationship_id=relationship_id,
            actor_person_id=user.user_id,
        )
    except Exception as exc:
        raise _error(
            exc,
            not_found="relationship_not_found",
            forbidden="relationship_forbidden",
            conflict="relationship_conflict",
        ) from exc
    return relationship.to_dict()


@router.post("/v1/relationships/{relationship_id}/resume")
async def resume_relationship(
    relationship_id: str,
    body: EmptyBody | None,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    try:
        relationship = await identity.resume_relationship(
            relationship_id=relationship_id,
            actor_person_id=user.user_id,
        )
    except Exception as exc:
        raise _error(
            exc,
            not_found="relationship_not_found",
            forbidden="relationship_forbidden",
            conflict="relationship_conflict",
        ) from exc
    return relationship.to_dict()


@router.post("/v1/relationships/{relationship_id}/dispute")
async def dispute_relationship(
    relationship_id: str,
    body: DisputeRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    try:
        relationship = await identity.dispute_relationship(
            relationship_id=relationship_id,
            actor_person_id=user.user_id,
            reason=body.reason,
        )
    except Exception as exc:
        raise _error(
            exc,
            not_found="relationship_not_found",
            forbidden="relationship_forbidden",
            conflict="relationship_conflict",
        ) from exc
    return relationship.to_dict()


@router.post("/v1/relationships/{relationship_id}/dispute/resolution")
async def acknowledge_dispute_resolution(
    relationship_id: str,
    body: EmptyBody | None,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    try:
        relationship = await identity.acknowledge_dispute_resolution(
            relationship_id=relationship_id,
            person_id=user.user_id,
        )
    except Exception as exc:
        raise _error(
            exc,
            not_found="relationship_not_found",
            forbidden="relationship_forbidden",
            conflict="relationship_conflict",
        ) from exc
    return relationship.to_dict()


@router.post("/v1/relationships/{relationship_id}/revoke")
async def revoke_relationship(
    relationship_id: str,
    body: RevokeRelationshipRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    try:
        relationship = await identity.revoke_relationship(
            relationship_id=relationship_id,
            actor_person_id=user.user_id,
            evidence_id=body.evidence_id,
        )
    except Exception as exc:
        raise _error(
            exc,
            not_found="relationship_not_found",
            forbidden="relationship_forbidden",
            conflict="relationship_conflict",
        ) from exc
    return relationship.to_dict()


# ---------------------------------------------------------------------------
# Versioned bindings
# ---------------------------------------------------------------------------


@router.get("/v1/devices/{device_id}/bindings")
async def list_binding_versions(
    device_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    manifests = await identity.list_binding_versions(
        device_id, actor_person_id=user.user_id
    )
    if not manifests:
        raise HTTPException(status_code=404, detail={"code": "binding_not_found"})
    if not any(_manifest_includes(manifest, user.user_id) for manifest in manifests):
        raise HTTPException(status_code=403, detail={"code": "binding_forbidden"})
    return {"bindings": [_manifest_payload(manifest) for manifest in manifests]}


@router.post("/v1/devices/{device_id}/binding/supersede")
async def supersede_binding(
    device_id: str,
    body: BindingMutationRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    roles, role_permissions = _role_permissions(body.roles)
    try:
        manifest = await identity.supersede_binding(
            device_id=device_id,
            declared_mode=body.declared_mode,
            primary_subject_ids=body.primary_subject_ids,
            roles=roles,
            role_permissions=role_permissions,
            family_space_id=body.family_space_id,
            valid_until=_aware_datetime(body.valid_until, "valid_until"),
            actor_person_id=user.user_id,
        )
    except Exception as exc:
        raise _error(
            exc,
            not_found="binding_not_found",
            forbidden="binding_forbidden",
            conflict="binding_conflict",
        ) from exc
    return _manifest_payload(manifest)


@router.post("/v1/devices/{device_id}/binding/unbind")
async def unbind_device(
    device_id: str,
    body: UnbindRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    try:
        binding = await identity.revoke_binding(
            device_id=device_id,
            actor_person_id=user.user_id,
            reason=body.reason,
        )
    except Exception as exc:
        raise _error(
            exc,
            not_found="binding_not_found",
            forbidden="binding_forbidden",
            conflict="binding_conflict",
        ) from exc
    return {"binding_id": binding.binding_id, "status": binding.status}


# ---------------------------------------------------------------------------
# Ownership transfer intents
# ---------------------------------------------------------------------------


@router.post("/v1/devices/{device_id}/transfers", status_code=status.HTTP_201_CREATED)
async def create_transfer(
    device_id: str,
    body: CreateTransferRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    try:
        intent = await identity.create_transfer_intent(
            device_id=device_id,
            to_account_owner_person_id=body.to_account_owner_person_id,
            step_up_evidence_id=body.step_up_evidence_id,
            policy_receipt_id=body.policy_receipt_id,
            idempotency_key=body.idempotency_key,
            valid_until=_aware_datetime(body.valid_until, "valid_until"),
            actor_person_id=user.user_id,
        )
    except Exception as exc:
        raise _error(
            exc,
            not_found="binding_not_found",
            forbidden="transfer_forbidden",
            conflict="transfer_conflict",
        ) from exc
    return intent.to_dict()


@router.get("/v1/devices/{device_id}/transfers")
async def list_device_transfers(
    device_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    intents = await identity.list_transfer_intents(
        device_id, actor_person_id=user.user_id
    )
    visible = [
        intent
        for intent in intents
        if user.user_id
        in (intent.from_account_owner_person_id, intent.to_account_owner_person_id)
    ]
    return {"transfers": [intent.to_dict() for intent in visible]}


@router.get("/v1/transfers/{transfer_id}")
async def get_transfer(
    transfer_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    try:
        intent = await identity.get_transfer_intent(
            transfer_id, actor_person_id=user.user_id
        )
    except IdentityNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "transfer_not_found"}) from exc
    if user.user_id not in (
        intent.from_account_owner_person_id,
        intent.to_account_owner_person_id,
    ):
        raise HTTPException(status_code=403, detail={"code": "transfer_forbidden"})
    return intent.to_dict()


@router.post("/v1/transfers/{transfer_id}/accept")
async def accept_transfer(
    transfer_id: str,
    body: BindingMutationRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    if body.idempotency_key is None:
        raise HTTPException(
            status_code=422,
            detail={"code": "idempotency_key_required"},
        )
    roles, role_permissions = _role_permissions(body.roles)
    try:
        manifest = await identity.accept_transfer_intent(
            transfer_id=transfer_id,
            actor_person_id=user.user_id,
            idempotency_key=body.idempotency_key or "",
            declared_mode=body.declared_mode,
            primary_subject_ids=body.primary_subject_ids,
            roles=roles,
            role_permissions=role_permissions,
            family_space_id=body.family_space_id,
            valid_until=_aware_datetime(body.valid_until, "valid_until"),
        )
    except Exception as exc:
        raise _error(
            exc,
            not_found="transfer_not_found",
            forbidden="transfer_forbidden",
            conflict="transfer_conflict",
        ) from exc
    return _manifest_payload(manifest)


@router.post("/v1/transfers/{transfer_id}/cancel")
async def cancel_transfer(
    transfer_id: str,
    body: CancelTransferRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    identity = _identity(request)
    try:
        intent = await identity.cancel_transfer_intent(
            transfer_id=transfer_id,
            actor_person_id=user.user_id,
            idempotency_key=body.idempotency_key,
            reason=body.reason,
        )
    except Exception as exc:
        raise _error(
            exc,
            not_found="transfer_not_found",
            forbidden="transfer_forbidden",
            conflict="transfer_conflict",
        ) from exc
    return intent.to_dict()
