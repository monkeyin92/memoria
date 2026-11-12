from __future__ import annotations

import logging
import ssl
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException

from services.control_api.app.database import MemoryStore
from services.control_api.app.session_directory import SessionDirectory, SessionDirectoryUnavailable
from services.session_runtime.errors import PersistentSessionSubjectChanged
from services.session_runtime.postgres_store import SessionRuntimeConflict
from services.session_runtime.service import (
    PersistentSessionDenied,
    PersistentSessionNotFound,
    PersistentSessionUnavailable,
    PostgresSessionRuntimeService,
)

logger = logging.getLogger(__name__)


def edge_internal_client_config(
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


async def close_active_device_session(
    *,
    settings: object,
    device_id: str,
    session_id: str,
) -> bool:
    """Close one Edge socket; reject transport failures and invalid receipts."""

    try:
        base_url, token, timeout, verify, cert = edge_internal_client_config(settings)
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


async def close_media_session_pipeline(
    *,
    store: MemoryStore,
    runtime_service: PostgresSessionRuntimeService | None,
    directory: SessionDirectory | None,
    settings: object,
    device_id: str,
    session_id: str,
    reason: str,
    actor_id: str,
    close_edge_socket: bool = False,
    expected_stream_epoch: int | None = None,
    expected_subject_id: str | None = None,
) -> dict[str, object]:
    """Close authority, routing and Edge before checkpointing the projection."""

    row = store.get_device_media_session(session_id=session_id)
    if row is None or row["device_id"] != device_id:
        raise HTTPException(status_code=404, detail={"code": "media_session_not_found"})
    if expected_stream_epoch is not None and int(row["stream_epoch"]) != expected_stream_epoch:
        raise HTTPException(
            status_code=409,
            detail={"code": "media_session_epoch_mismatch"},
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
            **(
                {"expected_subject_id": expected_subject_id}
                if expected_subject_id is not None
                else {}
            ),
        )
        already_closed = result.already_closed
    except PersistentSessionSubjectChanged:
        raise
    except PersistentSessionNotFound as exc:
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
    if directory is not None:
        try:
            await directory.expire(session_id)
        except SessionDirectoryUnavailable as exc:
            raise HTTPException(
                status_code=503,
                detail={"code": "media_session_directory_unavailable"},
            ) from exc
    if close_edge_socket:
        await close_active_device_session(
            settings=settings,
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
        if (
            expected_stream_epoch is not None
            and int(current["stream_epoch"]) != expected_stream_epoch
        ):
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
