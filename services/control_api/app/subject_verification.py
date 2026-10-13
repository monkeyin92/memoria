"""Trusted subject-profile upgrades used by capability gates."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, cast

from services.control_api.app.database import MemoryStore
from services.guardian.domain import (
    AgeEvidenceStatus,
    BirthYearBand,
    SubjectCategory,
    SubjectTransitionError,
    validate_subject_transition,
)
from services.identity.domain import IdentityNotFoundError, PersonSubject
from services.identity.service import IdentityService


async def ensure_account_person(
    store: MemoryStore, identity: IdentityService, *, user_id: str, now: datetime,
) -> PersonSubject:
    """Reconcile both stores before login succeeds or a binding is created.

    Identity commits its auditable registration first. SQLite then commits its
    profile/session-revocation transaction. Either step may fail; retrying does
    not duplicate the Identity mutation and finishes an interrupted sync.
    Phone binding is the existing registration policy, not age proof supplied
    by WeChat and never a runtime speaker-verification result.
    """
    profile = store.get_subject_profile(user_id=user_id) or {}
    current = (
        profile.get("subject_category") or "unknown",
        profile.get("birth_year_band") or "unknown",
        profile.get("age_evidence_status") or "unverified",
    )
    phone_hash = store.external_identity_subject_hash(
        user_id=user_id, provider="wechat_phone",
    )
    eligible = bool(phone_hash) and current in {
        ("unknown", "unknown", "unverified"), ("adult", "adult", "verified"),
    }
    try:
        person = await identity.get_person(user_id, actor_person_id=user_id)
    except IdentityNotFoundError:
        # Do not invent age evidence for legacy/manual adult profiles. The
        # phone-registration action below is the only automatic upgrade here.
        person = await identity.register_person(
            person_id=user_id, actor_person_id=user_id,
            display_name=str(profile.get("display_name") or "朋友"),
            timezone="Asia/Shanghai",
            subject_category=cast(Any, "unknown" if eligible else current[0]),
            age_band=cast(Any, "unknown" if eligible else current[1]),
            age_evidence_status=cast(Any, "unverified" if eligible else current[2]),
            now=now,
        )
    if not eligible:
        return person
    revision = int(profile.get("subject_revision") or 0) + (current[0] == "unknown")
    receipt = hashlib.sha256(json.dumps(
        {"user_id": user_id, "phone_binding": phone_hash, "subject_revision": revision},
        sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    person = await identity.reconcile_account_registration(
        person_id=user_id, evidence_id=f"control-wechat-phone-v1:{receipt}",
        source_revision=revision, now=now,
    )
    maybe_verify_adult_from_wechat_phone(store, user_id=user_id, now=now.isoformat())
    return person


def maybe_verify_adult_from_wechat_phone(
    store: MemoryStore,
    *,
    user_id: str,
    now: str,
) -> dict[str, Any] | None:
    """Promote unknown accounts to verified adult after WeChat phone binding.

    WeChat phone authorization is the trusted registration evidence for the
    account owner on the control plane.
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

