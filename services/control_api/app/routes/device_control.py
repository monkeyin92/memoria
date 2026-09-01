"""HTTP adapters for the server-owned device control plane.

Every subject-facing endpoint runs the account_gate capability matrix before
reading any private device state or performing a write. Capabilities that
are not declared in the rule table, and accounts without a subject category,
fail closed with 403. Device settings are versioned here and every semantic
change (including learning_mode) advances the runtime profile ledger so the
device observes a new runtime_profile_version.
"""

from __future__ import annotations

import hmac
import logging
import ssl
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from services.control_api.app.account_gate import (
    SubjectCapability,
    require_capability_for_account_id,
    require_writable_account,
)
from services.control_api.app.database import MemoryStore
from services.control_api.app.device_control import (
    AcousticCapabilityAuthority,
    AudioModeGateError,
    DeviceSettings,
    DeviceSettingsAuthority,
    DeviceSettingsConflictError,
    ProfileAckConflictError,
    RuntimeProfileLedger,
    allowed_audio_modes,
)
from services.control_api.app.media_runtime import DEVICE_STREAM_EPOCH_MAX
from services.control_api.app.security import (
    AuthenticatedUser,
    require_authenticated_user,
)
from services.control_api.app.session_directory import (
    SessionDirectory,
    SessionDirectoryUnavailable,
)
from services.control_api.app.wake_words import (
    resolve_wake_word_settings,
    validate_custom_wake_word,
    wake_word_catalog_payload,
    wake_word_validation_warnings,
)
from services.device_fleet.bootstrap_domain import OnboardingError
from services.device_fleet.bootstrap_service import DeviceOnboardingService
from services.session_runtime.postgres_store import SessionRuntimeConflict
from services.session_runtime.service import (
    PersistentSessionDenied,
    PersistentSessionNotFound,
    PersistentSessionUnavailable,
    PostgresSessionRuntimeService,
)

router = APIRouter(prefix="/v1/devices", tags=["device-control"])
logger = logging.getLogger(__name__)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DeviceSettingsPatch(_StrictModel):
    changes: dict[str, object]
    expected_settings_version: int | None = Field(default=None, ge=0)
    reason: str = Field(default="settings_update", min_length=1, max_length=256)


class DeviceRuntimeProfileAck(_StrictModel):
    profile_version: int = Field(ge=1)
    runtime_profile_id: str | None = Field(default=None, min_length=1, max_length=128)
    accepted: bool


class DeviceSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device_id: str
    settings_version: int
    volume_limit: int
    screen_brightness: int
    night_mode: bool
    do_not_disturb: bool
    learning_mode: str
    audio_mode: str
    wake_mode: str
    wake_word_id: str
    wake_word_pinyin: str
    wake_word_display: str
    allowed_barge_in: list[str]
    updated_by: str
    updated_at: str
    update_reason: str
    runtime_profile_version: int
    binding_id: str
    binding_version: int
    runtime_apply_status: Literal["current", "dispatched", "applied", "next_session"]


def _device_onboarding_service(request: Request) -> DeviceOnboardingService:
    service = getattr(request.app.state, "device_onboarding_service", None)
    if not isinstance(service, DeviceOnboardingService):
        raise HTTPException(
            status_code=503,
            detail={"code": "device_onboarding_unavailable"},
        )
    return service


def _require_capability(
    request: Request,
    user: AuthenticatedUser,
    capability: SubjectCapability,
) -> None:
    require_capability_for_account_id(
        user.user_id,
        capability,
        store=cast(MemoryStore, request.app.state.memory_store),
    )


def _require_device_activation(
    request: Request,
    *,
    account_id: str,
    device_id: str,
) -> dict[str, object]:
    try:
        return _device_onboarding_service(request).get_activation_status(
            actor_id=account_id,
            device_id=device_id,
        )
    except OnboardingError as error:
        if error.code in {"ACTOR_MISMATCH", "DEVICE_NOT_FOUND"}:
            raise HTTPException(
                status_code=404,
                detail={"code": "device_not_bound_to_account"},
            ) from error
        raise HTTPException(
            status_code=error.status_code,
            detail={"code": error.code},
        ) from error


