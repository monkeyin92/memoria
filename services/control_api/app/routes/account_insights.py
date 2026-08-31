"""Delivered-capability dashboard for operators and product surfaces."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Request

from services.control_api.app.database import MemoryStore
from services.control_api.app.security import AuthenticatedUser, require_authenticated_user
from services.speaker.domain import SpeakerAuthorityPort

router = APIRouter(prefix="/v1/account", tags=["account"])


def _store(request: Request) -> MemoryStore:
    return cast(MemoryStore, request.app.state.memory_store)


def _authority(request: Request) -> SpeakerAuthorityPort:
    return cast(SpeakerAuthorityPort, request.app.state.speaker_authority)


@router.get("/delivered-capabilities")
async def delivered_capabilities(
    request: Request,
    user: Annotated[AuthenticatedUser, Depends(require_authenticated_user)],
) -> dict[str, Any]:
    """Return only shipped capability signals (no TAM or unverified claims)."""

    store = _store(request)
    account_id = user.user_id
    profile = store.get_subject_profile(user_id=account_id) or {}
    speaker_profiles = await _authority(request).profiles(account_id)
    active_speakers = [item for item in speaker_profiles if item.status == "active"]
    memory_activity = store.memory_activity_summary(user_id=account_id)
    return {
        "as_of": datetime.now(UTC).isoformat(),
        "account_id": account_id,
        "product_positioning": "family_archive_terminal",
        "ai_disclosure_required": True,
        "wechat_bound": store.has_external_identity(
            user_id=account_id,
            provider="wechat_openid",
        ),
        "wechat_phone_verified": store.has_external_identity(
            user_id=account_id,
            provider="wechat_phone",
        ),
        "devices_bound": store.count_active_device_identities(account_id=account_id),
        "subject_category": profile.get("subject_category"),
        "age_evidence_status": profile.get("age_evidence_status"),
        "speaker_profiles_active": len(active_speakers),
        "speaker_enrollment_ready": (
            profile.get("subject_category") == "adult"
            and profile.get("birth_year_band") == "adult"
            and profile.get("age_evidence_status") == "verified"
        ),
        "reject_non_owner_voice": bool(profile.get("reject_non_owner_voice", True)),
        "memory_days_with_activity": memory_activity["memory_days_with_activity"],
        "total_messages": memory_activity["total_messages"],
        "model_training_contribution_enabled": False,
        "account_export_available": True,
        "account_deletion_available": True,
        "advertised_duplex_level": "none",
        "notes": (
            "Counts only delivered control-plane and identity signals. "
            "Device wake accuracy and half-duplex turn latency require hardware receipts."
        ),
    }
