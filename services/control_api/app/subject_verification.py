"""Trusted subject-profile upgrades used by capability gates."""

from __future__ import annotations

from typing import Any, cast

from services.control_api.app.database import MemoryStore
from services.guardian.domain import (
    AgeEvidenceStatus,
    BirthYearBand,
    SubjectCategory,
    SubjectTransitionError,
    validate_subject_transition,
)


def maybe_verify_adult_from_wechat_phone(
    store: MemoryStore,
    *,
    user_id: str,
    now: str,
) -> dict[str, Any] | None:
    """Promote unknown accounts to verified adult after WeChat phone binding.

    WeChat phone authorization is the trusted registration evidence for the
    account owner on the control plane.  This unlocks ``speaker_enrollment``
    without exposing biometric capture to the Mini Program client.
    """

    if not store.has_external_identity(user_id=user_id, provider="wechat_phone"):
        return None
    profile = store.get_subject_profile(user_id=user_id)
    if profile is None:
        return None
    current_category = cast(SubjectCategory, profile.get("subject_category") or "unknown")
    if current_category != "unknown":
        return None
    current_band = cast(BirthYearBand, profile.get("birth_year_band") or "unknown")
    current_evidence = cast(
        AgeEvidenceStatus,
        profile.get("age_evidence_status") or "unverified",
    )
    try:
        validate_subject_transition(
            current_category=current_category,
            current_birth_year_band=current_band,
            current_age_evidence_status=current_evidence,
            target_category="adult",
            target_birth_year_band="adult",
            target_age_evidence_status="verified",
        )
    except SubjectTransitionError:
        return None
    return store.update_subject_profile(
        user_id=user_id,
        subject_category="adult",
        birth_year_band="adult",
        age_evidence_status="verified",
        now=now,
    )


def speaker_enrollment_remediation(
    *,
    block_code: str | None,
    registered: bool,
    has_wechat_phone: bool,
    subject: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return client-safe next steps when speaker enrollment is blocked."""

    if block_code is None:
        return None
    if block_code == "account_not_registered":
        return {
            "code": block_code,
            "steps": ["register_account"],
            "message": "请先完成账号注册后再登记主人声纹。",
        }
    if block_code == "minor_forbidden":
        return {
            "code": block_code,
            "steps": ["use_adult_account"],
            "message": "未成年人主体不能登记主人声纹。",
        }
    if block_code == "subject_category_unavailable":
        return {
            "code": block_code,
            "steps": ["retry_later"],
            "message": "主体资料暂时不可用，请稍后重试。",
        }
    if block_code != "subject_capability_forbidden":
        return {
            "code": block_code,
            "steps": ["retry_later"],
            "message": "当前还不能登记主人声纹，请稍后重试。",
        }
    category = (subject or {}).get("subject_category")
    evidence = (subject or {}).get("age_evidence_status")
    if not registered:
        return {
            "code": block_code,
            "steps": ["register_account"],
            "message": "请先完成账号注册后再登记主人声纹。",
        }
    if not has_wechat_phone:
        return {
            "code": block_code,
            "steps": ["authorize_wechat_phone", "refresh_speaker_status"],
            "message": "请先在微信登录时授权手机号，以完成已验证成人主体确认。",
        }
    if category != "adult" or evidence != "verified":
        return {
            "code": block_code,
            "steps": ["refresh_speaker_status", "contact_support"],
            "message": "主体资料尚未满足主人声纹所需的已验证成人条件。",
        }
    return None