def _settings_response(
    settings: DeviceSettings,
    *,
    profile_version: int,
    binding_id: str,
    binding_version: int,
    runtime_apply_status: Literal["current", "dispatched", "applied", "next_session"] = "current",
) -> DeviceSettingsResponse:
    return DeviceSettingsResponse(
        device_id=settings.device_id,
        settings_version=settings.settings_version,
        volume_limit=settings.volume_limit,
        screen_brightness=settings.screen_brightness,
        night_mode=settings.night_mode,
        do_not_disturb=settings.do_not_disturb,
        learning_mode=settings.learning_mode,
        audio_mode=settings.audio_mode,
        wake_mode=settings.wake_mode,
        wake_word_id=settings.wake_word_id,
        wake_word_pinyin=settings.wake_word_pinyin,
        wake_word_display=settings.wake_word_display,
        allowed_barge_in=list(settings.allowed_barge_in),
        updated_by=settings.updated_by,
        updated_at=settings.updated_at.isoformat().replace("+00:00", "Z"),
        update_reason=settings.update_reason,
        runtime_profile_version=profile_version,
        binding_id=binding_id,
        binding_version=binding_version,
        runtime_apply_status=runtime_apply_status,
    )


async def _invalidate_active_device_session(
    request: Request,
    *,
    device_id: str,
    profile_version: int,
    apply_at: str,
) -> bool:
    """Tell the Edge to rotate an online device at the selected safe point."""

    dispatcher = getattr(request.app.state, "device_runtime_invalidator", None)
    if dispatcher is None:
        try:
            base_url, token, timeout, verify, cert = _edge_internal_client_config(
                request.app.state.settings
            )
        except Exception as exc:
            logger.warning("device runtime invalidation client is not configured: %s", exc)
            return False
        if not base_url or len(token) < 32:
            return False

        async def dispatcher(**payload: object) -> dict[str, object]:
            async with httpx.AsyncClient(timeout=timeout, verify=verify, cert=cert) as client:
                response = await client.post(
                    f"{base_url}/v1/internal/device-runtime/invalidate",
                    headers={"X-Memoria-Edge-Control-Token": token},
                    json=payload,
                )
                response.raise_for_status()
                value = response.json()
            if not isinstance(value, dict):
                raise ValueError("media edge returned no invalidation result")
            return cast(dict[str, object], value)

    try:
        result = await dispatcher(
            device_id=device_id,
            profile_version=profile_version,
            apply_at=apply_at,
        )
    except Exception as exc:
        logger.warning(
            "device runtime invalidation deferred device_id=%s version=%s error=%s",
            device_id,
            profile_version,
            type(exc).__name__,
        )
        return False
    if not isinstance(result, dict) or not isinstance(result.get("delivered"), bool):
        logger.warning(
            "device runtime invalidation returned an invalid result device_id=%s version=%s",
            device_id,
            profile_version,
        )
        return False
    return bool(result["delivered"])


def _edge_internal_client_config(
    settings: object,
) -> tuple[str, str, float, bool | ssl.SSLContext, tuple[str, str] | None]:
    base_url = str(getattr(settings, "media_edge_internal_control_url", "")).strip().rstrip("/")
    token_setting = getattr(settings, "media_edge_internal_control_token", None)
    token = (
        token_setting.get_secret_value().strip()
        if token_setting is not None and hasattr(token_setting, "get_secret_value")
        else str(token_setting or "").strip()
    )
    timeout = float(getattr(settings, "media_edge_control_timeout_s", 2.0))
    verify: bool | ssl.SSLContext = True
    cert: tuple[str, str] | None = None
    ca_file = str(getattr(settings, "media_edge_internal_control_ca_file", "")).strip()
    cert_file = str(getattr(settings, "media_edge_internal_control_client_cert_file", "")).strip()
    key_file = str(getattr(settings, "media_edge_internal_control_client_key_file", "")).strip()
    if ca_file or cert_file or key_file:
        if not (ca_file and cert_file and key_file):
            raise ValueError("incomplete Control-to-Edge mTLS configuration")
        verify = ssl.create_default_context(cafile=ca_file)
        cert = (cert_file, key_file)
    return base_url, token, timeout, verify, cert


