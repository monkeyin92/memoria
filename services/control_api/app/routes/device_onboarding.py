"""HTTP adapters for the Device Bootstrap/Claim/Activation vertical slice.

``main.py`` mounts this router and installs SQLite only outside production.
Production requires the dedicated PostgreSQL/FORCE-RLS authority and an
independent managed Activation signing seed; missing wiring returns 503 and
never falls back to an in-memory or client-supplied authority.
"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.device_fleet.bootstrap_domain import OnboardingError, b64url_decode
from services.device_fleet.bootstrap_service import DeviceOnboardingService

router = APIRouter(tags=["device-onboarding"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ClientInfo(_StrictModel):
    platform: str = Field(min_length=1, max_length=128)
    app_version: str = Field(min_length=1, max_length=128)
    base_library_version: str = Field(min_length=1, max_length=128)


class IntrospectRequest(_StrictModel):
    qr_payload: str = Field(min_length=1, max_length=16384)
    client_onboarding_id: str = Field(min_length=1, max_length=128)
    client: ClientInfo


class DeviceChallengeRequest(_StrictModel):
    device_id: str = Field(min_length=1, max_length=128)
    certificate_id: str = Field(min_length=1, max_length=128)


class NetworkResult(_StrictModel):
    got_ip: bool
    dns_ready: bool
    tls_ready: bool


class OnlineProofRequest(_StrictModel):
    device_id: str = Field(min_length=1, max_length=128)
    certificate_id: str = Field(min_length=1, max_length=128)
    bootstrap_nonce_hash: str = Field(min_length=64, max_length=64)
    mobile_nonce_hash: str = Field(min_length=64, max_length=64)
    challenge_id: str = Field(min_length=1, max_length=128)
    challenge_nonce: str = Field(min_length=43, max_length=43)
    firmware_version: str = Field(min_length=1, max_length=64)
    firmware_security_version: int = Field(ge=0)
    capability_manifest_hash: str = Field(min_length=64, max_length=64)
    network_result: NetworkResult
    monotonic_counter: int = Field(ge=1)
    signature: str = Field(min_length=86, max_length=86)


class ClaimRequest(_StrictModel):
    onboarding_session_id: str = Field(min_length=1, max_length=128)
    device_id: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=128)
    expected_state_version: int = Field(ge=1)


class ActivationAckRequest(_StrictModel):
    device_id: str = Field(min_length=1, max_length=128)
    certificate_id: str = Field(min_length=1, max_length=128)
    binding_id: str = Field(min_length=1, max_length=128)
    binding_version: int = Field(ge=1)
    activation_version: int = Field(ge=1)
    config_hash: str = Field(min_length=64, max_length=64)
    firmware_version: str = Field(min_length=1, max_length=64)
    monotonic_counter: int = Field(ge=1)
    applied_at: str = Field(min_length=1, max_length=64)
    signature: str = Field(min_length=86, max_length=86)


class OfflineMockDeviceRegistration(_StrictModel):
    device_id: str = Field(min_length=1, max_length=128)
    certificate_id: str = Field(min_length=1, max_length=128)
    public_key: str = Field(min_length=43, max_length=43)
    product_model: str = Field(min_length=1, max_length=128)
    hardware_revision: str = Field(min_length=1, max_length=64)
    firmware_version: str = Field(min_length=1, max_length=64)
    firmware_security_version: int = Field(ge=0)
    capability_manifest_hash: str = Field(min_length=64, max_length=64)
    minimum_firmware_security_version: int = Field(default=0, ge=0)


class DeviceSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    display_tail: str
    model: str
    firmware_version: str
    claim_status: Literal["unclaimed", "reserved", "bound", "suspended", "revoked"]


class ProvisioningResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: Literal["ble"]
    ble_name: str
    service_uuid: str
    protocol_version: int


class BootstrapSessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    onboarding_session_id: str
    state: str
    state_version: int
    activation_version: int
    expires_at: str
    device: DeviceSummaryResponse
    provisioning: ProvisioningResponse
    mobile_nonce: str | None = None
    claim_id: str | None = None
    binding_id: str | None = None
    activation_status: str | None = None


class ClaimResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    onboarding_session_id: str
    device_id: str
    status: Literal[
        "reserved",
        "binding_committing",
        "binding_created",
        "committed",
        "released",
        "expired",
        "conflict",
    ]
    expires_at: str


class ActivationStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activation_id: str
    device_id: str
    binding_id: str
    binding_version: int
    activation_version: int
    status: str
    config_hash: str
    acknowledged_at: str | None = None


def _service(request: Request) -> DeviceOnboardingService:
    service = getattr(request.app.state, "device_onboarding_service", None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "device_onboarding_unavailable"},
        )
    return cast(DeviceOnboardingService, service)


def _http_error(error: OnboardingError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail={"code": error.code})


def _device_signature(value: str | None) -> bytes | None:
    if value is None:
        return None
    try:
        return b64url_decode(value, field="device_signature", exact_length=64)
    except OnboardingError as error:
        raise _http_error(error) from error


@router.post(
    "/v1/device-bootstrap/introspect",
    response_model=BootstrapSessionResponse,
    response_model_exclude_none=True,
)
def introspect(
    body: IntrospectRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    try:
        return _service(request).introspect(
            actor_id=user.user_id,
            qr_payload=body.qr_payload,
            client_onboarding_id=body.client_onboarding_id,
            client=body.client.model_dump(mode="python"),
        )
    except OnboardingError as error:
        raise _http_error(error) from error


@router.get(
    "/v1/device-bootstrap/{onboarding_session_id}",
    response_model=BootstrapSessionResponse,
    response_model_exclude_none=True,
)
def get_bootstrap_session(
    onboarding_session_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    try:
        return _service(request).get_session(
            actor_id=user.user_id, onboarding_session_id=onboarding_session_id
        )
    except OnboardingError as error:
        raise _http_error(error) from error


@router.post(
    "/v1/device-bootstrap/{onboarding_session_id}/cancel",
    response_model=BootstrapSessionResponse,
    response_model_exclude_none=True,
)
def cancel_bootstrap_session(
    onboarding_session_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    try:
        return _service(request).cancel_session(
            actor_id=user.user_id, onboarding_session_id=onboarding_session_id
        )
    except OnboardingError as error:
        raise _http_error(error) from error


@router.post("/v1/device-bootstrap/{onboarding_session_id}/challenge")
def issue_device_challenge(
    onboarding_session_id: str,
    body: DeviceChallengeRequest,
    request: Request,
) -> dict[str, object]:
    try:
        return _service(request).issue_challenge(
            onboarding_session_id=onboarding_session_id,
            device_id=body.device_id,
            certificate_id=body.certificate_id,
        )
    except OnboardingError as error:
        raise _http_error(error) from error


@router.post("/v1/device-bootstrap/{onboarding_session_id}/online-proof")
def submit_device_online_proof(
    onboarding_session_id: str,
    body: OnlineProofRequest,
    request: Request,
) -> dict[str, object]:
    try:
        return _service(request).submit_online_proof(
            onboarding_session_id=onboarding_session_id,
            proof=body.model_dump(mode="python"),
        )
    except OnboardingError as error:
        raise _http_error(error) from error


@router.post(
    "/v1/device-claims",
    status_code=status.HTTP_201_CREATED,
    response_model=ClaimResponse,
)
def create_claim(
    body: ClaimRequest,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    try:
        return _service(request).reserve_claim(
            actor_id=user.user_id,
            onboarding_session_id=body.onboarding_session_id,
            device_id=body.device_id,
            idempotency_key=body.idempotency_key,
            expected_state_version=body.expected_state_version,
        )
    except OnboardingError as error:
        raise _http_error(error) from error


@router.get("/v1/device-claims/{claim_id}", response_model=ClaimResponse)
def get_claim(
    claim_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    try:
        return _service(request).get_claim(actor_id=user.user_id, claim_id=claim_id)
    except OnboardingError as error:
        raise _http_error(error) from error


@router.get(
    "/v1/device-activations/{device_id}", response_model=ActivationStatusResponse
)
def get_activation_status(
    device_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    try:
        return _service(request).get_activation_status(
            actor_id=user.user_id, device_id=device_id
        )
    except OnboardingError as error:
        raise _http_error(error) from error


@router.get("/v1/devices/{device_id}/activation-manifest")
def get_device_activation_manifest(
    device_id: str,
    request: Request,
    certificate_id: Annotated[str | None, Header(alias="X-Device-Certificate-ID")] = None,
    device_signature: Annotated[str | None, Header(alias="X-Device-Signature")] = None,
) -> dict[str, object]:
    if not certificate_id:
        raise HTTPException(status_code=401, detail={"code": "device_certificate_required"})
    try:
        manifest = _service(request).get_activation_manifest(
            device_id=device_id,
            certificate_id=certificate_id,
            request_signature=_device_signature(device_signature),
        )
        return manifest
    except OnboardingError as error:
        raise _http_error(error) from error


@router.post("/v1/devices/{device_id}/activation-ack")
def accept_device_activation_ack(
    device_id: str,
    body: ActivationAckRequest,
    request: Request,
) -> dict[str, object]:
    try:
        return _service(request).accept_activation_ack(
            device_id=device_id, ack=body.model_dump(mode="python")
        )
    except OnboardingError as error:
        raise _http_error(error) from error


@router.post("/v1/device-fleet/offline-mock/devices", status_code=status.HTTP_201_CREATED)
def register_offline_mock_device(
    body: OfflineMockDeviceRegistration,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    del user
    try:
        payload = body.model_dump(mode="python")
        public_key_b64 = str(payload.pop("public_key"))
        return _service(request).register_offline_mock_device(
            public_key_b64=public_key_b64,
            **payload,
        )
    except OnboardingError as error:
        raise _http_error(error) from error


__all__ = ["router"]