async def _read_live_device_runtime_status(
    request: Request,
    *,
    device_id: str,
) -> dict[str, object] | None:
    reader = getattr(request.app.state, "device_runtime_status_reader", None)
    if reader is None:
        try:
            base_url, token, timeout, verify, cert = _edge_internal_client_config(
                request.app.state.settings
            )
        except Exception as exc:
            logger.warning("device runtime status client is not configured: %s", exc)
            return None
        if not base_url or len(token) < 32:
            return None

        async def reader(**payload: object) -> dict[str, object]:
            requested_device_id = payload.get("device_id")
            if not isinstance(requested_device_id, str):
                raise ValueError("device runtime status requires device_id")
            async with httpx.AsyncClient(timeout=timeout, verify=verify, cert=cert) as client:
                response = await client.get(
                    f"{base_url}/v1/internal/device-runtime/status",
                    headers={"X-Memoria-Edge-Control-Token": token},
                    params={"device_id": requested_device_id},
                )
                response.raise_for_status()
                value = response.json()
            if not isinstance(value, dict):
                raise ValueError("media edge returned no device runtime status")
            return cast(dict[str, object], value)

    try:
        value = await reader(device_id=device_id)
    except Exception as exc:
        logger.warning("device runtime status unavailable device_id=%s error=%s", device_id, exc)
        return None
    if not isinstance(value, dict) or value.get("device_id") != device_id:
        return None
    return cast(dict[str, object], value)


class WakeWordValidateBody(_StrictModel):
    wake_word_id: str | None = None
    wake_word_pinyin: str | None = None
    wake_word_display: str | None = None


class WakeWordValidateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wake_word_id: str
    wake_word_pinyin: str
    wake_word_display: str
    syllables: int
    source: str
    warnings: list[str]


@router.post("/wake-word/validate", response_model=WakeWordValidateResponse)
async def validate_wake_word(body: WakeWordValidateBody) -> WakeWordValidateResponse:
    wake_word_id = body.wake_word_id or "mo_li"
    try:
        if wake_word_id == "custom":
            if body.wake_word_pinyin is None or body.wake_word_display is None:
                raise ValueError("custom wake words require wake_word_pinyin and wake_word_display")
            resolved = validate_custom_wake_word(
                pinyin=body.wake_word_pinyin,
                display=body.wake_word_display,
            )
        else:
            resolved = resolve_wake_word_settings(wake_word_id=wake_word_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return WakeWordValidateResponse(
        wake_word_id=resolved["wake_word_id"],
        wake_word_pinyin=resolved["wake_word_pinyin"],
        wake_word_display=resolved["wake_word_display"],
        syllables=resolved["syllables"],
        source=resolved["source"],
        warnings=wake_word_validation_warnings(resolved),
    )


@router.get("/wake-word-catalog")
async def get_wake_word_catalog() -> dict[str, object]:
    return {"items": wake_word_catalog_payload()}


@router.get(
    "/{device_id}/settings",
    response_model=DeviceSettingsResponse,
)
async def get_device_settings(
    device_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> DeviceSettingsResponse:
    _require_capability(request, user, "device_settings_read")
    activation = _require_device_activation(
        request,
        account_id=user.user_id,
        device_id=device_id,
    )
    store = cast(MemoryStore, request.app.state.memory_store)
    now = datetime.now(UTC)
    settings = DeviceSettingsAuthority(store).current(device_id, now=now)
    entry = RuntimeProfileLedger(store).current(device_id)
    live_runtime = await _read_live_device_runtime_status(request, device_id=device_id)
    runtime_apply_status: Literal["current", "applied"] = "current"
    if (
        entry is not None
        and live_runtime is not None
        and live_runtime.get("connected") is True
        and live_runtime.get("applied_profile_version") == entry.profile_version
        and live_runtime.get("applied_settings_version") == settings.settings_version
    ):
        runtime_apply_status = "applied"
    return _settings_response(
        settings,
        profile_version=entry.profile_version if entry is not None else 0,
        binding_id=str(activation["binding_id"]),
        binding_version=int(cast(int, activation["binding_version"])),
        runtime_apply_status=runtime_apply_status,
    )


@router.patch(
    "/{device_id}/settings",
    response_model=DeviceSettingsResponse,
)
async def patch_device_settings(
    device_id: str,
    body: DeviceSettingsPatch,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> DeviceSettingsResponse:
    _require_capability(request, user, "device_settings_manage")
    activation = _require_device_activation(
        request,
        account_id=user.user_id,
        device_id=device_id,
    )
    store = cast(MemoryStore, request.app.state.memory_store)
    now = datetime.now(UTC)
    authority = DeviceSettingsAuthority(store)
    try:
        updated = authority.update(
            device_id=device_id,
            actor_id=user.user_id,
            changes=body.changes,
            reason=body.reason,
            now=now,
            acoustic_capability=AcousticCapabilityAuthority(store).current(device_id),
            expected_version=body.expected_settings_version,
        )
    except DeviceSettingsConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "settings_version_conflict"},
        ) from exc
    except AudioModeGateError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "audio_mode_requires_aec_evidence"},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    entry = RuntimeProfileLedger(store).current(device_id)
    if entry is None:  # the settings+ledger transaction is all-or-nothing
        raise HTTPException(
            status_code=503,
            detail={"code": "runtime_profile_ledger_unavailable"},
        )
    safety_sensitive = bool(
        {"audio_mode", "wake_mode", "wake_word_id", "allowed_barge_in"}.intersection(body.changes)
    )
    delivered = await _invalidate_active_device_session(
        request,
        device_id=device_id,
        profile_version=entry.profile_version,
        apply_at="immediate_fail_closed" if safety_sensitive else "next_safe_point",
    )
    return _settings_response(
        updated,
        profile_version=entry.profile_version,
        binding_id=str(activation["binding_id"]),
        binding_version=int(cast(int, activation["binding_version"])),
        runtime_apply_status="dispatched" if delivered else "next_session",
    )


@router.post("/{device_id}/runtime-profile/ack")
async def ack_device_runtime_profile(
    device_id: str,
    body: DeviceRuntimeProfileAck,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, object]:
    _require_capability(request, user, "device_runtime_profile_sync")
    _require_device_activation(request, account_id=user.user_id, device_id=device_id)
    store = cast(MemoryStore, request.app.state.memory_store)
    try:
        return RuntimeProfileLedger(store).record_ack(
            device_id=device_id,
            profile_version=body.profile_version,
            runtime_profile_id=body.runtime_profile_id,
            acked_by=user.user_id,
            accepted=body.accepted,
            now=datetime.now(UTC),
        )
    except ProfileAckConflictError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc


@router.get("/{device_id}/runtime-profile/changes")
async def get_device_runtime_profile_changes(
    device_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
    after_version: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    _require_capability(request, user, "device_runtime_profile_sync")
    _require_device_activation(request, account_id=user.user_id, device_id=device_id)
    store = cast(MemoryStore, request.app.state.memory_store)
    entry = RuntimeProfileLedger(store).current(device_id)
    changed = entry is not None and entry.profile_version > after_version
    return {
        "device_id": device_id,
        "changed": changed,
        "current": entry.to_dict() if entry is not None else None,
    }


@router.get("/{device_id}/diagnostics/latest")
async def get_device_diagnostics_latest(
    device_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, object]:
    _require_capability(request, user, "device_settings_read")
    activation = _require_device_activation(
        request,
        account_id=user.user_id,
        device_id=device_id,
    )
    store = cast(MemoryStore, request.app.state.memory_store)
    now = datetime.now(UTC)
    capability = AcousticCapabilityAuthority(store).current(device_id)
    entry = RuntimeProfileLedger(store).current(device_id)
    live_runtime = await _read_live_device_runtime_status(request, device_id=device_id)
    if live_runtime is not None and live_runtime.get("connected") is True:
        latest_session = store.latest_device_media_session(device_id=device_id)
        if (
            latest_session is not None
            and live_runtime.get("session_id") == latest_session["session_id"]
        ):
            live_runtime["persisted_session"] = {
                "runtime": latest_session["runtime"],
                "audio_mode_requested": latest_session["audio_mode_requested"],
                "runtime_profile_version": latest_session["runtime_profile_version"],
                "settings_version": latest_session["settings_version"],
            }
    return {
        "device_id": device_id,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "binding": {
            "binding_id": str(activation["binding_id"]),
            "binding_version": int(cast(int, activation["binding_version"])),
            "activation_status": str(activation["status"]),
            "config_hash": str(activation["config_hash"]),
        },
        "settings": DeviceSettingsAuthority(store).current(device_id, now=now).to_dict(),
        "acoustic_capability": capability.to_dict() if capability is not None else None,
        "allowed_audio_modes": sorted(allowed_audio_modes(capability)),
        "runtime_profile_version": entry.profile_version if entry is not None else 0,
        # None means Edge status was not authoritatively reachable. A present
        # object with connected=false means Edge authoritatively observed no
        # accepted live session. Neither case is replaced with a guessed mode.
        "live_runtime": live_runtime,
    }


class EdgeSessionCloseReport(_StrictModel):
    """Bounded Edge-to-Control close report (Descartes deviceCloseReportPayload).

    Mirrors media_edge.DeviceSessionCloseReport field-for-field: device_id,
    session_id, stream_epoch (device protocol uint32), account_id (the device ticket JWT
    subject), reason, connected, connected_at (omitted when the connection
    never reported an accept time) and closed_at (RFC 3339 Nano). Because the
    model is extra="forbid" in both directions, a report missing any real
    field is rejected exactly like an unknown field -- the previous model
    rejected every genuine Edge report with 422. The independent report
    token travels in X-Memoria-Edge-Device-Close-Token and is validated
    before any private state is read; it is deliberately distinct from the
    Control-to-Edge runtime-control token.
    """

    device_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    stream_epoch: int = Field(ge=1, le=DEVICE_STREAM_EPOCH_MAX)
    account_id: str = Field(min_length=1, max_length=128)
    reason: Literal[
        "device_close",
        "superseded",
        "network",
        "edge_shutdown",
        "runtime_profile_invalidated",
    ]
    connected: bool
    connected_at: str = Field(min_length=1, max_length=64)
    closed_at: str = Field(min_length=1, max_length=64)

    # The device binary frame and JSON contract both use positive uint32.
    # Reject wider Python integers and bool/string coercion so a report cannot
    # alias a truncated transport epoch.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, strict=True)


_SESSION_CLOSE_REASONS = frozenset(
    {
        "device_close",
        "superseded",
        "network",
        "edge_shutdown",
        "runtime_profile_invalidated",
    }
)

_TRANSPORT_DISCONNECT_REASONS = frozenset(
    {
        "superseded",
        "network",
        "edge_shutdown",
    }
)

# Edge stamps close reports with its own wall clock; accept a generous
# bounded skew so a legitimate report is never mistaken for a forgery, while
# an absurd future timestamp still fails closed.
_CLOSE_REPORT_MAX_CLOCK_SKEW_S = 300


def _parse_close_report_time(value: str) -> datetime:
    """Parse one Edge close-report timestamp (RFC 3339, must carry an offset)."""

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("close report timestamps must be RFC 3339") from exc
    if parsed.tzinfo is None:
        raise ValueError("close report timestamps must carry a UTC offset")
    return parsed


internal_router = APIRouter(prefix="/v1/internal", tags=["device-internal"])


def _edge_control_client_config(
    settings: object,
) -> tuple[str, str, float, bool | ssl.SSLContext, tuple[str, str] | None]:
    return _edge_internal_client_config(settings)


async def _close_active_device_session(
    request: Request,
    *,
    device_id: str,
    session_id: str,
) -> bool:
    """Ask the Edge to close the live device socket for this session.

    This is the Control-to-Edge direction and uses the same runtime-control
    token as runtime-profile invalidation; it is NOT the Edge-to-Control
    close-report token. Failure is surfaced to the caller so a DELETE never
    leaves a live socket behind while claiming the session was closed.
    """

    try:
        base_url, token, timeout, verify, cert = _edge_control_client_config(
            request.app.state.settings
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": "media_edge_control_unavailable"},
        ) from exc
    if not base_url or len(token) < 32:
        raise HTTPException(
            status_code=503,
            detail={"code": "media_edge_control_unavailable"},
        )
    try:
        async with httpx.AsyncClient(timeout=timeout, verify=verify, cert=cert) as client:
            response = await client.post(
                f"{base_url}/v1/internal/device-runtime/session-close",
                headers={"X-Memoria-Edge-Control-Token": token},
                json={"device_id": device_id, "session_id": session_id},
            )
            response.raise_for_status()
            value = response.json()
    except Exception as exc:
        logger.warning(
            "device media edge socket close failed session_id=%s error=%s",
            session_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "media_edge_session_close_unavailable"},
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("delivered"), bool):
        raise HTTPException(
            status_code=503,
            detail={"code": "media_edge_session_close_unavailable"},
        )
    return bool(value["delivered"])


async def _close_media_session_pipeline(
    request: Request,
    *,
    device_id: str,
    session_id: str,
    reason: str,
    actor_id: str,
    close_edge_socket: bool = False,
    expected_stream_epoch: int | None = None,
) -> dict[str, object]:
    """Close one direct device session: authority + directory first, then projection.

    Close order is enforced: the Postgres session_closed authority transition
    and the SessionDirectory release both succeed before the SQLite
    projection is marked closed. An ordinary network disconnect therefore can
    never mark the projection closed while the authority is still active.
    Every step is idempotent, so a retry after a partial failure converges:
    the authority replay returns already_closed, the directory expire is a
    no-op when absent, and the projection keeps its first close_reason.
    """

    store = cast(MemoryStore, request.app.state.memory_store)
    row = store.get_device_media_session(session_id=session_id)
    if row is None or row["device_id"] != device_id:
        raise HTTPException(status_code=404, detail={"code": "media_session_not_found"})
    if expected_stream_epoch is not None and int(row["stream_epoch"]) != expected_stream_epoch:
        raise HTTPException(
            status_code=409,
            detail={"code": "media_session_epoch_mismatch"},
        )
    runtime_service = cast(
        PostgresSessionRuntimeService | None,
        getattr(request.app.state, "session_runtime_service", None),
    )
    if runtime_service is None:
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        )
    already_closed = False
    try:
        result = await runtime_service.close_session(
            actor_id=actor_id,
            session_id=session_id,
            reason_code=reason,
            now=datetime.now(UTC),
        )
        already_closed = result.already_closed
    except PersistentSessionNotFound as exc:
        # A genuine authority miss must be rejected, never treated as
        # already closed: without the Postgres authority transition the
        # projection must not be marked closed.
        raise HTTPException(
            status_code=404,
            detail={"code": "media_session_authority_not_found"},
        ) from exc
    except SessionRuntimeConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "media_session_close_conflict"},
        ) from exc
    except PersistentSessionDenied as exc:
        raise HTTPException(
            status_code=403,
            detail={"code": "media_session_close_denied"},
        ) from exc
    except PersistentSessionUnavailable as exc:
        logger.warning(
            "device media session close authority failed session_id=%s error=%s",
            session_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "session_runtime_authority_unavailable"},
        ) from exc
    directory = cast(
        SessionDirectory | None,
        getattr(request.app.state, "session_directory", None),
    )
    if directory is not None:
        try:
            await directory.expire(session_id)
        except SessionDirectoryUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "media_session_directory_unavailable"},
            ) from exc
    if close_edge_socket:
        # The live Edge socket is closed before the projection is marked
        # closed so an owner DELETE can never leave a connected session.
        await _close_active_device_session(
            request,
            device_id=device_id,
            session_id=session_id,
        )
    closed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    applied_projection = store.close_device_media_session(
        session_id=session_id,
        reason=reason,
        closed_at=closed_at,
        expected_stream_epoch=expected_stream_epoch,
    )
    if not applied_projection:
        current = store.get_device_media_session(session_id=session_id)
        if current is None:
            raise HTTPException(status_code=404, detail={"code": "media_session_not_found"})
        if expected_stream_epoch is not None and int(current["stream_epoch"]) != expected_stream_epoch:
            raise HTTPException(
                status_code=409,
                detail={"code": "media_session_epoch_mismatch"},
            )
        if current["closed_at"] is None:
            raise HTTPException(
                status_code=503,
                detail={"code": "media_session_projection_unavailable"},
            )
    return {
        "device_id": device_id,
        "session_id": session_id,
        "closed": True,
        "already_closed": already_closed,
        "reason": reason,
    }


@router.delete("/{device_id}/media-sessions/{session_id}")
async def delete_device_media_session(
    device_id: str,
    session_id: str,
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_writable_account)],
) -> dict[str, object]:
    """Owner close of one direct device media session.

    The capability matrix runs before any private device/session state is
    read. The close pipeline applies the Postgres session_closed authority
    transition first, releases the directory route, closes the live Edge
    socket, and only then marks the local projection closed.
    """

    _require_capability(request, user, "device_settings_manage")
    _require_device_activation(
        request,
        account_id=user.user_id,
        device_id=device_id,
    )
    return await _close_media_session_pipeline(
        request,
        device_id=device_id,
        session_id=session_id,
        reason="device_close",
        actor_id=user.user_id,
        close_edge_socket=True,
    )


@internal_router.post("/device-close")
async def report_edge_session_close(
    body: EdgeSessionCloseReport,
    request: Request,
) -> dict[str, object]:
    """Authenticated Edge close report (Edge-to-Control direction).

    Validates the independent close-report token from
    MEDIA_EDGE_DEVICE_CLOSE_REPORT_TOKEN (aligned with the Descartes edge
    reporter); this is NOT the Control-to-Edge runtime-control token. The
    reason is a bounded allowlist, the payload semantics are checked before
    any private state is read, and account_id / stream_epoch / device_id are
    cross-checked against the authoritative voice session and device media
    projections instead of being trusted from the wire. Transport-only losses
    (network, superseded, edge_shutdown) record diagnostics but preserve the
    Session authority for same-Session reconnect. Only terminal reasons
    (device_close and runtime_profile_invalidated) run the authority-first
    close pipeline. The Edge has already closed the socket, so no Edge
    socket-close step runs here.
    """

    token_setting = getattr(
        request.app.state,
        "device_close_report_token",
        None,
    )
    expected = (
        token_setting.get_secret_value().strip()
        if token_setting is not None and hasattr(token_setting, "get_secret_value")
        else str(token_setting or "").strip()
    )
    provided = request.headers.get("X-Memoria-Edge-Device-Close-Token", "").strip()
    if not expected or len(expected) < 32 or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail={"code": "edge_close_report_token_invalid"})
    if body.reason not in _SESSION_CLOSE_REASONS:
        raise HTTPException(status_code=422, detail={"code": "session_close_reason_invalid"})
    # The Edge only reports accepted, connected sessions, so connected=true
    # and an accept timestamp are mandatory, not merely informational.
    if not body.connected:
        raise HTTPException(
            status_code=422,
            detail={"code": "session_close_report_not_connected"},
        )
    try:
        closed_at = _parse_close_report_time(body.closed_at)
        connected_at = _parse_close_report_time(body.connected_at)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "session_close_report_time_invalid"},
        ) from exc
    now = datetime.now(UTC)
    if connected_at > closed_at:
        raise HTTPException(
            status_code=422,
            detail={"code": "session_close_report_time_invalid"},
        )
    if closed_at > now + timedelta(seconds=_CLOSE_REPORT_MAX_CLOCK_SKEW_S):
        raise HTTPException(
            status_code=422,
            detail={"code": "session_close_report_time_invalid"},
        )
    store = cast(MemoryStore, request.app.state.memory_store)
    row = store.get_device_media_session(session_id=body.session_id)
    if row is None or row["device_id"] != body.device_id:
        raise HTTPException(status_code=404, detail={"code": "media_session_not_found"})
    voice_session = store.get_voice_session_by_id(session_id=body.session_id)
    if voice_session is None or not voice_session.get("user_id"):
        # Without the authoritative account the close cannot be applied to
        # the Postgres authority; reject instead of writing the projection.
        raise HTTPException(
            status_code=409,
            detail={"code": "media_session_actor_unresolved"},
        )
    if str(voice_session["user_id"]) != body.account_id:
        # The report's account must be the authoritative voice session owner;
        # a mismatched account_id is never trusted to close someone else's
        # session.
        raise HTTPException(
            status_code=409,
            detail={"code": "media_session_account_mismatch"},
        )
    projected_epoch = int(row["stream_epoch"])
    if body.reason in _TRANSPORT_DISCONNECT_REASONS:
        if body.stream_epoch < projected_epoch:
            # Expected after a successful reconnect: the old socket reports
            # its own close after Control already advanced the projection.
            return {
                "device_id": body.device_id,
                "session_id": body.session_id,
                "closed": False,
                "reconnectable": row["closed_at"] is None,
                "stale": True,
                "reason": body.reason,
            }
        if body.stream_epoch > projected_epoch:
            raise HTTPException(
                status_code=409,
                detail={"code": "media_session_epoch_mismatch"},
            )
        if row["closed_at"] is not None:
            return {
                "device_id": body.device_id,
                "session_id": body.session_id,
                "closed": True,
                "reconnectable": False,
                "stale": True,
                "reason": str(row["close_reason"]),
            }
        recorded = store.record_device_media_disconnect(
            session_id=body.session_id,
            expected_stream_epoch=body.stream_epoch,
            reason=body.reason,
            disconnected_at=body.closed_at,
        )
        if not recorded:
            current = store.get_device_media_session(session_id=body.session_id)
            if current is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "media_session_not_found"},
                )
            if int(current["stream_epoch"]) > body.stream_epoch:
                return {
                    "device_id": body.device_id,
                    "session_id": body.session_id,
                    "closed": False,
                    "reconnectable": current["closed_at"] is None,
                    "stale": True,
                    "reason": body.reason,
                }
            raise HTTPException(
                status_code=409,
                detail={"code": "media_session_epoch_mismatch"},
            )
        return {
            "device_id": body.device_id,
            "session_id": body.session_id,
            "closed": False,
            "reconnectable": True,
            "stale": False,
            "reason": body.reason,
        }
    if projected_epoch != body.stream_epoch:
        # A terminal report whose epoch does not match the current projection
        # must never terminate the replacement transport's Session.
        raise HTTPException(
            status_code=409,
            detail={"code": "media_session_epoch_mismatch"},
        )
    return await _close_media_session_pipeline(
        request,
        device_id=body.device_id,
        session_id=body.session_id,
        reason=body.reason,
        actor_id=str(voice_session["user_id"]),
        expected_stream_epoch=body.stream_epoch,
    )
